#!/usr/bin/env python3
"""Task17: GitHub Actions から出典サイトへの到達性・安定性・excerpt照合を測るだけの調査スクリプト。

標準ライブラリのみを使用する（追加パッケージのインストールをしない）。
本文そのもの・excerptの中身は、結果ファイルにもログにも書き込まない。
既存の紙面作成スクリプト・紙面JSONは一切変更しない（読むだけ）。
"""

import hashlib
import json
import socket
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = REPO_ROOT / "trial" / "reports" / "actions_reach_targets.json"
REPORTS_DIR = REPO_ROOT / "trial" / "reports"

TIMEOUT_SECONDS = 30
GAP_BETWEEN_TARGETS_SECONDS = 1
GAP_BEFORE_SECOND_FETCH_SECONDS = 60


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


def fetch(url):
    """1回だけGETする。本文の生バイト列はこの関数の呼び出し中にのみ保持し、返り値には含めない。"""
    result = {
        "http_status": None,
        "final_url": None,
        "elapsed_ms": None,
        "content_type": None,
        "bytes": None,
        "sha256": None,
        "encoding_used": None,
        "is_pdf": None,
        "raw_for_excerpt_check": None,  # 呼び出し元でexcerpt照合にのみ使い、保存はしない
        "error": None,
    }
    req = urllib.request.Request(url, method="GET")
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
        result["error"] = None
    except Exception as e:
        result["error"] = classify_error(e)
    finally:
        result["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)

    result["bytes"] = len(raw)
    if raw:
        result["sha256"] = hashlib.sha256(raw).hexdigest()

    content_type = (result["content_type"] or "").lower()
    is_pdf = "pdf" in content_type or raw[:4] == b"%PDF" or url.lower().endswith(".pdf")
    result["is_pdf"] = is_pdf

    if raw and not is_pdf:
        encoding_used, decoded = decode_body(raw, result["content_type"])
        result["encoding_used"] = encoding_used
        result["raw_for_excerpt_check"] = decoded  # 呼び出し元で使い終わったら破棄する
    return result


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


def check_excerpt(decoded_text, excerpt):
    if excerpt is None:
        return "no_excerpt"
    if decoded_text is None:
        return False
    text_norm = unicodedata.normalize("NFKC", decoded_text)
    excerpt_norm = unicodedata.normalize("NFKC", excerpt)
    return excerpt_norm in text_norm


def run():
    if not TARGETS_PATH.exists():
        raise SystemExit(f"targets file not found: {TARGETS_PATH}")

    targets = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))["targets"]

    rows = []
    for i, target in enumerate(targets):
        target_id = target["target_id"]
        url = target["url"]
        excerpt = target.get("excerpt")
        print(f"[{target_id}] GET {url}")

        first = fetch(url)
        excerpt_found = "no_excerpt" if first["is_pdf"] else check_excerpt(
            first["raw_for_excerpt_check"], excerpt
        )
        # 生本文はここで捨てる（保存しない）
        first["raw_for_excerpt_check"] = None

        row = {
            "target_id": target_id,
            "url": url,
            "publisher": target.get("publisher"),
            "line_id": target.get("line_id"),
            "http_status": first["http_status"],
            "final_url": first["final_url"],
            "elapsed_ms": first["elapsed_ms"],
            "content_type": first["content_type"],
            "bytes": first["bytes"],
            "sha256_1st": first["sha256"],
            "sha256_2nd": None,
            "stable": None,
            "encoding_used": first["encoding_used"],
            "is_pdf": first["is_pdf"],
            "excerpt_found": excerpt_found,
            "error": first["error"],
        }

        print(
            f"    1st: status={row['http_status']} bytes={row['bytes']} "
            f"elapsed_ms={row['elapsed_ms']} error={row['error']}"
        )

        time.sleep(GAP_BEFORE_SECOND_FETCH_SECONDS)

        second = fetch(url)
        second["raw_for_excerpt_check"] = None
        row["sha256_2nd"] = second["sha256"]
        if row["sha256_1st"] is not None and second["sha256"] is not None:
            row["stable"] = row["sha256_1st"] == second["sha256"]
        else:
            row["stable"] = False
        if second["error"] and not row["error"]:
            row["error"] = f"2nd_fetch:{second['error']}"

        print(f"    2nd: status={second['http_status']} stable={row['stable']}")

        rows.append(row)

        if i < len(targets) - 1:
            time.sleep(GAP_BETWEEN_TARGETS_SECONDS)

    return rows


def write_reports(rows):
    now = datetime.now(JST)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    json_path = REPORTS_DIR / f"actions_reach_{stamp}.json"
    md_path = REPORTS_DIR / f"actions_reach_{stamp}.md"

    output = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "targets_file": "trial/reports/actions_reach_targets.json",
        "results": rows,
    }
    json_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        f"# GitHub Actions 到達性テスト結果 ({output['generated_at']})",
        "",
        "| target_id | publisher | http_status | elapsed_ms | bytes | is_pdf | stable | excerpt_found | error |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            "| {target_id} | {publisher} | {http_status} | {elapsed_ms} | {bytes} | "
            "{is_pdf} | {stable} | {excerpt_found} | {error} |".format(
                target_id=r["target_id"],
                publisher=r["publisher"] or "",
                http_status=r["http_status"],
                elapsed_ms=r["elapsed_ms"],
                bytes=r["bytes"],
                is_pdf=r["is_pdf"],
                stable=r["stable"],
                excerpt_found=r["excerpt_found"],
                error=r["error"] or "",
            )
        )
    lines.append("")
    lines.append("URL・final_url・content_typeなど詳細は同じ時刻の .json ファイルを参照。")
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
            f"{r['target_id']}: status={r['http_status']} stable={r['stable']} "
            f"excerpt_found={r['excerpt_found']} error={r['error']}"
        )


if __name__ == "__main__":
    main()
