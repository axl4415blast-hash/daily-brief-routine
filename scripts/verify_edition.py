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
      "ticker_source": "edinet" | "...",
      "relation_text": "...",
      "direction": "plus" | "minus",
      "evidence_grade": "primary" | "reported" | "inferred",
      "evidence_source_ref": "SRC-001" | null,
      "evidence_filer_name": "..." | null,
      "evidence_excerpt": "..." | null,
      "falsifier": "...",
      "baseline_date": "2026-09-24",
      "baseline_price_type": "close" | "open" | ...,
      "horizon_business_days": 20,
      "deadline_date": "2026-10-23",
      "line_ids": ["L-003-02"],
      "links": {"price_history": "https://..."}
    }
  ]
}

evidence_filer_name / evidence_excerpt は検査12(directionがminusの仮説の3条件)、
ticker_source / links.price_history は検査13(証券コードの確認)で使う。どちらも
今回追加した項目のため、依頼文には例示が無い(本スクリプトが定める形)。
"""
import argparse
import csv
import datetime as dt
import decimal
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import edinet_codelist
import edinet_fetch
import pick_industry_companies

KNOWN_CLAIMED_MARKS = {
    "source_number_match",
    "reported_unverified",
    "explainer",
    "unverified",
}

REQUIRED_EDITION_KEYS = ["edition_id", "date", "slot", "generated_at", "market_open", "sources", "sections"]
# "ticker" は以前ここに含まれていたが、検査13(ticker_missing/ticker_source_missing/
# ticker_mismatch)が4桁形式のチェックまで含めて専用に判定するため、ここからは外した。
REQUIRED_HYPOTHESIS_FIELDS = [
    "company_name", "relation_text", "falsifier",
    "baseline_date", "baseline_price_type", "horizon_business_days",
]
# links.price_history のURL(https://finance.yahoo.co.jp/quote/{証券コード}.T/history)から
# /quote/ と .T の間の文字列を取り出す(検査13の条件3で使う)。
PRICE_HISTORY_CODE_RE = re.compile(r"/quote/([^/]+)\.T(?:/|$)")
VALID_SOURCE_USAGES = {"quotable", "link_only", "snippet_only"}
VALID_PUBLISHER_TYPES = {
    "government_statistics", "central_bank", "company_disclosure",
    "international_org", "news", "other",
}
SOURCE_POLICY_COLUMNS = ("domain", "usage", "publisher_type", "independent_check", "attribution_template")
MORNING_DEADLINE = dt.time(8, 50)
NOON_DEADLINE = dt.time(14, 50)

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


def strip_ws(value):
    """前後の空白を取り除く。str.strip()は全角空白(U+3000)も空白として扱うため、
    これだけで前後の全角空白も除去できる。文字列でなければNoneを返す。"""
    if not isinstance(value, str):
        return None
    return value.strip()


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


_NUMBER_TOKEN_RE = re.compile(r"(?<![0-9.])-?[0-9]+(?:\.[0-9]+)?(?![0-9])")


def _to_decimal(value):
    """valueを10進数として解釈できればDecimalを返す。できなければNoneを返す。
    float(浮動小数点)ではなくstr(value)経由でDecimalに直すのは、2.0を2.0000000001の
    ような形の浮動小数点誤差なしにそのまま10進数として比べるため。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return decimal.Decimal(str(value))
        except decimal.InvalidOperation:
            return None
    if isinstance(value, str):
        try:
            return decimal.Decimal(value)
        except decimal.InvalidOperation:
            return None
    return None


def _is_half_width_digit(ch):
    """半角数字かどうかだけを見る。str.isdigit()は全角数字やローマ数字の上付き文字にも
    真を返してしまい、それらを「数字の続き」と誤認する(全角の数字境界を見落とす)ため使わない。"""
    return "0" <= ch <= "9"


def _has_leading_zero(token):
    """日付や連番の中の「09」「08」のような、先頭に0が付いた数字かどうかを見る。
    これらは数値として9・8と等しくなるが、紙面が書いた数字の裏付けにはならないため
    照合の対象から外す。0.75 のような小数と、0 そのものは対象にしない。"""
    digits = token[1:] if token.startswith("-") else token
    return len(digits) >= 2 and digits[0] == "0" and digits[1] != "."


def find_number(excerpt_norm, value):
    # valueが数値として解釈できるなら、文字列としてではなく数値として比べる。
    # "2.0"という表記のvalueが、format_number()で文字列"2"に直されて本文中の
    # "2.0"と一致しなくなる不具合(2.0/2/1.0のいずれも本文と一致しなくなっていた)を
    # 避けるため。
    decimal_value = _to_decimal(value)
    if decimal_value is not None:
        for match in _NUMBER_TOKEN_RE.finditer(excerpt_norm):
            token = match.group()
            if _has_leading_zero(token):
                continue
            try:
                token_value = decimal.Decimal(token)
            except decimal.InvalidOperation:
                continue
            if token_value == decimal_value:
                return True
        return False

    # valueが数値として解釈できない場合(文字列など)は、これまで通りの文字列探索に落とす。
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
        if _is_half_width_digit(before):
            ok = False
        if _is_half_width_digit(after):
            ok = False
        if after == ".":
            after2 = excerpt_norm[after_idx + 1] if after_idx + 1 < len(excerpt_norm) else ""
            if _is_half_width_digit(after2):
                ok = False
        if ok:
            return True
        start = idx + 1


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


