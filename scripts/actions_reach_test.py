#!/usr/bin/env python3
"""Task17追試(3回目・最後): 財務省PDF(暗号付き)とFRBのHTML(タグ除去後照合)を
読み切れるかを測るだけの調査スクリプト。

対象は trial/reports/actions_reach_targets3.json の3件のみ（T-05・T-06・T-04）。
出典の本文・excerptの中身は、結果ファイルにもログにも書き込まない。
既存の紙面作成スクリプト・紙面JSONは一切変更しない（読むだけ）。
"""

import html as html_module
import os
import re
import shutil
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
TARGETS_PATH = REPO_ROOT / "trial" / "reports" / "actions_reach_targets3.json"
REPORTS_DIR = REPO_ROOT / "trial" / "reports"

TIMEOUT_SECONDS = 30
GAP_BETWEEN_TARGETS_SECONDS = 1
REQUEST_USER_AGENT = (
    "daily-brief-source-check/1.0 "
    "(+https://github.com/axl4415blast-hash/daily-brief-routine)"
)

# --- pypdf[crypto] を1回だけインストールしてから読み込む。暗号付きPDFを解くための部品 ---
PYPDF_CRYPTO_INSTALL_ATTEMPTED = True
PYPDF_CRYPTO_INSTALL_OK = False
try:
    _proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "pypdf[crypto]"],
        check=False,
        timeout=180,
    )
    PYPDF_CRYPTO_INSTALL_OK = _proc.returncode == 0
except BaseException:
    PYPDF_CRYPTO_INSTALL_OK = False

PYPDF_AVAILABLE = False
pypdf = None
try:
    import pypdf as _pypdf

    pypdf = _pypdf
    PYPDF_AVAILABLE = True
except BaseException:
    pypdf = None
    PYPDF_AVAILABLE = False

# --- pypdfで読めなかった場合に限り使う代わりの手段。無ければ何もしない ---
PDFTOTEXT_PATH = shutil.which("pdftotext")


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


def prefix_match(text, excerpt):
    """excerptの先頭10文字（空白除去後）が本文中にあるか。本文そのものは返さない。"""
    if text is None or excerpt is None:
        return None
    text_n = strip_whitespace(normalize_nfkc(text))
    excerpt_n = strip_whitespace(normalize_nfkc(excerpt))
    prefix = excerpt_n[:10]
    if not prefix:
        return None
    return prefix in text_n


# HTMLタグ除去: <script>/<style>は中身ごと除去してから、残りのタグを空白1つに置き換える
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def strip_html_tags(text):
    text = SCRIPT_STYLE_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    text = normalize_nfkc(text)
    return text


def extract_pdf_text_pypdf(raw_bytes):
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


def extract_pdf_text_pdftotext(raw_bytes):
    """pypdfで読めなかった場合に限り使う代替手段。戻り値: (pages, full_text)。リポジトリには保存しない。"""
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as f:
            f.write(raw_bytes)
        proc = subprocess.run(
            [PDFTOTEXT_PATH, "-q", tmp_path, "-"],
            check=True,
            timeout=60,
            capture_output=True,
            text=True,
        )
        text = proc.stdout or ""
        pages = (text.count("\x0c") + 1) if text else 0
        return pages, text
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def base_row(target, info, mode):
    return {
        "target_id": target["target_id"],
        "url": target["url"],
        "publisher": target.get("publisher"),
        "mode": mode,
        "line_id": target.get("line_id"),
        "http_status": info["http_status"],
        "final_url": info["final_url"],
        "elapsed_ms": info["elapsed_ms"],
        "content_type": info["content_type"],
        "bytes": info["bytes"],
        "pdf_method": None,
        "pdf_pages": None,
        "pdf_chars": None,
        "body_chars": None,
        "prefix_found": None,
        "found_raw": None,
        "found_nospace": None,
        "error": info["error"],
    }


def append_error(row, message):
    row["error"] = (row["error"] + ";" if row["error"] else "") + message


def process_pdf_target(target):
    target_id = target["target_id"]
    url = target["url"]
    excerpt = target.get("excerpt")
    print(f"[{target_id}] (pdf_excerpt) GET {url}")

    info, raw = fetch_once(url)
    row = base_row(target, info, mode="pdf_excerpt")

    if info["http_status"] == 200 and raw and not info["error"]:
        pdf_method = "failed"
        pages, text = None, None
        pypdf_error = None

        if PYPDF_AVAILABLE:
            try:
                pages, text = extract_pdf_text_pypdf(raw)
                pdf_method = "pypdf"
            except Exception as e:
                pypdf_error = f"pypdf_error:{type(e).__name__}"

        if pdf_method == "failed" and PDFTOTEXT_PATH:
            try:
                pages, text = extract_pdf_text_pdftotext(raw)
                pdf_method = "pdftotext"
            except Exception as e:
                append_error(row, f"pdftotext_error:{type(e).__name__}")

        row["pdf_method"] = pdf_method

        if pdf_method == "failed":
            if pypdf_error:
                append_error(row, pypdf_error)
            row["found_raw"] = False
            row["found_nospace"] = False
        else:
            row["pdf_pages"] = pages
            row["pdf_chars"] = len(text) if text else 0
            row["body_chars"] = row["pdf_chars"]
            if row["pdf_chars"] == 0:
                row["found_raw"] = False
                row["found_nospace"] = False
                row["prefix_found"] = False
            else:
                row["found_raw"], row["found_nospace"] = excerpt_match(text, excerpt)
                row["prefix_found"] = prefix_match(text, excerpt)
    raw = None  # 生バイト列はここで破棄する（保存しない）

    print(
        f"    status={row['http_status']} bytes={row['bytes']} pdf_method={row['pdf_method']} "
        f"pdf_pages={row['pdf_pages']} pdf_chars={row['pdf_chars']} body_chars={row['body_chars']} "
        f"prefix_found={row['prefix_found']} found_raw={row['found_raw']} found_nospace={row['found_nospace']}"
    )
    return row


