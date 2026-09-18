"""EDINET(金融庁の開示システム)のAPIキーに触るコードを1本にまとめたスクリプト。

他のスクリプトや紙面作成のルーティンは、EDINETの情報が必要なときは必ず
このファイル(コマンドラインとして)を経由する。鍵を直接読む・URLを組み立てる
コードをここ以外に置かないこと。

使い方:
  # (a) その日の書類一覧を取る(.cache/edinet/companies.json も同時に作る)
  python3 scripts/edinet_fetch.py list --date 2026-09-24 --out .cache/sources/SRC-EDINET-LIST.json

  # (b) 書類の中身を取る(ZIPなら展開し、文字コードを判定してUTF-8で保存する)
  python3 scripts/edinet_fetch.py doc --doc-id S100XXXX --type 1 --out .cache/sources/SRC-005.txt

  # (c) 証券コードを問い合わせる(通信しない。保存済みのファイルだけを読む)
  python3 scripts/edinet_fetch.py ticker --list .cache/sources/SRC-EDINET-LIST.json --name "○○製作所株式会社"

終了コード: 0=成功 1=想定内の失敗(鍵未設定・通信失敗・該当なし等) 2=スクリプト自体のエラー

鍵の扱い:
  - EDINET_API_KEY 環境変数から読む。
  - 鍵の値は標準出力・標準エラー出力・例外メッセージ・保存するファイルの
    どこにも出さない。URLを組み立てるのは _build_url() の中だけであり、
    エラー時にURL全体を表示することもしない(鍵が混ざるため)。
"""
import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlencode

EDINET_LIST_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
EDINET_DOC_URL_TMPL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
COMPANIES_CACHE_PATH = Path(".cache/edinet/companies.json")
RATE_LIMIT_SECONDS = 5

TEXT_EXTENSIONS = (".txt", ".csv", ".htm", ".html", ".xml", ".xbrl")

UTF8_BOM = b"\xef\xbb\xbf"
UTF16LE_BOM = b"\xff\xfe"
UTF16BE_BOM = b"\xfe\xff"

SEC_CODE_REASON_TEXT = {
    "sec_code_null": "証券コードがnull",
    "sec_code_empty": "証券コードが空",
    "sec_code_not_5chars": "証券コードが5文字でない",
    "sec_code_not_ending_zero": "証券コードの末尾が0でない",
    "sec_code_unexpected_format": "証券コードの形が想定と違う",
}

# 証券コード協議会は2024年1月4日以降に新規上場承認を受けた株式について、証券コードの
# 2桁目・4桁目のいずれか、または両方に英大文字を組み入れている(例: 130A)。数字と紛らわしい
# B・E・I・O・Q・V・Z の7文字は使われないため、残り19文字だけを許可する。小文字は許可しない。
ALLOWED_CODE_LETTERS = "ACDFGHJKLMNPRSTUWXY"


class EdinetError(Exception):
    """通信・APIまわりの想定内のエラー(終了コード1で扱う)。"""


def _get_api_key():
    return os.environ.get("EDINET_API_KEY")


def _require_api_key():
    key = _get_api_key()
    if not key:
        print("EDINET_API_KEY が設定されていません", file=sys.stderr)
        return None
    return key


def _build_url(base, params):
    """URLを組み立てる唯一の関数。鍵を含むクエリを作るのはここだけにする。"""
    return f"{base}?{urlencode(params)}"


def _http_get(url):
    """通信を行う唯一の関数。失敗時は状態コードだけを含む例外を投げる(URL・鍵は含めない)。"""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise EdinetError(f"HTTPエラー: 状態コード {e.code}") from None
    except urllib.error.URLError:
        raise EdinetError("通信に失敗しました(接続エラー)") from None


def is_valid_ticker(ticker):
    """4文字の証券コードが、想定する形に合っているかどうかを判定する。

    1文字目・3文字目は数字。2文字目・4文字目は数字、または ALLOWED_CODE_LETTERS の
    英大文字。小文字は認めない。derive_ticker()と verify_edition.py の検査13が
    どちらもこの関数を使う(同じ判定を2か所に書かないため)。
    """
    if not isinstance(ticker, str) or len(ticker) != 4:
        return False
    if not (ticker[0].isdigit() and ticker[2].isdigit()):
        return False
    for i in (1, 3):
        if not (ticker[i].isdigit() or ticker[i] in ALLOWED_CODE_LETTERS):
            return False
    return True


