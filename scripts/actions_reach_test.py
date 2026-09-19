#!/usr/bin/env python3
"""Task17追試: PDFの一節照合と、FRBの403がUser-Agent名乗りで変わるかを測るだけの調査スクリプト。

対象は trial/reports/actions_reach_targets2.json の5件のみ（1回目の到達性・安定性測定は
trial/reports/actions_reach_targets.json 側で済んでいるため、ここでは繰り返さない）。
出典の本文・excerptの中身は、結果ファイルにもログにも書き込まない。
既存の紙面作成スクリプト・紙面JSONは一切変更しない（読むだけ）。
"""

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = REPO_ROOT / "trial" / "reports" / "actions_reach_targets2.json"
REPORTS_DIR = REPO_ROOT / "trial" / "reports"

TIMEOUT_SECONDS = 30
GAP_BETWEEN_TARGETS_SECONDS = 1
GAP_BETWEEN_UA_PROBE_REQUESTS_SECONDS = 3
UA_PROBE_USER_AGENT = (
    "daily-brief-source-check/1.0 "
    "(+https://github.com/axl4415blast-hash/daily-brief-routine)"
)

# --- pypdf の読み込み。無ければ1回だけインストールを試みる。それでも駄目なら諦めて先に進む ---
PYPDF_AVAILABLE = False
PYPDF_INSTALL_ATTEMPTED = False
pypdf = None
try:
    import pypdf as _pypdf

    pypdf = _pypdf
    PYPDF_AVAILABLE = True
except BaseException:
    PYPDF_INSTALL_ATTEMPTED = True
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "pypdf"],
            check=True,
            timeout=180,
        )
    except BaseException:
        pass
    try:
        import pypdf as _pypdf

        pypdf = _pypdf
        PYPDF_AVAILABLE = True
    except BaseException:
        pypdf = None
        PYPDF_AVAILABLE = False


def classify_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return None  # HTTP errors are recorded via http_status, not as "error"
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            return "timeout"
        if isinstance(reason, socket.gaierror):
            return "dns_resolution_failed"
        reason_text = str(reason)
        if "Connection refused" in reason_text:
            return "connection_refused"
        if "CERTIFICATE" in reason_text.upper():
            return "tls_error"
        return f"url_error:{reason_text}"
    return f"unknown_error:{type(exc).__name__}"


