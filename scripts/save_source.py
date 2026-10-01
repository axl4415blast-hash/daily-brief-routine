"""出典(PDF・HTML)を取得して、照合に使う本文(.txt)を機械で保存するスクリプト(改修28第2回)。

紙面を作るAIが出典の本文ファイルを自分で書くと、表を見て言葉を足した文など、実物に無い文が
本文に入り込んでも照合を通ってしまう(9/20夕号の貿易統計の10行)。このスクリプトは、取得から
文字にするところまでを機械で行い、元のファイルと記録ファイルも残す。照合(verify_edition.py)は
記録ファイルをもとに、元のファイルからもう一度文字にした結果が本文と一致するかを記録する。

使い方:
  python3 scripts/save_source.py --id SRC-003 --url https://www.customs.go.jp/...pdf
  (--out-dir を省略すると .cache/sources に保存する)

受け付けるURL: scripts/source_policy.csv で usage が quotable のドメインだけ。それ以外は通信せずに断る。
EDINETのドメイン(*.edinet-fsa.go.jp)は、edinet_fetch.py を使うよう案内して断る。

保存するもの(--out-dir の中):
  {id}.txt          本文(UTF-8)。紙面の content_sha256 はこのファイルのハッシュ
  raw/{id}.pdf      元のファイル(HTMLなら raw/{id}.html)
  {id}.meta.json    記録ファイル(取得元・取得日時・道具とその版・元のファイルと本文のハッシュなど)

同じIDのファイルが1つでもあれば断る(上書きしない)。失敗したら何も残さない。
成功したら、紙面の sources にそのまま写せる値(source_id・url・fetched_at・fetch_method・
content_sha256)を表示する。

終了コード: 0=成功 1=想定内の失敗(受け付けないURL・通信失敗・文字にできない等) 2=スクリプト自体のエラー
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import verify_edition

_SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_POLICY_PATH = _SCRIPT_DIR / "source_policy.csv"
DEFAULT_OUT_DIR = ".cache/sources"

FETCH_METHOD = "urllib"
FETCH_TIMEOUT_SECONDS = 60
MAX_BYTES = 50 * 1024 * 1024
# 名乗らないと(Pythonの既定の名乗り)、FRBのサイトは403を返す(改修28第2回で確認)。
USER_AGENT = "daily-brief-routine save_source.py (+https://github.com/axl4415blast-hash/daily-brief-routine)"

PDFTOTEXT = "pdftotext"
PDFTOTEXT_ARGS = ["-layout", "-enc", "UTF-8"]
HTML_TOOL = "verify_edition.strip_html_tags"

# 取れた文字(空白を除く)がこれより少なければ失敗にする(poppler-dataが無いと、税関のPDFから
# 26文字しか取れなかった)。置き換え文字(U+FFFD)が、取れた文字(空白を除く)のこの割合を超えても失敗。
MIN_TEXT_CHARS = 100
MAX_REPLACEMENT_RATIO = 0.01

_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)
_META_TAG_RE = re.compile(rb"<meta\b[^>]*>", re.IGNORECASE)
# WHATWGの文字コードの規則で、Shift_JISの名前はWindowsの拡張(cp932)として読む。
_ENCODING_ALIASES = {
    "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932",
    "ms_kanji": "cp932", "csshiftjis": "cp932", "windows-31j": "cp932", "ms932": "cp932",
}

# 照合の側(verify_edition.py)とテストが差し替えられるよう、道具を探す関数をここに置く。
find_tool = shutil.which


class SaveSourceError(Exception):
    """想定内の失敗(終了コード1で扱う)。"""


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def count_text_chars(text):
    """空白(全角を含む)を除いた文字数。"""
    return sum(1 for ch in text if not ch.isspace())


def check_url_allowed(url, policy):
    """URLが受け付けられるかを確かめる(通信はしない)。受け付けないならSaveSourceErrorを投げる。"""
    if not isinstance(url, str) or not re.match(r"^https?://", url, re.IGNORECASE):
        raise SaveSourceError(f"URLは http:// か https:// で始まる必要があります: {url!r}")
    if verify_edition.is_edinet_domain(url):
        raise SaveSourceError(
            "EDINETの書類は、このスクリプトではなく edinet_fetch.py で保存してください"
            "(例: python3 scripts/edinet_fetch.py doc --doc-id S100XXXX --type 1 --out .cache/sources/SRC-005.txt)。"
        )
    host = verify_edition.source_hostname(url)
    entry = policy.get(host) if host else None
    if entry is None or entry.get("usage") != "quotable":
        usage = entry.get("usage") if entry else "表に無い"
        raise SaveSourceError(
            f"このドメイン({host})は本文を保存できる出典ではありません(source_policy.csv の usage: {usage})。"
            "quotable のドメインだけを受け付けます。"
        )


def _make_redirect_handler(policy):
    class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            # 転送先も、受け付けるドメインでなければ通信せずに失敗にする。
            check_url_allowed(newurl, policy)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    return _CheckedRedirectHandler


def fetch_url(url, policy):
    """urllibで取得する。戻り値: (最終URL, Content-Type, 本体のバイト列)。
    テストはこの関数の代わりに、同じ形の値を返す関数を渡す(通信しない)。"""
    opener = urllib.request.build_opener(_make_redirect_handler(policy))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_BYTES:
                raise SaveSourceError(f"ファイルが大きすぎます({length}バイト。上限{MAX_BYTES}バイト)。")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise SaveSourceError(f"ファイルが大きすぎます(上限{MAX_BYTES}バイト)。")
            return response.geturl(), response.headers.get("Content-Type"), data
    except SaveSourceError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SaveSourceError(f"取得に失敗しました: {e}")


def detect_kind(data, content_type):
    """先頭が %PDF ならPDF。Content-TypeがHTMLか、先頭(空白・BOMを除く)が「<」ならHTML。
    それ以外はSaveSourceError。"""
    if data.startswith(b"%PDF"):
        return "pdf"
    head = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    if "html" in (content_type or "").lower() or head.lstrip().startswith(b"<"):
        return "html"
    raise SaveSourceError(f"PDFでもHTMLでもないファイルです(Content-Type: {content_type!r})。")


def pdftotext_version(tool_path):
    """pdftotext -v の出力から版を取り出す(取れなければNone)。"""
    try:
        result = subprocess.run([tool_path, "-v"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(rb"pdftotext version (\S+)", result.stdout + result.stderr)
    return match.group(1).decode("ascii", "replace") if match else None


def pdf_to_text(data):
    """pdftotext -layout -enc UTF-8 で文字にする。戻り値: (本文, 道具の版)。"""
    tool_path = find_tool(PDFTOTEXT)
    if not tool_path:
        raise SaveSourceError("pdftotext がありません(poppler-utils を入れてください)。")
    with tempfile.TemporaryDirectory() as d:
        pdf_path = Path(d) / "in.pdf"
        pdf_path.write_bytes(data)
        try:
            result = subprocess.run(
                [tool_path, *PDFTOTEXT_ARGS, str(pdf_path), "-"], capture_output=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise SaveSourceError(f"pdftotext を実行できませんでした: {e}")
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()[:300]
        raise SaveSourceError(f"pdftotext が失敗しました(終了コード{result.returncode}): {message}")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SaveSourceError(f"pdftotext の出力がUTF-8として読めません: {e}")
    return text, pdftotext_version(tool_path)


def decide_html_encoding(data, content_type):
    """HTMLの文字コードを、応答ヘッダー → metaタグ → UTF-8 の順に決める(推測はしない)。
    戻り値: (Pythonで使う文字コード名, どこから決めたか)。"""
    match = _CHARSET_RE.search((content_type or "").encode("latin-1", "replace"))
    found_in = "header"
    if not match:
        found_in = "meta"
        for tag in _META_TAG_RE.finditer(data[:65536]):
            match = _CHARSET_RE.search(tag.group())
            if match:
                break
    if not match:
        return "utf-8", "default"
    name = match.group(1).decode("ascii", "replace").strip().lower()
    return _ENCODING_ALIASES.get(name, name), found_in


def html_to_text(data, content_type):
    """HTMLを文字にする(文字コードを決めて読み、タグを除く)。読めなければSaveSourceError。
    戻り値: (本文, 文字コード名)。"""
    encoding, _found_in = decide_html_encoding(data, content_type)
    try:
        text = data.decode("utf-8-sig" if encoding in ("utf-8", "utf8") else encoding)
    except LookupError:
        raise SaveSourceError(f"HTMLの文字コード({encoding})を知りません。")
    except UnicodeDecodeError as e:
        raise SaveSourceError(f"HTMLを文字コード {encoding} で読めませんでした: {e}")
    return verify_edition.strip_html_tags(text), encoding


def convert(kind, data, content_type):
    """元のファイルを文字にする(保存のときと、照合でもう一度文字にするときの共通の処理)。
    戻り値: {"text", "tool", "tool_version", "html_encoding"}。"""
    if kind == "pdf":
        text, version = pdf_to_text(data)
        return {"text": text, "tool": PDFTOTEXT, "tool_version": version, "html_encoding": None}
    if kind == "html":
        text, encoding = html_to_text(data, content_type)
        return {"text": text, "tool": HTML_TOOL, "tool_version": sys.version.split()[0], "html_encoding": encoding}
    raise SaveSourceError(f"文字にできない種類です: {kind!r}")


def check_text_quality(text):
    """取れた文字が少なすぎる・置き換え文字が多すぎる場合はSaveSourceError。
    戻り値: (空白を除いた文字数, 置き換え文字の数)。"""
    chars = count_text_chars(text)
    replacements = text.count("�")
    if chars < MIN_TEXT_CHARS:
        raise SaveSourceError(
            f"取れた文字(空白を除く)が{chars}文字で、{MIN_TEXT_CHARS}文字に足りません"
            "(PDFなら poppler-data が無い、画像だけのPDFなどが考えられます)。"
        )
    if replacements > chars * MAX_REPLACEMENT_RATIO:
        raise SaveSourceError(f"読めない文字(置き換え文字U+FFFD)が{replacements}個あり、全体の1%を超えています。")
    return chars, replacements


def output_paths(out_dir, source_id):
    out_dir = Path(out_dir)
    return {
        "text": out_dir / f"{source_id}.txt",
        "meta": out_dir / f"{source_id}.meta.json",
        "raw_pdf": out_dir / "raw" / f"{source_id}.pdf",
        "raw_html": out_dir / "raw" / f"{source_id}.html",
    }


def _write_files_no_overwrite(files):
    """files: [(保存先のPath, バイト列), ...]。一時ファイルに書いてから、上書きしない形(os.link)で
    名前を付ける。途中で失敗したら、このとき作ったファイル・フォルダをすべて消してから例外を投げる。"""
    created_dirs = []
    temps = []
    finals = []
    try:
        for path, _data in files:
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for d in reversed(missing):
                d.mkdir()
                created_dirs.append(d)
        for path, data in files:
            fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
            temps.append(Path(tmp))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        for (path, _data), tmp in zip(files, temps):
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise SaveSourceError(f"同じ名前のファイルが既にあります(上書きしません): {path}")
            finals.append(path)
    except BaseException:
        for p in finals:
            p.unlink(missing_ok=True)
        for p in temps:
            p.unlink(missing_ok=True)
        for d in reversed(created_dirs):
            try:
                d.rmdir()
            except OSError:
                pass
        raise
    for p in temps:
        p.unlink(missing_ok=True)


def save_source(source_id, url, out_dir=DEFAULT_OUT_DIR, fetcher=None, now=None, policy_path=SOURCE_POLICY_PATH):
    """出典を取得して保存する。fetcher(url, policy)は (最終URL, Content-Type, バイト列) を返す関数で、
    省略すると fetch_url(通信する)。テストは通信しない関数を渡す。
    戻り値: (紙面の sources にそのまま写せる値(source_id・url・fetched_at・fetch_method・content_sha256),
             記録ファイルの中身)。"""
    if not isinstance(source_id, str) or not _SOURCE_ID_RE.match(source_id):
        raise SaveSourceError(f"--id は英数字・ハイフン・下線だけにしてください: {source_id!r}")
    try:
        policy = verify_edition.load_source_policy(policy_path)
    except verify_edition.EditionInvalid as e:
        raise SaveSourceError(str(e))
    check_url_allowed(url, policy)
    paths = output_paths(out_dir, source_id)
    existing = [str(p) for p in paths.values() if p.exists()]
    if existing:
        raise SaveSourceError(f"同じIDのファイルが既にあります(上書きしません): {', '.join(existing)}")

    fetched_at = (now or dt.datetime.now(verify_edition.JST)).astimezone(verify_edition.JST).replace(microsecond=0).isoformat()
    final_url, content_type, data = (fetcher or fetch_url)(url, policy)
    # 転送されていた場合も、最後のURLが受け付けるドメインかを確かめる。
    check_url_allowed(final_url, policy)
    kind = detect_kind(data, content_type)
    converted = convert(kind, data, content_type)
    text = converted["text"]
    text_chars, replacement_chars = check_text_quality(text)

    text_bytes = text.encode("utf-8")
    content_sha256 = sha256_hex(text_bytes)
    raw_path = paths["raw_pdf"] if kind == "pdf" else paths["raw_html"]
    meta = {
        "source_id": source_id,
        "url": url,
        "final_url": final_url,
        "fetched_at": fetched_at,
        "fetch_method": FETCH_METHOD,
        "kind": kind,
        "content_type": content_type,
        "tool": converted["tool"],
        "tool_version": converted["tool_version"],
        "tool_args": PDFTOTEXT_ARGS if kind == "pdf" else None,
        "html_encoding": converted["html_encoding"],
        "raw_file": f"raw/{raw_path.name}",
        "raw_sha256": sha256_hex(data),
        "raw_bytes": len(data),
        "content_sha256": content_sha256,
        "text_chars": text_chars,
        "replacement_chars": replacement_chars,
    }
    meta_bytes = (json.dumps(meta, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    # 記録ファイルを最後に置く(記録ファイルがあれば、本文と元のファイルもそろっている)。
    _write_files_no_overwrite([(raw_path, data), (paths["text"], text_bytes), (paths["meta"], meta_bytes)])
    return {
        "source_id": source_id,
        "url": url,
        "fetched_at": fetched_at,
        "fetch_method": FETCH_METHOD,
        "content_sha256": content_sha256,
    }, meta


def main(argv=None):
    parser = argparse.ArgumentParser(description="出典(PDF・HTML)を取得して、照合に使う本文を保存する")
    parser.add_argument("--id", required=True, help="出典ID(例: SRC-003)")
    parser.add_argument("--url", required=True, help="出典のURL(source_policy.csv で quotable のドメインだけ)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help=f"保存先のフォルダ(省略時 {DEFAULT_OUT_DIR})")
    args = parser.parse_args(argv)
    try:
        values, meta = save_source(args.id, args.url, args.out_dir)
    except SaveSourceError as e:
        print(f"[失敗] {e}", file=sys.stderr)
        return 1
    print(f"保存しました: {Path(args.out_dir) / (args.id + '.txt')}"
          f"(種類: {meta['kind']}、道具: {meta['tool']} {meta['tool_version']}、本文 {meta['text_chars']}文字)")
    print("紙面の sources に写す値:")
    print(json.dumps(values, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
