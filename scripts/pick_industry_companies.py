"""紙面の下段(企業欄)に出す会社を、記事ごとに機械で選ぶスクリプト。

紙面を作るAIは、記事ごとに「関係しそうな業種」を最大2つ選ぶだけで、会社名は
書かない(industry_picks)。会社を決めるのはこのスクリプトの役目で、EDINET
コードリスト(edinet_codelist.py)とニュースの呼び名の対応表(aliases.csv)を
使って、機械的に候補を決める。AIに会社名を書かせると「削られない書き方」に
寄っていく問題(件数にも検査結果にも表れない)を避けるための設計。

使い方:
  python3 scripts/pick_industry_companies.py \
    --edition trial/editions/2026-09-18/evening.json \
    --hypotheses trial/hypotheses/2026-09-18-evening.json

--edition は読むだけ(書き換えない)。--hypotheses は読み書きする
(industry_examples を作って保存する。既存の hypotheses 配列には触れない)。

終了コード: 0=成功 1=想定内の失敗(コードリスト未取得等) 2=スクリプト自体のエラー
"""
import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

import edinet_codelist

ALIASES_PATH = Path("scripts/aliases.csv")

# 業種あたり・記事あたり・号全体(下段)の上限。号全体(上段+下段)の上限は別途
# hypotheses配列の件数から動的に計算する(上段を先に数える)。
PER_INDUSTRY_LIMIT = 2
PER_ARTICLE_LIMIT = 2
LOWER_SECTION_LIMIT = 2
TOTAL_LIMIT = 5

RELATION_TEXT_MENTIONED = (
    "東証33業種の「{industry}」に属する上場企業の例です。"
    "この記事の本文にこの会社名が出ています（関係の中身は確認していません）。"
)
RELATION_TEXT_CAPITAL = (
    "東証33業種の「{industry}」に属する上場企業の例です。"
    "この業種で資本金がもっとも大きい会社から順に選んでいます。"
)

# same/parent以外の値になったentity_relationを検出したときに記録する理由。
ENTITY_RELATION_INVALID_REASON = "entity_relationがsame/parent以外だったため使用しなかった"

# industry_picksにAIがこれらの項目を書いていたら、会社名を書こうとしたとみなして
# その業種指定ごと飛ばす(会社を決めるのは機械の役目のため)。
FORBIDDEN_PICK_KEYS = {
    "company_name", "company", "companies", "ticker", "tickers",
    "edinet_code", "news_entity", "entity_relation",
}


class CodelistMissingError(Exception):
    """EDINETコードリストが未取得で、業種から会社を引けないときに投げる。"""


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def index_articles(edition):
    """紙面JSONから、article_idごとに「見出し・本文行の行ID集合・照合用テキスト」を
    引けるようにする。照合用テキストは見出し+その記事の全行のtextで、他の記事の
    行は絶対に混ぜない(記事をまたいで本文を照合すると、無関係な記事の会社名が
    別記事に「本文に出ている」と誤判定されるため)。"""
    articles = {}
    for section in edition.get("sections") or []:
        for article in section.get("articles") or []:
            article_id = article.get("article_id")
            if not article_id:
                continue
            lines = article.get("lines") or []
            headline = article.get("headline") or ""
            texts = [headline] + [line.get("text") or "" for line in lines]
            articles[article_id] = {
                "headline": headline,
                "line_ids": {line.get("line_id") for line in lines if line.get("line_id")},
                "text_blob": " ".join(texts),
            }
    return articles