SOURCE_TEXT_ENCODINGS = ("utf-8-sig", "utf-16", "cp932")
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def read_source_text(path):
    """出典本文ファイルを候補の文字コードで順に読む。

    EDINET(金融庁の開示システム)の書類取得APIはUTF-16LEでCSVを返すなど、
    出典の文字コードはUTF-8とは限らない。utf-8-sig→utf-16→cp932の順に
    デコードを試し、すべて失敗したら例外を投げずNoneを返す。読めない文字を
    無理に読み進める(errors="replace"など)ことはしない。

    utf-16はBOM(先頭の目印)が付いている場合に限って試す。BOMが無いのに
    utf-16として読もうとすると、Pythonは並び順を機種依存の既定値で
    決め打ちしてしまい、実際はcp932の文書を「化けた文字列」として
    エラーも出さずに読めてしまうことがある(誤判定に気づけない)ため。
    """
    try:
        raw_bytes = Path(path).read_bytes()
    except OSError:
        return None
    for encoding in SOURCE_TEXT_ENCODINGS:
        if encoding == "utf-16" and raw_bytes[:2] not in _UTF16_BOMS:
            continue
        try:
            return raw_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


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
    # market_openはキーの省略だけは許さない。null はこの後check_market_open()で
    # 営業日カレンダーから埋めるため、ここでは通す(bool/nullのみ許可)。
    if edition["market_open"] is not None and not isinstance(edition["market_open"], bool):
        raise EditionInvalid("market_open が真偽値でもnullでもありません。")
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
    """停止側(検査7): ng_words.txt に載っている語そのものを含む行を、その行だけ
    紙面から削除する。号全体は保存する(1語のために号全体を捨てない)。
    削除した行の情報(line_id/word/text)を返す。"""
    hits = []
    for section in edition["sections"]:
        for article in section.get("articles", []):
            lines = article.get("lines", [])
            kept = []
            for line in lines:
                text = line.get("text", "")
                hit_word = None
                if text:
                    for word in ng_words:
                        if word in text:
                            hit_word = word
                            break
                if hit_word:
                    hits.append({"line_id": line.get("line_id"), "word": hit_word, "text": text})
                else:
                    kept.append(line)
            article["lines"] = kept
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


def count_invalid_source_usages(edition):
    """usageがquotable/link_only/snippet_onlyのいずれでもない出典の数を数える(検査16関連)。
    usageが無い・null・空文字・想定外の値のものが対象。verification.source_usage_invalid_hits
    として画面に出す(usageの書き忘れなどを、実害の有無に関わらず気づけるようにするため)。"""
    return sum(1 for s in edition.get("sources", []) if s.get("usage") not in VALID_SOURCE_USAGES)


def load_source_policy(policy_path):
    """scripts/source_policy.csv を読み込み、ホスト名(小文字)→{usage, publisher_type}の
    辞書にして返す。usage/publisher_typeがAIの自己申告のままでは出典の立場・扱いを
    AI自身に決めさせることになるため、この表の値だけを正としてapply_source_policy()で
    上書きする。表そのものが読めない・値が不正な場合は、黙って全出典をsnippet_only等に
    倒すのではなく、号ごと保存を止める(設定ミスに気づけなくなるのを防ぐため)。"""
    path = Path(policy_path)
    if not path.is_file():
        raise EditionInvalid("scripts/source_policy.csv が読めません: ファイルがありません。")
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        raise EditionInvalid(f"scripts/source_policy.csv が読めません: {e}")

    policy = {}
    for row in rows:
        domain = (row.get("domain") or "").strip().lower()
        usage = (row.get("usage") or "").strip()
        publisher_type = (row.get("publisher_type") or "").strip()
        if not domain:
            raise EditionInvalid("scripts/source_policy.csv にdomainが空の行があります。")
        if domain in policy:
            raise EditionInvalid(f"scripts/source_policy.csv にdomain '{domain}' が重複しています。")
        if usage not in VALID_SOURCE_USAGES:
            raise EditionInvalid(
                f"scripts/source_policy.csv のusage '{usage}' (domain={domain}) が不正な値です。"
            )
        if publisher_type not in VALID_PUBLISHER_TYPES:
            raise EditionInvalid(
                f"scripts/source_policy.csv のpublisher_type '{publisher_type}' (domain={domain}) が不正な値です。"
            )
        policy[domain] = {"usage": usage, "publisher_type": publisher_type}
    return policy


def source_hostname(url):
    """出典urlからホスト名を取り出す。小文字化・ポート番号の除去はurlparseが行う。
    urlが無い・文字列でない・ホスト名が取れない場合はNoneを返す。"""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def apply_source_policy(edition, policy_path):
    """検査16対策: sources[].usage/publisher_typeを、AIの自己申告ではなく
    scripts/source_policy.csv の値で上書きする(表にあれば表の値が必ず勝つ)。
    表に無いドメイン・urlが無い/ホスト名が取れない出典はsnippet_only/otherにする
    (未知の出典を安全側=抜き出し不可の側へ倒す)。"""
    policy = load_source_policy(policy_path)

    overwritten = 0
    unlisted_domains = []
    for source in edition.get("sources", []):
        host = source_hostname(source.get("url"))
        entry = policy.get(host) if host else None
        if entry is not None:
            new_usage, new_publisher_type = entry["usage"], entry["publisher_type"]
        else:
            new_usage, new_publisher_type = "snippet_only", "other"
            if host and host not in unlisted_domains:
                unlisted_domains.append(host)

        if source.get("usage") != new_usage or source.get("publisher_type") != new_publisher_type:
            overwritten += 1
        source["usage"] = new_usage
        source["publisher_type"] = new_publisher_type

    return {"overwritten": overwritten, "unlisted_domains": unlisted_domains}