def process_html_target(target):
    target_id = target["target_id"]
    url = target["url"]
    excerpt = target.get("excerpt")
    print(f"[{target_id}] (html_excerpt) GET {url} (名乗りあり・1回だけ)")

    info, raw = fetch_once(url, extra_headers={"User-Agent": REQUEST_USER_AGENT})
    row = base_row(target, info, mode="html_excerpt")

    if info["http_status"] == 200 and raw and not info["error"]:
        _, decoded = decode_body(raw, info["content_type"])
        if decoded is None:
            append_error(row, "decode_failed")
        else:
            processed = strip_html_tags(decoded)
            row["body_chars"] = len(processed)
            if row["body_chars"] == 0:
                row["found_raw"] = False
                row["found_nospace"] = False
                row["prefix_found"] = False
            else:
                row["found_raw"], row["found_nospace"] = excerpt_match(processed, excerpt)
                row["prefix_found"] = prefix_match(processed, excerpt)
    raw = None  # 生バイト列はここで破棄する（保存しない）

    print(
        f"    status={row['http_status']} bytes={row['bytes']} body_chars={row['body_chars']} "
        f"prefix_found={row['prefix_found']} found_raw={row['found_raw']} found_nospace={row['found_nospace']}"
    )
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
        elif mode == "html_excerpt":
            row = process_html_target(target)
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

    json_path = REPORTS_DIR / f"actions_reach3_{stamp}.json"
    md_path = REPORTS_DIR / f"actions_reach3_{stamp}.md"

    pdftotext_used = any(r.get("pdf_method") == "pdftotext" for r in rows)

    run_settings = {
        "targets_file": "trial/reports/actions_reach_targets3.json",
        "pypdf_crypto_install_attempted": PYPDF_CRYPTO_INSTALL_ATTEMPTED,
        "pypdf_crypto_install_ok": PYPDF_CRYPTO_INSTALL_OK,
        "pypdf_available": PYPDF_AVAILABLE,
        "pdftotext_available": PDFTOTEXT_PATH is not None,
        "pdftotext_used": pdftotext_used,
        "user_agent_used": REQUEST_USER_AGENT,
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
        f"# GitHub Actions 到達性テスト追試(3回目・最後)結果 ({output['generated_at']})",
        "",
        "## この結果の測り方（設定）",
        "",
        f"- 対象一覧: `{run_settings['targets_file']}`",
        f"- pypdf[crypto] の導入: 試みた（{'成功' if run_settings['pypdf_crypto_install_ok'] else '失敗'}）、"
        f"pypdf が使えたか: {'はい' if run_settings['pypdf_available'] else 'いいえ'}",
        f"- pdftotext コマンド: {'利用可能' if run_settings['pdftotext_available'] else '利用不可'}、"
        f"今回の結果で実際に使ったか: {'はい' if run_settings['pdftotext_used'] else 'いいえ'}",
        f"- 使った名乗り（User-Agent）: `{run_settings['user_agent_used']}`（FRBのHTML取得に、最初から1回だけ使用）",
        "",
        "## 結果一覧",
        "",
        "| target_id | publisher | mode | http_status | bytes | pdf_method | pdf_pages | pdf_chars | "
        "body_chars | prefix_found | found_raw | found_nospace | error |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            "| {target_id} | {publisher} | {mode} | {http_status} | {bytes} | {pdf_method} | "
            "{pdf_pages} | {pdf_chars} | {body_chars} | {prefix_found} | {found_raw} | "
            "{found_nospace} | {error} |".format(
                target_id=r["target_id"],
                publisher=r["publisher"] or "",
                mode=r["mode"],
                http_status=r["http_status"] if r["http_status"] is not None else "",
                bytes=r["bytes"] if r["bytes"] is not None else "",
                pdf_method=r["pdf_method"] or "",
                pdf_pages=r["pdf_pages"] if r["pdf_pages"] is not None else "",
                pdf_chars=r["pdf_chars"] if r["pdf_chars"] is not None else "",
                body_chars=r["body_chars"] if r["body_chars"] is not None else "",
                prefix_found=r["prefix_found"] if r["prefix_found"] is not None else "",
                found_raw=r["found_raw"] if r["found_raw"] is not None else "",
                found_nospace=r["found_nospace"] if r["found_nospace"] is not None else "",
                error=r["error"] or "",
            )
        )
    lines.append("")
    lines.append(
        "excerptや本文の文章そのものはここには書かない（line_idで該当の号JSONの行を参照できる）。"
        "prefix_found/body_charsは、探し方の問題か出典そのものの問題かを切り分けるための目印。"
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
        print(
            f"{r['target_id']}: status={r['http_status']} pdf_method={r['pdf_method']} "
            f"pdf_pages={r['pdf_pages']} body_chars={r['body_chars']} "
            f"prefix_found={r['prefix_found']} found_raw={r['found_raw']} "
            f"found_nospace={r['found_nospace']} error={r['error']}"
        )


if __name__ == "__main__":
    main()