def derive_ticker(sec_code_raw):
    """secCodeから4桁(英字混在を含む)の証券コードを作る。作れないときは (None, 理由) を返す。

    5桁で末尾が'0'のときだけ末尾の1文字を落として先頭4文字を候補にする。数字かどうかでは
    なく、is_valid_ticker() による形の一致で判定する(2024年1月4日以降に新規上場承認を
    受けた株式は、証券コードの2桁目・4桁目のいずれか、または両方に英大文字が入るため)。
    """
    if sec_code_raw is None:
        return None, "sec_code_null"
    s = str(sec_code_raw).strip()
    if s == "":
        return None, "sec_code_empty"
    if len(s) != 5:
        return None, "sec_code_not_5chars"
    if not s.endswith("0"):
        return None, "sec_code_not_ending_zero"
    candidate = s[:4]
    if not is_valid_ticker(candidate):
        return None, "sec_code_unexpected_format"
    return candidate, None


def build_companies(raw_doc):
    """EDINET書類一覧APIの生レスポンス(documents.json)から companies.json 用の配列を作る。

    戻り値: (companies配列, 理由ごとの件数dict)
    """
    results = raw_doc.get("results") or []
    companies = []
    reason_counts = {}
    for r in results:
        sec_code_raw = r.get("secCode")
        ticker, reason = derive_ticker(sec_code_raw)
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        companies.append({
            "edinet_code": r.get("edinetCode"),
            "filer_name": r.get("filerName"),
            "sec_code_raw": sec_code_raw,
            "ticker": ticker,
            "doc_id": r.get("docID"),
            "doc_type_code": r.get("docTypeCode"),
            "doc_description": r.get("docDescription"),
            "submit_date_time": r.get("submitDateTime"),
        })
    return companies, reason_counts


def extract_text_payload(raw_bytes):
    """レスポンスがZIPならテキスト系ファイルの中でいちばん大きいものを選んで返す。
    ZIPでなければそのまま返す。戻り値: (bytes, 選んだファイル名 or None)。"""
    if raw_bytes[:2] != b"PK":
        return raw_bytes, None
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        candidates = [
            info for info in zf.infolist()
            if not info.is_dir() and Path(info.filename).suffix.lower() in TEXT_EXTENSIONS
        ]
        if not candidates:
            raise EdinetError("ZIP内にテキスト系ファイルが見つかりませんでした")
        chosen = max(candidates, key=lambda info: info.file_size)
        return zf.read(chosen.filename), chosen.filename


def decode_bytes(raw_bytes):
    """文字コードを判定してUTF-8のテキストに変換する。

    判定の順序(変えないこと):
      1. 先頭のBOM(UTF-8 / UTF-16LE / UTF-16BE)を見る。あればそれとして読む。
      2. BOMが無ければUTF-8として読む。
      3. 失敗したらcp932(Shift_JIS)として読む。
      4. すべて失敗したら (None, None) を返す(=変換しない。文字化けしたまま
         読めてしまうことを防ぐため、BOMが無いファイルをUTF-16として読もうとはしない)。

    戻り値: (テキスト or None, 使った文字コードの表示名 or None)
    """
    if raw_bytes[:3] == UTF8_BOM:
        try:
            return raw_bytes[3:].decode("utf-8"), "utf-8(BOM付き)"
        except UnicodeDecodeError:
            return None, None
    if raw_bytes[:2] == UTF16LE_BOM:
        try:
            return raw_bytes[2:].decode("utf-16-le"), "utf-16le(BOM付き)"
        except UnicodeDecodeError:
            return None, None
    if raw_bytes[:2] == UTF16BE_BOM:
        try:
            return raw_bytes[2:].decode("utf-16-be"), "utf-16be(BOM付き)"
        except UnicodeDecodeError:
            return None, None
    try:
        return raw_bytes.decode("utf-8"), "utf-8(BOM無し)"
    except UnicodeDecodeError:
        pass
    try:
        return raw_bytes.decode("cp932"), "cp932"
    except UnicodeDecodeError:
        pass
    return None, None


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_strip(s):
    """前後の空白(半角・全角とも)を取り除く。str.strip()は全角空白(U+3000)も
    空白として扱うため、これだけで前後の全角空白も除去できる。"""
    return (s or "").strip()


def find_company(companies, name):
    """filer_nameが完全一致する会社を探す。見つからなければ、前後の空白を
    取り除いた上でもう一度だけ試す。それ以上のあいまい一致はしない。"""
    for c in companies:
        if c.get("filer_name") == name:
            return c
    stripped_name = normalize_strip(name)
    for c in companies:
        if normalize_strip(c.get("filer_name")) == stripped_name:
            return c
    return None