def verify_line(line, sources_by_id, cache_dir):
    claimed = line["claimed_mark"]
    numbers = line.get("numbers", [])
    source_ref = line.get("source_ref")
    excerpt = line.get("excerpt")

    # 検査16: 本文を取得していない出典(usageがquotable以外)からのexcerptは認めない。
    # claimed_markの種類を問わず、行にsource_refとexcerptの両方があれば対象になる。
    # usageが記録されていない・null・空文字・想定外の値の出典も「quotableではない」ものと
    # して扱う(usageの書き忘れが、抜き出しを通す抜け道にならないようにするため)。
    # source_refがsources一覧に見つからない場合は対象にしない(該当する出典自体が
    # 特定できないため、他の既存検査に判定を委ねる)。
    if source_ref and excerpt:
        source = sources_by_id.get(source_ref)
        if source is not None and source.get("usage") != "quotable":
            line["excerpt"] = None
            return "unverified", "excerpt_not_allowed", None

    if claimed == "source_number_match":
        # 検査15: numbersが空なら、何も照合せずに合格印が付く抜け道になるため不合格にする。
        if not numbers:
            return "unverified", "numbers_empty", None

        attribution = line.get("attribution")
        processing_note = line.get("processing_note")
        if not source_ref or not excerpt or not attribution or not processing_note:
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

        body_text = read_source_text(cache_path)
        if body_text is None:
            return "unverified", "source_unreadable", None
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
    """検査9: change欄(新しい変化)の行について、出典の公表時刻が36時間以上前でないかを確かめる。
    36時間以上前と分かった行、公表時刻が読み取れなかった(null)行は、どちらも安全側に倒して
    行そのものを落とす。原因が違うため件数は別々に数える(stale_source_hits / unknown_published_at_hits)。
    change以外の欄(big/ripple/deep)は36時間ルールの対象外なので、対象にしない。"""
    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", [])}
    generated_dt = parse_datetime_assume_jst(edition.get("generated_at"))

    stale = 0
    unknown_published_at = 0
    skipped = 0
    for section in edition["sections"]:
        for article in section.get("articles", []):
            lines = article.get("lines", [])
            if section.get("section_id") != "change":
                continue
            kept_lines = []
            for line in lines:
                source_ref = line.get("source_ref")
                source = sources_by_id.get(source_ref)
                if not source:
                    kept_lines.append(line)
                    continue
                published_dt = parse_datetime_assume_jst(source.get("published_at"))
                if published_dt is None:
                    unknown_published_at += 1
                    continue
                if generated_dt is None:
                    skipped += 1
                    kept_lines.append(line)
                    continue
                delta_hours = (generated_dt - published_dt).total_seconds() / 3600
                if delta_hours >= 36:
                    stale += 1
                    continue
                kept_lines.append(line)
            article["lines"] = kept_lines
    return stale, unknown_published_at, skipped


def run_check_baseline_late(edition):
    """検査20: 号の遅延判定。generated_atの時刻(日本時間)が、morning号なら8:50、
    noon号なら14:50を過ぎていたらTrueを返す。evening号は常にFalse(判定しない)。
    generated_atが読み取れない場合もFalse(この検査では判定できないため)。"""
    slot = edition.get("slot")
    if slot not in ("morning", "noon"):
        return False
    generated_dt = parse_datetime_assume_jst(edition.get("generated_at"))
    if generated_dt is None:
        return False
    local_time = generated_dt.astimezone(JST).time()
    if slot == "morning":
        return local_time > MORNING_DEADLINE
    return local_time > NOON_DEADLINE


def check_market_open(edition, calendar_dir):
    """検査35: market_openをAIの自己申告ではなく営業日カレンダー(calendar/{年}.json の
    business_days)から確定させ、edition["market_open"]を必ず上書きする。
    「カレンダーに無いから休場日(false)にする」という作りにはしない。カレンダー自体が
    無い・読めない場合と、実際の休場日を区別できなくなり、falseにすると企業欄が
    全削除されてしまうため、この場合は号ごと保存を止める。"""
    date_str = edition.get("date")
    try:
        dt.datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise EditionInvalid(f"date '{date_str}' がYYYY-MM-DD形式の日付ではありません。")

    year = date_str[:4]
    calendar_path = Path(calendar_dir) / f"{year}.json"
    if not calendar_path.is_file():
        raise EditionInvalid(f"営業日カレンダー '{calendar_path}' がありません。")

    try:
        calendar_obj = load_json(calendar_path)
    except (json.JSONDecodeError, OSError) as e:
        raise EditionInvalid(f"営業日カレンダー '{calendar_path}' を読み込めません: {e}")

    business_days = calendar_obj.get("business_days")
    if not isinstance(business_days, list):
        raise EditionInvalid(f"営業日カレンダー '{calendar_path}' のbusiness_daysが配列ではありません。")

    reported = edition.get("market_open")
    computed = date_str in business_days
    edition["market_open"] = computed

    return {"overwritten": reported != computed, "reported": reported}


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


LINK_ONLY_USAGE = "link_only"


def check_evidence_source_ref(hyp, sources_by_id, cache_dir):
    """検査11: evidence_grade が primary の自己申告を機械で確かめる。
    合格なら None、不合格なら理由の文字列を返す。

    既知の限界: この検査は「その出典に会社名がそのまま出ている」ことしか確かめられない。
    無関係な文脈での言及(例えばある会社の開示資料に取引先として別の会社名が挙がっている場合など)
    を一次情報と誤認する可能性がある。
    """
    ref = hyp.get("evidence_source_ref")
    if not ref:
        return "evidence_source_ref_missing"

    source = sources_by_id.get(ref)
    if source is None:
        return "evidence_source_not_found"

    if source.get("usage") == LINK_ONLY_USAGE:
        return "evidence_source_link_only"

    cache_path = Path(cache_dir) / f"{ref}.txt"
    if not cache_path.is_file():
        return "evidence_source_not_found"

    body_text = read_source_text(cache_path)
    if body_text is None:
        return "evidence_source_unreadable"

    company_name = hyp.get("company_name")
    if not company_name or company_name not in body_text:
        return "evidence_company_name_not_found"

    return None