def load_aliases():
    """scripts/aliases.csv を edinet_code ごとに引けるようにする。
    ファイルが無ければ空のまま返す(エイリアス無しでも動く)。"""
    aliases_by_edinet_code = {}
    if not ALIASES_PATH.is_file():
        return aliases_by_edinet_code
    with open(ALIASES_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            edinet_code = row.get("edinet_code")
            if not edinet_code:
                continue
            aliases_by_edinet_code.setdefault(edinet_code, []).append({
                "news_name": row.get("news_name"),
                "entity_relation": row.get("entity_relation"),
            })
    return aliases_by_edinet_code


def _normalize_match_name(s):
    """会社名・本文を照合用に正規化する(3.3の手順そのまま):
    NFKC正規化 -> 大文字化 -> 法人格の表記・中黒・長音・ハイフン・空白を除去。
    edinet_codelist._normalize_company_name より厳しい(大文字化・中黒/長音/
    ハイフン除去まで行う)ため、ここで独自に定義する。"""
    normalized = unicodedata.normalize("NFKC", s or "").upper()
    for token in edinet_codelist.CORPORATE_DESIGNATORS:
        normalized = normalized.replace(token, "")
    normalized = normalized.replace("・", "").replace("ー", "").replace("-", "")
    return re.sub(r"\s+", "", normalized)


def _is_word_forming(ch):
    """一致した照合名の直後の1文字が、単語の続きとみなせる文字(カタカナ・漢字・
    英数字)かどうかを返す。ひらがな・句読点・記号・文字列の終わりは「境界」
    とみなす(True を返さない)。

    これにより、登録されていない長い固有名詞の一部を、短い照合名の一致だと
    誤認しない(例: 'NTTデータ'の'NTT'、'近鉄百貨店'の'近鉄')。一方で
    '兼松は18日'のように、直後がひらがな(助詞)なら一致を認める。

    直前の文字は確認しない。見出しと複数行のtextを1つに連結する際、区切りが
    失われる(空白は正規化で消える)ため、直前側で判定すると本来認めるべき
    一致まで誤って弾いてしまうおそれがあるため。"""
    if ch is None:
        return False
    if ch.isascii() and ch.isalnum():
        return True
    code = ord(ch)
    if 0x30A0 <= code <= 0x30FF:
        return True
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return True
    return False


def _filter_usable_names(entries):
    """他社の照合名の一部になっている照合名は使わない(例: 'タカラ'は'タカラトミー'
    の一部なので使わない)。同じ会社の別表記(エイリアス)同士が重なるのは問題ない。
    entries: [(match_name, company_key, news_entity, entity_relation), ...]"""
    names_by_company = {}
    for match_name, company_key, *_rest in entries:
        if not match_name:
            continue
        names_by_company.setdefault(company_key, set()).add(match_name)

    def is_substring_of_other_company(match_name, company_key):
        for other_key, other_names in names_by_company.items():
            if other_key == company_key:
                continue
            for other_name in other_names:
                if match_name != other_name and match_name in other_name:
                    return True
        return False

    return [
        entry for entry in entries
        if entry[0] and not is_substring_of_other_company(entry[0], entry[1])
    ]


def _find_mentions(blob, entries):
    """本文(blob、正規化済み)の中で、照合名の長い順に非重複で探す。見つけた
    時点でその文字範囲を消費し、短い名前が同じ範囲に重なるのを防ぐ(最長一致)。
    前後の文字が同じ文字種で続く場合は、登録されていない長い固有名詞の一部と
    みなして採用しない。

    戻り値: {company_key: 勝った entry} (1社につき最初に当たったentryだけ)。"""
    consumed = [False] * len(blob)
    found = {}
    ordered = sorted(entries, key=lambda e: (-len(e[0]), e[0]))
    for entry in ordered:
        match_name, company_key = entry[0], entry[1]
        if not match_name or company_key in found:
            continue
        start = 0
        while True:
            idx = blob.find(match_name, start)
            if idx == -1:
                break
            end = idx + len(match_name)
            start = idx + 1
            if any(consumed[idx:end]):
                continue
            next_ch = blob[end] if end < len(blob) else None
            if _is_word_forming(next_ch):
                continue
            for i in range(idx, end):
                consumed[i] = True
            found[company_key] = entry
            break
    return found


def _build_entries(candidates, aliases_by_edinet_code):
    """候補企業(その業種の上場企業一覧)ごとに、正式名+エイリアスの照合名一覧を作る。
    正式名と正規化後に同じ文字列になるエイリアスは、正式名側として扱う(重複させない)。

    正式名での一致はentity_relation="same"にする。エイリアスでの一致は別名表の
    entity_relationをそのまま使うが、値が"self"なら"same"に読み替える
    (edinet_codelist.pyのcompanyコマンドと同じ扱い)。読み替えてもsame/parent
    のいずれでもない値だった場合、そのエイリアスは使わない(本文照合の対象にしない)。"""
    entries = []
    for c in candidates:
        official_match = _normalize_match_name(c["company_name"])
        entries.append((official_match, c["edinet_code"], None, "same"))
        for alias in aliases_by_edinet_code.get(c["edinet_code"], []):
            alias_match = _normalize_match_name(alias["news_name"])
            if not alias_match or alias_match == official_match:
                continue
            entity_relation = alias["entity_relation"]
            if entity_relation == "self":
                entity_relation = "same"
            if entity_relation not in ("same", "parent"):
                print(
                    f"[警告] {ENTITY_RELATION_INVALID_REASON}: edinet_code={c['edinet_code']!r} "
                    f"news_name={alias['news_name']!r} entity_relation={alias['entity_relation']!r}",
                    file=sys.stderr,
                )
                continue
            entries.append((alias_match, c["edinet_code"], alias["news_name"], entity_relation))
    return entries


def select_companies_for_pick(candidates, article, aliases_by_edinet_code, selected_edinet_codes, slot):
    """1件のindustry_pickについて、選ぶ会社を最大slot件返す。
    candidates: その業種の上場企業一覧(資本金の多い順、edinet_codelist.get_companies_by_industryの結果)。
    article: index_articles()が作った1記事ぶんの辞書({"text_blob": ...}を含む)。
    selected_edinet_codes: 号全体で既に選ばれた会社のedinet_codeの集合(重複選択を避ける)。

    戻り値: [{"candidate":..., "selection_rule":..., "news_entity":..., "entity_relation":...,
              "match_name_len": 数値かNone}, ...]"""
    if slot <= 0:
        return []

    usable_candidates = [c for c in candidates if c["edinet_code"] not in selected_edinet_codes]
    if not usable_candidates:
        return []

    # 資本金の多い順、同額ならEDINETコードの昇順に並べ直す(3.6: 何度実行しても
    # 同じ結果になること)。呼び出し元(edinet_codelist.get_companies_by_industry)
    # は既にこの順で返すが、この関数だけでも決定的な順序を保証できるようにする。
    usable_candidates = sorted(
        usable_candidates,
        key=lambda c: (c["capital_million"] is None, -(c["capital_million"] or 0), c["edinet_code"]),
    )

    entries = _build_entries(usable_candidates, aliases_by_edinet_code)
    usable_entries = _filter_usable_names(entries)
    blob = _normalize_match_name(article["text_blob"])
    matched = _find_mentions(blob, usable_entries)

    mentioned = [c for c in usable_candidates if c["edinet_code"] in matched]
    remaining = [c for c in usable_candidates if c["edinet_code"] not in matched]
    chosen = (mentioned + remaining)[:slot]

    result = []
    for c in chosen:
        if c["edinet_code"] in matched:
            winning = matched[c["edinet_code"]]
            result.append({
                "candidate": c,
                "selection_rule": "mentioned_in_text",
                "news_entity": winning[2],
                "entity_relation": winning[3],
                "match_name_len": len(winning[0]),
            })
        else:
            result.append({
                "candidate": c,
                "selection_rule": "capital_rank",
                "news_entity": None,
                "entity_relation": "same",
                "match_name_len": None,
            })
    return result


def _pick_has_company_like_field(pick):
    return any(key in pick for key in FORBIDDEN_PICK_KEYS)


def validate_pick(pick, articles_by_id, industry_cache):
    """1件のindustry_pickが使える形かどうかを確かめる。
    戻り値: (ok, 不合格の理由(ok=Falseのとき) or None, 解決した業種名, その業種の
    edinet_codelist.get_companies_by_industryの結果)。
    コードリストが未取得の場合はCodelistMissingErrorを投げる(想定内の失敗として
    main側で終了コード1にする)。"""
    if _pick_has_company_like_field(pick):
        return False, "industry_picksに会社名らしき項目が書かれている", None, None

    article_id = pick.get("article_id")
    article = articles_by_id.get(article_id)
    if article is None:
        return False, "記事が紙面に見つからない", None, None

    line_ids = pick.get("industry_line_ids") or []
    if not line_ids:
        return False, "industry_line_idsが空", None, None
    for lid in line_ids:
        if lid not in article["line_ids"]:
            return False, f"行ID'{lid}'がこの記事に存在しない", None, None

    industry_raw = pick.get("industry")
    if industry_raw in industry_cache:
        result = industry_cache[industry_raw]
    else:
        result = edinet_codelist.get_companies_by_industry(industry_raw, limit=10**9)
        industry_cache[industry_raw] = result

    if result is None:
        raise CodelistMissingError(
            "コードリストが取得されていません。先に python3 scripts/edinet_codelist.py fetch を実行してください。"
        )
    if "reason" in result:
        return False, f"業種'{industry_raw}'は{result['reason']}", None, None
    if "error" in result:
        return False, f"業種'{industry_raw}'はコードリストの業種名に存在しない", None, None

    resolved_industry = result["companies"][0]["industry"] if result["companies"] else industry_raw
    return True, None, resolved_industry, result


def _truncate_to_two_per_article(industry_picks):
    """同じ記事に業種の指定が3つ以上あれば、先頭2つだけを使う(3.以降は理由付きで
    飛ばす)。article_idの出現順を保つ。"""
    grouped = {}
    order = []
    for pick in industry_picks:
        article_id = pick.get("article_id")
        if article_id not in grouped:
            grouped[article_id] = []
            order.append(article_id)
        grouped[article_id].append(pick)

    kept = []
    skipped = []
    for article_id in order:
        picks = grouped[article_id]
        kept.extend(picks[:2])
        for extra in picks[2:]:
            skipped.append((extra, "同じ記事に3つ目以降の業種指定があったため無視"))
    return kept, skipped


def _has_any_mention(candidates, article, aliases_by_edinet_code):
    """その業種の候補企業に、記事の本文で言及されている会社が1つでもあるかを返す。
    下段の枠を複数のindustry_picksで分け合うときの優先順位づけに使う(本文に
    名前が出ている会社を確保できるpickを、資本金順で埋めるだけのpickより
    先に処理するため)。"""
    if not candidates:
        return False
    entries = _build_entries(candidates, aliases_by_edinet_code)
    usable_entries = _filter_usable_names(entries)
    blob = _normalize_match_name(article["text_blob"])
    return bool(_find_mentions(blob, usable_entries))


def run(hypotheses_doc, articles_by_id, aliases_by_edinet_code):
    """industry_picksを処理し、industry_examplesと集計を作る。
    既存のhypotheses配列には一切触れない(読んで件数を数えるだけ)。

    下段の枠(号全体で最大2社)を複数のindustry_picksで分け合うときは、まず
    各pickに1社ずつ配り(1業種2社・1記事2社・上段と合わせて5社、の上限内で)、
    それでも枠が余れば2社目以降を同じ優先順で配る。1社ずつ配る順番は、
    「本文に名前が出ている会社を選べるpickを先にする、同条件なら
    industry_picksの配列順のまま」で決める(常に同じ結果になるようにするため)。
    """
    hyp_count = len(hypotheses_doc.get("hypotheses") or [])
    overall_budget = min(LOWER_SECTION_LIMIT, max(0, TOTAL_LIMIT - hyp_count))

    industry_picks = hypotheses_doc.get("industry_picks") or []
    total_pick_count = len(industry_picks)
    truncated_picks, skip_log = _truncate_to_two_per_article(industry_picks)

    industry_cache = {}
    valid_entries = []

    try:
        for pick in truncated_picks:
            ok, reason, industry_name, industry_result = validate_pick(pick, articles_by_id, industry_cache)
            if not ok:
                skip_log.append((pick, reason))
                continue
            article = articles_by_id[pick.get("article_id")]
            has_mention = _has_any_mention(industry_result["companies"], article, aliases_by_edinet_code)
            valid_entries.append({
                "pick": pick, "industry_name": industry_name, "industry_result": industry_result,
                "article": article, "has_mention": has_mention, "count": 0,
            })
    except CodelistMissingError as e:
        return {"fatal_error": str(e)}

    # 安定ソート(sorted)なので、has_mentionだけをキーにすれば、同条件のものは
    # industry_picksの配列順のまま残る。
    ordered_entries = sorted(valid_entries, key=lambda e: 0 if e["has_mention"] else 1)

    examples = []
    per_industry_count = {}
    per_article_count = {}
    selected_edinet_codes = set()
    mentioned_count = 0
    mentioned_short_count = 0
    short_name_matches = []
    capital_rank_count = 0
    edition_id = hypotheses_doc.get("edition_id") or ""
    seq_holder = [1]

    def allocate_one(entry):
        nonlocal mentioned_count, mentioned_short_count, capital_rank_count
        pick = entry["pick"]
        article_id = pick.get("article_id")
        industry_name = entry["industry_name"]

        overall_remaining = overall_budget - len(examples)
        industry_remaining = PER_INDUSTRY_LIMIT - per_industry_count.get(industry_name, 0)
        article_remaining = PER_ARTICLE_LIMIT - per_article_count.get(article_id, 0)
        slot = min(1, overall_remaining, industry_remaining, article_remaining)
        if slot <= 0:
            return False

        chosen = select_companies_for_pick(
            entry["industry_result"]["companies"], entry["article"], aliases_by_edinet_code,
            selected_edinet_codes, slot,
        )
        if not chosen:
            return False

        item = chosen[0]
        c = item["candidate"]
        selected_edinet_codes.add(c["edinet_code"])
        per_industry_count[industry_name] = per_industry_count.get(industry_name, 0) + 1
        per_article_count[article_id] = per_article_count.get(article_id, 0) + 1

        if item["selection_rule"] == "mentioned_in_text":
            relation_text_tmpl = RELATION_TEXT_MENTIONED
        else:
            relation_text_tmpl = RELATION_TEXT_CAPITAL

        example = {
            "example_id": f"{edition_id}-x{seq_holder[0]}",
            "article_id": article_id,
            "event_id": pick.get("event_id"),
            "industry": industry_name,
            "line_ids": pick.get("industry_line_ids"),
            "company_name": c["company_name"],
            "ticker": c["ticker"],
            "ticker_source": "edinet_codelist",
            "news_entity": item["news_entity"],
            "entity_relation": item["entity_relation"],
            "evidence_grade": "inferred",
            "selection_rule": item["selection_rule"],
            "relation_text": relation_text_tmpl.format(industry=industry_name),
            "industry_source": "edinet_codelist",
            "industry_retrieved_date": c["retrieved_date"],
            "attribution": entry["industry_result"]["attribution"],
            "processing_note": entry["industry_result"]["processing_note"],
        }
        examples.append(example)
        entry["count"] += 1
        seq_holder[0] += 1

        if item["selection_rule"] == "mentioned_in_text":
            mentioned_count += 1
            if item["match_name_len"] <= 3:
                mentioned_short_count += 1
                short_name_matches.append(item["news_entity"] or c["company_name"])
        else:
            capital_rank_count += 1
        return True

    # 1回目: 優先順のまま、各pickに1社ずつ配る。
    for entry in ordered_entries:
        allocate_one(entry)

    # 2回目以降: 余った枠を、同じ優先順で追加に配る
    # (1業種2社・1記事2社・下段全体2社・上段と合わせて5社、の範囲で)。
    progressed = True
    while progressed and len(examples) < overall_budget:
        progressed = False
        for entry in ordered_entries:
            if len(examples) >= overall_budget:
                break
            if allocate_one(entry):
                progressed = True

    for entry in ordered_entries:
        if entry["count"] == 0:
            skip_log.append((entry["pick"], "枠が無かったため選ばれなかった"))

    zero_pick_count = total_pick_count - sum(1 for e in valid_entries if e["count"] > 0)

    return {
        "examples": examples,
        "skip_log": skip_log,
        "mentioned_count": mentioned_count,
        "mentioned_short_count": mentioned_short_count,
        "short_name_matches": short_name_matches,
        "capital_rank_count": capital_rank_count,
        "hyp_count": hyp_count,
        "overall_budget": overall_budget,
        "total_pick_count": total_pick_count,
        "zero_pick_count": zero_pick_count,
    }


def print_summary(result):
    if result.get("fatal_error"):
        print(result["fatal_error"], file=sys.stderr)
        return

    for pick, reason in result["skip_log"]:
        print(
            f"飛ばした: article_id={pick.get('article_id')!r} "
            f"industry={pick.get('industry')!r} 理由: {reason}"
        )

    total = len(result["examples"])
    print(f"上段(hypotheses)の件数: {result['hyp_count']}件 / 下段の枠: {result['overall_budget']}社")
    print(f"industry_picks: {result['total_pick_count']}件(うち0社になったもの: {result['zero_pick_count']}件)")
    print(f"選んだ会社: {total}社")
    print(
        f"  ・mentioned_in_text: {result['mentioned_count']}社"
        f"(うち3文字以下の名前で一致: {result['mentioned_short_count']}件)"
    )
    if result["short_name_matches"]:
        print(f"    3文字以下で一致した名前: {'、'.join(result['short_name_matches'])}")
    print(f"  ・capital_rank: {result['capital_rank_count']}社")


def main():
    parser = argparse.ArgumentParser(description="紙面の下段(企業欄)に出す会社を、記事ごとに選ぶ")
    parser.add_argument("--edition", required=True)
    parser.add_argument("--hypotheses", required=True)
    args = parser.parse_args()

    edition = load_json(args.edition)
    articles_by_id = index_articles(edition)

    hyp_path = Path(args.hypotheses)
    hypotheses_doc = load_json(hyp_path)

    aliases_by_edinet_code = load_aliases()

    result = run(hypotheses_doc, articles_by_id, aliases_by_edinet_code)

    if result.get("fatal_error"):
        print_summary(result)
        return 1

    hypotheses_doc["industry_examples"] = result["examples"]
    with open(hyp_path, "w", encoding="utf-8") as f:
        json.dump(hypotheses_doc, f, ensure_ascii=False, indent=1)

    print_summary(result)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