def fetch_once(url, extra_headers=None):
    """1回だけGETする。生バイト列は戻り値に含めず、別に返す（呼び出し元が使い終わったら破棄する）。"""
    result = {
        "http_status": None,
        "final_url": None,
        "elapsed_ms": None,
        "content_type": None,
        "bytes": None,
        "error": None,
    }
    req = urllib.request.Request(url, headers=extra_headers or {}, method="GET")
    start = time.monotonic()
    raw = b""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            result["http_status"] = resp.status
            result["final_url"] = resp.geturl()
            result["content_type"] = resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        result["http_status"] = e.code
        result["final_url"] = e.geturl() if hasattr(e, "geturl") else url
        result["content_type"] = e.headers.get("Content-Type") if e.headers else None
    except Exception as e:
        result["error"] = classify_error(e)
    finally:
        result["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)

    result["bytes"] = len(raw)
    return result, raw


def decode_body(raw, content_type_header):
    candidates = []
    if content_type_header:
        for part in content_type_header.split(";"):
            part = part.strip().lower()
            if part.startswith("charset="):
                candidates.append(part.split("=", 1)[1].strip())
    candidates += ["utf-8", "cp932"]

    seen = set()
    for enc in candidates:
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            return enc, raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return None, None


def normalize_nfkc(s):
    return unicodedata.normalize("NFKC", s)


WHITESPACE_RE = re.compile(r"[\s　]+")


def strip_whitespace(s):
    return WHITESPACE_RE.sub("", s)


def excerpt_match(text, excerpt):
    """A: そのままNFKC正規化して探す（found_raw）。B: さらに空白類を除いて探す（found_nospace）。"""
    if text is None or excerpt is None:
        return None, None
    text_n = normalize_nfkc(text)
    excerpt_n = normalize_nfkc(excerpt)
    found_raw = excerpt_n in text_n
    found_nospace = strip_whitespace(excerpt_n) in strip_whitespace(text_n)
    return found_raw, found_nospace


def extract_pdf_text(raw_bytes):
    """一時ファイルに保存して全ページのテキストを取り出す。戻り値: (pages, full_text)。リポジトリには保存しない。"""
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as f:
            f.write(raw_bytes)
        reader = pypdf.PdfReader(tmp_path)
        pages = len(reader.pages)
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
        full_text = "\n".join(texts)
        return pages, full_text
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def process_pdf_target(target):
    target_id = target["target_id"]
    url = target["url"]
    excerpt = target.get("excerpt")
    print(f"[{target_id}] (pdf_excerpt) GET {url}")

    info, raw = fetch_once(url)
    row = {
        "target_id": target_id,
        "url": url,
        "publisher": target.get("publisher"),
        "mode": "pdf_excerpt",
        "line_id": target.get("line_id"),
        "http_status": info["http_status"],
        "final_url": info["final_url"],
        "elapsed_ms": info["elapsed_ms"],
        "content_type": info["content_type"],
        "bytes": info["bytes"],
        "pdf_pages": None,
        "pdf_chars": None,
        "pdf_lib_missing": False,
        "pdf_text_empty": False,
        "found_raw": None,
        "found_nospace": None,
        "error": info["error"],
    }

    if info["http_status"] == 200 and raw and not info["error"]:
        if not PYPDF_AVAILABLE:
            row["pdf_lib_missing"] = True
        else:
            try:
                pages, text = extract_pdf_text(raw)
                row["pdf_pages"] = pages
                row["pdf_chars"] = len(text)
                if row["pdf_chars"] == 0:
                    row["pdf_text_empty"] = True
                    row["found_raw"] = False
                    row["found_nospace"] = False
                else:
                    row["found_raw"], row["found_nospace"] = excerpt_match(text, excerpt)
            except Exception as e:
                row["error"] = (row["error"] + ";" if row["error"] else "") + (
                    f"pdf_extract_error:{type(e).__name__}"
                )
    raw = None  # 生バイト列はここで破棄する（保存しない）

    print(
        f"    status={row['http_status']} bytes={row['bytes']} "
        f"pdf_pages={row['pdf_pages']} pdf_chars={row['pdf_chars']} "
        f"found_raw={row['found_raw']} found_nospace={row['found_nospace']} "
        f"pdf_lib_missing={row['pdf_lib_missing']} pdf_text_empty={row['pdf_text_empty']}"
    )
    return row


def process_ua_target(target):
    target_id = target["target_id"]
    url = target["url"]
    excerpt = target.get("excerpt")
    print(f"[{target_id}] (ua_probe) GET {url} (1回目: 名乗りなし)")

    info1, raw1 = fetch_once(url)
    status_noua = info1["http_status"]
    bytes_noua = info1["bytes"]
    print(f"    1回目: status={status_noua} bytes={bytes_noua} error={info1['error']}")
    raw1 = None

    time.sleep(GAP_BETWEEN_UA_PROBE_REQUESTS_SECONDS)

    print(f"[{target_id}] (ua_probe) GET {url} (2回目: 名乗りあり)")
    info2, raw2 = fetch_once(url, extra_headers={"User-Agent": UA_PROBE_USER_AGENT})
    status_ua = info2["http_status"]
    bytes_ua = info2["bytes"]
    print(f"    2回目: status={status_ua} bytes={bytes_ua} error={info2['error']}")

    found_raw = None
    found_nospace = None
    if status_ua == 200 and raw2:
        _, decoded = decode_body(raw2, info2["content_type"])
        if decoded is not None:
            found_raw, found_nospace = excerpt_match(decoded, excerpt)
    raw2 = None  # 生バイト列はここで破棄する（保存しない）

    error = None
    if info1["error"] or info2["error"]:
        parts = []
        if info1["error"]:
            parts.append(f"1st:{info1['error']}")
        if info2["error"]:
            parts.append(f"2nd:{info2['error']}")
        error = ";".join(parts)

    row = {
        "target_id": target_id,
        "url": url,
        "publisher": target.get("publisher"),
        "mode": "ua_probe",
        "line_id": target.get("line_id"),
        "status_noua": status_noua,
        "status_ua": status_ua,
        "bytes_noua": bytes_noua,
        "bytes_ua": bytes_ua,
        "final_url_ua": info2["final_url"],
        "elapsed_ms_noua": info1["elapsed_ms"],
        "elapsed_ms_ua": info2["elapsed_ms"],
        "found_raw": found_raw,
        "found_nospace": found_nospace,
        "error": error,
    }
    return row


def run():
    if not TARGETS_PATH.exists():
        raise SystemExit(f"targets file not found: {TARGETS_PATH}")

    import json

    targets = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))["targets"]

    rows = []
    for i, target in enumerate(targets):
        mode = target.get("mode")
        if mode == "pdf_excerpt":
            row = process_pdf_target(target)
        elif mode == "ua_probe":
            row = process_ua_target(target)
        else:
            raise SystemExit(f"unknown mode for {target.get('target_id')}: {mode}")
        rows.append(row)

        if i < len(targets) - 1:
            time.sleep(GAP_BETWEEN_TARGETS_SECONDS)

    return rows