def check_minus_direction(hyp, sources_by_id, cache_dir):
    """検査12: direction が minus の仮説は、次の3条件をすべて満たすときだけ残す。
      1. evidence_grade が primary
      2. evidence_filer_name が company_name と(前後の空白を除いて)完全一致する
      3. evidence_excerpt が evidence_source_ref の出典本文にそのまま存在する
         (検査1と同じ照合の仕方)
    direction が plus の仮説はこの検査の対象外(常に合格)。evidence_excerptが
    nullでも問題ない。"""
    if hyp.get("direction") != "minus":
        return True

    if hyp.get("evidence_grade") != "primary":
        return False

    company_name = strip_ws(hyp.get("company_name"))
    filer_name = strip_ws(hyp.get("evidence_filer_name"))
    if not company_name or not filer_name or company_name != filer_name:
        return False

    excerpt = hyp.get("evidence_excerpt")
    if not excerpt:
        return False
    ref = hyp.get("evidence_source_ref")
    source = sources_by_id.get(ref) if ref else None
    if source is None:
        return False
    cache_path = Path(cache_dir) / f"{ref}.txt"
    body_text = read_source_text(cache_path)
    if body_text is None:
        return False
    if normalize_text(excerpt) not in normalize_text(body_text):
        return False

    return True


def load_edinet_companies(cache_dir):
    """検査13(証券コードの確認)で使う、EDINET書類一覧から作った「会社名→証券コード」の
    対応を読む。--cache フォルダの中の SRC-EDINET-LIST.json(edinet_fetch.py listが
    そのまま保存した生データ)を優先し、無ければ .cache/edinet/companies.json
    (edinet_fetch.py list が同時に作る、company_name/tickerの形に整形済みのファイル)を見る。
    どちらも見つからなければ None を返す(=検査13の条件3を適用しない)。"""
    raw_path = Path(cache_dir) / "SRC-EDINET-LIST.json"
    if raw_path.is_file():
        try:
            raw_doc = load_json(raw_path)
        except (json.JSONDecodeError, OSError):
            return None
        companies, _ = edinet_fetch.build_companies(raw_doc)
        return companies

    companies_path = edinet_fetch.COMPANIES_CACHE_PATH
    if companies_path.is_file():
        try:
            return load_json(companies_path)
        except (json.JSONDecodeError, OSError):
            return None

    return None


def check_ticker_fields(hyp, edinet_companies):
    """検査13: ticker/ticker_sourceの確認。次のいずれかに当たったら不合格(None以外を返す)。
      1. ticker が null・空、または証券コードの形(edinet_fetch.is_valid_ticker、英字混在を含む)
         に合わない
      2. ticker_source が null・空
      3. EDINET書類一覧が読める場合に、company_name と ticker の組がその一覧に見つからない
         (links.price_history のURL「https://finance.yahoo.co.jp/quote/{証券コード}.T/history」の
         /quote/ と .T の間の文字列が ticker と違う場合も含む。その形に合わないURLは比較せず飛ばす)
    EDINET書類一覧が読めない(edinet_companiesがNone)場合、条件3は適用しない。
    証券コードの形の判定はedinet_fetch.is_valid_ticker()を使う(derive_ticker()と同じ判定を
    2か所に書かないため)。"""
    ticker = hyp.get("ticker")
    if not (isinstance(ticker, str) and edinet_fetch.is_valid_ticker(ticker)):
        return "ticker_missing"

    if not hyp.get("ticker_source"):
        return "ticker_source_missing"

    if edinet_companies is None:
        return None

    company_name = strip_ws(hyp.get("company_name"))
    match = None
    for c in edinet_companies:
        if strip_ws(c.get("filer_name")) == company_name:
            match = c
            break
    if match is None or match.get("ticker") != ticker:
        return "ticker_mismatch"

    price_history = (hyp.get("links") or {}).get("price_history")
    if price_history:
        m = PRICE_HISTORY_CODE_RE.search(price_history)
        if m and m.group(1) != ticker:
            return "ticker_mismatch"

    return None


def run_check_hypothesis_evidence(hyps, sources_by_id, cache_dir):
    """evidence_grade が primary の仮説だけを検査11にかけ、不合格なら inferred へ格下げする。
    direction が minus の仮説は、この後で走る検査12(check_minus_direction)が
    evidence_gradeがprimaryでなくなったことを検知して該当仮説を削除する。
    不合格の原因が出典ファイルの文字コード問題(evidence_source_unreadable)だった件数は、
    それ以外の原因と分けて返す(原因が違うため)。"""
    downgraded = 0
    unreadable = 0
    for hyp in hyps:
        if hyp.get("evidence_grade") != "primary":
            continue
        reason = check_evidence_source_ref(hyp, sources_by_id, cache_dir)
        if not reason:
            continue
        hyp["evidence_grade"] = "inferred"
        if reason == "evidence_source_unreadable":
            unreadable += 1
        else:
            downgraded += 1
    return downgraded, unreadable


def check_hypothesis(hyp, edition, line_ids, business_days, ng_words, sources_by_id, cache_dir, edinet_companies):
    if not check_minus_direction(hyp, sources_by_id, cache_dir):
        return "minus_condition_failed"

    ticker_reason = check_ticker_fields(hyp, edinet_companies)
    if ticker_reason:
        return ticker_reason

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


def run_hypothesis_checks(hypotheses_doc, edition, business_days, ng_words, cache_dir, edinet_companies):
    line_ids = {}
    for section, article, line in iter_lines(edition):
        line_ids[line.get("line_id")] = line.get("mark")

    hyps = hypotheses_doc.get("hypotheses", [])
    reasons = {}
    kept = []

    market_open = edition.get("market_open", True)
    baseline_late = bool(edition.get("baseline_late"))
    if not market_open or baseline_late:
        if hyps:
            # 検査14: market_openがfalse、またはbaseline_lateがtrueなのにhypothesesが
            # 空でない場合は全件削除する。両方に該当する場合はmarket_closedとして数える。
            if not market_open:
                reasons["market_closed"] = reasons.get("market_closed", 0) + len(hyps)
            else:
                reasons["baseline_late"] = reasons.get("baseline_late", 0) + len(hyps)
        hypotheses_doc["hypotheses"] = []
        return len(hyps), reasons

    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", [])}
    evidence_downgrades, evidence_unreadable = run_check_hypothesis_evidence(hyps, sources_by_id, cache_dir)
    if evidence_downgrades:
        reasons["primary_evidence_unverified"] = evidence_downgrades
    if evidence_unreadable:
        reasons["evidence_source_unreadable"] = evidence_unreadable

    for hyp in hyps:
        reason = check_hypothesis(hyp, edition, line_ids, business_days, ng_words, sources_by_id, cache_dir, edinet_companies)
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


