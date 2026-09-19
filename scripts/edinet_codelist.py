"""EDINETコードリスト(会社の一覧・業種・証券コード)を扱うスクリプト。

EDINETコードリストは、書類一覧API(edinet_fetch.py)とは別物で、固定URLへの
単純なGETで取得できる静的なZIPファイルである。APIキーは一切使わない。

使い方:
  # (a) 取得して .cache/reference/ に保存する(今日の分が既にあれば取得しない)
  python3 scripts/edinet_codelist.py fetch

  # (b) 業種ごとの件数を、件数の多い順に見る
  python3 scripts/edinet_codelist.py industries

  # (c) 業種を指定して、資本金の多い順に会社を引く
  python3 scripts/edinet_codelist.py industry 銀行業 --limit 5

  # (d) ニュースの呼び名 -> 上場会社 の対応表(aliases.csv)を作り直す
  python3 scripts/edinet_codelist.py build-aliases --input aliases_input.csv --out scripts/aliases.csv

終了コード: 0=成功 1=想定内の失敗(通信失敗・保存済みCSV無し等) 2=スクリプト自体のエラー
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import edinet_fetch

CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
CACHE_DIR = Path(".cache/reference")
LOG_PATH = Path("trial/reference/codelist_log.jsonl")
RETRY_WAIT_SECONDS = 5

# CSVの1行目はメタ情報(ダウンロード実行日など)で、列名は2行目にある。
# 列名はEDINET側の原本のまま(全角の「ＥＤＩＮＥＴ」を含む)。
COL_EDINET_CODE = "ＥＤＩＮＥＴコード"
COL_LISTED = "上場区分"
COL_CAPITAL = "資本金"
COL_FILER_NAME = "提出者名"
COL_INDUSTRY = "提出者業種"
COL_TICKER_RAW = "証券コード"

LISTED_VALUE = "上場"

# サービス業／その他製品／その他金融業は業種として広すぎて意味を持たないため、
# 業種から会社を引く仕組みでは使わない(1.4)。
EXCLUDED_INDUSTRIES = {"サービス業", "その他製品", "その他金融業"}
EXCLUDED_REASON = "この業種は対象外です"

ATTRIBUTION_TMPL = (
    "出典：EDINET閲覧（提出）サイト EDINETコードリスト（{url}）、"
    "PDL1.0（https://www.digital.go.jp/resources/open_data/public_data_license_v1.0）"
    "（{date}に取得）"
)
PROCESSING_NOTE = (
    "EDINET閲覧（提出）サイトをもとに本サイト作成"
    "（上場区分が「上場」の行を抽出し、証券コードの末尾0を除いた）"
)

# 法人格の表記ゆれ(前後どちらに付くか・略記か)を吸収するために取り除く語。
# 長い語を先に置くこと(例: 「合同会社」を「合」で誤って壊さないため、
# 単純な部分文字列置換なので順序は結果に影響しないが、意図を明確にするため)。
CORPORATE_DESIGNATORS = [
    "株式会社", "有限会社", "合同会社", "合名会社", "合資会社", "相互会社",
    "（株）", "(株)", "㈱",
    "（有）", "(有)", "㈲",
]


def _normalize_industry_name(s):
    """業種名を比較用に正規化する。NFKC正規化(全角英数字・全角空白を半角に
    揃える)をした上で、前後・途中の空白をすべて除く。中黒(・)と長音(ー)は
    業種名の一部なのでそのまま残す。"""
    normalized = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", normalized)


def _normalize_company_name(s):
    """会社名を比較用に正規化する。NFKC正規化をした上で、法人格(株式会社・
    (株)・㈱・合同会社など、前後どちらに付いていても)と空白を取り除く。"""
    normalized = unicodedata.normalize("NFKC", s or "")
    for token in CORPORATE_DESIGNATORS:
        normalized = normalized.replace(token, "")
    return re.sub(r"\s+", "", normalized)


class CodelistError(Exception):
    """通信・ZIP展開まわりの想定内のエラー(終了コード1で扱う)。"""


def _http_get(url):
    """通信を1回だけ行う。失敗時は状態コードだけを含む例外を投げる。"""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise CodelistError(f"HTTPエラー: 状態コード {e.code}") from None
    except urllib.error.URLError:
        raise CodelistError("通信に失敗しました(接続エラー)") from None


def _http_get_with_retry(url):
    """通信を行う。失敗したら5秒待って1回だけ再試行する。"""
    try:
        return _http_get(url)
    except CodelistError:
        time.sleep(RETRY_WAIT_SECONDS)
        return _http_get(url)


def _today_jst():
    """日本時間の今日の日付を'YYYY-MM-DD'で返す。渡される「今日の日付」はUTC基準の
    ことがあるため、必ずここで日本時間として取り直す。"""
    return dt.datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()


def _format_japanese_date(date_str):
    """'2026-09-19' を '2026年9月19日' の形にする(出典表記用)。"""
    y, m, d = date_str.split("-")
    return f"{int(y)}年{int(m)}月{int(d)}日"


def _parse_csv_text(text):
    """CSV本文(1行目のメタ情報を除いた部分)をパースする。"""
    buf = io.StringIO(text)
    buf.readline()  # 1行目はメタ情報なので読み飛ばす。列名は2行目にある。
    reader = csv.DictReader(buf)
    return list(reader)


def _parse_capital(raw):
    """資本金(百万円)の文字列を整数に変換する。空・数値でなければNoneを返す
    (=資本金の多い順に並べるとき、この行は最後に回す対象)。"""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _dated_csv_path(retrieved_date):
    return CACHE_DIR / f"EdinetcodeDlInfo_{retrieved_date}.csv"


def load_codelist():
    """.cache/reference/ に保存済みのCSVのうち、日付が一番新しいものを読み込む。

    戻り値: (行のリスト, 使ったファイルの取得日"YYYY-MM-DD")。
    保存済みのCSVが1つも無ければ (None, None) を返す(自動ではダウンロードしない)。
    """
    if not CACHE_DIR.is_dir():
        return None, None
    candidates = sorted(CACHE_DIR.glob("EdinetcodeDlInfo_*.csv"))
    if not candidates:
        return None, None
    latest = candidates[-1]
    retrieved_date = latest.stem[len("EdinetcodeDlInfo_"):]
    text = latest.read_bytes().decode("cp932")
    return _parse_csv_text(text), retrieved_date


def _industry_counts(rows):
    """上場かつ証券コードありの行を業種ごとに数え、件数の多い順(同数なら業種名の
    昇順)に並べて返す。戻り値: [(業種名, 件数), ...]。"""
    counts = {}
    for r in rows:
        if r.get(COL_LISTED) != LISTED_VALUE:
            continue
        if not (r.get(COL_TICKER_RAW) or "").strip():
            continue
        industry = r.get(COL_INDUSTRY) or ""
        counts[industry] = counts.get(industry, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _resolve_industry_name(industry, known_industries):
    """指定された業種名を正規化して、コードリストに実在する業種名(表記ゆれ無し
    の原文)に解決する。一致するものが無ければNoneを返す。"""
    target = _normalize_industry_name(industry)
    for name in known_industries:
        if _normalize_industry_name(name) == target:
            return name
    return None


def get_companies_by_industry(industry, limit=5):
    """指定された業種の会社を、上場かつ証券コードありの行から資本金(百万円)の
    多い順にlimit件返す。資本金が同じ場合はEDINETコードの昇順にする(何度実行
    しても同じ順序になるようにするため)。

    業種名の比較はNFKC正規化と空白除去をした上で行う(紙面を作るAI側の文字列に
    空白・全角半角の違いが混じっても、完全一致でないだけで静かに0件になる
    ことを防ぐため)。

    戻り値:
      - 使えない業種(EXCLUDED_INDUSTRIES)が指定された場合:
        {"reason": "この業種は対象外です", "companies": []}
      - 保存済みCSVが無い場合: None(呼び出し側が案内する)
      - 正規化しても一致する業種名がコードリストに無い場合:
        {"error": "指定された業種名がコードリストに見つかりません", "known_industries": [...]}
      - それ以外: {"attribution": ..., "processing_note": ..., "companies": [...]}
    """
    normalized_input = _normalize_industry_name(industry)
    if any(_normalize_industry_name(x) == normalized_input for x in EXCLUDED_INDUSTRIES):
        return {"reason": EXCLUDED_REASON, "companies": []}

    rows, retrieved_date = load_codelist()
    if rows is None:
        return None

    known_industries = [name for name, _cnt in _industry_counts(rows)]
    resolved_industry = _resolve_industry_name(industry, known_industries)
    if resolved_industry is None:
        return {
            "error": "指定された業種名がコードリストに見つかりません",
            "known_industries": known_industries,
        }

    candidates = []
    for r in rows:
        if r.get(COL_LISTED) != LISTED_VALUE:
            continue
        if r.get(COL_INDUSTRY) != resolved_industry:
            continue
        sec_code_raw = (r.get(COL_TICKER_RAW) or "").strip()
        if not sec_code_raw:
            continue
        ticker, _reason = edinet_fetch.derive_ticker(sec_code_raw)
        if ticker is None:
            continue
        candidates.append({
            "company_name": r.get(COL_FILER_NAME),
            "edinet_code": r.get(COL_EDINET_CODE),
            "ticker": ticker,
            "industry": resolved_industry,
            "capital_million": _parse_capital(r.get(COL_CAPITAL)),
            "retrieved_date": retrieved_date,
        })

    candidates.sort(key=lambda c: (
        c["capital_million"] is None,
        -(c["capital_million"] or 0),
        c["edinet_code"],
    ))

    return {
        "attribution": ATTRIBUTION_TMPL.format(url=CODELIST_URL, date=_format_japanese_date(retrieved_date)),
        "processing_note": PROCESSING_NOTE,
        "companies": candidates[:limit],
    }


def cmd_fetch(args):
    today = _today_jst()
    out_path = _dated_csv_path(today)
    if out_path.is_file():
        print(f"取得済み: {out_path}")
        return 0

    try:
        zip_bytes = _http_get_with_retry(CODELIST_URL)
    except CodelistError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            csv_name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if csv_name is None:
                print("ZIP内にCSVファイルが見つかりませんでした", file=sys.stderr)
                return 1
            csv_bytes = zf.read(csv_name)
    except zipfile.BadZipFile:
        print("取得したファイルがZIPとして読めませんでした", file=sys.stderr)
        return 1

    try:
        text = csv_bytes.decode("cp932")
    except UnicodeDecodeError:
        print("CSVの文字コード(cp932)での読み込みに失敗しました", file=sys.stderr)
        return 1

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(csv_bytes)

    rows = _parse_csv_text(text)
    listed = [r for r in rows if r.get(COL_LISTED) == LISTED_VALUE]
    listed_with_ticker = [r for r in listed if (r.get(COL_TICKER_RAW) or "").strip()]

    log_entry = {
        "retrieved_date": today,
        "zip_bytes": len(zip_bytes),
        "csv_bytes": len(csv_bytes),
        "content_sha256": hashlib.sha256(zip_bytes).hexdigest(),
        "rows_total": len(text.splitlines()),
        "listed": len(listed),
        "listed_with_ticker": len(listed_with_ticker),
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    print(f"ZIPのバイト数: {log_entry['zip_bytes']}")
    print(f"CSVのバイト数: {log_entry['csv_bytes']}")
    print(f"行数: {log_entry['rows_total']}")
    print(f"上場: {log_entry['listed']}件")
    print(f"上場かつ証券コードあり: {log_entry['listed_with_ticker']}件")
    return 0


def cmd_industries(args):
    rows, _retrieved_date = load_codelist()
    if rows is None:
        print("先に fetch を実行してください", file=sys.stderr)
        return 1

    counts = _industry_counts(rows)
    for name, cnt in counts:
        print(f"{name}: {cnt}件")
    print(f"業種の数: {len(counts)}")
    return 0


ALIASES_FIELDNAMES = ["news_name", "official_name", "entity_relation", "edinet_code", "ticker"]


def _build_listed_name_index(rows):
    """上場かつ証券コードありの行を、正規化した提出者名をキーにして引けるように
    する。1つの正規化名に複数行が対応することがある(=official_nameだけでは
    会社を1つに決められない)ため、値は常にリストにする。"""
    index = {}
    for r in rows:
        if r.get(COL_LISTED) != LISTED_VALUE:
            continue
        if not (r.get(COL_TICKER_RAW) or "").strip():
            continue
        key = _normalize_company_name(r.get(COL_FILER_NAME))
        index.setdefault(key, []).append(r)
    return index


def build_aliases(input_rows, rows):
    """news_name/official_name/entity_relationの入力行を、コードリストの
    「提出者名」と突き合わせてedinet_code・tickerを引く。

    official_name はEDINETの知識から書かず、必ずここでコードリストから引いた
    値だけを使う(思い出して書いた値には上場廃止・誤番号が混ざるため)。

    戻り値: (resolved, unresolved, ambiguous)
      resolved: aliases.csv に書く5列の辞書のリスト
      unresolved: 1つも当たらなかった official_name のリスト
      ambiguous: (official_name, 当たった行のリスト) のリスト(2件以上当たった場合)
    """
    index = _build_listed_name_index(rows)
    resolved = []
    unresolved = []
    ambiguous = []
    for item in input_rows:
        official_name = item["official_name"]
        matches = index.get(_normalize_company_name(official_name), [])
        if len(matches) == 0:
            unresolved.append(official_name)
            continue
        if len(matches) > 1:
            ambiguous.append((official_name, matches))
            continue
        r = matches[0]
        sec_code_raw = (r.get(COL_TICKER_RAW) or "").strip()
        ticker, _reason = edinet_fetch.derive_ticker(sec_code_raw)
        resolved.append({
            "news_name": item["news_name"],
            "official_name": official_name,
            "entity_relation": item["entity_relation"],
            "edinet_code": r.get(COL_EDINET_CODE),
            "ticker": ticker,
        })
    return resolved, unresolved, ambiguous


def cmd_build_aliases(args):
    with open(args.input, encoding="utf-8", newline="") as f:
        input_rows = list(csv.DictReader(f))

    rows, _retrieved_date = load_codelist()
    if rows is None:
        print("先に fetch を実行してください", file=sys.stderr)
        return 1

    resolved, unresolved, ambiguous = build_aliases(input_rows, rows)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ALIASES_FIELDNAMES)
        writer.writeheader()
        writer.writerows(resolved)

    print(f"書き出した対応表: {out_path} ({len(resolved)}件)")

    if unresolved:
        print("", file=sys.stderr)
        print(f"引けなかった official_name({len(unresolved)}件):", file=sys.stderr)
        for name in unresolved:
            print(f"  ・{name}", file=sys.stderr)

    if ambiguous:
        print("", file=sys.stderr)
        print(f"複数の会社に当たった official_name({len(ambiguous)}件):", file=sys.stderr)
        for name, matches in ambiguous:
            detail = ", ".join(f"{m.get(COL_EDINET_CODE)}({m.get(COL_FILER_NAME)})" for m in matches)
            print(f"  ・{name} → {detail}", file=sys.stderr)

    return 0


def cmd_industry(args):
    result = get_companies_by_industry(args.industry, limit=args.limit)
    if result is None:
        print("先に fetch を実行してください", file=sys.stderr)
        return 1
    if "error" in result:
        print(result["error"], file=sys.stderr)
        print("実在する業種名:", file=sys.stderr)
        for name in result["known_industries"]:
            print(f"  ・{name}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def main():
    parser = argparse.ArgumentParser(description="EDINETコードリストを扱うツール")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch", help="コードリストを取得して保存する(今日の分が既にあれば取得しない)")
    sub.add_parser("industries", help="業種ごとの件数を、件数の多い順に見る")

    p_industry = sub.add_parser("industry", help="業種を指定して、資本金の多い順に会社を引く")
    p_industry.add_argument("industry")
    p_industry.add_argument("--limit", type=int, default=5)

    p_build_aliases = sub.add_parser("build-aliases", help="ニュースの呼び名と上場会社の対応表を作り直す")
    p_build_aliases.add_argument("--input", required=True)
    p_build_aliases.add_argument("--out", required=True)

    args = parser.parse_args()

    if args.command == "fetch":
        return cmd_fetch(args)
    if args.command == "industries":
        return cmd_industries(args)
    if args.command == "industry":
        return cmd_industry(args)
    if args.command == "build-aliases":
        return cmd_build_aliases(args)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