def cmd_list(args):
    api_key = _require_api_key()
    if not api_key:
        return 1

    url = _build_url(EDINET_LIST_URL, {"date": args.date, "type": "2", "Subscription-Key": api_key})
    time.sleep(RATE_LIMIT_SECONDS)
    try:
        raw_bytes = _http_get(url)
    except EdinetError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        text = raw_bytes.decode("utf-8")
        raw_doc = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"取得した書類一覧の解析に失敗しました: {type(e).__name__}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    companies, reason_counts = build_companies(raw_doc)

    COMPANIES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(COMPANIES_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(companies, f, ensure_ascii=False, indent=1)

    total = len(companies)
    with_sec_code = sum(1 for c in companies if c["sec_code_raw"])
    type_counts = {}
    for c in companies:
        t = c["doc_type_code"]
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"取得件数: {total}")
    print(f"証券コードが付いている件数: {with_sec_code}")
    print("書類種別ごとの件数:")
    for t, cnt in sorted(type_counts.items(), key=lambda kv: (kv[0] is None, str(kv[0]))):
        print(f"  ・{t}: {cnt}件")

    if reason_counts:
        parts = [f"{SEC_CODE_REASON_TEXT.get(r, r)}={cnt}件" for r, cnt in reason_counts.items()]
        print("tickerを作れなかった件数の内訳: " + ", ".join(parts), file=sys.stderr)

    return 0


def cmd_doc(args):
    api_key = _require_api_key()
    if not api_key:
        return 1

    url = _build_url(
        EDINET_DOC_URL_TMPL.format(doc_id=args.doc_id),
        {"type": args.type, "Subscription-Key": api_key},
    )
    time.sleep(RATE_LIMIT_SECONDS)
    try:
        raw_bytes = _http_get(url)
    except EdinetError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        payload, chosen_name = extract_text_payload(raw_bytes)
    except EdinetError as e:
        print(str(e), file=sys.stderr)
        return 1
    if chosen_name:
        print(f"選んだファイル: {chosen_name}")

    text, encoding_used = decode_bytes(payload)
    if text is None:
        print(
            "文字コードを判定できませんでした(UTF-8・cp932のいずれでも読めません)。"
            "変換せずに終了します。",
            file=sys.stderr,
        )
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"文字コード判定: {encoding_used}")
    print(f"SHA256: {digest}")
    return 0


def cmd_ticker(args):
    if args.list:
        try:
            raw_doc = load_json(args.list)
        except (OSError, json.JSONDecodeError) as e:
            print(f"一覧ファイルを読み込めません: {type(e).__name__}", file=sys.stderr)
            return 1
        companies, _ = build_companies(raw_doc)
    else:
        if not COMPANIES_CACHE_PATH.is_file():
            print(f"{COMPANIES_CACHE_PATH} が見つかりません", file=sys.stderr)
            return 1
        try:
            companies = load_json(COMPANIES_CACHE_PATH)
        except (OSError, json.JSONDecodeError) as e:
            print(f"会社一覧を読み込めません: {type(e).__name__}", file=sys.stderr)
            return 1

    company = find_company(companies, args.name)
    if company is None:
        print(f"'{args.name}' に完全一致する会社が見つかりませんでした", file=sys.stderr)
        return 1

    ticker = company.get("ticker")
    if not ticker:
        print(f"'{args.name}' は見つかりましたが、証券コードがありません", file=sys.stderr)
        return 1

    print(ticker)
    return 0


def main():
    parser = argparse.ArgumentParser(description="EDINET APIへの唯一の窓口")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="その日の書類一覧を取る")
    p_list.add_argument("--date", required=True)
    p_list.add_argument("--out", required=True)

    p_doc = sub.add_parser("doc", help="書類の中身を取る")
    p_doc.add_argument("--doc-id", required=True)
    p_doc.add_argument("--type", required=True, choices=["1", "5"])
    p_doc.add_argument("--out", required=True)

    p_ticker = sub.add_parser("ticker", help="証券コードを問い合わせる(通信しない)")
    p_ticker.add_argument("--list")
    p_ticker.add_argument("--name", required=True)

    args = parser.parse_args()

    if args.command == "list":
        return cmd_list(args)
    if args.command == "doc":
        return cmd_doc(args)
    if args.command == "ticker":
        return cmd_ticker(args)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