# 東証33業種のうち、業種から会社を選ぶ仕組みでは使わない業種名(本システムでは
# 外国法人・組合を対象にしないため)。EXCLUDED_INDUSTRIES(サービス業・その他製品・
# その他金融業)はedinet_codelist.py側の定数をそのまま使う。
FOREIGN_INDUSTRY_NAME = "外国法人・組合"


def build_allowed_industries(codelist_rows):
    """下段(industry_examples)で使ってよい業種名の一覧を、コードリストの実データから作る
    (5.1: 業種名をこのスクリプトに手で書き写さない)。コードリストが無い場合は空集合を返す。"""
    if codelist_rows is None:
        return set()
    names = {name for name, _cnt in edinet_codelist._industry_counts(codelist_rows)}
    names.discard(FOREIGN_INDUSTRY_NAME)
    return names - edinet_codelist.EXCLUDED_INDUSTRIES


def check_lower_ticker(example):
    """検査13: 下段のticker/ticker_sourceの確認。どちらかが空なら不合格。"""
    if not example.get("ticker") or not example.get("ticker_source"):
        return "lower_ticker_missing"
    return None


def _lookup_codelist_company(example, codelist_rows):
    """下段の会社をコードリストの提出者名で引く(別名表は使わない。下段の
    company_nameは常にコードリストの正式名のため)。コードリストが無ければNone。"""
    if codelist_rows is None:
        return None
    return edinet_codelist.find_company_by_name(example.get("company_name"), codelist_rows, [])


def check_lower_listed(example, codelist_rows):
    """検査21: ticker_sourceがedinet_codelistなのに、コードリスト上その会社が
    「上場」で証券コードありの行として見つからない場合は不合格。"""
    if example.get("ticker_source") != "edinet_codelist":
        return None
    match = _lookup_codelist_company(example, codelist_rows)
    if match is None:
        return "lower_not_listed"
    return None


def check_lower_industry(example, allowed_industries):
    """検査22: industryが許可リスト(5.1)に無い、またはimpact_kindがnullでなければ不合格。"""
    if example.get("industry") not in allowed_industries:
        return "lower_industry_not_allowed"
    if example.get("impact_kind") is not None:
        return "lower_industry_not_allowed"
    return None


def check_lower_relation_text(example):
    """検査27: relation_textが作業Aの2つの定型文のどちらとも完全一致しない、
    またはrelation_textにcompany_nameが含まれていれば不合格。"""
    industry = example.get("industry")
    valid_texts = {
        pick_industry_companies.RELATION_TEXT_MENTIONED.format(industry=industry),
        pick_industry_companies.RELATION_TEXT_CAPITAL.format(industry=industry),
    }
    relation_text = example.get("relation_text")
    if relation_text not in valid_texts:
        return "lower_relation_text_mismatch"
    company_name = example.get("company_name")
    if company_name and company_name in relation_text:
        return "lower_relation_text_mismatch"
    return None


def check_lower_line_mark(example, line_marks):
    """検査28: line_idsが空、またはその行の確定した印(mark)がsource_number_match/
    reported_unverifiedのいずれでもなければ(見つからない行IDを含めて)不合格。"""
    line_ids = example.get("line_ids") or []
    if not line_ids:
        return "lower_line_mark_invalid"
    for lid in line_ids:
        if line_marks.get(lid) not in ("source_number_match", "reported_unverified"):
            return "lower_line_mark_invalid"
    return None


def check_lower_ticker_match(example, codelist_rows):
    """検査31: tickerが、コードリスト上の同じ会社の証券コードと一致しなければ不合格。"""
    match = _lookup_codelist_company(example, codelist_rows)
    if match is None or match.get("ticker") != example.get("ticker"):
        return "lower_ticker_mismatch"
    return None


def check_industry_example(example, line_marks, codelist_rows, allowed_industries):
    """下段の1社について、検査13→21→22→27→28→31の順に確かめ、最初に不合格になった
    理由を返す(全て合格ならNone)。"""
    for check_fn, args in (
        (check_lower_ticker, (example,)),
        (check_lower_listed, (example, codelist_rows)),
        (check_lower_industry, (example, allowed_industries)),
        (check_lower_relation_text, (example,)),
        (check_lower_line_mark, (example, line_marks)),
        (check_lower_ticker_match, (example, codelist_rows)),
    ):
        reason = check_fn(*args)
        if reason:
            return reason
    return None


def run_industry_example_checks(examples, edition, codelist_rows):
    """下段の各社に検査13・21・22・27・28・31をかけ、不合格の会社だけを取り除く。
    戻り値: (残った下段の会社のリスト, 理由ごとの不合格件数, 使った許可リスト(業種名の集合))。"""
    line_marks = {}
    for section, article, line in iter_lines(edition):
        line_marks[line.get("line_id")] = line.get("mark")

    allowed_industries = build_allowed_industries(codelist_rows)

    kept = []
    reasons = {}
    for example in examples:
        reason = check_industry_example(example, line_marks, codelist_rows, allowed_industries)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            kept.append(example)
    return kept, reasons, allowed_industries


GRADE_PRIORITY = {"primary": 0, "reported": 1, "inferred": 2}


def _trim_group_by_priority(members):
    """membersのうち、残す分(keepのidオブジェクト集合)と削る分(drop)を、
    優先順位(primary→reported→inferred)で決める。同じ印の中では配列の並び順
    (idx)が小さい方を残し、大きい方(後ろ)から削る。呼び出し元がlimit件まで
    切り詰める前提で使う内部ヘルパー。"""
    by_grade = {}
    for m in members:
        by_grade.setdefault(m["grade"], []).append(m)
    ordered = []
    for grade in ("primary", "reported", "inferred"):
        ordered.extend(sorted(by_grade.get(grade, []), key=lambda m: m["idx"]))
    return ordered


