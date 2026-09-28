"""直近3営業日分の完成した号から、記事ごとの見出し・号・出典URLを一覧にするスクリプト。

紙面を書くAIが、直近の号と同じ話を重ねて書かないよう参考にするためのもの。

使い方:
  python3 scripts/recent_headlines.py --date 2026-09-28 --slot evening \
      --calendar calendar --editions-index editions/index.json \
      --out .cache/sources/RECENT-HEADLINES.json

「完成した号」とは editions/index.json に載っている号のこと(build_index.pyが
verification(照合結果)の無い号を一覧から外すため、未照合の号はここでも
対象にならない)。

「3営業日」の数え方は、verify_edition.recent_business_days()を使う(1か所に
まとめてあり、27-2で作る続報の判定からも同じ関数を使う)。--date自身を含めて
直近3営業日分を数える。対象になる号は、その3営業日のうち --date より前の日の
号すべてと、--date当日については --slot より前の時間帯(朝<昼<夕方)の号だけ
(まだ発行されていない、または今まさに検査中の号自身は含めない)。

改修27-1(4-11): このスクリプトは実行のたびに必ず --out へ結果を書く。成功すれば
status:"ok"、想定内の失敗(一覧が読めない等)ならstatus:"error"で理由を添えて
書く(このファイル自体が書けない場合を除き、必ず何かを書く)。呼び出し側の
照合スクリプト(verify_edition.py)は、この1つのファイルの中身(status・
date・slot)を見るだけで、このスクリプトが今回の号のために正しく実行された
かどうかを知れる(「印のファイル」を、専用の別ファイルにせず、この出力ファイル
自身のstatus欄で兼ねる方式)。

紙面を書くAI自身がこの一覧を必要としても、このスクリプトが失敗したという
理由だけで号の作成を止める必要はない(失敗は記録されるだけで、それ以上の
強制力は持たない)。

終了コード: 0=成功 1=想定内の失敗(一覧・カレンダーが読めない等) 2=スクリプト自体のエラー
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_edition as ve

DEFAULT_CALENDAR_DIR = "calendar"
DEFAULT_EDITIONS_INDEX = "editions/index.json"
DEFAULT_OUT_PATH = ".cache/sources/RECENT-HEADLINES.json"
WINDOW_BUSINESS_DAYS = 3

SLOT_ORDER = {"morning": 0, "noon": 1, "evening": 2}


class RecentHeadlinesError(Exception):
    """想定内の失敗(終了コード1で扱う)。"""


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_before(entry_date, entry_slot, target_date, target_slot):
    """entry(既にある号)が、target(今回の号)より前かどうかを判定する。
    日付が違えばその前後だけで決まる。同じ日付なら、時間帯の順(朝<昼<夕方)で
    比べる(依頼のQ5の回答: 同じ日の、今の号より前の時間帯の号も含める)。
    slot名が想定外(SLOT_ORDERに無い)の場合は、安全側でFalse(対象外)にする。"""
    if entry_date != target_date:
        return entry_date < target_date
    entry_order = SLOT_ORDER.get(entry_slot)
    target_order = SLOT_ORDER.get(target_slot)
    if entry_order is None or target_order is None:
        return False
    return entry_order < target_order


def select_recent_index_entries(index_entries, window_dates, target_date, target_slot):
    """editions/index.jsonのentries(build_index.pyが書く形)から、直近3営業日の
    窓(window_dates)に入り、かつtargetより前の号だけを選ぶ。"""
    window_set = set(window_dates)
    selected = [
        e for e in index_entries
        if e.get("date") in window_set and is_before(e.get("date"), e.get("slot"), target_date, target_slot)
    ]
    selected.sort(key=lambda e: (e.get("date", ""), SLOT_ORDER.get(e.get("slot"), -1)))
    return selected


def collect_articles(entry):
    """1つの号の実ファイル(edition_path。editions/index.jsonに書かれている、
    実行時のカレントディレクトリからの相対パス)を読み、記事ごとの見出し・
    出典URLを取り出す。号のファイル自体が読めない場合は、その号だけ飛ばす
    (一覧全体は失敗にしない)。"""
    edition_path = Path(entry["edition_path"])
    try:
        edition = load_json(edition_path)
    except (OSError, json.JSONDecodeError):
        return []

    sources_by_id = {s.get("source_id"): s for s in edition.get("sources") or []}
    articles = []
    for section in edition.get("sections") or []:
        for article in section.get("articles") or []:
            source_urls = []
            seen = set()
            for line in article.get("lines") or []:
                source = sources_by_id.get(line.get("source_ref"))
                url = source.get("url") if source else None
                if url and url not in seen:
                    seen.add(url)
                    source_urls.append(url)
            articles.append({
                "edition_id": entry.get("edition_id"),
                "date": entry.get("date"),
                "slot": entry.get("slot"),
                "section_id": section.get("section_id"),
                "article_id": article.get("article_id"),
                "headline": article.get("headline"),
                "source_urls": source_urls,
            })
    return articles


def build_result(target_date, target_slot, calendar_dir, editions_index_path):
    calendar_path = Path(calendar_dir)
    if not calendar_path.is_dir():
        raise RecentHeadlinesError(f"営業日カレンダー '{calendar_path}' がありません。")
    business_days = ve.load_business_days(calendar_path)
    if not business_days:
        raise RecentHeadlinesError(f"営業日カレンダー '{calendar_path}' から営業日を読み取れません。")

    window_dates = ve.recent_business_days(business_days, target_date, WINDOW_BUSINESS_DAYS)

    index_path = Path(editions_index_path)
    if not index_path.is_file():
        raise RecentHeadlinesError(f"号の一覧 '{index_path}' がありません。")
    try:
        index_doc = load_json(index_path)
    except (OSError, json.JSONDecodeError) as e:
        raise RecentHeadlinesError(f"号の一覧 '{index_path}' を読み込めません: {e}") from None

    index_entries = index_doc.get("editions")
    if not isinstance(index_entries, list):
        raise RecentHeadlinesError(f"号の一覧 '{index_path}' のeditionsが配列ではありません。")

    selected_entries = select_recent_index_entries(index_entries, window_dates, target_date, target_slot)

    articles = []
    for entry in selected_entries:
        articles.extend(collect_articles(entry))

    return {
        "status": "ok",
        "date": target_date,
        "slot": target_slot,
        "generated_at": dt.datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
        "window_business_days": window_dates,
        "editions": [e.get("edition_id") for e in selected_entries],
        "articles": articles,
    }


def write_output(out_path, record):
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="号の日付(YYYY-MM-DD)")
    parser.add_argument("--slot", required=True, choices=["morning", "noon", "evening"])
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR_DIR)
    parser.add_argument("--editions-index", default=DEFAULT_EDITIONS_INDEX)
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    args = parser.parse_args()

    try:
        result = build_result(args.date, args.slot, args.calendar, args.editions_index)
    except RecentHeadlinesError as e:
        # 改修27-1(4-11): 失敗しても、印のファイル自体は必ず書く(status:"error")。
        # verify_edition.check_recent_headlines_status()がこれを読んで
        # recent_headlines_failedを記録する。
        error_record = {
            "status": "error", "date": args.date, "slot": args.slot,
            "generated_at": dt.datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
            "error": str(e),
        }
        try:
            write_output(args.out, error_record)
        except OSError:
            pass
        print(str(e), file=sys.stderr)
        return 1

    write_output(args.out, result)
    print(f"直近{WINDOW_BUSINESS_DAYS}営業日分の号: {len(result['editions'])}本、記事: {len(result['articles'])}件")
    for a in result["articles"]:
        urls = "、".join(a["source_urls"]) if a["source_urls"] else "(出典URLなし)"
        print(f"  ・[{a['date']} {a['slot']}] {a['headline']} — {urls}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
