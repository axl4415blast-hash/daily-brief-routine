"""紙面JSONの数字が出典に実在するかを機械で確かめ、各行の mark を確定させるスクリプト。

使い方:
  python3 scripts/verify_edition.py \
    --edition editions/2026-09-24/morning.json \
    --hypotheses hypotheses/2026-09-24-morning.json \
    --cache .cache/sources \
    --calendar calendar

終了コード: 0=保存してよい 1=保存してはいけない 2=スクリプト自体のエラー

仮説ファイル(--hypotheses)の想定するJSON構造(依頼文に例示が無いため本スクリプトが定める形):
{
  "edition_id": "2026-09-24-morning",
  "hypotheses": [
    {
      "hypothesis_id": "H-1",
      "company_name": "...",
      "ticker": "1234",
      "relation_text": "...",
      "direction": "plus" | "minus",
      "evidence_grade": "primary" | "secondary" | ...,
      "falsifier": "...",
      "baseline_date": "2026-09-24",
      "baseline_price_type": "close" | "open" | ...,
      "horizon_business_days": 20,
      "deadline_date": "2026-10-23",
      "line_ids": ["L-003-02"]
    }
  ]
}
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

KNOWN_CLAIMED_MARKS = {
    "source_number_match",
    "reported_unverified",
    "explainer",
    "unverified",
}

REQUIRED_EDITION_KEYS = ["edition_id", "date", "slot", "generated_at", "market_open", "sources", "sections"]
REQUIRED_HYPOTHESIS_FIELDS = [
    "company_name", "ticker", "relation_text", "falsifier",
    "baseline_date", "baseline_price_type", "horizon_business_days",
]

_WS_RE = re.compile(r"[ \t\r\n　]")
_COMMA_RE = re.compile(r"(?<=[0-9]),(?=[0-9])")
_DASH_CHARS = ["〜", "～", "－", "—", "−", "~"]
_QUOTE_MAP = {"“": '"', "”": '"', "‘": "'", "’": "'"}


class EditionInvalid(Exception):
    """検査A/B/Cで不合格になったときに投げる(終了コード1)。"""


def normalize_text(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    for ch in _DASH_CHARS:
        s = s.replace(ch, "-")
    for k, v in _QUOTE_MAP.items():
        s = s.replace(k, v)
    s = _WS_RE.sub("", s)
    s = _COMMA_RE.sub("", s)
    return s


def format_number(value):
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def find_number(excerpt_norm, value):
    needle = format_number(value)
    if not needle:
        return False
    start = 0
    while True:
        idx = excerpt_norm.find(needle, start)
        if idx == -1:
            return False
        before = excerpt_norm[idx - 1] if idx > 0 else ""
        after_idx = idx + len(needle)
        after = excerpt_norm[after_idx] if after_idx < len(excerpt_norm) else ""
        ok = True
        if before.isdigit():
            ok = False
        if after.isdigit():
            ok = False
        if after == ".":
            after2 = excerpt_norm[after_idx + 1] if after_idx + 1 < len(excerpt_norm) else ""
            if after2.isdigit():
                ok = False
        if ok:
            return True
        start = idx + 1


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_ng_words(path):
    words = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if w:
                words.append(w)
    return words


def check_a_structure(edition):
    for key in REQUIRED_EDITION_KEYS:
        if key not in edition:
            raise EditionInvalid(f"必須項目 '{key}' がありません。")
    if not isinstance(edition["sections"], list):
        raise EditionInvalid("sections が配列ではありません。")
    if not isinstance(edition["market_open"], bool):
        raise EditionInvalid("market_open が真偽値ではありません。")
    if not isinstance(edition.get("sources"), list):
        raise EditionInvalid("sources が配列ではありません。")

    for section in edition["sections"]:
        if "section_id" not in section:
            raise EditionInvalid("section に section_id がありません。")
        articles = section.get("articles")
        if not isinstance(articles, list):
            raise EditionInvalid(f"section '{section.get('section_id')}' の articles が配列ではありません。")
        for article in articles:
            if "article_id" not in article:
                raise EditionInvalid("article に article_id がありません。")
            lines = article.get("lines")
            if not isinstance(lines, list):
                raise EditionInvalid(f"article '{article.get('article_id')}' の lines が配列ではありません。")
            for line in lines:
                for key in ("line_id", "text", "claimed_mark", "numbers"):
                    if key not in line:
                        raise EditionInvalid(f"line に '{key}' がありません(article={article.get('article_id')})。")
                if not isinstance(line["numbers"], list):
                    raise EditionInvalid(f"line '{line.get('line_id')}' の numbers が配列ではありません。")
                if line["claimed_mark"] not in KNOWN_CLAIMED_MARKS:
                    raise EditionInvalid(
                        f"line '{line.get('line_id')}' の claimed_mark '{line['claimed_mark']}' は不正な値です。"
                    )


def check_b_edition_id(edition, edition_path):
    p = Path(edition_path)
    expected = f"{p.parent.name}-{p.stem}"
    actual = edition.get("edition_id")
    if actual != expected:
        raise EditionInvalid(f"edition_id '{actual}' がファイルパスから期待される '{expected}' と一致しません。")


def iter_lines(edition):
    for section in edition["sections"]:
        for article in section.get("articles", []):
            for line in article.get("lines", []):
                yield section, article, line


def check_c_stop_words(edition, ng_words):
    """停止側: ng_words.txt に載っている語そのものの検出。1件でもあれば号は保存されない。"""
    hits = []
    for section, article, line in iter_lines(edition):
        text = line.get("text", "")
        if not text:
            continue
        for word in ng_words:
            if word in text:
                hits.append({"line_id": line.get("line_id"), "word": word, "text": text})
    return hits


def mask_excluded_words(text, exclude_words):
    """近接ルールの判定用に、除外語を同じ文字数の○へ置き換えたコピーを作る(元の文字列は変更しない)。"""
    masked = text
    for word in exclude_words:
        if word:
            masked = masked.replace(word, "○" * len(word))
    return masked


def check_watch_proximity(edition, exclude_words):
    """注意側: 「株価」「株式」「銘柄」の前後15文字以内に「買」または「売」がある行を検出する。
    ただし除外語リストに載っている語(株式会社・売上高など)は判定用コピーでは○に置き換えてから見る。
    号は止めない(終了コードに影響しない)。"""
    hits = []
    for section, article, line in iter_lines(edition):
        text = line.get("text", "")
        if not text:
            continue
        masked = mask_excluded_words(text, exclude_words)
        for trigger in ("株価", "株式", "銘柄"):
            start = 0
            while True:
                idx = masked.find(trigger, start)
                if idx == -1:
                    break
                window_start = max(0, idx - 15)
                window_end = min(len(masked), idx + len(trigger) + 15)
                window = masked[window_start:window_end]
                if "買" in window or "売" in window:
                    hits.append({
                        "line_id": line.get("line_id"),
                        "word": f"{trigger}(周辺15文字以内に買/売)",
                        "text": text,
                    })
                start = idx + 1
    return hits


def verify_line(line, sources_by_id, cache_dir):
    claimed = line["claimed_mark"]
    numbers = line.get("numbers", [])

    if claimed == "source_number_match":
        source_ref = line.get("source_ref")
        excerpt = line.get("excerpt")
        attribution = line.get("attribution")
        if not source_ref or not excerpt or not attribution:
            return "unverified", "missing_field", None

        source = sources_by_id.get(source_ref)
        cache_path = Path(cache_dir) / f"{source_ref}.txt"
        if source is None or not cache_path.is_file():
            return "unverified", "source_unfetchable", None

        raw_bytes = cache_path.read_bytes()
        actual_hash = hashlib.sha256(raw_bytes).hexdigest()
        expected_hash = source.get("content_sha256")
        if not expected_hash:
            return "unverified", "hash_missing", None
        if actual_hash != expected_hash:
            return "unverified", "hash_mismatch", None

        body_text = raw_bytes.decode("utf-8")
        body_norm = normalize_text(body_text)
        excerpt_norm = normalize_text(excerpt)
        if excerpt_norm not in body_norm:
            return "unverified", "excerpt_not_found", None

        missing_numbers = []
        for num in numbers:
            if not find_number(excerpt_norm, num.get("value")):
                missing_numbers.append(num)
        if missing_numbers:
            return "unverified", "number_not_in_excerpt", missing_numbers

        return "source_number_match", None, None

    if claimed == "reported_unverified":
        return "reported_unverified", None, None

    if claimed == "explainer":
        if not numbers:
            return "explainer", None, None
        return "unverified", "mark_mismatch", None

    return "unverified", None, None


def run_line_verification(edition, cache_dir):
    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", [])}
    stats = {
        "lines_total": 0,
        "passed": 0,
        "unverified": 0,
        "reported_unverified": 0,
        "explainer": 0,
        "unverified_reasons": {},
    }
    number_failure_details = []

    for section, article, line in iter_lines(edition):
        stats["lines_total"] += 1
        mark, reason, missing_numbers = verify_line(line, sources_by_id, cache_dir)
        line["mark"] = mark
        line["mark_reason"] = reason

        if mark == "source_number_match":
            stats["passed"] += 1
        elif mark == "reported_unverified":
            stats["reported_unverified"] += 1
        elif mark == "explainer":
            stats["explainer"] += 1
        elif mark == "unverified":
            stats["unverified"] += 1
            if reason:
                stats["unverified_reasons"][reason] = stats["unverified_reasons"].get(reason, 0) + 1

        if reason == "number_not_in_excerpt" and missing_numbers:
            number_failure_details.append({
                "line_id": line.get("line_id"),
                "missing_numbers": missing_numbers,
            })

    return stats, number_failure_details


def run_check_d_inferences(edition):
    dropped = 0
    for section in edition["sections"]:
        for article in section.get("articles", []):
            inferences = article.get("inferences")
            if not isinstance(inferences, list):
                continue
            kept = []
            for inf in inferences:
                if all(inf.get(k) for k in ("text", "falsifier", "check_metric", "check_by")):
                    kept.append(inf)
                else:
                    dropped += 1
            article["inferences"] = kept
    return dropped


JST = dt.timezone(dt.timedelta(hours=9))


def parse_datetime_assume_jst(s):
    """タイムゾーンが付いていない日時は日本時間とみなして補う。読み取れなければ None。"""
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=JST)
    return d


def run_check_e_stale_sources(edition):
    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", [])}
    generated_dt = parse_datetime_assume_jst(edition.get("generated_at"))

    stale = 0
    skipped = 0
    for section, article, line in iter_lines(edition):
        if section.get("section_id") != "change":
            continue
        source_ref = line.get("source_ref")
        source = sources_by_id.get(source_ref)
        if not source:
            continue
        published_dt = parse_datetime_assume_jst(source.get("published_at"))
        if generated_dt is None or published_dt is None:
            skipped += 1
            continue
        delta_hours = (generated_dt - published_dt).total_seconds() / 3600
        if delta_hours >= 36:
            stale += 1
    return stale, skipped


def load_business_days(calendar_dir):
    days = []
    for path in sorted(Path(calendar_dir).glob("*.json")):
        try:
            obj = load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        days.extend(obj.get("business_days", []))
    days.sort()
    return days


def compute_deadline(business_days, baseline_date, horizon):
    try:
        idx = business_days.index(baseline_date)
        return business_days[idx + horizon]
    except (ValueError, IndexError):
        return None


def check_hypothesis(hyp, edition, line_ids, business_days, ng_words):
    if hyp.get("direction") == "minus" and hyp.get("evidence_grade") != "primary":
        return "direction_minus_requires_primary"

    for field in REQUIRED_HYPOTHESIS_FIELDS:
        value = hyp.get(field)
        if value is None or value == "":
            return "missing_field"

    hyp_line_ids = hyp.get("line_ids") or []
    if not hyp_line_ids:
        return "missing_field"
    for lid in hyp_line_ids:
        if lid not in line_ids:
            return "line_id_not_found"

    if hyp.get("evidence_grade") == "primary":
        for lid in hyp_line_ids:
            if line_ids.get(lid) == "unverified":
                return "primary_requires_verified_line"

    horizon = hyp.get("horizon_business_days")
    baseline_date = hyp.get("baseline_date")
    if isinstance(horizon, int) and baseline_date:
        expected_deadline = compute_deadline(business_days, baseline_date, horizon)
        if expected_deadline is None or expected_deadline != hyp.get("deadline_date"):
            return "deadline_date_mismatch"
    else:
        return "deadline_date_mismatch"

    relation_text = hyp.get("relation_text", "")
    for word in ng_words:
        if word in relation_text:
            return "relation_text_recommendation"
    for banned in ("プラス", "マイナス", "好材料", "悪材料"):
        if banned in relation_text:
            return "relation_text_conclusive_word"

    return None


def run_hypothesis_checks(hypotheses_doc, edition, business_days, ng_words):
    line_ids = {}
    for section, article, line in iter_lines(edition):
        line_ids[line.get("line_id")] = line.get("mark")

    hyps = hypotheses_doc.get("hypotheses", [])
    reasons = {}
    kept = []

    if not edition.get("market_open", True):
        for hyp in hyps:
            reasons["market_closed"] = reasons.get("market_closed", 0) + 1
        hypotheses_doc["hypotheses"] = []
        return len(hyps), reasons

    for hyp in hyps:
        reason = check_hypothesis(hyp, edition, line_ids, business_days, ng_words)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            kept.append(hyp)

    if len(kept) > 5:
        excess = kept[5:]
        kept = kept[:5]
        reasons["too_many_hypotheses"] = reasons.get("too_many_hypotheses", 0) + len(excess)

    hypotheses_doc["hypotheses"] = kept
    total_violations = sum(reasons.values())
    return total_violations, reasons


def print_report(edition_path, stats, stop_hits, watch_hits, dropped_inferences, stale_hits, stale_skipped,
                  hypothesis_violations, hypothesis_reasons, number_failure_details, ok):
    print("=" * 60)
    print(f"照合結果: {edition_path}")
    print("=" * 60)
    if not ok:
        print("→ この号は保存できません。")
        return

    print(f"行の総数: {stats['lines_total']}")
    print(f"  合格(出典と数字が一致): {stats['passed']}")
    print(f"  未確認へ格下げ: {stats['unverified']}")
    print(f"  未確認のまま(自己申告どおり): {stats['reported_unverified']}")
    print(f"  解説として合格: {stats['explainer']}")

    if stats["unverified_reasons"]:
        print("格下げの理由の内訳:")
        reason_text = {
            "missing_field": "出典番号・抜き出し・出典表記のいずれかが空だった",
            "source_unfetchable": "出典の本文が手元に保存されていなかった",
            "hash_mismatch": "保存されている出典の本文が、記録されたハッシュと一致しなかった",
            "hash_missing": "出典の本文のハッシュが記録されていなかった",
            "excerpt_not_found": "抜き出した文が出典の本文の中に見つからなかった",
            "number_not_in_excerpt": "数字が抜き出した文の中に見つからなかった",
            "mark_mismatch": "数字が入っているのに「解説」として申告されていた",
        }
        for reason, count in stats["unverified_reasons"].items():
            print(f"  ・{reason_text.get(reason, reason)}: {count}件")

    if number_failure_details:
        print("数字が見つからなかった行:")
        for detail in number_failure_details:
            values = ", ".join(str(n.get("value")) for n in detail["missing_numbers"])
            print(f"  ・{detail['line_id']}: {values}")

    print(f"推奨表現(停止)の検出件数: {len(stop_hits)}")
    print(f"推奨表現(注意)の検出件数: {len(watch_hits)}")
    if watch_hits:
        print("  注意に挙がった行(号は保存されています):")
        for hit in watch_hits:
            print(f"    ・{hit['line_id']}: 「{hit['word']}」 (本文: {hit['text']})")
    print(f"出典が古い(36時間以上前)行の件数: {stale_hits}")
    print(f"出典の日時が読み取れず判定できなかった行の件数: {stale_skipped}")
    print(f"必須項目が空で削除した推論の件数: {dropped_inferences}")

    if hypothesis_reasons:
        print(f"仮説の削除件数: {hypothesis_violations}")
        reason_text = {
            "direction_minus_requires_primary": "下振れ方向なのに根拠の強さが最上位でなかった",
            "missing_field": "必須項目が空だった",
            "line_id_not_found": "紙面に存在しない行を参照していた",
            "primary_requires_verified_line": "根拠が最上位なのに参照行が未確認だった",
            "deadline_date_mismatch": "確認期限の日付が営業日計算と合わなかった",
            "relation_text_conclusive_word": "断定的な言葉(プラス/マイナス/好材料/悪材料)が入っていた",
            "relation_text_recommendation": "説明文に推奨表現が入っていた",
            "market_closed": "市場が休みの号に仮説が入っていた",
            "too_many_hypotheses": "仮説が上限(5件)を超えていた",
        }
        for reason, count in hypothesis_reasons.items():
            print(f"  ・{reason_text.get(reason, reason)}: {count}件")
    elif hypothesis_violations:
        print(f"仮説の削除件数: {hypothesis_violations}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", required=True)
    parser.add_argument("--hypotheses")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--calendar", required=True)
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    ng_words_path = script_dir / "ng_words.txt"
    ng_words_exclude_path = script_dir / "ng_words_exclude.txt"

    try:
        edition_path = Path(args.edition)
        try:
            edition = load_json(edition_path)
        except (json.JSONDecodeError, OSError) as e:
            raise EditionInvalid(f"紙面JSONを読み込めません: {e}")

        check_a_structure(edition)
        check_b_edition_id(edition, edition_path)

        ng_words = load_ng_words(ng_words_path)
        ng_words_exclude = load_ng_words(ng_words_exclude_path)

        stop_hits = check_c_stop_words(edition, ng_words)
        if stop_hits:
            print("=" * 60)
            print(f"照合結果: {args.edition}")
            print("=" * 60)
            print("→ この号は保存できません。推奨表現が見つかりました。")
            for hit in stop_hits:
                print(f"  ・{hit['line_id']}: 「{hit['word']}」 (本文: {hit['text']})")
            return 1

        watch_hits = check_watch_proximity(edition, ng_words_exclude)

        stats, number_failure_details = run_line_verification(edition, args.cache)
        dropped_inferences = run_check_d_inferences(edition)
        stale_hits, stale_skipped = run_check_e_stale_sources(edition)

        hypothesis_violations = 0
        hypothesis_reasons = {}
        hypotheses_doc = None
        if args.hypotheses:
            hypotheses_doc = load_json(args.hypotheses)
            business_days = load_business_days(args.calendar)
            hypothesis_violations, hypothesis_reasons = run_hypothesis_checks(
                hypotheses_doc, edition, business_days, ng_words
            )

        run_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()
        edition["verification"] = {
            "script_version": "1.1.0",
            "run_at": run_at,
            "lines_total": stats["lines_total"],
            "passed": stats["passed"],
            "unverified": stats["unverified"],
            "reported_unverified": stats["reported_unverified"],
            "explainer": stats["explainer"],
            "recommendation_hits": len(stop_hits),
            "recommendation_watch_hits": len(watch_hits),
            "stale_source_hits": stale_hits,
            "stale_check_skipped": stale_skipped,
            "inference_dropped": dropped_inferences,
            "hypothesis_violations": hypothesis_violations,
            "unverified_reasons": stats["unverified_reasons"],
        }

        with open(edition_path, "w", encoding="utf-8") as f:
            json.dump(edition, f, ensure_ascii=False, indent=1)

        if args.hypotheses:
            with open(args.hypotheses, "w", encoding="utf-8") as f:
                json.dump(hypotheses_doc, f, ensure_ascii=False, indent=1)

        print_report(
            args.edition, stats, stop_hits, watch_hits, dropped_inferences, stale_hits, stale_skipped,
            hypothesis_violations, hypothesis_reasons, number_failure_details, ok=True,
        )
        return 0

    except EditionInvalid as e:
        print("=" * 60)
        print(f"照合結果: {args.edition}")
        print("=" * 60)
        print(f"→ この号は保存できません。理由: {e}")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