def _apply_slot_limit(slots, key_fn, limit):
    """slotsを key_fn でグループ分けし、各グループがlimit件を超えていたら、
    優先順位(primary→reported→inferred、同じ印は配列順の後ろから)で超過分を削る。
    key_fnがNoneを返すslotはそのグループ分けの対象外(削らない)。
    戻り値: (残ったslots, 削られたslots)。"""
    groups = {}
    for s in slots:
        key = key_fn(s)
        if key is None:
            continue
        groups.setdefault(key, []).append(s)

    dropped_ids = set()
    for key, members in groups.items():
        if len(members) <= limit:
            continue
        ordered = _trim_group_by_priority(members)
        for m in ordered[limit:]:
            dropped_ids.add(id(m))

    remaining = [s for s in slots if id(s) not in dropped_ids]
    dropped = [s for s in slots if id(s) in dropped_ids]
    return remaining, dropped


def run_slot_allocation(hypotheses_doc, edition):
    """検査29: 上段・下段の個別の検査が終わった後に、号全体の枠を数える。
      ・上段と下段に同じ会社(ticker)がいたら下段側を削除
      ・下段(industry_examples)は最大2社
      ・1業種あたり最大2社(下段のみ。上段にはindustryが無いため対象外)
      ・1記事あたり最大2社(上段+下段の合計)
      ・号全体は最大5社(上段+下段の合計)
    超過分の削除は、残す優先順位をprimary→reported→inferredとし、同じ印の中では
    配列の並び順の後ろから削る(何度実行しても同じ結果になるように)。
    hypotheses_doc["hypotheses"]/["industry_examples"]を、残った分だけに更新する。
    戻り値: (削除した件数の合計, 理由ごとの内訳)。"""
    hyps = hypotheses_doc.get("hypotheses") or []
    examples = hypotheses_doc.get("industry_examples") or []

    line_to_article = {}
    for section, article, line in iter_lines(edition):
        line_to_article[line.get("line_id")] = article.get("article_id")

    def hyp_article_id(h):
        for lid in h.get("line_ids") or []:
            article_id = line_to_article.get(lid)
            if article_id:
                return article_id
        return None

    slots = []
    for idx, h in enumerate(hyps):
        slots.append({
            "origin": "upper", "obj": h, "idx": idx,
            "ticker": h.get("ticker"), "article_id": hyp_article_id(h),
            "industry": None, "grade": h.get("evidence_grade"),
        })
    for idx, e in enumerate(examples):
        slots.append({
            "origin": "lower", "obj": e, "idx": idx,
            "ticker": e.get("ticker"), "article_id": e.get("article_id"),
            "industry": e.get("industry"), "grade": "inferred",
        })

    reasons = {}

    def record(dropped, reason):
        if dropped:
            reasons[reason] = reasons.get(reason, 0) + len(dropped)

    # 規則: 上段と下段に同じ会社(ticker)がいたら下段側を削除する。
    upper_tickers = {s["ticker"] for s in slots if s["origin"] == "upper" and s["ticker"]}
    duplicates = [s for s in slots if s["origin"] == "lower" and s["ticker"] in upper_tickers]
    if duplicates:
        dup_ids = {id(s) for s in duplicates}
        slots = [s for s in slots if id(s) not in dup_ids]
        record(duplicates, "slot_duplicate_with_upper")

    slots, dropped = _apply_slot_limit(
        slots, lambda s: "lower" if s["origin"] == "lower" else None,
        pick_industry_companies.LOWER_SECTION_LIMIT,
    )
    record(dropped, "slot_lower_section_limit")

    slots, dropped = _apply_slot_limit(
        slots, lambda s: s["industry"] if s["origin"] == "lower" else None,
        pick_industry_companies.PER_INDUSTRY_LIMIT,
    )
    record(dropped, "slot_per_industry_limit")

    slots, dropped = _apply_slot_limit(
        slots, lambda s: s["article_id"],
        pick_industry_companies.PER_ARTICLE_LIMIT,
    )
    record(dropped, "slot_per_article_limit")

    slots, dropped = _apply_slot_limit(
        slots, lambda s: "all",
        pick_industry_companies.TOTAL_LIMIT,
    )
    record(dropped, "slot_total_limit")

    kept_upper = [s["obj"] for s in sorted((s for s in slots if s["origin"] == "upper"), key=lambda s: s["idx"])]
    kept_lower = [s["obj"] for s in sorted((s for s in slots if s["origin"] == "lower"), key=lambda s: s["idx"])]

    hypotheses_doc["hypotheses"] = kept_upper
    hypotheses_doc["industry_examples"] = kept_lower

    total_violations = sum(reasons.values())
    return total_violations, reasons