def write_reports(rows):
    import json

    now = datetime.now(JST)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    json_path = REPORTS_DIR / f"actions_reach2_{stamp}.json"
    md_path = REPORTS_DIR / f"actions_reach2_{stamp}.md"

    run_settings = {
        "targets_file": "trial/reports/actions_reach_targets2.json",
        "second_fetch_for_stability": False,
        "wait_before_second_fetch_seconds": 0,
        "note_on_second_fetch": (
            "1回目で安定性(stable)は測定済みのため、この追試では通常ターゲットの2回目取得と"
            "60秒待機を行わない。ua_probeのみ、UA比較のため同一URLに2回アクセスする"
            "（間は60秒ではなく3秒）。"
        ),
        "gap_between_ua_probe_requests_seconds": GAP_BETWEEN_UA_PROBE_REQUESTS_SECONDS,
        "pypdf_available": PYPDF_AVAILABLE,
        "pypdf_install_attempted": PYPDF_INSTALL_ATTEMPTED,
        "user_agent_used_in_probe": UA_PROBE_USER_AGENT,
    }

    output = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "run_settings": run_settings,
        "results": rows,
    }
    json_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        f"# GitHub Actions 到達性テスト追試(2回目)結果 ({output['generated_at']})",
        "",
        "## この結果の測り方（設定）",
        "",
        f"- 対象一覧: `{run_settings['targets_file']}`",
        f"- 2回目の取得（安定性再測定）: {'あり' if run_settings['second_fetch_for_stability'] else 'なし'}"
        f"（{run_settings['note_on_second_fetch']}）",
        f"- pypdf が使えたか: {'はい' if run_settings['pypdf_available'] else 'いいえ'}"
        f"（インストールを試みた: {'はい' if run_settings['pypdf_install_attempted'] else 'いいえ（最初から import 済み）'}）",
        f"- ua_probe で名乗った User-Agent: `{run_settings['user_agent_used_in_probe']}`",
        f"- ua_probe の2回のアクセス間隔: {run_settings['gap_between_ua_probe_requests_seconds']}秒",
        "",
        "## 結果一覧",
        "",
        "| target_id | publisher | mode | http_status | bytes | pdf_pages | pdf_chars | "
        "pdf_lib_missing | pdf_text_empty | found_raw | found_nospace | error |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r["mode"] == "pdf_excerpt":
            http_status = str(r["http_status"])
            bytes_col = str(r["bytes"])
            pdf_pages = r["pdf_pages"]
            pdf_chars = r["pdf_chars"]
            pdf_lib_missing = r["pdf_lib_missing"]
            pdf_text_empty = r["pdf_text_empty"]
        else:
            http_status = f"noua={r['status_noua']} / ua={r['status_ua']}"
            bytes_col = f"noua={r['bytes_noua']} / ua={r['bytes_ua']}"
            pdf_pages = ""
            pdf_chars = ""
            pdf_lib_missing = ""
            pdf_text_empty = ""

        lines.append(
            "| {target_id} | {publisher} | {mode} | {http_status} | {bytes} | {pdf_pages} | "
            "{pdf_chars} | {pdf_lib_missing} | {pdf_text_empty} | {found_raw} | {found_nospace} | {error} |".format(
                target_id=r["target_id"],
                publisher=r["publisher"] or "",
                mode=r["mode"],
                http_status=http_status,
                bytes=bytes_col,
                pdf_pages=pdf_pages if pdf_pages is not None else "",
                pdf_chars=pdf_chars if pdf_chars is not None else "",
                pdf_lib_missing=pdf_lib_missing,
                pdf_text_empty=pdf_text_empty,
                found_raw=r["found_raw"] if r["found_raw"] is not None else "",
                found_nospace=r["found_nospace"] if r["found_nospace"] is not None else "",
                error=r["error"] or "",
            )
        )
    lines.append("")
    lines.append(
        "excerptの文章そのものはここには書かない（line_idで該当の号JSONの行を参照できる）。"
        "final_urlなど詳細は同じ時刻の .json ファイルを参照。"
    )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nwrote {json_path}")
    print(f"wrote {md_path}")
    return json_path, md_path


def main():
    rows = run()
    write_reports(rows)
    print("\n=== summary ===")
    for r in rows:
        if r["mode"] == "pdf_excerpt":
            print(
                f"{r['target_id']}: status={r['http_status']} "
                f"pdf_pages={r['pdf_pages']} pdf_chars={r['pdf_chars']} "
                f"found_raw={r['found_raw']} found_nospace={r['found_nospace']} "
                f"pdf_lib_missing={r['pdf_lib_missing']} error={r['error']}"
            )
        else:
            print(
                f"{r['target_id']}: status_noua={r['status_noua']} status_ua={r['status_ua']} "
                f"found_raw={r['found_raw']} found_nospace={r['found_nospace']} error={r['error']}"
            )


if __name__ == "__main__":
    main()