def print_report(edition_path, stats, stop_hits, watch_hits, dropped_inferences, stale_hits,
                  unknown_published_at_hits, stale_skipped,
                  hypothesis_violations, hypothesis_reasons, number_failure_details, ok,
                  baseline_late=False, ticker_crosscheck="skipped", source_usage_invalid_hits=0,
                  industry_report=None, source_policy_unlisted_domains=None):
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
            "source_unreadable": "出典ファイルが文字コードの問題で読めなかった",
            "numbers_empty": "数字を1つも書かずに「出典と数字が一致」と申告していた",
            "excerpt_not_allowed": "本文を取得していない出典(quotable以外)からの抜き出しだった(excerptは削除した)",
        }
        for reason, count in stats["unverified_reasons"].items():
            print(f"  ・{reason_text.get(reason, reason)}: {count}件")

    if number_failure_details:
        print("数字が見つからなかった行:")
        for detail in number_failure_details:
            values = ", ".join(str(n.get("value")) for n in detail["missing_numbers"])
            print(f"  ・{detail['line_id']}: {values}")

    print(f"推奨表現(停止)により削除した行数: {len(stop_hits)}")
    if stop_hits:
        print("  削除した行(号は保存されています):")
        for hit in stop_hits:
            print(f"    ・{hit['line_id']}: 「{hit['word']}」 (本文: {hit['text']})")
    print(f"推奨表現(注意)の検出件数: {len(watch_hits)}")
    if watch_hits:
        print("  注意に挙がった行(号は保存されています):")
        for hit in watch_hits:
            print(f"    ・{hit['line_id']}: 「{hit['word']}」 (本文: {hit['text']})")
    print(f"出典が古い(36時間以上前)行の件数: {stale_hits}")
    print(f"出典の公表時刻が分からず、鮮度を確認できなかったため落とした行の件数: {unknown_published_at_hits}")
    print(f"出典の日時が読み取れず判定できなかった行の件数: {stale_skipped}")
    print(f"必須項目が空で削除した推論の件数: {dropped_inferences}")
    print(f"号の遅延判定(baseline_late): {baseline_late}")
    print(f"証券コードの突き合わせ(ticker_crosscheck): {ticker_crosscheck}")
    print(f"usageが正しく書かれていない出典の件数(source_usage_invalid_hits): {source_usage_invalid_hits}")
    unlisted = source_policy_unlisted_domains or []
    print(f"出典ポリシー表(source_policy.csv)に無かったドメイン: {'、'.join(unlisted) if unlisted else 'なし'}")

    if hypothesis_reasons:
        print(f"仮説に関する指摘件数: {hypothesis_violations}")
        reason_text = {
            "missing_field": "必須項目が空だった(削除)",
            "line_id_not_found": "紙面に存在しない行を参照していた(削除)",
            "primary_requires_verified_line": "根拠が最上位なのに参照行が未確認だった(削除)",
            "deadline_date_mismatch": "確認期限の日付が営業日計算と合わなかった(削除)",
            "relation_text_conclusive_word": "断定的な言葉(プラス/マイナス/好材料/悪材料)が入っていた(削除)",
            "relation_text_recommendation": "説明文に推奨表現が入っていた(削除)",
            "market_closed": "市場が休みの号に仮説が入っていた(削除)",
            "baseline_late": "号が遅延していた(baseline_late)ため仮説が入っていた(削除)",
            "too_many_hypotheses": "仮説が上限(5件)を超えていた(削除)",
            "primary_evidence_unverified": "根拠が最上位(primary)の自己申告なのに、出典本文に会社名を確認できなかった(inferredへ格下げ。方向がminusならこの後さらに削除される)",
            "evidence_source_unreadable": "根拠の出典ファイルが文字コードの問題で読めなかった(inferredへ格下げ)",
            "minus_condition_failed": "下振れ方向(minus)の3条件(根拠primary・提出者名の一致・抜き出しの実在)のいずれかを満たさなかった(削除)",
            "ticker_missing": "証券コードが無い、または証券コードの形に合わなかった(削除)",
            "ticker_source_missing": "証券コードの出典(ticker_source)が空だった(削除)",
            "ticker_mismatch": "証券コードがEDINET書類一覧の記録と一致しなかった(削除)",
        }
        for reason, count in hypothesis_reasons.items():
            print(f"  ・{reason_text.get(reason, reason)}: {count}件")
    elif hypothesis_violations:
        print(f"仮説に関する指摘件数: {hypothesis_violations}")

    if industry_report:
        print(f"業種の指定(industry_picks)の件数: {industry_report['industry_picks_total']}件")
        if industry_report["ai_written_examples_discarded"]:
            print(f"AIが書いたindustry_examplesを破棄した件数: {industry_report['ai_written_examples_discarded']}件")
        print(f"下段(業種から選んだ企業欄)の会社数: {industry_report['industry_examples_total']}社")

        violations = industry_report["industry_example_violations"]
        if violations["total"]:
            print(f"下段の検査で削除した会社数: {violations['total']}件")
            lower_reason_text = {
                "lower_ticker_missing": "証券コードまたはticker_sourceが空だった(削除)",
                "lower_not_listed": "コードリスト上「上場」として見つからなかった(削除)",
                "lower_industry_not_allowed": "業種が許可リストに無い、またはimpact_kindが空でなかった(削除)",
                "lower_relation_text_mismatch": "定型文と一致しない、または会社名が文中に含まれていた(削除)",
                "lower_line_mark_invalid": "根拠の行の確定した印が対象外だった、または行が見つからなかった(削除)",
                "lower_ticker_mismatch": "証券コードがコードリストの記録と一致しなかった(削除)",
            }
            for reason, count in violations["reasons"].items():
                print(f"  ・{lower_reason_text.get(reason, reason)}: {count}件")

        slot = industry_report["slot_violations"]
        if slot["total"]:
            print(f"枠配分(検査29)で削除した会社数: {slot['total']}件")
            slot_reason_text = {
                "slot_duplicate_with_upper": "上段と下段に同じ会社がいたため下段側を削除",
                "slot_lower_section_limit": "下段の上限(2社)を超えていた",
                "slot_per_industry_limit": "1業種あたりの上限(2社)を超えていた",
                "slot_per_article_limit": "1記事あたりの上限(2社)を超えていた",
                "slot_total_limit": "号全体の上限(5社)を超えていた",
            }
            for reason, count in slot["reasons"].items():
                print(f"  ・{slot_reason_text.get(reason, reason)}: {count}件")

        short_match = industry_report["short_name_match_count"]
        print(f"3文字以下の名前で本文一致した件数: {short_match['count']}件")
        if short_match["names"]:
            print(f"  該当した名前: {'、'.join(short_match['names'])}")


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
    source_policy_path = script_dir / "source_policy.csv"

    try:
        edition_path = Path(args.edition)
        try:
            edition = load_json(edition_path)
        except (json.JSONDecodeError, OSError) as e:
            raise EditionInvalid(f"紙面JSONを読み込めません: {e}")

        check_a_structure(edition)
        check_b_edition_id(edition, edition_path)

        # usage/publisher_typeはAIの自己申告を信用せず、表の値で必ず上書きする。
        source_policy_result = apply_source_policy(edition, source_policy_path)

        # 検査35: market_openはAIの自己申告ではなく営業日カレンダーで確定させる。
        market_open_result = check_market_open(edition, args.calendar)

        ng_words = load_ng_words(ng_words_path)
        ng_words_exclude = load_ng_words(ng_words_exclude_path)

        # 検査7: 停止語を含む行はその行だけを削除する(号全体は保存する)。
        stop_hits = check_c_stop_words(edition, ng_words)

        watch_hits = check_watch_proximity(edition, ng_words_exclude)

        stats, number_failure_details = run_line_verification(edition, args.cache)
        source_usage_invalid_hits = count_invalid_source_usages(edition)
        dropped_inferences = run_check_d_inferences(edition)
        stale_hits, unknown_published_at_hits, stale_skipped = run_check_e_stale_sources(edition)

        # 検査20: 号の遅延判定。市場のtrue/falseとは独立に、生成時刻から判定する。
        baseline_late = run_check_baseline_late(edition)
        edition["baseline_late"] = baseline_late

        edinet_companies = load_edinet_companies(args.cache)
        ticker_crosscheck = "applied" if edinet_companies is not None else "skipped"

        hypothesis_violations = 0
        hypothesis_reasons = {}
        hypotheses_doc = None
        industry_report = None
        if args.hypotheses:
            hypotheses_doc = load_json(args.hypotheses)
            hypotheses_doc["baseline_late"] = baseline_late
            business_days = load_business_days(args.calendar)
            hypothesis_violations, hypothesis_reasons = run_hypothesis_checks(
                hypotheses_doc, edition, business_days, ng_words, args.cache, edinet_companies
            )

            # 5. 下段(業種から選ぶ企業欄)の選定。規則6: 上段の検査が終わって残った
            # 会社のtickerを、下段の候補から除く(社名の文字列では比べない)。
            excluded_tickers = {
                h.get("ticker") for h in hypotheses_doc["hypotheses"] if h.get("ticker")
            }
            articles_by_id = pick_industry_companies.index_articles(edition)
            aliases_by_edinet_code = pick_industry_companies.load_aliases()
            pick_result = pick_industry_companies.run(
                hypotheses_doc, articles_by_id, aliases_by_edinet_code, excluded_tickers
            )

            if pick_result.get("fatal_error"):
                # コードリストが未取得。号全体は保存し、下段だけ空のまま扱う。
                hypotheses_doc["industry_examples"] = []
            else:
                hypotheses_doc["industry_examples"] = pick_result["examples"]

            # 6. 下段の個別の検査(検査13・21・22・27・28・31)。
            codelist_rows, _codelist_date = edinet_codelist.load_codelist()
            kept_examples, lower_reasons, allowed_industries = run_industry_example_checks(
                hypotheses_doc["industry_examples"], edition, codelist_rows
            )
            hypotheses_doc["industry_examples"] = kept_examples

            # 7. 枠配分の検査29は、上段・下段の個別の検査が終わった後に行う。
            slot_total, slot_reasons = run_slot_allocation(hypotheses_doc, edition)

            industry_report = {
                "industry_picks_total": pick_result.get("total_pick_count", 0),
                "industry_examples_total": len(hypotheses_doc["industry_examples"]),
                "industry_example_violations": {
                    "total": sum(lower_reasons.values()), "reasons": lower_reasons,
                },
                "ai_written_examples_discarded": pick_result.get("ai_written_examples_discarded", 0),
                "slot_violations": {"total": slot_total, "reasons": slot_reasons},
                "short_name_match_count": {
                    "count": pick_result.get("mentioned_short_count", 0),
                    "names": pick_result.get("short_name_matches", []),
                },
                "allowed_industries_count": len(allowed_industries),
            }

        run_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()
        edition["verification"] = {
            "script_version": "2.0.0",
            "run_at": run_at,
            "lines_total": stats["lines_total"],
            "passed": stats["passed"],
            "unverified": stats["unverified"],
            "reported_unverified": stats["reported_unverified"],
            "explainer": stats["explainer"],
            "recommendation_stop_hits": len(stop_hits),
            "recommendation_stop_removed": len(stop_hits),
            "recommendation_warn_hits": len(watch_hits),
            "recommendation_warnings": [
                {"line_id": hit["line_id"], "word": hit["word"]} for hit in watch_hits
            ],
            "stale_source_hits": stale_hits,
            "unknown_published_at_hits": unknown_published_at_hits,
            "stale_check_skipped": stale_skipped,
            "inference_dropped": dropped_inferences,
            "hypothesis_violations": hypothesis_violations,
            "unverified_reasons": stats["unverified_reasons"],
            "ticker_crosscheck": ticker_crosscheck,
            "baseline_late": baseline_late,
            "source_usage_invalid_hits": source_usage_invalid_hits,
            "source_policy_applied": True,
            "source_policy_overwritten": source_policy_result["overwritten"],
            "source_policy_unlisted_domains": source_policy_result["unlisted_domains"],
            "market_open_source": "calendar",
            "market_open_overwritten": market_open_result["overwritten"],
            "market_open_reported": market_open_result["reported"],
        }
        if industry_report is not None:
            edition["verification"].update(industry_report)

        with open(edition_path, "w", encoding="utf-8") as f:
            json.dump(edition, f, ensure_ascii=False, indent=1)

        if args.hypotheses:
            with open(args.hypotheses, "w", encoding="utf-8") as f:
                json.dump(hypotheses_doc, f, ensure_ascii=False, indent=1)

        print_report(
            args.edition, stats, stop_hits, watch_hits, dropped_inferences, stale_hits,
            unknown_published_at_hits, stale_skipped,
            hypothesis_violations, hypothesis_reasons, number_failure_details, ok=True,
            baseline_late=baseline_late, ticker_crosscheck=ticker_crosscheck,
            source_usage_invalid_hits=source_usage_invalid_hits,
            industry_report=industry_report,
            source_policy_unlisted_domains=source_policy_result["unlisted_domains"],
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
