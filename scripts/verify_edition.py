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
      "evidence_grade": "primary" | "reported" | "inferred",
      "evidence_source_ref": "SRC-001" | null,
      "evidence_filer_name": "..." | null,
      "baseline_date": "2026-09-24",
      "baseline_price_type": "close" | "open" | ...,
      "line_ids": ["L-003-02"],
      "links": {"price_history": "https://..."}
    }
  ]
}

ticker_source / links.price_history は検査13(証券コードの確認)で使う。
改修27-2: direction・evidence_excerptは廃止した項目(新しい号には書かれない。過去の号の
ファイルには残っているが読まない)。それを読んでいた検査12は廃止し、
検査番号12は欠番のままにする(他の検査の番号はずらさない)。
改修31第1回(3-6): 照合した号では、上段の仮説からdirection・evidence_excerpt・falsifierを取り除く
(remove_deprecated_hypothesis_keys。件数をdeprecated_keys_removedに記録)。過去の号は照合し直されない
(検査24で止まる)ため、そのファイルの値は残る。紙面の推論欄のfalsifierは別物なので消さない。
evidence_filer_name / evidence_doc_type / evidence_role /
impact_kind / impact_kind_source / auto_check_target はAIには書かせず、出典URLの書類管理番号
からEDINET書類一覧を引いてapply_edinet_evidence()が機械で確定する(check_hypothesis()の
ループより先に実行する)。どちらも今回追加した項目のため、依頼文には例示が無い
(本スクリプトが定める形)。

改修27-1(決定1・4-5、紙面を書くAIへの指示第7.1版): horizon_business_days・deadline_dateは
apply_observation_window()が、impact_kind(price_statedなら5営業日、それ以外は20営業日)から
機械で必ず埋める(AIに書かせない。書いても必ず上書きする)。上段の仮説のfalsifierもAIに
書かせなくなったため、REQUIRED_HYPOTHESIS_FIELDSから外した(推論欄のfalsifierは
run_check_d_inferences()が今までどおり必須のまま扱う。取り違えないこと)。
"""
import argparse
import csv
import datetime as dt
import decimal
import hashlib
import html
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

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
    "company_name", "relation_text",
    "baseline_date", "baseline_price_type",
]
# 改修27-1(決定1): falsifierは上段の必須項目から外した(紙面を書くAIが書かなくなったため)。
# run_check_d_inferences()が読む推論欄のfalsifierは別物で、そちらは今までどおり必須。
# horizon_business_daysも改修27-1(4-5)でapply_observation_window()が機械で必ず埋めるため、
# AIに書かせる必須項目からは外した(deadline_dateはもともとここに無い)。
# links.price_history のURL(https://finance.yahoo.co.jp/quote/{証券コード}.T/history)から
# /quote/ と .T の間の文字列を取り出す(検査13で使う)。
PRICE_HISTORY_CODE_RE = re.compile(r"/quote/([^/]+)\.T(?:/|$)")
# 出典のURL(https://disclosure2.edinet-fsa.go.jp/api/v2/documents/{書類管理番号}?type=1)から
# 書類管理番号を取り出す(evidence_filer_name/evidence_doc_type/evidence_role/impact_kindを
# 機械で確定する処理で使う)。editions/配下の実データで確認できた形はこれ1種類のみ
# (2026年9月21日時点)。見たことのない形のURLは無理に解釈せず、取り出せなかった件数を
# edinet_url_unparsedとして記録する。
EDINET_DOC_ID_RE = re.compile(
    r"^https://disclosure2\.edinet-fsa\.go\.jp/api/v2/documents/([^/?]+)(?:\?|$)"
)
# 書類種別コード(docTypeCode)からimpact_kindを機械で決めるための対応表。ここに無い
# コードにはimpact_kindを付けない(fact_onlyは機械では付けない。届出の種類が幅広く、
# 事実と違う表示になりうるため)。あとから対応表を広げやすいよう、ここに1か所でまとめる。
EDINET_DOC_TYPE_IMPACT_KIND = {
    "240": "price_stated",   # 公開買付届出書
    "250": "price_stated",   # 訂正公開買付届出書
    "270": "price_stated",   # 公開買付報告書
    "280": "price_stated",   # 訂正公開買付報告書
    "350": "amount_stated",  # 大量保有報告書・変更報告書
    "360": "amount_stated",  # 訂正報告書(大量保有・変更)
    "220": "amount_stated",  # 自己株券買付状況報告書
    "230": "amount_stated",  # 訂正自己株券買付状況報告書
}
# 改修27-1(4-10): tob_sideを機械で決める対象(公開買付関係、書類種別コード240〜280)。
# 240:公開買付届出書 250:訂正公開買付届出書 260:公開買付撤回届出書
# 270:公開買付報告書 280:訂正公開買付報告書
TOB_DOC_TYPE_CODES = {"240", "250", "260", "270", "280"}
VALID_SOURCE_USAGES = {"quotable", "link_only", "snippet_only"}
VALID_PUBLISHER_TYPES = {
    "government_statistics", "central_bank", "company_disclosure",
    "international_org", "news", "other",
}
SOURCE_POLICY_COLUMNS = (
    "domain", "usage", "publisher_type", "independent_check",
    "attribution_template", "processing_note_template",
)
MORNING_DEADLINE = dt.time(8, 50)

_WS_RE = re.compile(r"[ \t\r\n　]")
_COMMA_RE = re.compile(r"(?<=[0-9]),(?=[0-9])")
# 改修28第1回: pdftotextはPDFのマイナスを「‐」(U+2010)・「‑」(U+2011)で出すことがあるため、
# ハイフンの仲間の「‐」「‑」「‒」(U+2012)も「-」に揃える(揃えないと、抜き出しも数字も一致しない)。
_DASH_CHARS = ["〜", "～", "－", "—", "−", "~", "\u2010", "\u2011", "\u2012"]
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


_ANY_WS_RE = re.compile(r"\s+")


def normalize_filer_name_for_compare(value):
    """提出者名の比較用の形にする(改修30)。NFKC正規化(全角英数字・全角空白を半角に)をして、
    空白(全角・半角・タブ・改行)をすべて取り除く。法人格(株式会社など)は提出者名の
    正式な一部なので取り除かない。文字列でない・空になる場合はNoneを返す。
    AIが書いた company_name と、EDINET書類一覧の提出者名(「株式会社　商船三井」のように
    社名の間に全角空白が入ることがある)を同じ規則で比べるために使う。"""
    if not isinstance(value, str):
        return None
    normalized = _ANY_WS_RE.sub("", unicodedata.normalize("NFKC", value))
    return normalized or None


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


_HTML_STYLE_SCRIPT_RE = re.compile(r"(?is)<(style|script)[^>]*>.*?</\1>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html_tags(html_text):
    """改修27-1(4-9): EDINETのHTML本文(iXBRLのhtmlファイル)からタグを取り除いた
    素のテキストを作る。まず<style>・<script>ブロックを丸ごと除き(文字色や
    フォント指定の中の文字が本文に紛れ込むのを防ぐ)、次にタグを除き、最後に
    HTMLエンティティ(&amp;等)を元の文字に戻す。呼び出し側(read_source_body_for_checks)
    がキャッシュの元ファイルを書き換えずに毎回メモリの中でこの結果を作る。"""
    if not html_text:
        return html_text
    without_style_script = _HTML_STYLE_SCRIPT_RE.sub("", html_text)
    without_tags = _HTML_TAG_RE.sub("", without_style_script)
    return html.unescape(without_tags)


def read_source_body_for_checks(cache_path, source):
    """改修27-1(4-9): 検査1(excerpt・数字の一致)・検査11(一次情報の会社名)・
    検査36(published_atの検算)で使う出典本文を読む。read_source_text()で読んだ生のテキストのうち、出典が
    EDINET(is_edinet_domain)のものだけ、タグを取り除いてから返す(4-9で処理する
    のはEDINETの書類の本文がタグ入りのHTMLのため。EDINET以外の出典はタグを
    含まない普通の本文のため、そのまま返す)。
    キャッシュの元ファイル自体は一切書き換えない(read_source_text()経由で
    読むだけ)。呼ぶたびにメモリの中で1回だけタグ除去を行う(元ファイルを
    書き換えて使い回すことはしない)。"""
    raw_text = read_source_text(cache_path)
    if raw_text is None:
        return None
    url = (source or {}).get("url")
    if is_edinet_domain(url):
        return strip_html_tags(raw_text)
    return raw_text


# ---------------------------------------------------------------------------
# 改修29第1回: 出典の本文を「1行」に区切る関数と、1行の中で数字を確かめる関数。
# normalize_text()・_WS_RE・find_number()は変えない(日付の検索・記録の数え方・今あるテストが使うため)。
# ---------------------------------------------------------------------------

# 普通の文字の本文(PDFをpdftotextで文字にしたもの等)の1行の区切り。改行(\n・\r\n・\r)と
# 改ページ(\f)で分ける。改ページは、この1行の判定でだけ改行として扱う(normalize_text()では消さない)。
_SOURCE_LINE_SPLIT_RE = re.compile(r"\r\n|[\r\n\f]")

# HTMLの区切りで「文中のタグ」として扱う(区切りにしない)タグ。これ以外のタグ(p・div・h1〜h6・
# td・li・br など)は区切りにする。ix:で始まるタグ(EDINETのiXBRLの値の札)も文中のタグとして扱う。
_HTML_INLINE_TAG_NAMES = frozenset({
    "span", "a", "b", "i", "u", "s", "sub", "sup", "font", "em", "strong", "small", "big",
    "abbr", "code", "ruby", "rt", "rp", "wbr", "img", "label", "nobr",
})
_HTML_TAG_NAME_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9:._-]*)")


def split_html_lines(html_text):
    """HTML(タグ入り)を「1行」の区切りのリストにする(事前調査R2-2の作り方)。
    ①<style>・<script>を消す ②文中のタグ(_HTML_INLINE_TAG_NAMES・ix:〜)は区切りにしない
    ③<tr>〜</tr>の中は区切らない(入れ子の表も外側の行に含める) ④それ以外のブロックのタグと
    <br>で区切る。タグの見分け方はstrip_html_tags()と同じ正規表現を使うので、区切りを
    つなげ直すとstrip_html_tags()の結果と同じ文字になる(呼び出し側で確かめる)。"""
    text = _HTML_STYLE_SCRIPT_RE.sub("", html_text or "")
    units = []
    current = []
    tr_depth = 0
    pos = 0
    for m in _HTML_TAG_RE.finditer(text):
        if m.start() > pos:
            current.append(text[pos:m.start()])
        pos = m.end()
        name_match = _HTML_TAG_NAME_RE.match(m.group())
        if not name_match:
            continue
        name = name_match.group(2).lower()
        closing = name_match.group(1) == "/"
        self_closing = m.group().rstrip(">").rstrip().endswith("/")
        if name.startswith("ix:") or name in _HTML_INLINE_TAG_NAMES:
            continue
        if name == "tr":
            if closing:
                tr_depth = max(0, tr_depth - 1)
                if tr_depth == 0 and current:
                    units.append("".join(current))
                    current = []
            elif not self_closing:
                if tr_depth == 0 and current:
                    units.append("".join(current))
                    current = []
                tr_depth += 1
            continue
        if tr_depth > 0:
            continue
        if current:
            units.append("".join(current))
            current = []
    if pos < len(text):
        current.append(text[pos:])
    if current:
        units.append("".join(current))
    return [html.unescape(u) for u in units]


def _normalize_for_line_join(text):
    """区切りをつなげ直した文字と照合の本文を比べるときのそろえ方。normalize_text()と同じ
    そろえ方に、改ページ(\f)を消すことだけを足す(\fは1行の判定で改行として扱い、区切りの
    印として取り除くため。normalize_text()は改行を消すが\fは消さない)。"""
    return normalize_text((text or "").replace("\f", ""))


def _read_raw_html_for_lines(cache_path):
    """EDINET以外のHTMLの出典について、{id}.meta.jsonのkindがhtmlで、raw/{id}.htmlが
    あれば、save_source.pyと同じ決め方の文字コードで読んだHTMLを返す。
    戻り値: (HTMLの文字 or None, 読めなかった理由 or None)。元のファイルが無い・
    記録ファイルが無い/htmlでない場合は(None, None)(本文の改行で分ける)。"""
    cache_path = Path(cache_path)
    source_id = cache_path.stem
    meta_path = cache_path.parent / f"{source_id}.meta.json"
    raw_path = cache_path.parent / "raw" / f"{source_id}.html"
    if not meta_path.is_file() or not raw_path.is_file():
        return None, None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(meta, dict) or meta.get("kind") != "html":
        return None, None
    # save_source.pyはこのファイルを読み込むため、ここで(呼ばれたときに)読み込む(循環を避ける)。
    import save_source
    try:
        data = raw_path.read_bytes()
        encoding, _found_in = save_source.decide_html_encoding(data, meta.get("content_type"))
        return data.decode("utf-8-sig" if encoding in ("utf-8", "utf8") else encoding), None
    except (OSError, LookupError, UnicodeDecodeError):
        return None, "raw_html_unreadable"


def split_source_lines(cache_path, source, body_text=None):
    """出典の本文を「1行」のリストに区切る。
    ・EDINET(is_edinet_domain): キャッシュの{id}.txt(タグ入りのHTML)からsplit_html_lines()で区切る。
    ・EDINET以外のHTML: {id}.meta.jsonのkindがhtmlでraw/{id}.htmlがあれば、そこから同じ作り方で
      区切る。元のファイルが無ければ、本文の改行で分ける。
    ・PDF・普通の文字の本文: 改行(\n・\r\n・\r)と改ページ(\f)で分ける。
    区切りをつなげ直した文字が、照合の本文(read_source_body_for_checks()の結果)と
    _normalize_for_line_join()のそろえ方の後で1文字も違わないことを確かめ、違えば区切らない。
    戻り値: (行のリスト, None) か (None, 区切れなかった理由)。"""
    if body_text is None:
        body_text = read_source_body_for_checks(cache_path, source)
    if body_text is None:
        return None, "source_unreadable"
    if is_edinet_domain((source or {}).get("url")):
        raw_text = read_source_text(cache_path)
        if raw_text is None:
            return None, "source_unreadable"
        lines = split_html_lines(raw_text)
    else:
        raw_html, problem = _read_raw_html_for_lines(cache_path)
        if problem:
            return None, problem
        if raw_html is not None:
            lines = split_html_lines(raw_html)
        else:
            lines = _SOURCE_LINE_SPLIT_RE.split(body_text)
    if _normalize_for_line_join("".join(lines)) != _normalize_for_line_join(body_text):
        return None, "join_mismatch"
    return lines, None


def find_excerpt_lines(lines, excerpt_norm):
    """抜き出し(normalize_text()でそろえた後)が丸ごと入る行の番号(0始まり)のリスト。"""
    return [i for i, seg in enumerate(lines) if excerpt_norm in normalize_text(seg)]


def find_spanned_lines(lines, excerpt_norm):
    """どの1行にも入らない抜き出しが、続けて並んだどの行にまたがっていたか(行の番号の
    リスト、最初の行から最後の行まで)。各行をそろえてつないだ文字の中に見つからなければ
    空のリスト(check_excerpts.pyの表示用)。"""
    norms = [normalize_text(seg) for seg in lines]
    joined = "".join(norms)
    k = joined.find(excerpt_norm)
    if k < 0 or not excerpt_norm:
        return []
    end = k + len(excerpt_norm)
    touched = []
    offset = 0
    for i, n in enumerate(norms):
        if n and offset < end and offset + len(n) > k:
            touched.append(i)
        offset += len(n)
    return touched


_LINE_WS_CHARS = frozenset(" \t\r\n　")
_HALFWIDTH_VOICED_MARKS = "ﾞﾟ゙゚"
# 1桁の数字のあとに「空白ちょうど1つ＋数字・小数点・カンマ」が続く並び(税関のPDFの
# 「1 3 . 7」「6 1 , 6 7 4」、日付の「1 6」など)。前後が数字・小数点・カンマや
# 「それ＋空白1つ」に続く場合は並びの途中なので当てない(列が空白2つ以上で区切られた表の
# 数は、空白1つでは並ばないためつながらない)。
_SPACED_DIGITS_RE = re.compile(r"(?<![0-9.,])(?<![0-9.,] )[0-9](?: [0-9.,])+(?![0-9.,])(?! [0-9.,])")
# 改修29: 「▲」「△」は負の数の印として読む。第1回は数字の直前の「▲」だけだったが、第2回から、
# 記号と数字の間に空白(半角・全角の空白・タブ。HTMLの1行の中の改行も同じ1行の空白として扱う)しか
# 無いものと、「△」も読む。記号と数字の間に空白以外の文字があるもの(凡例の「（△）」等)は読まない。
_NEGATIVE_MARKS = "▲△"


def _line_clusters(seg):
    """1文字(と、その後ろに付く結合文字・半角の濁点/半濁点)ずつ、元の位置と一緒に返す。
    NFKCで「ｶﾞ」→「ガ」のように文字数が変わっても、元の行の位置との対応を保つため。"""
    i = 0
    while i < len(seg):
        j = i + 1
        while j < len(seg) and (seg[j] in _HALFWIDTH_VOICED_MARKS or unicodedata.combining(seg[j])):
            j += 1
        yield i, seg[i:j]
        i = j


def _map_line_chars(seg, keep_space):
    """本文の1行を、normalize_text()と同じそろえ方の文字にし、各文字が元の行の何文字目
    から来たかの対応表と一緒に返す。keep_spaceが真なら、空白を消さずに1文字ずつ
    半角の空白にそろえる(空白の数は変えない)。数字にはさまれたカンマは消す。"""
    chars = []
    index = []
    for i, cluster in _line_clusters(seg):
        n = unicodedata.normalize("NFKC", cluster)
        for ch in _DASH_CHARS:
            n = n.replace(ch, "-")
        for k, v in _QUOTE_MAP.items():
            n = n.replace(k, v)
        for ch in n:
            if ch in _LINE_WS_CHARS:
                if not keep_space:
                    continue
                ch = " "
            chars.append(ch)
            index.append(i)
    keep = [
        j for j, ch in enumerate(chars)
        if not (ch == "," and 0 < j < len(chars) - 1
                and _is_half_width_digit(chars[j - 1]) and _is_half_width_digit(chars[j + 1]))
    ]
    return "".join(chars[j] for j in keep), [index[j] for j in keep]


def line_number_tokens(seg):
    """本文の1行から、その行の空白で数を切り出す(案3・緩)。戻り値: [(数の文字, 元の行での
    最初の文字の位置, 最後の文字の位置)]。
    ・空白2つ以上は区切り。空白1つで並ぶ1桁の数字の並び(数字が2つ以上)はつなげる(緩)。
    ・ダッシュ類(_DASH_CHARSを「-」にそろえたもの)の直後の数は負。
    ・「▲」「△」の後ろに、空白だけをはさんで(またはすぐに)数字が続けば、その数は負。
      負の数の最初の文字の位置は記号の位置にする(抜き出しが記号を含まなければ、その数は
      抜き出しに丸ごと入らない)。同じ1行の中だけを見るので、行をまたぐ記号は読まない。"""
    s, idx = _map_line_chars(seg, keep_space=True)
    out = []
    out_idx = []
    pos = 0
    for m in _SPACED_DIGITS_RE.finditer(s):
        if sum(ch.isdigit() for ch in m.group()) < 2:
            continue
        out.append(s[pos:m.start()])
        out_idx.extend(idx[pos:m.start()])
        for k in range(m.start(), m.end()):
            if s[k] in " ,":
                continue
            out.append(s[k])
            out_idx.append(idx[k])
        pos = m.end()
    out.append(s[pos:])
    out_idx.extend(idx[pos:])
    chars = list("".join(out))
    for i, ch in enumerate(chars):
        if ch not in _NEGATIVE_MARKS:
            continue
        j = i + 1
        while j < len(chars) and chars[j] == " ":
            j += 1
        if j < len(chars) and _is_half_width_digit(chars[j]):
            # 数字の直前の1文字を「-」にし(記号そのもの、または間の空白の最後の1つ)、記号は空白にする。
            # 「-」の位置は記号の元の位置にそろえる。
            chars[i] = " "
            chars[j - 1] = "-"
            out_idx[j - 1] = out_idx[i]
    s = "".join(chars)
    return [(m.group(), out_idx[m.start()], out_idx[m.end() - 1]) for m in _NUMBER_TOKEN_RE.finditer(s)]


def find_number_in_line(seg, excerpt_norm, value):
    """抜き出しが当たった本文の1行segについて、その行の空白で切り出した数のうち、抜き出しが
    当たった範囲に丸ごと入っている数の中にvalueがあるかを見る(案3・緩)。抜き出しが行の中に
    何か所か当たれば、どれか1か所で見つかれば真。valueが数として読めない(文字など)なら、
    今までどおりfind_number()で抜き出しの中を探す。"""
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return find_number(excerpt_norm, value)
    seg_norm, seg_idx = _map_line_chars(seg, keep_space=False)
    tokens = line_number_tokens(seg)
    start = 0
    while excerpt_norm:
        k = seg_norm.find(excerpt_norm, start)
        if k < 0:
            return False
        first = seg_idx[k]
        last = seg_idx[k + len(excerpt_norm) - 1]
        for token, a, b in tokens:
            if a < first or b > last or _has_leading_zero(token):
                continue
            try:
                if decimal.Decimal(token) == decimal_value:
                    return True
            except decimal.InvalidOperation:
                continue
        start = k + 1
    return False


def check_excerpt_numbers(lines, excerpt_norm, numbers):
    """1行の検査と数字の確認(照合とcheck_excerpts.pyが共通で使う)。
    linesがNone(区切れなかった出典)なら1行の検査はせず、数字は今までどおり
    find_number(抜き出し)で確かめる。
    戻り値: {"fits": 真偽 or None(判定しない), "hit_lines": 当たった行の番号,
             "missing_numbers": 見つからなかったnumbersの要素}。
    ・1行に収まる場合: 当たった行のどれか1行で、numbersがすべて見つかれば合格
      (見つからない数は、いちばん多く見つかった行での残り)。
    ・1行に収まらない場合: 改修29第2回で、数字は確かめない(missing_numbersは空。
      呼び出し側がexcerpt_spans_linesとして印を下げる)。第1回のfind_number()での確認はやめた。"""
    def missing_by_excerpt():
        return [num for num in numbers if not find_number(excerpt_norm, num.get("value"))]

    if lines is None:
        return {"fits": None, "hit_lines": [], "missing_numbers": missing_by_excerpt()}
    hit_lines = find_excerpt_lines(lines, excerpt_norm)
    if not hit_lines:
        return {"fits": False, "hit_lines": [], "missing_numbers": []}
    best = None
    for i in hit_lines:
        missing = [num for num in numbers if not find_number_in_line(lines[i], excerpt_norm, num.get("value"))]
        if best is None or len(missing) < len(best):
            best = missing
        if not missing:
            break
    return {"fits": True, "hit_lines": hit_lines, "missing_numbers": best}


def load_ng_words(path):
    words = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if w:
                words.append(w)
    return words


# 改修27-2 第8回の追加2: AIが書いた値の型(文字・null・配列・辞書)の検査。
# スクリプトが「文字」として使う値(辞書の鍵・集合の要素・文字列の処理に使うもの)に、リスト・辞書・
# 数・真偽値が書かれていると、途中で止まって終了コード2(スクリプトのエラー)になり、その日の号が
# 公開されなかった。紙面は構造の検査で「保存できない」(終了コード1)、上段の仮説は該当する仮説
# だけの削除にする。nullは今までどおり(文字として使う値でも、nullは許す)。
EDITION_STRING_FIELDS = ("edition_id", "date", "slot", "generated_at")
SOURCE_STRING_FIELDS = ("source_id", "publisher", "title", "url", "published_at", "content_sha256")
SECTION_STRING_FIELDS = ("section_id",)
ARTICLE_STRING_FIELDS = ("article_id", "headline")
LINE_STRING_FIELDS = ("line_id", "text", "claimed_mark", "source_ref", "excerpt")
NUMBER_STRING_FIELDS = ("label",)
INFERENCE_STRING_FIELDS = ("text", "falsifier", "check_metric", "check_by")
HYPOTHESIS_STRING_FIELDS = (
    "hypothesis_id", "company_name", "relation_text", "evidence_grade", "evidence_source_ref",
    "ticker", "ticker_source", "baseline_date", "baseline_price_type", "baseline_observed_at",
    "impact_reason", "article_id", "added_by",
)
INDUSTRY_PICK_STRING_FIELDS = ("article_id", "industry", "event_id")


def _is_str_or_null(value):
    return value is None or isinstance(value, str)


def _type_name(value):
    return {"list": "リスト", "dict": "辞書", "int": "数", "float": "数", "bool": "真偽値"}.get(type(value).__name__, type(value).__name__)


def _require_str_or_null(obj, fields, where):
    for field in fields:
        value = obj.get(field)
        if not _is_str_or_null(value):
            raise EditionInvalid(f"{where}の '{field}' が文字でもnullでもありません({_type_name(value)}が書かれています)。")


def validate_edition_field_types(edition):
    """紙面の値の型を確かめる(check_a_structure()から呼ぶ)。文字として使う値が文字でもnullでもない、
    辞書であるべき値(出典・section・記事・行・数字・推論、verification)が辞書でない、配列であるべき値
    (numbers・inferences)が配列でない場合は、EditionInvalid(終了コード1)。
    メッセージには、場所(記事ID・行IDなど、分かる範囲)と項目名を書く。
    sections・sources・articles・linesそのものが配列でない場合は、この関数では何もしない
    (check_a_structure()の、配列かどうかの検査が扱う)。"""
    _require_str_or_null(edition, EDITION_STRING_FIELDS, "紙面")
    for field in ("verification",):
        value = edition.get(field)
        if value is not None and not isinstance(value, dict):
            raise EditionInvalid(f"紙面の '{field}' が辞書でもnullでもありません({_type_name(value)}が書かれています)。")
    verification = edition.get("verification")
    if isinstance(verification, dict):
        first_run = verification.get("first_run")
        if first_run is not None and not isinstance(first_run, dict):
            raise EditionInvalid(f"紙面の 'verification.first_run' が辞書でもnullでもありません({_type_name(first_run)}が書かれています)。")

    sources = edition.get("sources")
    if isinstance(sources, list):
        for i, source in enumerate(sources):
            where = f"出典 sources[{i}]"
            if not isinstance(source, dict):
                raise EditionInvalid(f"{where} が辞書ではありません({_type_name(source)}が書かれています)。")
            if isinstance(source.get("source_id"), str):
                where = f"出典 {source['source_id']}"
            _require_str_or_null(source, SOURCE_STRING_FIELDS, where)

    sections = edition.get("sections")
    if not isinstance(sections, list):
        return
    for si, section in enumerate(sections):
        where = f"section sections[{si}]"
        if not isinstance(section, dict):
            raise EditionInvalid(f"{where} が辞書ではありません({_type_name(section)}が書かれています)。")
        if isinstance(section.get("section_id"), str):
            where = f"section {section['section_id']}"
        _require_str_or_null(section, SECTION_STRING_FIELDS, where)
        articles = section.get("articles")
        if not isinstance(articles, list):
            continue
        for ai, article in enumerate(articles):
            a_where = f"{where}の記事 articles[{ai}]"
            if not isinstance(article, dict):
                raise EditionInvalid(f"{a_where} が辞書ではありません({_type_name(article)}が書かれています)。")
            if isinstance(article.get("article_id"), str):
                a_where = f"記事 {article['article_id']}"
            _require_str_or_null(article, ARTICLE_STRING_FIELDS, a_where)
            lines = article.get("lines")
            if isinstance(lines, list):
                for li, line in enumerate(lines):
                    l_where = f"{a_where}の行 lines[{li}]"
                    if not isinstance(line, dict):
                        raise EditionInvalid(f"{l_where} が辞書ではありません({_type_name(line)}が書かれています)。")
                    if isinstance(line.get("line_id"), str):
                        l_where = f"{a_where}の行 {line['line_id']}"
                    _require_str_or_null(line, LINE_STRING_FIELDS, l_where)
                    numbers = line.get("numbers")
                    if isinstance(numbers, list):
                        for ni, number in enumerate(numbers):
                            n_where = f"{l_where}の数字 numbers[{ni}]"
                            if not isinstance(number, dict):
                                raise EditionInvalid(f"{n_where} が辞書ではありません({_type_name(number)}が書かれています)。")
                            _require_str_or_null(number, NUMBER_STRING_FIELDS, n_where)
            inferences = article.get("inferences")
            if inferences is not None:
                if not isinstance(inferences, list):
                    raise EditionInvalid(f"{a_where}の 'inferences' が配列でもnullでもありません({_type_name(inferences)}が書かれています)。")
                for ii, inference in enumerate(inferences):
                    i_where = f"{a_where}の推論 inferences[{ii}]"
                    if not isinstance(inference, dict):
                        raise EditionInvalid(f"{i_where} が辞書ではありません({_type_name(inference)}が書かれています)。")
                    _require_str_or_null(inference, INFERENCE_STRING_FIELDS, i_where)


def hypothesis_type_problems(hyp):
    """上段の仮説1件の、型の問題のある項目名の一覧(なければ空)。仮説が辞書でなければ["(仮説が辞書でない)"]。
    文字として使う値(HYPOTHESIS_STRING_FIELDS)が文字でもnullでもない、links が辞書でもnullでもない、
    links.price_history が文字でもnullでもない、line_ids が配列でもnullでもない・中身に文字でないものがある、
    の場合にその項目名を返す。nullは問題にしない(今までどおり)。"""
    if not isinstance(hyp, dict):
        return ["(仮説が辞書でない)"]
    problems = [f for f in HYPOTHESIS_STRING_FIELDS if not _is_str_or_null(hyp.get(f))]
    links = hyp.get("links")
    if links is not None and not isinstance(links, dict):
        problems.append("links")
    elif isinstance(links, dict) and not _is_str_or_null(links.get("price_history")):
        problems.append("links.price_history")
    line_ids = hyp.get("line_ids")
    if line_ids is not None and (not isinstance(line_ids, list) or any(not isinstance(l, str) for l in line_ids)):
        problems.append("line_ids")
    return problems


def validate_hypotheses_doc_structure(doc):
    """仮説ファイル全体の型を確かめる(EditionInvalid = 終了コード1)。辞書であること、hypothesesが
    配列(キーが無いのは可)、industry_picksが配列かnull(キーが無いのは可)、edition_id・generated_atが
    文字かnull。"""
    if not isinstance(doc, dict):
        raise EditionInvalid(f"仮説ファイルの一番外側が辞書(オブジェクト)ではありません({_type_name(doc)}が書かれています)。")
    _require_str_or_null(doc, ("edition_id", "generated_at"), "仮説ファイル")
    if "hypotheses" in doc and not isinstance(doc["hypotheses"], list):
        raise EditionInvalid(f"仮説ファイルの 'hypotheses' が配列ではありません({_type_name(doc['hypotheses'])}が書かれています)。")
    if doc.get("industry_picks") is not None and not isinstance(doc["industry_picks"], list):
        raise EditionInvalid(f"仮説ファイルの 'industry_picks' が配列でもnullでもありません({_type_name(doc['industry_picks'])}が書かれています)。")


def remove_type_invalid_hypotheses(doc):
    """型の問題のある上段の仮説だけを削除する(理由 field_type_invalid)。仮説の値を読むどの処理よりも先に呼ぶ。
    戻り値: [{"index": 仮説の位置(0始まり)、"hypothesis_id": 文字ならその値・そうでなければNone、
              "fields": 項目名の一覧}, ...]。"""
    hyps = doc.get("hypotheses")
    if not isinstance(hyps, list):
        return []
    kept, removed = [], []
    for index, hyp in enumerate(hyps):
        problems = hypothesis_type_problems(hyp)
        if problems:
            hid = hyp.get("hypothesis_id") if isinstance(hyp, dict) else None
            removed.append({"index": index, "hypothesis_id": hid if isinstance(hid, str) else None, "fields": problems})
        else:
            kept.append(hyp)
    doc["hypotheses"] = kept
    return removed


def remove_type_invalid_industry_picks(doc):
    """型の問題のある業種の指定(industry_picks)だけを取り除く。辞書でない、article_id・industry・event_idが
    文字でもnullでもない、industry_line_idsが配列でもnullでもない・中身に文字でないものがある、の場合。
    戻り値: [{"index": 位置(0始まり), "fields": 項目名の一覧}, ...]。"""
    picks = doc.get("industry_picks")
    if not isinstance(picks, list):
        return []
    kept, removed = [], []
    for index, pick in enumerate(picks):
        if not isinstance(pick, dict):
            problems = ["(業種の指定が辞書でない)"]
        else:
            problems = [f for f in INDUSTRY_PICK_STRING_FIELDS if not _is_str_or_null(pick.get(f))]
            ids = pick.get("industry_line_ids")
            if ids is not None and (not isinstance(ids, list) or any(not isinstance(l, str) for l in ids)):
                problems.append("industry_line_ids")
        if problems:
            removed.append({"index": index, "fields": problems})
        else:
            kept.append(pick)
    doc["industry_picks"] = kept
    return removed


def check_a_structure(edition):
    if not isinstance(edition, dict):
        raise EditionInvalid(f"紙面JSONの一番外側が辞書(オブジェクト)ではありません({_type_name(edition)}が書かれています)。")
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
    # 改修27-2 第8回の追加2: 値の型の検査(文字として使う値が文字でもnullでもない、など)。
    # 下の行ごとの検査(claimed_markが既知の値かなど)より先に行う(リストが書かれていると、
    # 下の検査で止まってしまうため)。
    validate_edition_field_types(edition)

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


def check_edition_date(edition, run_at_dt):
    """検査24(要件3.5(9)): 号のdateが実行時刻の日付と一致するかを確かめる。
    0:00〜4:59に実行された夕方号(evening)だけは「前日の夕方号」として扱うため、
    実行時刻の前日を期待する。それ以外(それ以外の時刻の夕方号、朝号、昼号)は
    実行時刻の日付をそのまま期待する。判定に使うのは実行時刻(run_at_dt)であり、
    generated_at(AIの自己申告)は使わない。一致しなければ号を保存しない。"""
    slot = edition.get("slot")
    local_dt = run_at_dt.astimezone(JST)
    if slot == "evening" and local_dt.time() < dt.time(5, 0):
        expected_date = (local_dt - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        expected_date = local_dt.strftime("%Y-%m-%d")
    actual_date = edition.get("date")
    if actual_date != expected_date:
        raise EditionInvalid(
            f"date '{actual_date}' が期待した日付 '{expected_date}' と一致しません"
            f"({describe_edition_timing(edition, run_at_dt)})。"
        )


def expected_date_for_run(run_at_dt):
    """改修27-2第2回: 実行時刻(JST)だけから期待する号の日付を返す。0:00〜4:59は前日
    (前日の夕方号)、それ以外は当日。compute_expected_slot()と組にして、エラーの文に
    「期待した日付・時間帯」を書くために使う。"""
    local_dt = run_at_dt.astimezone(JST)
    if local_dt.time() < SLOT_EXPECTED_MORNING_START:
        return (local_dt - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return local_dt.strftime("%Y-%m-%d")


def describe_edition_timing(edition, run_at_dt):
    """検査24のエラーの文に入れる説明(期待した日付・時間帯、号の日付・時間帯、実行時刻)。"""
    return (
        f"期待した日付: {expected_date_for_run(run_at_dt)}, 期待した時間帯: {compute_expected_slot(run_at_dt)}, "
        f"号の日付: {edition.get('date')}, 号の時間帯: {edition.get('slot')}, "
        f"実行時刻: {run_at_dt.astimezone(JST).isoformat()}"
    )


def check_edition_slot(edition, run_at_dt):
    """検査24(改修27-2第2回・S14): 号のslotが、実行時刻(JST)から決まる時間帯
    (compute_expected_slot())と一致しなければ号を保存しない(終了コード1)。
      5:00〜10:59 → morning / 11:00〜15:59 → noon / 16:00〜4:59 → evening
    (0:00〜4:59は前日の夕方号。日付の判定はcheck_edition_date()が行う)。
    判定に使うのは実行時刻だけで、generated_at(AIの自己申告)は使わない。
    戻り値: 期待した時間帯(一致した場合)。"""
    slot_expected = compute_expected_slot(run_at_dt)
    if edition.get("slot") != slot_expected:
        raise EditionInvalid(
            f"slot '{edition.get('slot')}' が期待した時間帯 '{slot_expected}' と一致しません"
            f"({describe_edition_timing(edition, run_at_dt)})。"
        )
    return slot_expected


SLOT_EXPECTED_MORNING_START = dt.time(5, 0)
SLOT_EXPECTED_NOON_START = dt.time(11, 0)
SLOT_EXPECTED_EVENING_START = dt.time(16, 0)


def compute_expected_slot(run_at_dt):
    """改修27-1(決定4): 実行時刻(JST)から期待する時間帯(slot)を返す。
      5:00〜10:59 → morning
      11:00〜15:59 → noon
      16:00〜23:59、0:00〜4:59 → evening
    日付(前日か当日か)はcheck_edition_date()が別に判定するため、ここでは時刻だけから
    時間帯を決める。改修27-2第2回から、ずれていれば号を止める(check_edition_slot())。"""
    local_time = run_at_dt.astimezone(JST).time()
    if SLOT_EXPECTED_MORNING_START <= local_time < SLOT_EXPECTED_NOON_START:
        return "morning"
    if SLOT_EXPECTED_NOON_START <= local_time < SLOT_EXPECTED_EVENING_START:
        return "noon"
    return "evening"


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
        # 改修27-1(4-6): attribution_template・processing_note_templateも、
        # usage/publisher_typeと同じくここで読み、値が空の行は設定ミスとして
        # 号ごと保存を止める(黙ってnullの出典表記を量産しないため)。
        attribution_template = (row.get("attribution_template") or "").strip()
        processing_note_template = (row.get("processing_note_template") or "").strip()
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
        if not attribution_template:
            raise EditionInvalid(
                f"scripts/source_policy.csv のattribution_template (domain={domain}) が空です。"
            )
        if not processing_note_template:
            raise EditionInvalid(
                f"scripts/source_policy.csv のprocessing_note_template (domain={domain}) が空です。"
            )
        policy[domain] = {
            "usage": usage, "publisher_type": publisher_type,
            "attribution_template": attribution_template,
            "processing_note_template": processing_note_template,
        }
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


# 改修27-1(決定3・4-6): source_policy.csvに無いドメインの出典で使う汎用ひな形。
GENERIC_ATTRIBUTION_TEMPLATE = "出典：{publisher}「{title}」（{url}）"
GENERIC_PROCESSING_NOTE_TEMPLATE = "{publisher}「{title}」（{url}）をもとに本サイト作成"


def fill_source_template(template, publisher, title, url):
    """改修27-1(4-6): ひな形(template)に含まれるプレースホルダ({publisher}/{title}/
    {url})のうち、実際にそのひな形が使っているものだけを見て、値を埋める。
    使っているプレースホルダの値が1つでも空(null・空文字・空白のみ)なら、
    'None'や空の「」を含む文を作ってしまわないよう、Noneを返す(生成をやめる)。
    ひな形が使っていないプレースホルダの値は問わない(EDINETのひな形は
    {publisher}/{title}を使わないため、これらが空でも生成してよい)。"""
    values = {"publisher": publisher, "title": title, "url": url}
    for name, value in values.items():
        if "{" + name + "}" not in template:
            continue
        if not value or not str(value).strip():
            return None
    return template.format(**values)


def resolve_source_templates(host, policy):
    """改修27-1(4-6): host(小文字のホスト名)から、attribution/processing_noteの
    ひな形を決める。source_policy.csvに載っていればその値、無ければ決定3の
    汎用ひな形を使う。"""
    entry = policy.get(host) if host else None
    if entry is not None:
        return entry["attribution_template"], entry["processing_note_template"]
    return GENERIC_ATTRIBUTION_TEMPLATE, GENERIC_PROCESSING_NOTE_TEMPLATE


def apply_source_attribution(edition, policy_path):
    """改修27-1(4-6): sources[].attribution/processing_noteと、本文の各行
    (source_refが指す出典の値を使う)のattribution/processing_noteを、AIの自己申告
    ではなくひな形から機械で作る(AIの値は一致・不一致にかかわらず必ず上書きする)。

    検査3(source_number_matchの必須項目)より前に呼ぶこと。そうしないと、AIが
    attribution/processing_noteにnullを置いた行が、上書きされる前に検査3で
    missing_fieldになってしまう。

    改修27-1第6回: EDINETの出典は、{url}の代わりにview_url(apply_edinet_view_url()が
    決めた、読者が実際に開けるURL)を使う。view_urlがまだ書かれていない場合に
    備え、無ければurlに戻す(apply_edinet_view_url()をこの関数より前に呼ぶこと)。

    ひな形に埋める値(publisher・title・url)が足りずattribution・processing_noteの
    どちらかでも作れなかった場合は、両方ともnullのままにする(件数を
    attribution_generation_skippedに記録する。行がどうなるか自体は今までどおり
    検査3・empty_title_or_url_refsに任せる)。

    戻り値: {"attribution_overwritten": 値が変わった出典・行の件数(nullから値に
    した件数も含む。sources・lines合わせた件数),
    "attribution_generation_skipped": ひな形が作れなかった件数}。"""
    policy = load_source_policy(policy_path)
    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", [])}
    counts = {"attribution_overwritten": 0, "attribution_generation_skipped": 0}

    def apply_to(target, source):
        host = source_hostname(source.get("url"))
        attribution_template, processing_note_template = resolve_source_templates(host, policy)
        publisher, title = source.get("publisher"), source.get("title")
        # 改修27-1第6回: EDINETの出典は、attribution/processing_noteの{url}に
        # 読者が開けるview_url(apply_edinet_view_url()が決めた値)を使う。
        # view_urlが無い出典(EDINET以外・対象外のEDINETのURL)は、今までどおり
        # urlをそのまま使う。
        url = source.get("view_url") or source.get("url")
        new_attribution = fill_source_template(attribution_template, publisher, title, url)
        new_processing_note = fill_source_template(processing_note_template, publisher, title, url)
        if new_attribution is None or new_processing_note is None:
            counts["attribution_generation_skipped"] += 1
        if target.get("attribution") != new_attribution or target.get("processing_note") != new_processing_note:
            counts["attribution_overwritten"] += 1
        target["attribution"] = new_attribution
        target["processing_note"] = new_processing_note

    for source in edition.get("sources", []):
        apply_to(source, source)

    for _section, _article, line in iter_lines(edition):
        ref = line.get("source_ref")
        if not ref:
            continue
        source = sources_by_id.get(ref)
        if source is None:
            continue
        apply_to(line, source)

    return counts


def verify_excerpt_against_source(source, source_ref, excerpt, numbers, cache_dir, line_check=None):
    """出典の本文と抜き出し・数字を照合する部分(verify_line()と、改修29第1回の
    check_excerpts.pyが共通で使う)。ハッシュ確認 → 本文が読めるか → excerpt_not_found →
    1行の検査(改修29。第2回から、どの1行にも収まらなければexcerpt_spans_linesで印を下げる) →
    数字の確認、の順に見る。
    line_checkに辞書を渡すと、1行の検査の結果を書き込む:
      "fits": 真(どれか1行に収まる)・偽(収まらない)・None(区切れず判定しない)
      "skipped_reason": 区切れなかった理由(fitsがNoneのとき)
      "lines": 区切った行のリスト、"hit_lines": 当たった行の番号
    戻り値: verify_line()と同じ(印, 理由, 見つからなかった数字)。"""
    cache_path = Path(cache_dir) / f"{source_ref}.txt"
    if not cache_path.is_file():
        return "unverified", "source_unfetchable", None

    raw_bytes = cache_path.read_bytes()
    actual_hash = hashlib.sha256(raw_bytes).hexdigest()
    expected_hash = source.get("content_sha256")
    if not expected_hash:
        return "unverified", "hash_missing", None
    if actual_hash != expected_hash:
        return "unverified", "hash_mismatch", None

    # 改修27-1(4-9): EDINETの出典はHTMLタグを取り除いた本文で照合する
    # (キャッシュの元ファイル・ハッシュの確認は上のraw_bytesのまま変えない)。
    body_text = read_source_body_for_checks(cache_path, source)
    if body_text is None:
        return "unverified", "source_unreadable", None
    body_norm = normalize_text(body_text)
    excerpt_norm = normalize_text(excerpt)
    if excerpt_norm not in body_norm:
        return "unverified", "excerpt_not_found", None

    # 改修29: 抜き出しが本文のどれか1行に丸ごと入るかを調べる。第2回から、どの1行にも
    # 収まらない(2行以上をつないでいた)行は印を下げる(excerpt_spans_lines)。区切れない出典は
    # 1行の判定をせず、今までどおりの照合だけにする(印は下げず、記録だけ)。
    lines, skipped_reason = split_source_lines(cache_path, source, body_text)
    result = check_excerpt_numbers(lines, excerpt_norm, numbers)
    if line_check is not None:
        line_check["fits"] = result["fits"]
        line_check["skipped_reason"] = skipped_reason
        line_check["lines"] = lines
        line_check["hit_lines"] = result["hit_lines"]
    if result["fits"] is False:
        return "unverified", "excerpt_spans_lines", None
    missing_numbers = result["missing_numbers"]
    if missing_numbers:
        return "unverified", "number_not_in_excerpt", missing_numbers

    return "source_number_match", None, None


def verify_line(line, sources_by_id, cache_dir, line_check=None):
    claimed = line["claimed_mark"]
    numbers = line.get("numbers", [])
    source_ref = line.get("source_ref")
    excerpt = line.get("excerpt")

    # 改修28第1回: 「報道で見た・未確認」と申告した行でも、出典(source_ref)が空(null・空文字・
    # 空白のみ・文字でない値)なら、読者はどの報道かを確かめられないため印をunverifiedにする。
    # 検査33(存在しないIDの出典)より先に見る(文字でない値を「存在しないID」と区別するため)。
    if claimed == "reported_unverified" and _is_blank(source_ref):
        return "unverified", "reported_without_source", None

    # 検査33: source_refが空でないのに、sources一覧にそのIDが見つからない場合は
    # 不合格にする。claimed_markの種類を問わず、本文の行すべてが対象(source_number_match
    # に限らない)。source_refがnull・空の行は対象外(存在しないIDを指しているわけ
    # ではないため)。この判定を最初に行うことで、後続の検査(16など)に届く時点では
    # source_refが真であれば必ずsources一覧に見つかることが保証される。
    if source_ref and source_ref not in sources_by_id:
        return "unverified", "source_ref_not_found", None

    # 検査16: 本文を取得していない出典(usageがquotable以外)からのexcerptは認めない。
    # claimed_markの種類を問わず、行にsource_refとexcerptの両方があれば対象になる。
    # usageが記録されていない・null・空文字・想定外の値の出典も「quotableではない」ものと
    # して扱う(usageの書き忘れが、抜き出しを通す抜け道にならないようにするため)。
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

        # 検査33により、ここに到達した時点でsource_refは必ずsources一覧に見つかっている
        # (source is Noneにはならない)。source_unfetchableは「sourcesには載っているが、
        # キャッシュに本文のファイルが無い」という意味だけになった。
        source = sources_by_id[source_ref]
        return verify_excerpt_against_source(source, source_ref, excerpt, numbers, cache_dir, line_check)

    if claimed == "reported_unverified":
        # 改修31第1回(3-3): 「報道で見た・未確認」の印は、出典が検索結果の断片(fetch_methodが
        # websearch_snippet、かつusageがsnippet_only)のときだけ付ける。それ以外の出典なら
        # unverified(reported_source_not_snippet)。検査33(出典IDの実在)・検査16(抜き出しの禁止)の後。
        if not is_search_snippet_source(sources_by_id.get(source_ref)):
            return "unverified", "reported_source_not_snippet", None
        return "reported_unverified", None, None

    if claimed == "explainer":
        if not numbers:
            return "explainer", None, None
        return "unverified", "mark_mismatch", None

    return "unverified", None, None


def _is_blank(value):
    """文字列でない・空・空白のみ(全角空白を含む)なら真。"""
    return not isinstance(value, str) or not value.strip()


SEARCH_SNIPPET_FETCH_METHOD = "websearch_snippet"
SEARCH_SNIPPET_USAGE = "snippet_only"


def is_search_snippet_source(source):
    """改修31第1回(3-3): 出典が検索結果の断片かどうか。fetch_method(AIが書く値)が
    websearch_snippet、かつusage(apply_source_policy()が表の値で上書きした後の値)が
    snippet_onlyのときだけ真。両方を条件にするのは、fetch_methodだけだとAIが書けば通り、
    usageだけだと表に無いドメイン(本文を取得していてもsnippet_onlyになる)を通すため。"""
    if not isinstance(source, dict):
        return False
    return (source.get("fetch_method") == SEARCH_SNIPPET_FETCH_METHOD
            and source.get("usage") == SEARCH_SNIPPET_USAGE)


# 改修31第1回(3-4): 出典の本文ファイルの確認(run_check_source_body)で、行の印を下げる食い違い。
# reconvert・reconvert_failed(文字にし直した結果の食い違い)とreconvert_skippedは記録だけ
# (道具の版の違いで印が下がらないようにするため)。
SOURCE_BODY_HARD_MISMATCHES = ("meta_unreadable", "url", "content_sha256", "raw_missing", "raw_sha256")


def compute_source_body_downgrades(source_body_check):
    """改修31第1回(3-4): run_check_source_body()の結果から、行の印を下げる出典を決める。
    戻り値: {source_id: 理由}。理由は、記録ファイルが無い出典がsource_body_not_machine_saved、
    reconvert_mismatchのうちSOURCE_BODY_HARD_MISMATCHESのどれかが食い違った出典が
    source_body_mismatch。"""
    downgrades = {}
    for source_id in source_body_check["not_machine_saved"]["source_ids"]:
        downgrades[source_id] = "source_body_not_machine_saved"
    for detail in source_body_check["reconvert_mismatch"].get("details", []):
        if set(detail.get("mismatched") or []) & set(SOURCE_BODY_HARD_MISMATCHES):
            downgrades[detail["source_id"]] = "source_body_mismatch"
    return downgrades


def find_empty_title_or_url_sources(edition):
    """改修27-2(S12): titleかurlが空(null・空文字・空白のみ)の出典のsource_idの集合。
    読者が「何の資料か」「どこで開けるか」を確かめられない出典のため、この出典を
    参照する行は確定した印をunverifiedにする(run_line_verification()が使う)。"""
    return {
        s.get("source_id") for s in edition.get("sources", [])
        if _is_blank(s.get("title")) or _is_blank(s.get("url"))
    }


def run_line_verification(edition, cache_dir, body_downgrades=None):
    """body_downgrades: 改修31第1回(3-4)。{source_id: 理由}(compute_source_body_downgrades()の結果)。
    この出典を参照し、claimed_markがsource_number_matchの行は、確定した印をunverifiedにする
    (題名かURLが空の出典(empty_title_or_url)の行は、そちらを優先する)。"""
    body_downgrades = body_downgrades or {}
    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", [])}
    empty_sources = find_empty_title_or_url_sources(edition)
    stats = {
        "lines_total": 0,
        "passed": 0,
        "unverified": 0,
        "reported_unverified": 0,
        "explainer": 0,
        "unverified_reasons": {},
        # 改修27-2(S12): titleかurlが空の出典を参照していた行のline_id(記録専用のキー
        # empty_title_or_url_refsの元)。
        "empty_title_or_url_line_ids": [],
        # 改修28第1回: 出典の無い「報道で見た・未確認」の行のline_id(記録専用のキー
        # reported_without_sourceの元)。
        "reported_without_source_line_ids": [],
        # 改修29: 抜き出しが本文のどの1行にも収まらなかった行のline_id(キーexcerpt_spans_linesの
        # 元。第2回からこの行の印はunverified)と、1行に区切れず1行の判定をしなかった出典の
        # source_id(記録専用のキーexcerpt_line_check_skippedの元。印は下げない)。
        "excerpt_spans_lines_line_ids": [],
        "excerpt_line_check_skipped_source_ids": [],
        # 改修31第1回(3-3): 出典が検索結果の断片でない「報道で見た・未確認」の行のline_id。
        "reported_source_not_snippet_line_ids": [],
        # 改修31第1回(3-4): 本文ファイルの確認で印を下げた行(line_idと理由)。
        "source_body_downgraded": [],
    }
    number_failure_details = []

    for section, article, line in iter_lines(edition):
        stats["lines_total"] += 1
        line_check = {}
        mark, reason, missing_numbers = verify_line(line, sources_by_id, cache_dir, line_check)
        if line_check.get("fits") is False:
            stats["excerpt_spans_lines_line_ids"].append(line.get("line_id"))
        elif "fits" in line_check and line_check["fits"] is None:
            skipped_ids = stats["excerpt_line_check_skipped_source_ids"]
            if line.get("source_ref") not in skipped_ids:
                skipped_ids.append(line.get("source_ref"))
        # 改修27-2(S12): titleかurlが空の出典を参照する行は、claimed_markの種類を問わず
        # 確定した印をunverifiedにする(他の理由で既にunverifiedでも、理由はこちらに
        # 揃える)。verify_line()の中の副作用(検査16のexcerpt削除など)は残る。
        ref = line.get("source_ref")
        if ref and ref in empty_sources:
            mark, reason, missing_numbers = "unverified", "empty_title_or_url", None
            stats["empty_title_or_url_line_ids"].append(line.get("line_id"))
        elif (isinstance(ref, str) and ref in body_downgrades
              and line.get("claimed_mark") == "source_number_match"):
            # 改修31第1回(3-4): 機械で保存されたと確かめられない本文の出典を参照する行は、
            # 抜き出しが本文にあっても印をunverifiedにする(他の理由で既にunverifiedでも、理由はこちらに揃える)。
            mark, reason, missing_numbers = "unverified", body_downgrades[ref], None
            stats["source_body_downgraded"].append({"line_id": line.get("line_id"), "reason": reason})
        if reason == "reported_without_source":
            stats["reported_without_source_line_ids"].append(line.get("line_id"))
        if reason == "reported_source_not_snippet":
            stats["reported_source_not_snippet_line_ids"].append(line.get("line_id"))
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


# 改修27-2(S13): 根拠がreported(検索結果の断片に社名があっただけ)の上段の会社の
# relation_textの定型文。定数はここ1か所に置く。
REPORTED_RELATION_TEXT = "検索結果の断片に社名あり（本文は未確認）"


def compute_reported_relation_text_mismatch(hyps):
    """改修27-2(S13): evidence_gradeがreportedの上段の会社のrelation_textが、定型文
    (REPORTED_RELATION_TEXT)と完全一致しない仮説を数える(記録専用。会社は消さない)。
    仮説が検査で消される前の全件を対象にし、evidence_gradeはAIが書いた値で見る。
    戻り値: {"count": 件数, "hypothesis_ids": [...]}。"""
    ids = [
        h.get("hypothesis_id") for h in hyps
        if h.get("evidence_grade") == "reported"
        and h.get("relation_text") != REPORTED_RELATION_TEXT
    ]
    return {"count": len(ids), "hypothesis_ids": ids}


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


# 改修27-2第6回(S7): 推論欄の各推論の4項目。空なら削除する(run_check_d_inferences)のと、
# 上場会社の名前が入っていたら削除する(run_check_inference_company_names)の両方の対象。
INFERENCE_FIELDS = ("text", "falsifier", "check_metric", "check_by")


def build_listed_company_matcher(codelist_rows):
    """改修27-2第6回(S7): 推論欄の会社名の検査で使う、上場会社の照合の道具を作る。
    コードリストの「上場かつ証券コードあり」の会社を対象に、下段の選定と同じ規則
    (pick_industry_companies の照合名・aliases.csv・一般語辞書。探すときは最長一致と直後の文字の
    確認)で、照合名の一覧を作る。関数は写さずpick_industry_companiesのものを呼ぶ。
    下段の選定にある「他の会社の名前の一部になっている照合名は使わない」規則(旧規則4、
    _filter_usable_names)は、ここでは使わない。使うと、兼松のように、グループ会社の
    名前(兼松エンジニアリング)の一部になっている親会社の名前まで外れ、推論欄に本当に
    書かれた会社名を見逃すため。長い社名の中の一部に当たってしまうことは、最長一致
    (長い名前を先に見つけて、その範囲を使う)で防ぐ。
    コードリストが読めない(None)ならNoneを返す(この検査を行わない)。
    戻り値: {"entries": 照合名の一覧, "names_by_code": {edinet_code: 提出者名}}。"""
    if codelist_rows is None:
        return None
    candidates = []
    for row in codelist_rows:
        if row.get(edinet_codelist.COL_LISTED) != edinet_codelist.LISTED_VALUE:
            continue
        if not (row.get(edinet_codelist.COL_TICKER_RAW) or "").strip():
            continue
        candidates.append({
            "company_name": row.get(edinet_codelist.COL_FILER_NAME),
            "edinet_code": row.get(edinet_codelist.COL_EDINET_CODE),
        })
    entries = pick_industry_companies._build_entries(
        candidates, pick_industry_companies.load_aliases(),
        generic_words=pick_industry_companies.load_generic_words(),
    )
    return {
        "entries": entries,
        "names_by_code": {c["edinet_code"]: c["company_name"] for c in candidates},
    }


# 改修27-2第6回の追加: 推論欄の会社名の検査だけ、照合名の直後の判定を広げる(下段の選定は変えない)。
# 直後が漢字のとき当てるのは、照合名(正規化後)がこの文字数以上のときだけ(2文字の名前は
# 「電算機類」「モデルベース開発」のように普通の言葉の一部になりやすいため)。
INFERENCE_KANJI_FOLLOW_MIN_LEN = 3
_CORPORATE_DESIGNATOR_FORMS = tuple(sorted(
    set(edinet_codelist.CORPORATE_DESIGNATORS)
    | {unicodedata.normalize("NFKC", d) for d in edinet_codelist.CORPORATE_DESIGNATORS},
    key=lambda d: -len(d),
))


def _is_kanji(ch):
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF


def _skip_normalized_away_chars(tail):
    """照合用の正規化で消える文字(長音「ー」「ｰ」・ハイフン・空白(全角を含む)・結合文字・半角の濁点)を、
    先頭から読み飛ばした残りを返す。元の文で、照合名の直後の法人格・「グループ」を見るときに、
    正規化で消えた文字が間に挟まって見逃さないため(「カバー株式会社」「兼松　株式会社」)。
    「・」は読み飛ばさない(規則1そのものなので、あとで判定に使う)。"""
    i = 0
    while i < len(tail):
        ch = tail[i]
        folded = unicodedata.normalize("NFKC", ch)
        if ch.isspace() or folded.isspace() or unicodedata.combining(ch) or ch in "\uff9e\uff9f" or folded in ("ー", "-"):
            i += 1
        else:
            break
    return tail[i:]


def make_inference_accept_end(text, norm, positions):
    """推論欄の会社名の検査で、pick_industry_companies._find_mentions()に渡す判定を作る
    (照合名の直後の判定。text=元の文、norm・positions=正規化した文と元の位置の対応)。
    次のどれかなら、その出現を会社名として当てる。
      ・今までの規則で当たるもの(直後がひらがな・記号・文の終わりなど、語の続きでない)
      ・規則1: 元の文で、直後(正規化で消える長音・ハイフン・空白を読み飛ばしたあと)が
        法人格の表記(株式会社・(株)など)か「・」
        (「兼松株式会社及び」「トヨタ自動車・清水建設」。正規化で法人格と「・」が消えて
        区切りが見えなくなるため、元の文で見る)
      ・規則3: 直後が「グループ」(正規化で長音が消えて「グルプ」になるため、元の文で見る)
      ・規則2: 直後が漢字で、照合名が3文字以上(INFERENCE_KANJI_FOLLOW_MIN_LEN)のとき
    直後がカタカナ・英数字のものは、規則1・3に当たらない限り当てない。
    positionsがNone(位置の対応が作れなかった)なら、今までの規則だけで判定する。"""
    def accept(start, end, entry):
        next_ch = norm[end] if end < len(norm) else None
        if not pick_industry_companies._is_word_forming(next_ch):
            return True
        if positions is None:
            return False
        tail = _skip_normalized_away_chars(text[positions[end - 1] + 1:])
        tail_nfkc = unicodedata.normalize("NFKC", tail)
        if tail.startswith(("・", "･")) or any(
            tail.startswith(d) or tail_nfkc.startswith(d) for d in _CORPORATE_DESIGNATOR_FORMS
        ):
            return True
        if tail_nfkc.startswith("グループ"):
            return True
        if _is_kanji(next_ch) and len(entry[0]) >= INFERENCE_KANJI_FOLLOW_MIN_LEN:
            return True
        return False
    return accept


def find_listed_company_mentions(text, matcher):
    """textの中に出ている上場会社を、下段の選定と同じ規則(照合名・別名・一般語辞書・最長一致)で
    探す。照合名の直後の判定だけは、下段より広い(make_inference_accept_end())。
    照合名の直前の判定(改修28第1回。カタカナ語の途中の照合名を当てない)は下段と同じ
    (pick_industry_companies.make_accept_start())。
    戻り値: [{"company_name": 提出者名, "matched_word": 当たった照合名, "alias": 別名で当たった
    場合の別名(なければNone)}, ...]。textが文字列でない・空なら空のリスト。"""
    if matcher is None or not isinstance(text, str) or not text:
        return []
    blob, positions = pick_industry_companies._normalize_match_name_with_positions(text)
    found = pick_industry_companies._find_mentions(
        blob, matcher["entries"], accept_end=make_inference_accept_end(text, blob, positions),
        accept_start=pick_industry_companies.make_accept_start(text, positions),
    )
    return [
        {"company_name": matcher["names_by_code"].get(key), "matched_word": entry[0], "alias": entry[2]}
        for key, entry in found.items()
    ]


def run_check_inference_company_names(edition, matcher):
    """検査18の追加(改修27-2第6回・S7): 推論欄の各推論の4項目(text・falsifier・check_metric・
    check_by)のどれかに上場会社の名前が含まれていたら、その推論1件を削除する
    (同じ記事の他の推論は残す)。会社名の探し方はfind_listed_company_mentions()
    (下段の選定と同じ規則で、照合名の直後の判定だけが広い)。
    4項目が空なら削除する今の検査(run_check_d_inferences)とは別で、そちらは残してある
    (推論欄のfalsifierは必須のまま)。matcherがNone(コードリストが読めない日)なら何もしない。

    戻り値: {"count": 削除した推論の数,
             "removed": [{"article_id": 記事ID,
                          "hits": [{"field": 項目名, "company_name": 社名, "matched_word": 当たった照合名,
                                    "alias": 別名で当たった場合の別名 or None}, ...]}, ...]}。"""
    removed = []
    if matcher is None:
        return {"count": 0, "removed": removed}
    for section in edition.get("sections", []):
        for article in section.get("articles", []):
            inferences = article.get("inferences")
            if not isinstance(inferences, list):
                continue
            kept = []
            for inf in inferences:
                hits = []
                if isinstance(inf, dict):
                    for field in INFERENCE_FIELDS:
                        for mention in find_listed_company_mentions(inf.get(field), matcher):
                            hits.append({"field": field, **mention})
                if hits:
                    removed.append({"article_id": article.get("article_id"), "hits": hits})
                else:
                    kept.append(inf)
            article["inferences"] = kept
    return {"count": len(removed), "removed": removed}


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


def run_check_e_stale_sources(edition, run_at_dt):
    """検査10: change欄(新しい変化)の行について、出典が「新しい」かを、出典の種類で
    分けて確かめる。change以外の欄(big/ripple/deep)は対象にしない(鮮度の条件は
    change欄だけのため)。

      ・時刻付きの出典(published_date_onlyが偽。EDINETの個々の書類を含む):
        実行時刻(run_at_dt)から36時間を超えて古ければ落とす(今までどおり)。基準は
        AIの自己申告(generated_at)ではなく実行時刻。境目は「実行時刻を分単位に切り捨てて、
        36時間を超えたら古い」(36時間ちょうどは新しい側に残す。EDINETのsubmitDateTimeが
        分単位のため、実行時刻の秒で境目の判定が変わらないようにする。改修27-1・4-2)。
      ・日付だけの出典(published_date_onlyが真。書類一覧の2つを含む。改修27-2第3回・S4):
        号の日付(edition["date"])と同じ日か、その前日(暦日。営業日ではない)なら新しい。
        それより前(前々日以前)も、号の日付より後(未来の日付)も、落とす。基準は
        実行時刻の日付ではなく号の日付(0:00〜4:59に作る夕方号は号の日付が前日のため)。
        号の日付が読めない場合は、判定できないので落とす(通常はcheck_market_open()が
        先に号ごと止めるため到達しない)。
      ・published_atがnull・読み取れない出典: 新しくないとして落とす。

    落とした行は、時刻付き・日付だけのどちらも、stale(古い行の合計)に数える。
    合計の内訳はstale_by_kind({"timed": n, "date_only": n})で返す(stale == timed + date_only)。
    published_atが読み取れず落とした行は、原因が違うためunknown_published_atに別に数える。

    戻り値: (stale, unknown_published_at, stale_by_kind)。"""
    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", [])}
    run_at_dt_minute = run_at_dt.replace(second=0, microsecond=0)

    edition_date = None
    try:
        edition_date = dt.date.fromisoformat(edition.get("date"))
    except (TypeError, ValueError):
        pass
    earliest_fresh_date = edition_date - dt.timedelta(days=1) if edition_date else None

    stale_by_kind = {"timed": 0, "date_only": 0}
    unknown_published_at = 0
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
                published_at = source.get("published_at")
                if source.get("published_date_only"):
                    # 日付だけの出典。時刻を持たないので、日付そのもので比べる。
                    if not is_date_only_string(published_at):
                        unknown_published_at += 1
                        continue
                    if edition_date is None or not (
                        earliest_fresh_date <= dt.date.fromisoformat(published_at) <= edition_date
                    ):
                        stale_by_kind["date_only"] += 1
                        continue
                    kept_lines.append(line)
                    continue
                published_dt = parse_datetime_assume_jst(published_at)
                if published_dt is None:
                    unknown_published_at += 1
                    continue
                delta_hours = (run_at_dt_minute - published_dt).total_seconds() / 3600
                if delta_hours > 36:
                    stale_by_kind["timed"] += 1
                    continue
                kept_lines.append(line)
            article["lines"] = kept_lines
    stale = stale_by_kind["timed"] + stale_by_kind["date_only"]
    return stale, unknown_published_at, stale_by_kind


def _reiwa_year(seireki_year):
    """西暦を令和の年数に直す(令和1年=2019年)。"""
    return seireki_year - 2018


_ENGLISH_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


def _english_month_forms(month):
    """英語の日付に使う月の書き方の一覧。正式名(September)と、3文字の略記(Sep.・Sep)。
    9月だけは4文字の略記(Sept.・Sept)も加える。"""
    full = _ENGLISH_MONTH_NAMES[month - 1]
    abbr = full[:3]
    forms = [full, abbr + ".", abbr]
    if month == 9:
        forms += ["Sept.", "Sept"]
    return list(dict.fromkeys(forms))  # 順番を保ったまま重複を除く(Mayなど)


def published_at_candidates(year, month, day):
    """検査36で本文を探す、発表日の書き方の候補。
    日本語・数字の6通り(要件定義書v12 13章)と、改修27-2第4回で足した英語
    (September 18, 2026 / Sept. 18, 2026 / Sep. 18, 2026 / Sep 18, 2026)。
    月日にゼロ埋めが要る書き方(2件)以外は、ゼロ埋めしない元の月日をそのまま使う
    (英語の日も September 1, 2026 のようにゼロ埋めしない。ただし改修30で、日が1〜9のときだけ
    September 01, 2026 のようにゼロ埋めした形も足した)。大文字・小文字の違いは
    date_found_in_text()が同じとみなす。"""
    forms = [
        f"{year:04d}-{month:02d}-{day:02d}",
        f"{year:04d}/{month:02d}/{day:02d}",
        f"{year:04d}/{month}/{day}",
        f"{year:04d}年{month}月{day}日",
        f"令和{_reiwa_year(year)}年{month}月{day}日",
        f"{month}月{day}日",
    ]
    forms += [f"{name} {day}, {year:04d}" for name in _english_month_forms(month)]
    if day < 10:
        # 改修30: 日が1〜9のときは、日を2桁にした書き方(October 07, 2026)も足す。
        # FRBの議事要旨は「Last Update: October 07, 2026」とゼロ埋めで書いてある。
        forms += [f"{name} {day:02d}, {year:04d}" for name in _english_month_forms(month)]
    return forms


def _normalize_for_date_search(text):
    """日付を探すための正規化。normalize_text()と同じく、NFKC(全角数字・全角英字を半角に)・
    ダッシュ類の統一・空白の除去を行うが、次の2点が違う。
      ・数字の間のカンマを取り除かない(英語の日付 'September 18, 2026, 3:00 p.m.' の
        年の後ろのカンマが消えて、時刻の数字とくっつくのを防ぐため)
      ・英字を小文字にそろえる(September と september を同じとみなす)"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    for ch in _DASH_CHARS:
        text = text.replace(ch, "-")
    return _WS_RE.sub("", text).lower()


def date_found_in_text(text, candidates):
    """本文(text、未加工)の中に、候補(published_at_candidates()の書き方)のどれかが
    日付として現れるかを返す。

    改修27-2第4回: ゼロ埋めしない書き方(2026/9/1・9月1日・September 1, 2026)が、
    別の日の書き方(2026/9/12・12月1日・September 12, 2026など)の一部に一致して
    しまわないよう、次の規則で一致を判定する。
      ・候補が数字で始まるとき: 見つかった箇所の直前の文字が数字なら一致としない
        (例: 「12026/9/1」の中の「2026/9/1」、「12月1日」の中の「2月1日」)
      ・候補が数字で終わるとき: 見つかった箇所の直後の文字が数字なら一致としない
        (例: 「2026/9/12」の中の「2026/9/1」)
      ・候補が「日」や「,2026」ではなく漢字で終わるとき(「9月18日」など)は、直後に
        数字が続いてよい(「9月18日15時」の「日」は日付の終わりを示しているため)。
    これは、依頼の「直後の文字が数字なら一致としない」を、日付の終わりが数字でない
    書き方まで機械的に当てはめると「9月18日15時30分」まで落としてしまうため、
    候補の端が数字のときだけに限ったもの。"""
    haystack = _normalize_for_date_search(text)
    for candidate in candidates:
        needle = _normalize_for_date_search(candidate)
        if not needle:
            continue
        check_before = _is_half_width_digit(needle[0])
        check_after = _is_half_width_digit(needle[-1])
        start = 0
        while True:
            idx = haystack.find(needle, start)
            if idx == -1:
                break
            end = idx + len(needle)
            before = haystack[idx - 1] if idx > 0 else ""
            after = haystack[end] if end < len(haystack) else ""
            if not ((check_before and _is_half_width_digit(before)) or (check_after and _is_half_width_digit(after))):
                return True
            start = idx + 1
    return False


def _count_lines_by_source(edition):
    counts = {}
    for _section, _article, line in iter_lines(edition):
        ref = line.get("source_ref")
        if ref:
            counts[ref] = counts.get(ref, 0) + 1
    return counts


def run_check_published_at(edition, cache_dir):
    """検査36(要件定義書v12 5.6・13章)の、時刻付きの出典の分: 出典のpublished_at(発表日)が
    本物かどうかを、出典本文にその日付の書き方(published_at_candidates())のどれかが
    含まれているかで確かめる。この関数は記録するだけで、行は一切落とさない
    (markは変更しない)。日付だけの出典の分は、行を落とす
    run_check_published_date_only_required()が別に行う。時刻付きの出典(EDINET以外)でchangeの行を
    落とす判定は、改修31第1回からrun_check_published_timed_date_required()が別に行う(候補に前日を足す)。

    対象は、published_atが空でなく、かつキャッシュに本文のファイルがあって読める
    時刻付きの出典だけ。published_atがnull、キャッシュが無い・読めない出典は対象外(件数にも
    入れない)。判定は出典ごとに1回だけ行う(同じ出典を参照する行が複数あっても、
    本文の読み込みと照合は1回)。

    published_date_onlyが真の出典(日付だけ)は、書類一覧の2つも含めて、この関数の
    対象から外す(件数にも入れない)。書類一覧はpublished_atが一覧の取得条件から来る
    日付だけで本文の日付表記と比べる意味が無く、それ以外の日付だけの出典は
    run_check_published_date_only_required()が扱う(改修27-2第4回)。

    戻り値: (確認できなかった出典を参照する本文の行の数, 確認できなかった出典IDの一覧)。"""
    line_counts_by_source = _count_lines_by_source(edition)

    unverified_hits = 0
    unverified_sources = []
    for source in edition.get("sources", []):
        if source.get("published_date_only"):
            continue
        source_id = source.get("source_id")
        published_dt = parse_datetime_assume_jst(source.get("published_at"))
        if published_dt is None:
            continue
        cache_path = Path(cache_dir) / f"{source_id}.txt"
        if not cache_path.is_file():
            continue
        # 改修27-1(4-9): EDINETの出典はHTMLタグを取り除いた本文で照合する。
        body_text = read_source_body_for_checks(cache_path, source)
        if body_text is None:
            continue

        # 改修28第1回: 探す日付は、①日本時間に直した日付と、②published_atに書かれた時差の
        # ままの日付(海外の発表元は本文に現地の日付を書くため。FRBの2026-09-16T14:00:00-04:00は、
        # 日本時間では9月17日だが本文には September 16, 2026 と書かれる)の2つ。どちらかが
        # 見つかれば確認できたとする。時差が書かれていない値は日本時間とみなす(①と②が同じ日付に
        # なるので、候補は1つ)。
        local_date = published_dt.astimezone(JST).date()
        own_date = published_dt.date()
        candidates = published_at_candidates(local_date.year, local_date.month, local_date.day)
        if own_date != local_date:
            candidates += published_at_candidates(own_date.year, own_date.month, own_date.day)
        if not date_found_in_text(body_text, candidates):
            unverified_hits += line_counts_by_source.get(source_id, 0)
            unverified_sources.append(source_id)

    return unverified_hits, unverified_sources


SOURCE_BODY_CHECK_KINDS = ("machine_saved", "not_machine_saved", "reconvert_mismatch", "reconvert_skipped")


def _check_one_source_body(source, cache_dir):
    """run_check_source_body()の1出典ぶん。戻り値: (分類, 食い違いの詳細 or None)。"""
    # 改修28第2回: save_source.pyはこのファイルを読み込むため、ここで(呼ばれたときに)読み込む
    # (ファイルの先頭で読み込むと、読み込みが循環する)。
    import save_source

    source_id = source.get("source_id")
    cache_dir = Path(cache_dir)
    meta_path = cache_dir / f"{source_id}.meta.json"
    if not meta_path.is_file():
        return "not_machine_saved", None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        meta = None
    if not isinstance(meta, dict) or meta.get("kind") not in ("pdf", "html"):
        return "reconvert_mismatch", {"source_id": source_id, "mismatched": ["meta_unreadable"]}

    mismatched = []
    if meta.get("url") != source.get("url"):
        mismatched.append("url")
    text_sha256 = hashlib.sha256((cache_dir / f"{source_id}.txt").read_bytes()).hexdigest()
    if meta.get("content_sha256") != text_sha256:
        mismatched.append("content_sha256")
    # 元のファイルの場所は、記録ファイルに書かれた値ではなく、IDと種類から決める。
    raw_path = cache_dir / "raw" / f"{source_id}.{meta['kind']}"
    raw = raw_path.read_bytes() if raw_path.is_file() else None
    if raw is None:
        mismatched.append("raw_missing")
    elif hashlib.sha256(raw).hexdigest() != meta.get("raw_sha256"):
        mismatched.append("raw_sha256")
    detail = {"source_id": source_id, "mismatched": mismatched}
    if mismatched:
        return "reconvert_mismatch", detail

    if meta["kind"] == "pdf" and not save_source.find_tool(save_source.PDFTOTEXT):
        return "reconvert_skipped", None
    try:
        converted = save_source.convert(meta["kind"], raw, meta.get("content_type"))
    except save_source.SaveSourceError:
        mismatched.append("reconvert_failed")
        return "reconvert_mismatch", detail
    if hashlib.sha256(converted["text"].encode("utf-8")).hexdigest() != text_sha256:
        mismatched.append("reconvert")
        detail["tool_version_saved"] = meta.get("tool_version")
        detail["tool_version_now"] = converted["tool_version"]
        return "reconvert_mismatch", detail
    return "machine_saved", None


def run_check_source_body(edition, cache_dir):
    """改修28第2回: 出典の本文ファイルが、機械(save_source.py)で保存されたものかを確かめて記録する
    (この関数自体は記録だけ。改修31第1回(3-4)から、呼び出し側がこの結果をcompute_source_body_downgrades()に
    渡し、行の印を下げる)。対象は、EDINET以外でusageがquotableの出典のうち、本文ファイル
    ({source_id}.txt)があるもの。usageはapply_source_policy()が表の値で上書きした後の値を見る。
    次の4つに分ける。
      ・machine_saved: 記録ファイル({id}.meta.json)があり、URL・本文のハッシュ・元のファイルのハッシュが
        一致し、元のファイルをもう一度同じ道具で文字にした結果のハッシュも本文と一致した
      ・not_machine_saved: 記録ファイルが無い(AIが本文ファイルを書いたなど)
      ・reconvert_mismatch: 記録ファイルが読めない(meta_unreadable)、URL(url)・本文のハッシュ
        (content_sha256)が食い違う、元のファイルが無い(raw_missing)・ハッシュが食い違う(raw_sha256)、
        もう一度文字にできない(reconvert_failed)・文字にした結果が食い違う(reconvert)。どれが
        食い違ったかをdetailsに書く
      ・reconvert_skipped: 道具(pdftotext)が無くて、もう一度文字にできなかった(ほかは一致)
    戻り値: {分類: {"count": 件数, "source_ids": [...]}, ...}。reconvert_mismatchには"details"も付く。"""
    result = {kind: {"count": 0, "source_ids": []} for kind in SOURCE_BODY_CHECK_KINDS}
    result["reconvert_mismatch"]["details"] = []
    for source in edition.get("sources", []):
        if not isinstance(source, dict) or source.get("usage") != "quotable" or is_edinet_domain(source.get("url")):
            continue
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not (Path(cache_dir) / f"{source_id}.txt").is_file():
            continue
        kind, detail = _check_one_source_body(source, cache_dir)
        result[kind]["count"] += 1
        result[kind]["source_ids"].append(source_id)
        if detail is not None:
            result["reconvert_mismatch"]["details"].append(detail)
    return result


def run_check_published_date_only_required(edition, cache_dir):
    """検査36(改修27-2第4回・S2): 日付だけの出典(published_date_onlyが真。書類一覧の
    2つ SRC-EDINET-LIST・SRC-EDINET-LIST-PREV は除く)は、本文にその日付が書かれて
    いなければ「日付不明」とし、その出典を参照する「新しい変化」(section_idがchange)の
    行を、その場で落とす。change以外の枠(big・ripple・deep)の行は落とさない
    (鮮度の条件はchange枠だけのため)。

    日付不明にする理由は次の3つで、理由を分けて記録する。
      ・not_in_body: 本文は読めたが、published_atの日付がどの書き方でも見つからない
      ・body_missing: キャッシュに本文のファイルが無い(取得していない出典など)
      ・body_unreadable: 本文のファイルはあるが、文字コードの問題で読めない
    時刻付きの出典は対象外(改修31第1回から、行を落とす run_check_published_timed_date_required() と、
    記録だけの run_check_published_at() が扱う)。

    検査10(run_check_e_stale_sources)より前に呼ぶこと。ここで落とした行は検査10に
    届かないので、unknown_published_at_hits・stale_source_hitsには数えない
    (published_date_not_foundだけで数える)。

    戻り値: {"count": 日付不明にした出典の数(参照する行の有無・枠を問わない),
             "source_ids": [...], "reasons": {source_id: 理由},
             "dropped_line_ids": [落とした行のline_id]}。"""
    not_found = {}
    for source in edition.get("sources", []):
        if not source.get("published_date_only"):
            continue
        source_id = source.get("source_id")
        if source_id in EDINET_DOCLIST_SOURCE_IDS:
            continue
        published_at = source.get("published_at")
        if not is_date_only_string(published_at):
            continue  # 通常は起きない(印はpublished_atの形から機械が書く)。読めない出典は検査10が扱う。
        cache_path = Path(cache_dir) / f"{source_id}.txt"
        if not cache_path.is_file():
            not_found[source_id] = "body_missing"
            continue
        body_text = read_source_body_for_checks(cache_path, source)
        if body_text is None:
            not_found[source_id] = "body_unreadable"
            continue
        published_date = dt.date.fromisoformat(published_at)
        candidates = published_at_candidates(published_date.year, published_date.month, published_date.day)
        if not date_found_in_text(body_text, candidates):
            not_found[source_id] = "not_in_body"

    dropped_line_ids = []
    for section in edition.get("sections", []):
        if section.get("section_id") != "change":
            continue
        for article in section.get("articles", []):
            kept = []
            for line in article.get("lines", []):
                if line.get("source_ref") in not_found:
                    dropped_line_ids.append(line.get("line_id"))
                else:
                    kept.append(line)
            article["lines"] = kept

    return {
        "count": len(not_found),
        "source_ids": list(not_found),
        "reasons": dict(not_found),
        "dropped_line_ids": dropped_line_ids,
    }


def timed_published_at_candidates(published_dt):
    """改修31第1回(3-5): 時刻付きの出典で本文を探す日付の候補。run_check_published_at()と同じ2つ
    (日本時間に直した日付・published_atに書かれた時差のままの日付)に、それぞれの前日を足す
    (海外の資料で、AIが現地の日付を日本時間で書いてしまった場合を救うため)。"""
    dates = []
    for base in (published_dt.astimezone(JST).date(), published_dt.date()):
        for day in (base, base - dt.timedelta(days=1)):
            if day not in dates:
                dates.append(day)
    candidates = []
    for day in dates:
        candidates += published_at_candidates(day.year, day.month, day.day)
    return candidates


def run_check_published_timed_date_required(edition, cache_dir):
    """検査36の時刻付きの出典の分(改修31第1回・3-5): published_atが時刻付き(parse_datetime_assume_jst()で
    読める・日付だけではない)で、EDINETの出典(is_edinet_domain。個々の書類の時刻は機械が書類一覧から
    書くため。書類一覧の2つもEDINETのドメイン)でない出典は、本文にその日付
    (timed_published_at_candidates())が書かれていなければ「日付不明」とし、その出典を参照する
    「新しい変化」(section_idがchange)の行をその場で落とす。change以外の枠の行は落とさない。
    理由は日付だけの出典(run_check_published_date_only_required)と同じ3つ
    (not_in_body・body_missing・body_unreadable)。
    run_check_published_date_only_required()と同じく検査10より前に呼ぶ(ここで落とした行は
    検査10に届かない)。記録だけのrun_check_published_at()は今までどおり別に動く。
    戻り値: run_check_published_date_only_required()と同じ形。"""
    not_found = {}
    for source in edition.get("sources", []):
        if source.get("published_date_only"):
            continue
        if is_edinet_domain(source.get("url")):
            continue
        published_dt = parse_datetime_assume_jst(source.get("published_at"))
        if published_dt is None:
            continue  # nullや読めない値は検査10(unknown_published_at)が扱う。
        source_id = source.get("source_id")
        cache_path = Path(cache_dir) / f"{source_id}.txt"
        if not cache_path.is_file():
            not_found[source_id] = "body_missing"
            continue
        body_text = read_source_body_for_checks(cache_path, source)
        if body_text is None:
            not_found[source_id] = "body_unreadable"
            continue
        if not date_found_in_text(body_text, timed_published_at_candidates(published_dt)):
            not_found[source_id] = "not_in_body"

    dropped_line_ids = []
    for section in edition.get("sections", []):
        if section.get("section_id") != "change":
            continue
        for article in section.get("articles", []):
            kept = []
            for line in article.get("lines", []):
                if line.get("source_ref") in not_found:
                    dropped_line_ids.append(line.get("line_id"))
                else:
                    kept.append(line)
            article["lines"] = kept

    return {
        "count": len(not_found),
        "source_ids": list(not_found),
        "reasons": dict(not_found),
        "dropped_line_ids": dropped_line_ids,
    }


NUMBER_COVERAGE_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*")


def compute_number_coverage(edition):
    """修正4: 本文の数字の個数と、行が申告したnumbersの件数の差を記録する
    (要件定義書v12 13章と同じく、判定には使わない記録専用)。紙面を作るAIが、
    照合を避けるためにnumbersを書かずに済ませていないかを見る材料。

    対象はiter_lines()が返す本文のすべての行(excerptではなく行のtext)。
    textをnormalize_text()で正規化してからNUMBER_COVERAGE_TOKEN_RE
    (\\d+(?:[.,]\\d+)*)に一致する個数を数える。normalize_text()が数字間の
    カンマを既に取り除くため「1,901」は1個、「2026年9月18日」は3個になる。
    漢数字は数えない。

    差(gap)は日付・年号も数えるため、正常な行でも大きく出る。この値で行を
    落としたり印を変えたりしない(号ごとの比較のための記録)。

    呼び出しはline["mark"](run_line_verification()が確定させた値)が
    付いた後、かつ行の削除(停止語・出典の鮮度)が終わった後に行うこと。
    by_markはその時点で号に残っている行の内訳になる。"""
    total_text_tokens = 0
    total_numbers_declared = 0
    by_mark = {}
    for _section, _article, line in iter_lines(edition):
        text_tokens = len(NUMBER_COVERAGE_TOKEN_RE.findall(normalize_text(line.get("text") or "")))
        numbers_declared = len(line.get("numbers") or [])
        total_text_tokens += text_tokens
        total_numbers_declared += numbers_declared
        mark = line.get("mark") or "unknown"
        bucket = by_mark.setdefault(mark, {"text_number_tokens": 0, "numbers_declared": 0})
        bucket["text_number_tokens"] += text_tokens
        bucket["numbers_declared"] += numbers_declared
    return {
        "text_number_tokens": total_text_tokens,
        "numbers_declared": total_numbers_declared,
        "gap": total_text_tokens - total_numbers_declared,
        "by_mark": by_mark,
    }


def count_sources_published_at_null(edition):
    """修正5: published_at(公表日時)が書かれていない出典の件数を数える
    (edition["sources"]の全件が対象。change枠の行が落ちた件数を数える
    既存のunknown_published_at_hitsとは別集計で、そちらの値は変えない)。
    値がnull・空文字・キー自体が無い場合を「書かれていない」とみなす。
    行は落とさない(記録専用)。"""
    count = 0
    source_ids = []
    for source in edition.get("sources") or []:
        if not source.get("published_at"):
            count += 1
            source_ids.append(source.get("source_id"))
    return {"count": count, "source_ids": source_ids}


# 改修27-1(4-12): 記録専用のキー(会社も行も消さない)で使う語のリスト。
SPECULATIVE_WORDS = ("恩恵", "見込まれる", "意識される", "なりやすい")
BANNED_WORDS = ("注目", "おすすめ", "有望", "代表", "主要", "有力")


def _is_date_or_count_label(label):
    """numbers[].labelが「日付」または「件数」で始まるかどうかを見る。"""
    return isinstance(label, str) and (label.startswith("日付") or label.startswith("件数"))


def compute_date_only_number_lines(edition):
    """改修27-1(4-12): 登録した数字(numbers)がすべて「日付」か「件数」で始まる
    labelだけの行を数える(記録専用。行は消さない)。numbersが空の行は対象外
    (「すべて日付・件数」ではなく「そもそも数字が無い」ため)。
    行を消す前(停止語・出典の鮮度の検査より前)に数えること。
    戻り値: {"count": 件数, "line_ids": [...]}。"""
    line_ids = []
    for _section, _article, line in iter_lines(edition):
        numbers = line.get("numbers") or []
        if not numbers:
            continue
        if all(_is_date_or_count_label(n.get("label")) for n in numbers):
            line_ids.append(line.get("line_id"))
    return {"count": len(line_ids), "line_ids": line_ids}


def compute_self_declared_unverified(edition):
    """改修27-1(4-12): AIが最初からclaimed_mark='unverified'と自己申告していた行を
    数える(記録専用。行は消さない。機械が後から'unverified'と判定したmarkとは別物)。
    行を消す前に数えること。
    戻り値: {"count": 件数, "line_ids": [...]}。"""
    line_ids = [
        line.get("line_id") for _section, _article, line in iter_lines(edition)
        if line.get("claimed_mark") == "unverified"
    ]
    return {"count": len(line_ids), "line_ids": line_ids}


def compute_banned_word_hits_edition(edition):
    """改修27-1(4-12、Q9の回答): 禁止語(BANNED_WORDS)が、紙面側でAIが書いた
    表示用の文(見出し・本文の行・推論欄)のどこかに出た件数と場所を記録する
    (記録専用。会社も行も消さない)。出典からの抜き出し文(excerpt)・出典の
    題名(title)は対象にしない。仮説(hypotheses)側の分は
    compute_banned_word_hits_hyps()が別に返す(呼び出し側でこのリストに追加する)。
    行を消す前に数えること。"""
    hits = []
    for section in edition.get("sections") or []:
        for article in section.get("articles") or []:
            headline = article.get("headline") or ""
            for word in BANNED_WORDS:
                if word in headline:
                    hits.append({"word": word, "location": "headline", "id": article.get("article_id")})
            for line in article.get("lines") or []:
                text = line.get("text") or ""
                for word in BANNED_WORDS:
                    if word in text:
                        hits.append({"word": word, "location": "line_text", "id": line.get("line_id")})
            for inf in article.get("inferences") or []:
                if not isinstance(inf, dict):
                    continue
                inf_text = inf.get("text") or ""
                for word in BANNED_WORDS:
                    if word in inf_text:
                        hits.append({"word": word, "location": "inference_text", "id": article.get("article_id")})
    return hits


def compute_banned_word_hits_hyps(hyps):
    """改修27-1(4-12、Q9の回答): 禁止語が、上段の仮説のrelation_text(上段の説明文)・
    impact_reasonのどこかに出た件数と場所を記録する(記録専用。会社は消さない)。
    仮説が検査で消される前の全件を対象にする。"""
    hits = []
    for hyp in hyps:
        for field, location in (
            ("relation_text", "hypothesis_relation_text"),
            ("impact_reason", "hypothesis_impact_reason"),
        ):
            text = hyp.get(field) or ""
            for word in BANNED_WORDS:
                if word in text:
                    hits.append({"word": word, "location": location, "id": hyp.get("hypothesis_id")})
    return hits


def compute_speculative_word_counts(hyps):
    """改修27-1(4-12): 「恩恵」「見込まれる」「意識される」「なりやすい」が、
    上段の仮説のrelation_text・impact_reasonにそれぞれ何回出てきたかを数える
    (記録専用。会社は消さない)。仮説が検査で消される前の全件を対象にする。
    戻り値: {"relation_text": {語: 件数, ...}, "impact_reason": {語: 件数, ...}}。"""
    counts = {
        "relation_text": {w: 0 for w in SPECULATIVE_WORDS},
        "impact_reason": {w: 0 for w in SPECULATIVE_WORDS},
    }
    for hyp in hyps:
        for field in ("relation_text", "impact_reason"):
            text = hyp.get(field) or ""
            for word in SPECULATIVE_WORDS:
                counts[field][word] += text.count(word)
    return counts


def compute_change_verified_lines_by_section(edition):
    """改修27-1(4-12): 枠(section_id)ごとに、出典で裏の取れた行
    (mark=='source_number_match')の数を数える(記録専用)。停止語・出典の鮮度等で
    消えた行より後、最終的に号に残っている行で数えること。
    戻り値: {section_id: 件数, ...}。"""
    counts = {}
    for section in edition.get("sections") or []:
        section_id = section.get("section_id")
        verified = sum(
            1
            for article in section.get("articles") or []
            for line in article.get("lines") or []
            if line.get("mark") == "source_number_match"
        )
        counts[section_id] = verified
    return counts


# ---------------------------------------------------------------------------
# 改修31第3回: 記録だけ足すもの(会社も行も消さない。行の印・会社・終了コードは変えない)。
#   3-1 upper_trading_between / 3-2 unregistered_numbers / 3-3 short_excerpts /
#   3-4 reported_name_in_snippet / 3-5 relation_text_role_mismatch /
#   3-6 industries_shown(仮説ファイルの最上位)・industry_picks_not_shown
# ---------------------------------------------------------------------------

# 東証の売買立会の時間(前場9:00〜11:30・後場12:30〜15:30。2024年11月5日から後場の終わりが15:30。
# JPXの公表で確認。半日だけの取引日・システム障害の日はcalendar/からは分からない)。
TRADING_SESSIONS = (((9, 0), (11, 30)), ((12, 30), (15, 30)))


def trading_minutes_between(start_dt, end_dt, business_days):
    """start_dtからend_dtまでの間の、営業日(business_days。'YYYY-MM-DD'の一覧)の取引時間(TRADING_SESSIONS)の
    分数。日本時間で数える。end_dtがstart_dt以前なら0。"""
    start, end = start_dt.astimezone(JST), end_dt.astimezone(JST)
    if end <= start:
        return 0
    business = set(business_days)
    total = 0
    day = start.date()
    while day <= end.date():
        if day.isoformat() in business:
            for (h1, m1), (h2, m2) in TRADING_SESSIONS:
                open_dt = dt.datetime(day.year, day.month, day.day, h1, m1, tzinfo=JST)
                close_dt = dt.datetime(day.year, day.month, day.day, h2, m2, tzinfo=JST)
                lo, hi = max(open_dt, start), min(close_dt, end)
                if hi > lo:
                    total += int((hi - lo).total_seconds() // 60)
        day += dt.timedelta(days=1)
    return total


def compute_upper_trading_between(hyps, sources_by_id, edition, business_days, run_at_dt):
    """改修31第3回(3-1): 上段の会社ごとに、根拠の書類の提出から株価の基準時点までに、取引時間が何分はさまったかを
    記録する(記録だけ。会社は消さない)。基準時点は、朝号・夕方号が会社のbaseline_dateの9:00、昼号が照合の実行時刻
    (読者が株価を見るのはこれより後なので、取引時間は最小の値になる)。
    reason(計算できなかった理由。計算できたらnull): submitted_at_missing(出典が無い・published_atが無い)・
    submitted_at_not_timed(日付だけで時刻が無い)・baseline_unknown(基準時点が分からない)・
    submitted_after_baseline(提出が基準時点より後)・business_days_unknown(営業日カレンダーが期間をカバーしない)。
    戻り値: [{"hypothesis_id", "company_name", "submitted_at", "baseline_point", "trading_minutes", "reason"}]。"""
    records = []
    slot = edition.get("slot")
    for hyp in hyps:
        source = sources_by_id.get(hyp.get("evidence_source_ref"))
        published_at = source.get("published_at") if isinstance(source, dict) else None
        submitted = None if is_date_only_string(published_at) else parse_datetime_assume_jst(published_at)
        baseline = None
        if slot == "noon":
            baseline = run_at_dt
        elif slot in ("morning", "evening") and is_date_only_string(hyp.get("baseline_date")):
            baseline = dt.datetime.fromisoformat(hyp["baseline_date"] + "T09:00:00+09:00")
        record = {
            "hypothesis_id": hyp.get("hypothesis_id"), "company_name": hyp.get("company_name"),
            "submitted_at": submitted.isoformat() if submitted else None,
            "baseline_point": baseline.astimezone(JST).isoformat() if baseline else None,
            "trading_minutes": None, "reason": None,
        }
        if submitted is None:
            record["reason"] = "submitted_at_not_timed" if is_date_only_string(published_at) else "submitted_at_missing"
        elif baseline is None:
            record["reason"] = "baseline_unknown"
        elif baseline <= submitted:
            record["reason"] = "submitted_after_baseline"
        elif (not business_days or business_days[0] > submitted.astimezone(JST).date().isoformat()
              or business_days[-1] < baseline.astimezone(JST).date().isoformat()):
            record["reason"] = "business_days_unknown"
        else:
            record["trading_minutes"] = trading_minutes_between(submitted, baseline, business_days)
        records.append(record)
    return records


# 日付・時刻の数字(年・月・日・時・分の直前の数字)は照合の対象にしないため、未登録の数に数えない。
_DATE_TIME_UNIT_CHARS = "年月日時分"


def compute_unregistered_numbers(edition):
    """改修31第3回(3-2): 確定した印がsource_number_matchの行について、textの数字(compute_number_coverage()と同じ
    数え方)のうち、numbersに登録した値(値として比べる。1.0と1は同じ)に無いものを数える(記録だけ)。
    年・月・日・時・分の直前の数字は数えず、数えなかった分をexcluded_date_time_tokensに残す。
    同じ数字が本文に2回あって登録が1つなら、残りの1回を未登録とする(登録した値を1回ずつ使う)。
    戻り値: {"lines_with_unregistered", "total_unregistered", "excluded_date_time_tokens",
             "lines": [{"line_id", "values"}]}(linesは未登録が1つ以上ある行だけ)。"""
    lines_out = []
    total = 0
    excluded = 0
    for _s, _a, line in iter_lines(edition):
        if line.get("mark") != "source_number_match":
            continue
        text = normalize_text(line.get("text") or "")
        registered = [_to_decimal(v) for v in excerpt_log_number_values(line)]
        values = []
        for match in NUMBER_COVERAGE_TOKEN_RE.finditer(text):
            following = text[match.end():match.end() + 1]
            if following and following in _DATE_TIME_UNIT_CHARS:
                excluded += 1
                continue
            try:
                token_value = decimal.Decimal(match.group().replace(",", ""))
            except decimal.InvalidOperation:
                values.append(match.group())
                continue
            for i, reg in enumerate(registered):
                if reg is not None and reg == token_value:
                    del registered[i]
                    break
            else:
                values.append(match.group())
        if values:
            lines_out.append({"line_id": line.get("line_id"), "values": values})
            total += len(values)
    return {
        "lines_with_unregistered": len(lines_out), "total_unregistered": total,
        "excluded_date_time_tokens": excluded, "lines": lines_out,
    }


SHORT_EXCERPT_THRESHOLD = 3


def count_excerpt_letters(excerpt):
    """抜き出しの、数字・記号・空白を除いた文字(ひらがな・カタカナ・漢字・英字など。Unicodeの文字の種類が
    Lで始まるもの)の数。normalize_text()でそろえてから数える。"""
    return sum(1 for ch in normalize_text(excerpt or "") if unicodedata.category(ch).startswith("L"))


def compute_short_excerpts(edition):
    """改修31第3回(3-3): 確定した印がsource_number_matchの行のexcerptのうち、数字・記号・空白を除いた文字が
    SHORT_EXCERPT_THRESHOLD(3)文字以下のものを記録する(記録だけ。見出しの無い数字だけの抜き出しを見つけるため)。
    戻り値: {"threshold", "count", "zero_letter_count", "lines": [{"line_id", "letters"}]}。"""
    lines_out = []
    for _s, _a, line in iter_lines(edition):
        excerpt = line.get("excerpt")
        if line.get("mark") != "source_number_match" or not isinstance(excerpt, str):
            continue
        letters = count_excerpt_letters(excerpt)
        if letters <= SHORT_EXCERPT_THRESHOLD:
            lines_out.append({"line_id": line.get("line_id"), "letters": letters})
    return {
        "threshold": SHORT_EXCERPT_THRESHOLD, "count": len(lines_out),
        "zero_letter_count": sum(1 for x in lines_out if x["letters"] == 0), "lines": lines_out,
    }


def compute_reported_name_in_snippet(hyps, sources_by_id, cache_dir):
    """改修31第3回(3-4): 報道由来(evidence_gradeがreported)の上段の会社について、根拠の出典の本文
    ({cache_dir}/{source_id}.txt)に会社の名前があるかを、検査11と同じ方法(find_company_name_stage())で調べる
    (記録だけ。長い社名の一部かどうかの判定は行わない)。
    result: found(stageに見つかった段階)・not_found・body_missing(出典が無い・本文のファイルが無い)・
    body_unreadable(文字コードで読めない)。
    戻り値: [{"hypothesis_id", "company_name", "source_ref", "result", "stage"}]。"""
    records = []
    generic_words = pick_industry_companies.load_generic_words()
    for hyp in hyps:
        if hyp.get("evidence_grade") != "reported":
            continue
        ref = hyp.get("evidence_source_ref")
        source = sources_by_id.get(ref) if isinstance(ref, str) else None
        record = {"hypothesis_id": hyp.get("hypothesis_id"), "company_name": hyp.get("company_name"),
                  "source_ref": ref, "result": "body_missing", "stage": None}
        cache_path = Path(cache_dir) / f"{ref}.txt" if source is not None else None
        if cache_path is not None and cache_path.is_file():
            body_text = read_source_body_for_checks(cache_path, source)
            if body_text is None:
                record["result"] = "body_unreadable"
            else:
                stage, _only_in_longer = find_company_name_stage(
                    hyp.get("company_name"), body_text, None, generic_words,
                )
                record["result"], record["stage"] = ("found", stage) if stage else ("not_found", None)
        records.append(record)
    return records


# 改修31第3回(3-5): relation_textの言葉と、機械が決めた立場(evidence_role・tob_side)の対応。
# 「対象者」だけでは判定しない(公開買付の書類は、買付側から見て対象会社を「対象者」と呼ぶため)。
RELATION_ROLE_WORDS = (
    ("自ら提出", "evidence_role", "filer_self"),
    ("買付者", "tob_side", "bidder"),
    ("公開買付けの対象", "tob_side", "target"),
    ("公開買付の対象", "tob_side", "target"),
)


def compute_relation_text_role_mismatch(hyps):
    """改修31第3回(3-5): relation_textにある言葉と、機械が決めたevidence_role・tob_sideが合わない上段の会社を
    記録する(記録だけ。会社は消さない)。戻り値: [{"hypothesis_id", "company_name", "word", "evidence_role",
    "tob_side"}](1つの会社が複数の言葉に当たれば、言葉ごとに1件)。"""
    records = []
    for hyp in hyps:
        relation_text = unicodedata.normalize("NFKC", hyp.get("relation_text") or "")
        for word, field, expected in RELATION_ROLE_WORDS:
            if word in relation_text and hyp.get(field) != expected:
                records.append({
                    "hypothesis_id": hyp.get("hypothesis_id"), "company_name": hyp.get("company_name"),
                    "word": word, "evidence_role": hyp.get("evidence_role"), "tob_side": hyp.get("tob_side"),
                })
    return records


def compute_industries_shown(hypotheses_doc, edition, codelist_rows, skip_reason=None):
    """改修31第3回(3-6): 画面で「関係しそうな業種」として業種名だけを出す業種の一覧を作る(会社の枠が尽きて
    会社が出なかった業種も入る)。入れるのは、次をすべて満たす業種の指定(industry_picks)だけ:
      ・記事が紙面にある ・同じ記事の3つ目以降の指定ではない(pick_industry_companiesと同じく1記事2つまで)
      ・33業種の許可リスト(build_allowed_industries()。check_lower_industry()と同じ判定)にある
      ・industry_line_idsが空でなく、すべてその記事の行で、確定した印が事実系(check_lower_line_mark()と同じ判定)
      ・同じ記事・同じ業種で、先に入ったものがない
    業種名は、コードリストの表記(空白・全角半角のゆれを直したもの)で書く。
    skip_reason: 企業欄を出さない号は"market_closed"か"baseline_late"(一覧は空)。
    戻り値: (入れた業種[{"article_id","industry"}], 入れなかった指定[{"article_id","industry","reason"}], 状態)。
    状態: ok・market_closed・baseline_late・codelist_unavailable(許可リストが作れない日。一覧は空)。"""
    picks = [p for p in hypotheses_doc.get("industry_picks") or [] if isinstance(p, dict)]
    shown, not_shown = [], []

    def reject(pick, reason):
        not_shown.append({"article_id": pick.get("article_id"), "industry": pick.get("industry"), "reason": reason})

    if skip_reason is not None or codelist_rows is None:
        status = skip_reason or "codelist_unavailable"
        for pick in picks:
            reject(pick, status)
        return shown, not_shown, status

    allowed = {edinet_codelist._normalize_industry_name(name): name for name in build_allowed_industries(codelist_rows)}
    articles = pick_industry_companies.index_articles(edition)
    line_marks = {line.get("line_id"): line.get("mark") for _s, _a, line in iter_lines(edition)}
    picks_seen_per_article = {}
    shown_keys = set()
    for pick in picks:
        article_id = pick.get("article_id")
        article = articles.get(article_id) if isinstance(article_id, str) else None
        if article is None:
            reject(pick, "article_not_found")
            continue
        picks_seen_per_article[article_id] = picks_seen_per_article.get(article_id, 0) + 1
        if picks_seen_per_article[article_id] > 2:
            reject(pick, "over_two_per_article")
            continue
        industry = allowed.get(edinet_codelist._normalize_industry_name(pick.get("industry")))
        if industry is None:
            reject(pick, "industry_not_allowed")
            continue
        line_ids = pick.get("industry_line_ids") or []
        if not line_ids:
            reject(pick, "industry_line_ids_empty")
        elif any(lid not in article["line_ids"] for lid in line_ids):
            reject(pick, "industry_line_ids_not_in_article")
        elif check_lower_line_mark({"line_ids": line_ids}, line_marks):
            reject(pick, "industry_line_ids_not_fact")
        elif (article_id, industry) in shown_keys:
            reject(pick, "duplicate_in_article")
        else:
            shown_keys.add((article_id, industry))
            shown.append({"article_id": article_id, "industry": industry})
    return shown, not_shown, "ok"


RECENT_HEADLINES_FILENAME = "RECENT-HEADLINES.json"


def check_recent_headlines_status(cache_dir, edition_date, edition_slot):
    """改修27-1(4-11): scripts/recent_headlines.pyの実行結果を読み、失敗したかどうかを
    判定する。recent_headlines.pyは実行のたびに必ず--outへファイルを書く
    (成功時はstatus:'ok'、失敗時はstatus:'error')ため、この1つのファイルを読む
    だけで失敗を知れる(印のファイル方式。失敗しても号の作成は止めない。
    このファイル自体が無い・読めない場合も「失敗」として扱う)。
    戻り値: 失敗していればTrue、正常に(この号のために)実行されていればFalse。"""
    path = Path(cache_dir) / RECENT_HEADLINES_FILENAME
    if not path.is_file():
        return True
    try:
        record = load_json(path)
    except (json.JSONDecodeError, OSError):
        return True
    if record.get("status") != "ok":
        return True
    if record.get("date") != edition_date or record.get("slot") != edition_slot:
        return True
    return False


# ---------------------------------------------------------------------------
# 改修31第2回: check_excerpts.py の実行記録(キャッシュの CHECK-EXCERPTS-{edition_id}.jsonl)を読んで
# まとめる。記録だけで、行の印・会社・終了コードは変えない。紙面を作るAIは記録を消したり書き換えたり
# できるので、記録が無い・壊れているときも、そのことを status に書いて続ける(号は止めない)。
# ---------------------------------------------------------------------------

EXCERPT_LOG_FILENAME_TMPL = "CHECK-EXCERPTS-{edition_id}.jsonl"
EXCERPT_LOG_EDITION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(?:morning|noon|evening)$")
EXCERPT_LOG_SCRIPT_VERSION = "excerpt-log-1"


def excerpt_log_path(cache_dir, edition_id):
    """実行記録のファイルの場所。edition_idが{日付}-{morning|noon|evening}の形でなければNone
    (ファイル名にそのまま使うため、形に合わない値では記録を書かない・読まない)。"""
    if not isinstance(edition_id, str) or not EXCERPT_LOG_EDITION_ID_RE.match(edition_id):
        return None
    return Path(cache_dir) / EXCERPT_LOG_FILENAME_TMPL.format(edition_id=edition_id)


def excerpt_log_number_values(line):
    """行のnumbersから値だけを取り出したリスト(numbersがリストでなければ空)。"""
    numbers = line.get("numbers") if isinstance(line, dict) else None
    if not isinstance(numbers, list):
        return []
    return [n.get("value") if isinstance(n, dict) else n for n in numbers]


def excerpt_log_line_snapshot(edition):
    """紙面のすべての行の {line_id, claimed_mark, numbers(値だけ), source_ref}(実行記録の元)。"""
    return [
        {
            "line_id": line.get("line_id"), "claimed_mark": line.get("claimed_mark"),
            "numbers": excerpt_log_number_values(line), "source_ref": line.get("source_ref"),
        }
        for _s, _a, line in iter_lines(edition)
    ]


def _excerpt_log_values_equal(a, b):
    """数字の値の比べ方。数として読めるものは値で比べる(1.0と1を別物にしない)。"""
    da, db = _to_decimal(a), _to_decimal(b)
    try:
        if da is not None and db is not None:
            return da == db
    except decimal.InvalidOperation:
        pass
    return a == b


def _excerpt_log_record_ok(rec):
    """実行記録の1行の形が使えるか(AIが書き換えうるファイルなので、形を確かめてから使う)。"""
    return (
        isinstance(rec, dict) and isinstance(rec.get("edition_id"), str) and isinstance(rec.get("run_at"), str)
        and isinstance(rec.get("edition_sha256"), str) and isinstance(rec.get("lines"), list)
        and all(isinstance(l, dict) and isinstance(l.get("line_id"), str) for l in rec["lines"])
        and isinstance(rec.get("counts"), dict) and isinstance(rec.get("exit_code"), int)
        and not isinstance(rec.get("exit_code"), bool)
    )


def read_excerpt_log(cache_dir, edition_id):
    """実行記録を読む。戻り値: {"exists": ファイルがあるか, "records": この号の使える記録(書いた順),
    "unreadable_lines": 読めない行(JSONでない・形が違う)の数, "other_edition_lines": 別の号の行の数}。"""
    result = {"exists": False, "records": [], "unreadable_lines": 0, "other_edition_lines": 0}
    path = excerpt_log_path(cache_dir, edition_id)
    if path is None or not path.is_file():
        return result
    result["exists"] = True
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        result["unreadable_lines"] = 1
        return result
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            result["unreadable_lines"] += 1
            continue
        if not _excerpt_log_record_ok(rec):
            result["unreadable_lines"] += 1
        elif rec["edition_id"] != edition_id:
            result["other_edition_lines"] += 1
        else:
            result["records"].append(rec)
    return result


def compute_excerpt_check_log(cache_dir, edition, edition_sha256):
    """改修31第2回(3-4): check_excerpts.pyの実行記録をまとめて、verification.excerpt_check_logに書く値を作る
    (記録だけ。印・会社・終了コードは変えない)。editionは、照合が書き換える前の紙面(読んだ直後)。
    edition_sha256は、照合が読んだ紙面ファイルのバイト列のハッシュ。
    status: ok(この号の記録が1回以上ある)／missing(ファイルが無い・この号の記録も問題のある行も無い)／
    unreadable(読めない行がある)／other_edition(別の号の行がある)。読める行は、問題があっても集計に使い、
    problemsに読めない行・別の号の行の数を書く。記録が1つも使えないときは、1回目との比べ方の欄はnull。
    どんな書き方の記録でも例外で止めない(止めると終了コードが変わるため)。"""
    try:
        return _compute_excerpt_check_log(cache_dir, edition, edition_sha256)
    except Exception as e:   # 記録の形が想定外でも、号は止めない
        return {"status": "unreadable", "problems": {"error": type(e).__name__}, "run_count": 0,
                "first_run_at": None, "last_run_at": None, "edited_after_last_check": None,
                "removed_line_ids": None, "added_line_ids": None, "changed_marks": None, "numbers_removed": None,
                "last_counts": None, "last_exit_code": None}


def _compute_excerpt_check_log(cache_dir, edition, edition_sha256):
    log = read_excerpt_log(cache_dir, edition.get("edition_id"))
    records = log["records"]
    problems = {"unreadable_lines": log["unreadable_lines"], "other_edition_lines": log["other_edition_lines"]}
    if log["unreadable_lines"]:
        status = "unreadable"
    elif log["other_edition_lines"]:
        status = "other_edition"
    elif records:
        status = "ok"
    else:
        status = "missing"
    summary = {
        "status": status, "problems": problems, "run_count": len(records),
        "first_run_at": records[0]["run_at"] if records else None,
        "last_run_at": records[-1]["run_at"] if records else None,
        "edited_after_last_check": (records[-1]["edition_sha256"] != edition_sha256) if records else None,
        "removed_line_ids": None, "added_line_ids": None, "changed_marks": None, "numbers_removed": None,
        "last_counts": records[-1]["counts"] if records else None,
        "last_exit_code": records[-1]["exit_code"] if records else None,
    }
    if not records:
        return summary
    now = {}
    for item in excerpt_log_line_snapshot(edition):
        now.setdefault(item["line_id"], item)
    first = {}
    for item in records[0]["lines"]:
        first.setdefault(item["line_id"], item)
    summary["removed_line_ids"] = [lid for lid in first if lid not in now]
    summary["added_line_ids"] = [lid for lid in now if lid not in first]
    summary["changed_marks"] = [
        {"line_id": lid, "from": first[lid].get("claimed_mark"), "to": now[lid]["claimed_mark"]}
        for lid in first if lid in now and first[lid].get("claimed_mark") != now[lid]["claimed_mark"]
    ]
    numbers_removed = []
    for lid in first:
        if lid not in now:
            continue
        remaining = list(now[lid]["numbers"])
        gone = []
        old_values = first[lid].get("numbers")
        for value in old_values if isinstance(old_values, list) else []:
            for i, cur in enumerate(remaining):
                if _excerpt_log_values_equal(value, cur):
                    del remaining[i]
                    break
            else:
                gone.append(value)
        if gone:
            numbers_removed.append({"line_id": lid, "values": gone})
    summary["numbers_removed"] = numbers_removed
    return summary


def compute_rerun_detected(existing_verification):
    """修正8: この号が既にverification(照合結果)を持っていたか、つまり今回が
    2回目以降の照合かどうかを返す。実行時刻には一切依存しない、号のデータ
    (existing_verificationの有無)だけで決まる純粋な判定。
    判定(検査の合否)には使わない。existing_verificationは紙面を作るAIが
    書けるファイルの中にある値のため、これを条件に検査を飛ばす作りにしない。"""
    return existing_verification is not None


def run_check_baseline_late(edition, run_at_dt):
    """検査20: 号の遅延判定。スクリプトの実行時刻(run_at_dt、日本時間)が、morning号なら
    8:50を過ぎていたらTrueを返す。noon号・evening号は常にFalse(判定しない)。
    昼号(noon)は基準が「読んだ時点の株価」であり、時刻の制限が無いため対象外
    (要件定義書v12 3.5(3))。
    generated_at(AIの自己申告)はこの判定にはいっさい使わない。AIが書き換えられる値を
    基準にすると、実際は門限を過ぎているのに間に合ったことにできてしまうため。"""
    slot = edition.get("slot")
    if slot != "morning":
        return False
    local_time = run_at_dt.astimezone(JST).time()
    return local_time > MORNING_DEADLINE


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


def recent_business_days(business_days, date_str, count):
    """改修27-1(4-11): business_days(昇順)の中から、date_str以前(date_str自身を
    含む)で直近count日分の営業日を昇順で返す。date_str自身が営業日でなくても、
    date_str以前の営業日から数える。「3営業日」の数え方をここに1か所にまとめ、
    scripts/recent_headlines.pyと、27-2で作る続報の判定の両方から使う。
    business_daysが空、または直近count日分に満たない場合は、あるだけ返す。"""
    upto = [d for d in business_days if d <= date_str]
    if not upto:
        return []
    return upto[-count:]


# 改修27-2第9回: 号をまたいで比べる範囲(号をまたぐ重複・続報の判定・scripts/recent_headlines.py
# で共通)。一覧(editions/index.json)は作業フォルダ基準の固定パスで読む(照合のコマンドに
# 引数は足さない)。一覧の中のedition_path/hypotheses_pathも作業フォルダ基準。
EDITIONS_INDEX_PATH = "editions/index.json"
RECENT_WINDOW_BUSINESS_DAYS = 3
SLOT_ORDER = {"morning": 0, "noon": 1, "evening": 2}


def is_before(entry_date, entry_slot, target_date, target_slot):
    """entry(既にある号)が、target(今回の号)より前かどうかを判定する。
    日付が違えばその前後だけで決まる。同じ日付なら、時間帯の順(朝<昼<夕方)で
    比べる。slot名が想定外(SLOT_ORDERに無い)の場合は、安全側でFalse(対象外)にする。
    (改修27-2第9回でscripts/recent_headlines.pyから移した)"""
    if entry_date != target_date:
        return entry_date < target_date
    # 改修27-2第9回の2回目: slotがリスト等(文字でない)でも止まらないよう、文字だけを引く。
    entry_order = SLOT_ORDER.get(entry_slot) if isinstance(entry_slot, str) else None
    target_order = SLOT_ORDER.get(target_slot) if isinstance(target_slot, str) else None
    if entry_order is None or target_order is None:
        return False
    return entry_order < target_order


def select_recent_editions(index_entries, business_days, edition_date, edition_slot, edition_id,
                           count=RECENT_WINDOW_BUSINESS_DAYS):
    """改修27-2第9回: editions/index.jsonの号(index_entries)のうち、比べる範囲に入る号を
    日付・時間帯の古い順で返す(ファイルは読まず、選ぶだけ)。
      ・範囲は、recent_business_days(business_days, edition_date, count)の最も古い日から
        edition_dateまでの暦日のすべての日(土日・祝日の号も含める)
      ・同じ日付の号は、時間帯の順で今回より前のものだけ
      ・今回の号と同じedition_idの号は外す
    date・slot・edition_idが文字でない等、形の壊れた行は飛ばす(改修27-2第9回の2回目で
    slot・edition_idにも広げた)。営業日が1つも無ければ空配列。"""
    window = recent_business_days(business_days, edition_date, count)
    if not window:
        return []
    start = window[0]
    selected = []
    for entry in index_entries:
        if not isinstance(entry, dict):
            continue
        entry_date = entry.get("date")
        if not isinstance(entry_date, str) or not (start <= entry_date <= edition_date):
            continue
        if not isinstance(entry.get("slot"), str) or not isinstance(entry.get("edition_id"), str):
            continue
        if not is_before(entry_date, entry.get("slot"), edition_date, edition_slot):
            continue
        if entry.get("edition_id") == edition_id:
            continue
        selected.append(entry)
    selected.sort(key=lambda e: (e["date"], SLOT_ORDER.get(e.get("slot"), -1)))
    return selected


def load_editions_index(path=EDITIONS_INDEX_PATH):
    """改修27-2第9回: editions/index.jsonを読み、号の一覧(editions配列)を返す。
    ファイルが無い・壊れている・editionsが配列でない場合はNone(号は止めない)。"""
    try:
        doc = load_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    entries = doc.get("editions") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return None
    return entries


def recent_edition_shape_problem(edition):
    """改修27-2第9回の2回目: 過去の号の紙面のうち、号をまたぐ比較で読む値の形を確かめる。
    sources・sections・articles・linesが配列で、その要素が辞書であること、source_id・url・
    article_id・headline・source_refが文字かnullであること。問題があればその場所(文字列)を、
    無ければNoneを返す(想定外の形の号は、比べる対象から外すため)。"""
    sources = edition.get("sources", [])
    if not isinstance(sources, list):
        return "sources"
    for source in sources:
        if not isinstance(source, dict):
            return "sources[]"
        for key in ("source_id", "url"):
            if not _is_str_or_null(source.get(key)):
                return f"sources[].{key}"
    sections = edition.get("sections", [])
    if not isinstance(sections, list):
        return "sections"
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("articles", []), list):
            return "sections[]"
        for article in section.get("articles", []):
            if not isinstance(article, dict) or not isinstance(article.get("lines", []), list):
                return "articles[]"
            for key in ("article_id", "headline"):
                if not _is_str_or_null(article.get(key)):
                    return f"articles[].{key}"
            for line in article.get("lines", []):
                if not isinstance(line, dict) or not _is_str_or_null(line.get("source_ref")):
                    return "lines[]"
    return None


def recent_hypotheses_shape_problem(hypotheses):
    """改修27-2第9回の2回目: 過去の号の仮説の配列のうち、号をまたぐ重複で読む値の形を確かめる
    (要素が辞書で、hypothesis_id・ticker・evidence_source_refが文字かnull)。"""
    for hyp in hypotheses:
        if not isinstance(hyp, dict):
            return "hypotheses[]"
        for key in ("hypothesis_id", "ticker", "evidence_source_ref"):
            if not _is_str_or_null(hyp.get(key)):
                return f"hypotheses[].{key}"
    return None


def load_recent_editions(entries):
    """改修27-2第9回: select_recent_editions()で選んだ号の紙面・仮説のファイルを読む。
    戻り値: (読めた号の一覧, 読めなかったものの一覧)。
      ・読めた号: {"entry", "edition", "hypotheses"}。hypothesesは仮説の配列で、仮説ファイルが
        読めなかった号はNone(その号は号をまたぐ重複の判定だけ飛ばし、続報の判定には使う)。
      ・紙面・仮説が読めても、比較で読む値の形が想定外なら読めなかったものとして扱う
        (recent_edition_shape_problem・recent_hypotheses_shape_problem)。
      ・読めなかったもの: {"edition_id", "file": "edition"|"hypotheses"}。紙面が読めなければ
        その号は丸ごと飛ばす。仮説ファイルが無く、hypotheses_countが0なら正常(仮説0件)。"""
    loaded = []
    unreadable = []
    for entry in entries:
        edition_id = entry.get("edition_id")
        try:
            edition = load_json(entry.get("edition_path"))
        except (TypeError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            edition = None
        # 改修27-2第9回の2回目: 読めても中身が想定外の形なら、読めなかった号と同じに扱う。
        if not isinstance(edition, dict) or recent_edition_shape_problem(edition) is not None:
            unreadable.append({"edition_id": edition_id, "file": "edition"})
            continue

        hyp_path = entry.get("hypotheses_path")
        hypotheses = None
        if entry.get("hypotheses_count") == 0 and not (isinstance(hyp_path, str) and Path(hyp_path).exists()):
            hypotheses = []
        else:
            try:
                hyp_doc = load_json(hyp_path)
            except (TypeError, OSError, json.JSONDecodeError, UnicodeDecodeError):
                hyp_doc = None
            if (isinstance(hyp_doc, dict) and isinstance(hyp_doc.get("hypotheses"), list)
                    and recent_hypotheses_shape_problem(hyp_doc["hypotheses"]) is None):
                hypotheses = hyp_doc["hypotheses"]
            else:
                unreadable.append({"edition_id": edition_id, "file": "hypotheses"})
        loaded.append({"entry": entry, "edition": edition, "hypotheses": hypotheses})
    return loaded, unreadable


def compute_deadline(business_days, baseline_date, horizon):
    try:
        idx = business_days.index(baseline_date)
        return business_days[idx + horizon]
    except (ValueError, IndexError):
        return None


def first_business_day_on_or_after(business_days, date_str):
    """business_days(昇順)の中から、date_str以降で最初の営業日を返す。無ければNone。"""
    for day in business_days:
        if day >= date_str:
            return day
    return None


OBSERVATION_WINDOW_PRICE_STATED_DAYS = 5
OBSERVATION_WINDOW_DEFAULT_DAYS = 20


def observation_window_horizon(impact_kind):
    """改修27-1(4-5): impact_kindから観察の営業日数を機械で決める。price_statedは5営業日、
    それ以外(amount_stated・fact_only・null)は20営業日。"""
    if impact_kind == "price_stated":
        return OBSERVATION_WINDOW_PRICE_STATED_DAYS
    return OBSERVATION_WINDOW_DEFAULT_DAYS


def compute_deadline_base_date(hyp, business_days):
    """期限日を数え始める日(起算日)を返す。戻り値: (起算日 or None, Noneならその理由)。

    検査17の例外(baseline_late_inputが真の場合、要件3.5(11)): 昼号の基準価格をその日の
    うちに入力しなかった場合、期限日はbaseline_dateではなく、実際に株価を見た日
    (baseline_observed_at)から数え直す。その日が営業日でなければ、その日より後の
    最初の営業日を起点にする(元のcheck_hypothesis()の実装と同じ規則)。
    apply_observation_window()と(検算のための)check_hypothesis()の両方から呼ぶため、
    ここに1か所だけ書く。"""
    if hyp.get("baseline_late_input"):
        observed_dt = parse_datetime_assume_jst(hyp.get("baseline_observed_at"))
        if observed_dt is None:
            return None, "baseline_observed_at_unparseable"
        observed_date = observed_dt.astimezone(JST).strftime("%Y-%m-%d")
        base_date = first_business_day_on_or_after(business_days, observed_date)
        if base_date is None:
            return None, "baseline_observed_at_no_business_day"
        return base_date, None

    baseline_date = hyp.get("baseline_date")
    if not baseline_date:
        return None, "baseline_date_missing"
    return baseline_date, None


def recount_deadline_by_stepping(business_days, base_date, horizon):
    """期限日を、compute_deadline()とは別の書き方(business_daysを先頭から1日ずつ数える)で
    求め直す。検査17の検算(改修27-1・4-5)で、compute_deadline()自身のインデックス計算に
    バグがあっても気づけるようにするため、あえて実装を分けている。
    base_dateがbusiness_daysに無い、horizon分の営業日が足りない場合はNoneを返す。"""
    if base_date not in business_days:
        return None
    remaining = horizon
    for day in business_days:
        if day <= base_date:
            continue
        remaining -= 1
        if remaining == 0:
            return day
    return None


def apply_observation_window(hyps, business_days):
    """改修27-1(4-5): horizon_business_days・deadline_dateを、AIに書かせず
    impact_kind(apply_edinet_evidence()が確定済みのもの)と営業日カレンダーから機械で
    確定する。AIが書いた値は一致・不一致にかかわらず必ず上書きする。
    呼び出し側で、apply_edinet_evidence()(impact_kindを確定する)より後、
    check_hypothesis()のループより前に実行すること。

    戻り値: {"horizon_overridden": 上書きで値が変わった件数,
             "deadline_uncomputable": 期限日を計算できなかった件数
             (この場合はdeadline_dateをnullにする。会社を消すのはこの場合だけ)}。"""
    counts = {"horizon_overridden": 0, "deadline_uncomputable": 0}
    for hyp in hyps:
        old_horizon = hyp.get("horizon_business_days")
        old_deadline = hyp.get("deadline_date")

        horizon = observation_window_horizon(hyp.get("impact_kind"))
        base_date, _reason = compute_deadline_base_date(hyp, business_days)
        deadline = compute_deadline(business_days, base_date, horizon) if base_date is not None else None

        hyp["horizon_business_days"] = horizon
        hyp["deadline_date"] = deadline

        if deadline is None:
            counts["deadline_uncomputable"] += 1
        if old_horizon != horizon or old_deadline != deadline:
            counts["horizon_overridden"] += 1
    return counts


LINK_ONLY_USAGE = "link_only"


NAME_MATCH_STAGES = ("raw", "nfkc", "match_name")


def build_longer_name_index(codelist_rows):
    """改修27-2第5回(S5・Q6): 「より長い別の社名の一部としてしか出ていない」を見分けるために、
    コードリスト(EDINETの提出者名。上場・非上場を問わず全件)から、段階ごとの「長い社名の
    候補」の一覧を作る。コードリストが読めない(None)ならNoneを返す(この判定を飛ばす)。

    各段階の候補は、その段階の比べ方にそろえた書き方で持つ(法人格つきと、法人格を除いた
    書き方の両方)。会社ごとの照合名(own)も添える(判定の相手が自分自身の別表記
    (例: 株式会社ニックスとニックス)のとき、長い別の社名とみなさないため)。
      raw       : 提出者名そのまま / 法人格を除いたもの
      nfkc      : 上をNFKCにしたもの
      match_name: 下段の選定と同じ照合名(pick_industry_companies._normalize_match_name)
    戻り値: {"raw": [(候補, own)...], "nfkc": [...], "match_name": [...]}。"""
    if codelist_rows is None:
        return None
    index = {stage: [] for stage in NAME_MATCH_STAGES}
    seen = {stage: set() for stage in NAME_MATCH_STAGES}

    def add(stage, form, own):
        if form and (form, own) not in seen[stage]:
            seen[stage].add((form, own))
            index[stage].append((form, own))

    for row in codelist_rows:
        name = (row.get(edinet_codelist.COL_FILER_NAME) or "").strip()
        if not name:
            continue
        own = pick_industry_companies._normalize_match_name(name)
        stripped = name
        for token in edinet_codelist.CORPORATE_DESIGNATORS:
            stripped = stripped.replace(token, "")
        for form in (name, stripped):
            add("raw", form, own)
            add("nfkc", unicodedata.normalize("NFKC", form), own)
        add("match_name", own, own)
    return index


def _all_indexes(haystack, needle):
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return
        yield idx
        start = idx + 1


def _occurrence_inside_longer_name(haystack, idx, needle, longer_forms):
    """haystackのidx位置に出ているneedleが、longer_formsのどれか(needleを含む、より長い
    社名)の一部になっているかを返す(その長い社名がその位置を覆って本文に実在する場合)。"""
    for longer in longer_forms:
        offset = 0
        while True:
            pos = longer.find(needle, offset)
            if pos == -1:
                break
            begin = idx - pos
            if begin >= 0 and haystack[begin:begin + len(longer)] == longer:
                return True
            offset = pos + 1
    return False


def find_company_name_stage(company_name, body_text, longer_name_index=None, generic_words=None):
    """改修27-2第5回(S5): 上段の会社名が、出典本文に出ているかを、次の3段階で順に試す。
    最初に見つかった段階を返す(本文は、EDINETならタグ除去済みのもの)。
      1. raw       : company_nameをそのまま探す
      2. nfkc      : 社名と本文の両方をNFKCにそろえて探す(全角・半角の違いを吸収)
      3. match_name: 下段の選定と同じ照合名(pick_industry_companies._normalize_match_name。
                     法人格・中黒・長音・ハイフン・空白を除き、大文字化)で探す。
                     直後の文字の確認(_is_word_forming)も下段の選定と同じ規則。
                     一般語辞書(generic_words)に載る照合名は、この段階では使わない
    longer_name_index(build_longer_name_index()の結果)があるときは、どの段階でも、
    見つかった箇所がすべて「より長い別の社名(コードリスト)の一部」なら、その段階は
    一致としない(1か所でも単独で出ていれば一致)。longer_name_indexがNoneなら、この判定は
    行わない(コードリストが読めない日)。

    戻り値: (見つかった段階 or None, 長い社名の一部としてしか見つからなかった段階があったか)。"""
    if not company_name:
        return None, False
    if generic_words is None:
        generic_words = pick_industry_companies.load_generic_words()
    own_match_name = pick_industry_companies._normalize_match_name(company_name)
    only_in_longer = False
    for stage in NAME_MATCH_STAGES:
        accept_end = None
        if stage == "raw":
            haystack, needle = body_text, company_name
        elif stage == "nfkc":
            haystack = unicodedata.normalize("NFKC", body_text)
            needle = unicodedata.normalize("NFKC", company_name)
        else:
            needle = own_match_name
            if not needle or needle in generic_words:
                continue
            haystack = pick_industry_companies._normalize_match_name(body_text)
            accept_end = lambda end, h=haystack: not pick_industry_companies._is_word_forming(h[end] if end < len(h) else None)
        if not needle:
            continue
        occurrences = [
            i for i in _all_indexes(haystack, needle)
            if accept_end is None or accept_end(i + len(needle))
        ]
        if not occurrences:
            continue
        if longer_name_index is None:
            return stage, False
        longer_forms = [
            form for form, own in longer_name_index[stage]
            if form != needle and needle in form and own != own_match_name
        ]
        if any(not _occurrence_inside_longer_name(haystack, i, needle, longer_forms) for i in occurrences):
            return stage, False
        only_in_longer = True
    return None, only_in_longer


def check_evidence_source_ref_detail(hyp, sources_by_id, cache_dir, longer_name_index=None, generic_words=None):
    """検査11: evidence_grade が primary の自己申告を機械で確かめる。
    戻り値: {"reason": 合格ならNone・不合格なら理由の文字列, "stage": 会社名が見つかった段階
    (NAME_MATCH_STAGESのどれか。見つからなければNone)}。

    理由: evidence_source_ref_missing / evidence_source_not_found / evidence_source_link_only /
    evidence_source_unreadable(本文が文字コードで読めない) / evidence_company_name_not_found /
    evidence_company_name_only_in_longer_name(より長い別の社名の一部としてしか出ていない)。

    既知の限界: この検査は「その出典に会社名が出ている」ことしか確かめられない。
    無関係な文脈での言及(例えばある会社の開示資料に取引先として別の会社名が挙がっている場合など)
    を一次情報と誤認する可能性がある。
    """
    ref = hyp.get("evidence_source_ref")
    if not ref:
        return {"reason": "evidence_source_ref_missing", "stage": None}

    source = sources_by_id.get(ref)
    if source is None:
        return {"reason": "evidence_source_not_found", "stage": None}

    if source.get("usage") == LINK_ONLY_USAGE:
        return {"reason": "evidence_source_link_only", "stage": None}

    cache_path = Path(cache_dir) / f"{ref}.txt"
    if not cache_path.is_file():
        return {"reason": "evidence_source_not_found", "stage": None}

    # 改修27-1(4-9): EDINETの出典はHTMLタグを取り除いた本文で会社名を探す。
    body_text = read_source_body_for_checks(cache_path, source)
    if body_text is None:
        return {"reason": "evidence_source_unreadable", "stage": None}

    stage, only_in_longer = find_company_name_stage(
        hyp.get("company_name"), body_text, longer_name_index, generic_words,
    )
    if stage is not None:
        return {"reason": None, "stage": stage}
    if only_in_longer:
        return {"reason": "evidence_company_name_only_in_longer_name", "stage": None}
    return {"reason": "evidence_company_name_not_found", "stage": None}


def check_evidence_source_ref(hyp, sources_by_id, cache_dir, longer_name_index=None, generic_words=None):
    """検査11(理由だけを返す版): 合格ならNone、不合格なら理由の文字列。
    詳しい結果(見つかった段階)はcheck_evidence_source_ref_detail()。"""
    return check_evidence_source_ref_detail(hyp, sources_by_id, cache_dir, longer_name_index, generic_words)["reason"]


EDINET_DOCLIST_FILENAMES = {"today": "SRC-EDINET-LIST.json", "prev": "SRC-EDINET-LIST-PREV.json"}


def load_edinet_companies(cache_dir):
    """検査13(証券コードの確認)・apply_edinet_evidence()の書類の引き当てで使う、
    EDINET書類一覧から作った「会社名→証券コード」等の対応を読む。

    改修27-1(4-4): --cache フォルダの中の SRC-EDINET-LIST.json(号の日付の一覧)と
    SRC-EDINET-LIST-PREV.json(直前の営業日の一覧)の両方を読み、合わせて1つの配列に
    する。片方しか読めなければ、読めた方だけで続ける(号は止めない。呼び出し側で
    読めなかった日付をedinet_doclist_partialとして記録する)。

    .cache/edinet/companies.json を読む経路は改修27-1(4-4のQ4回答)で廃止した。
    「一覧ファイル自体は今日取得したはずなのに、前回実行時の古い一覧を黙って使って
    しまう」危険があったため。

    戻り値: (companies配列 or None, {"today": bool, "prev": bool}=それぞれ読めたか)。
    両方とも読めなければ companies は None(=検査13の条件3もapply_edinet_evidence()も
    適用しない、従来の「一覧が無い」扱いと同じ)。"""
    companies = []
    availability = {"today": False, "prev": False}
    for key, filename in EDINET_DOCLIST_FILENAMES.items():
        path = Path(cache_dir) / filename
        if not path.is_file():
            continue
        try:
            raw_doc = load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        day_companies, _reason_counts = edinet_fetch.build_companies(raw_doc)
        companies.extend(day_companies)
        availability[key] = True

    if not availability["today"] and not availability["prev"]:
        return None, availability
    return companies, availability


def last_business_day_before(business_days, date_str):
    """business_days(昇順)の中から、date_strより前(date_str自身は含まない)で
    最後の営業日を返す。無ければNone。改修27-1(4-4)で、edinet_doclist_partialに
    記録する「直前の営業日」の日付を出すために使う。"""
    result = None
    for day in business_days:
        if day >= date_str:
            break
        result = day
    return result


def format_edinet_submit_datetime(submit_date_time_raw):
    """改修27-1(4-2): EDINETのsubmitDateTime(「YYYY-MM-DD hh:mm」形式、時差の表記なし。
    EDINET API仕様書v2)を、日本時間の+09:00付きISO8601文字列に変える。
    読み取れなければNoneを返す。"""
    if not submit_date_time_raw:
        return None
    try:
        naive = dt.datetime.strptime(submit_date_time_raw, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None
    return naive.replace(tzinfo=JST).isoformat()


# 改修27-1第3回の小さな修正1: 書類一覧そのもの(4-3)として扱うのは、source_idが
# この2つのどちらかの出典だけにする。edinet_fetch.pyの --out に指定するファイル名
# (4-4のコマンド例)と同じ名前。
EDINET_DOCLIST_SOURCE_IDS = {"SRC-EDINET-LIST", "SRC-EDINET-LIST-PREV"}


def apply_edinet_source_published_at(edition, cache_dir, edinet_companies):
    """改修27-1(4-2・4-3): EDINETの出典のpublished_atを、AIの自己申告ではなく機械で
    書き込む(AIの値は使わない。一致・不一致にかかわらず必ず上書きする)。EDINET以外の
    出典には触れない(usageを表で上書きするapply_source_policy()と同じく、判定より前に
    行う。検査10・検査36がpublished_at/published_date_onlyを読むより前に呼ぶこと)。

      ・個々の書類(urlに書類管理番号を含む。4-2): 書類一覧のsubmitDateTimeから
        時刻まで書き込む。一覧が読めない・その書類IDが一覧に見つからない場合は
        published_atをnullにする(nullになった件数は分からないため上書き件数だけ数える。
        個々の理由は数えない)。
      ・書類一覧そのもの(urlは書類管理番号を含まないEDINETのURL、かつsource_idが
        EDINET_DOCLIST_SOURCE_IDSのどちらか。4-3): 出典と同じ名前(source_id)の
        キャッシュファイル({source_id}.json)を読み、その取得条件
        (metadata.parameter.date)を日付だけ書き込み、published_date_only を真にする。
        ファイルが読めなければpublished_atをnullにし、published_date_onlyは立てない。
      ・改修27-1第3回の小さな修正1: urlは書類管理番号を含まないEDINETのURLだが、
        source_idが一覧の2つのどちらでもない出典(会社の検索ページなど)は、一覧
        ではないと判断し、published_atには一切触れない(AIの値をそのまま残す)。
        件数だけedinet_other_url_hitsとして数える。

    戻り値: (published_atの値が変わった出典の件数, 一覧でも個々の書類でもない
    EDINETのURLだった出典の件数)。"""
    doc_by_id = {}
    if edinet_companies is not None:
        for c in edinet_companies:
            doc_id = c.get("doc_id")
            if doc_id:
                doc_by_id[doc_id] = c

    overwritten = 0
    edinet_other_url_hits = 0
    for source in edition.get("sources", []):
        url = source.get("url")
        if not is_edinet_domain(url):
            continue

        doc_id = extract_edinet_doc_id(url)
        if doc_id is not None:
            record = doc_by_id.get(doc_id)
            new_published_at = format_edinet_submit_datetime(record.get("submit_date_time")) if record else None
            if source.get("published_at") != new_published_at:
                overwritten += 1
            source["published_at"] = new_published_at
            continue

        source_id = source.get("source_id")
        if source_id not in EDINET_DOCLIST_SOURCE_IDS:
            edinet_other_url_hits += 1
            continue

        list_path = Path(cache_dir) / f"{source_id}.json"
        new_published_at = None
        new_published_date_only = False
        if list_path.is_file():
            try:
                raw_doc = load_json(list_path)
            except (json.JSONDecodeError, OSError):
                raw_doc = None
            if raw_doc is not None:
                date_str = ((raw_doc.get("metadata") or {}).get("parameter") or {}).get("date")
                if date_str:
                    new_published_at = date_str
                    new_published_date_only = True

        if source.get("published_at") != new_published_at:
            overwritten += 1
        source["published_at"] = new_published_at
        source["published_date_only"] = new_published_date_only

    return overwritten, edinet_other_url_hits


_DATE_ONLY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def is_date_only_string(value):
    """改修27-2(S1): published_atが「YYYY-MM-DD」の形(時刻が無い)で、実在する日付かどうか。
    時刻付き(2026-09-18T16:03:00+09:00など)・null・文字列でないもの・実在しない日付
    (2026-13-45など)は偽。前後の空白は許さない(そのままの形だけを日付だけとみなす)。"""
    if not isinstance(value, str) or not _DATE_ONLY_RE.match(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def apply_published_date_only(edition):
    """改修27-2(S1): すべての出典のpublished_date_onlyを、AIの自己申告ではなく
    published_atの形から機械で書き込む(AIが書いてきても必ず上書きする)。
    「YYYY-MM-DD」の形なら真、時刻付き・nullなら偽(キーは必ず書く)。
    書類一覧の2つ(apply_edinet_source_published_at()が日付だけを書いたもの)も、
    この規則でそのまま真になる(一覧が読めずpublished_atがnullのときは偽)。
    apply_edinet_source_published_at()より後、検査10・検査36より前に呼ぶこと。
    戻り値: {"count": 真にした出典の数, "source_ids": [...]}。"""
    source_ids = []
    for source in edition.get("sources", []):
        is_date_only = is_date_only_string(source.get("published_at"))
        source["published_date_only"] = is_date_only
        if is_date_only:
            source_ids.append(source.get("source_id"))
    return {"count": len(source_ids), "source_ids": source_ids}


def collect_edinet_doc_files(edition, cache_dir):
    """改修27-1(4-8): EDINETの個々の書類を指す出典について、edinet_fetch.pyが
    書き出したSRC-xxx.files.json(どのファイルを本文として選んだか、
    _select_document_files()参照)を読み、記録用にそのまま写す。

    ファイルが無い・読めない出典は記録に含めない(選ばれた本文自体はキャッシュに
    既にあるものを使うだけで、この記録が読めなくても号は止めない)。

    戻り値: {source_id: {"files": [...], "fallback": bool}, ...}。"""
    result = {}
    for source in edition.get("sources", []):
        if extract_edinet_doc_id(source.get("url")) is None:
            continue
        source_id = source.get("source_id")
        if not source_id:
            continue
        files_path = Path(cache_dir) / f"{source_id}.files.json"
        if not files_path.is_file():
            continue
        try:
            record = load_json(files_path)
        except (json.JSONDecodeError, OSError):
            continue
        result[source_id] = {
            "files": record.get("files"),
            "fallback": record.get("fallback"),
        }
    return result


def is_edinet_domain(url):
    """URLのホスト名がEDINET(edinet-fsa.go.jp)のものかどうかを判定する。
    disclosure2.edinet-fsa.go.jp・api.edinet-fsa.go.jpのどちらもEDINETのドメインとして扱う。"""
    if not url:
        return False
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host == "edinet-fsa.go.jp" or host.endswith(".edinet-fsa.go.jp")


def extract_edinet_doc_id(url):
    """出典のURLからEDINETの書類管理番号を取り出す。EDINET_DOC_ID_REの形に合わなければ
    (EDINETのドメインかどうかにかかわらず)Noneを返す。"""
    if not url:
        return None
    m = EDINET_DOC_ID_RE.match(url)
    return m.group(1) if m else None


# 改修27-1第6回: 出典のurl(api/v2/documents/{書類管理番号}?type=1)はEDINETのAPI用の
# アドレスで、ブラウザで開くと「規定外操作が行われました」となり読者は開けない
# (依頼者がPCで確認済み)。読者向けの閲覧画面は別のURLになる。urlそのものは
# 書類管理番号の取り出し・提出時刻の書き込み・各検査の目印として使い続けるため
# 変えず、読者向けのURLはview_urlという別の項目に分けて持つ。
EDINET_DOC_VIEW_URL_TMPL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}"
EDINET_VIEW_TOP_URL = "https://disclosure2.edinet-fsa.go.jp/"


def compute_edinet_view_url(source):
    """改修27-1第6回: EDINETの出典について、読者が実際に開けるURL(view_url)を
    機械で決める。
      ・個々の書類(urlから書類管理番号が取れる) → その書類の閲覧画面
        (WZEK0040.aspx?{書類管理番号})
      ・書類一覧そのもの(source_idがEDINET_DOCLIST_SOURCE_IDSのどちらか) →
        読者が開ける一覧専用のページが無いため、EDINET閲覧サイトのトップ
      ・それ以外のEDINETのURL(edinet_other_url_hitsに数えるもの)、EDINET以外の
        出典 → None(view_urlは書かない。attribution等は今までどおりurlを使う)
    """
    url = source.get("url")
    if not is_edinet_domain(url):
        return None
    doc_id = extract_edinet_doc_id(url)
    if doc_id is not None:
        return EDINET_DOC_VIEW_URL_TMPL.format(doc_id=doc_id)
    if source.get("source_id") in EDINET_DOCLIST_SOURCE_IDS:
        return EDINET_VIEW_TOP_URL
    return None


def apply_edinet_view_url(edition):
    """改修27-1第6回: すべての出典のview_urlを、AIの自己申告ではなく機械で書き込む
    (AIが書いてきても必ず上書きする。EDINET以外・対象外のEDINETのURLはNoneにする)。
    apply_source_attribution()より前に呼ぶこと(attribution/processing_noteの
    {url}にview_urlを使うため)。
    戻り値: view_urlが書かれた(Noneでない)出典の件数。"""
    count = 0
    for source in edition.get("sources", []):
        view_url = compute_edinet_view_url(source)
        source["view_url"] = view_url
        if view_url is not None:
            count += 1
    return count


def compute_tob_side(hyp, record, new_role, codelist_rows, counts):
    """改修27-1(4-10、Q8の回答): 書類種別コードが240〜280(公開買付関係、TOB_DOC_TYPE_CODES)
    の書類について、tob_sideを機械で決める。
      ・提出者本人(evidence_role=filer_self) → bidder
      ・companyのEDINETコードがsubjectEdinetCodeと一致(evidence_role=mentioned) → target
      ・subjectEdinetCodeが取れない場合は、今の規則(mentioned→target)に倒し、
        その件数をtob_side_subject_code_missingとして数える
      ・それ以外(対象書類でない、コードが不一致、companyのEDINETコードが分からない)はnull
    company(hypothesis)自身のEDINETコードは、参照した書類の提出者(record)とは別に、
    コードリスト(codelist_rows)からcompany_nameで引き直す(record.edinet_codeは
    「この書類を提出した会社」のコードであり、company_nameがmentionedのときは別の会社の
    コードのため)。"""
    if record is None:
        return None
    doc_type_code = record.get("doc_type_code")
    if doc_type_code not in TOB_DOC_TYPE_CODES:
        return None

    if new_role == "filer_self":
        return "bidder"

    subject_edinet_code = record.get("subject_edinet_code")
    if not subject_edinet_code:
        counts["tob_side_subject_code_missing"] += 1
        return "target"

    company_edinet_code = None
    if codelist_rows is not None:
        match = edinet_codelist.find_company_by_name(hyp.get("company_name"), codelist_rows, [])
        if match is not None:
            company_edinet_code = match.get("edinet_code")

    if company_edinet_code is not None and company_edinet_code == subject_edinet_code:
        return "target"
    return None


def apply_edinet_evidence(hyps, sources_by_id, edinet_companies, codelist_rows):
    """上段の仮説ごとに、evidence_filer_name/evidence_doc_type/evidence_role/impact_kind/
    impact_kind_source/auto_check_target/tob_sideを、AIに書かせず出典URLの書類管理番号から
    EDINET書類一覧を引いて確定する。AIが書いた値は一致・不一致にかかわらず必ず上書きする
    (判定に使ってよいのはスクリプトが自分で決めた値だけのため)。impact_reasonはAIが書いた
    値のまま上書きしない。

    戻り値: verificationに記録する件数・内訳をまとめたdict。"""
    counts = {
        "evidence_filer_name_overridden": 0,
        "evidence_doc_type_overridden": 0,
        "impact_kind_overridden": 0,
        "impact_kind_source_counts": {},
        "impact_kind_undetermined_by_doc_type": {},
        "edinet_url_unparsed": 0,
        # 改修27-1(4-10): tob_sideを決めるときにsubjectEdinetCodeが取れず、
        # 今の規則(mentioned→target)に倒した件数。
        "tob_side_subject_code_missing": 0,
    }

    doc_by_id = {}
    if edinet_companies is not None:
        for c in edinet_companies:
            doc_id = c.get("doc_id")
            if doc_id:
                doc_by_id[doc_id] = c

    for hyp in hyps:
        company_name = normalize_filer_name_for_compare(hyp.get("company_name"))
        ref = hyp.get("evidence_source_ref")
        source = sources_by_id.get(ref) if ref else None
        url = source.get("url") if source else None

        doc_id = extract_edinet_doc_id(url)
        if doc_id is None and is_edinet_domain(url):
            counts["edinet_url_unparsed"] += 1

        record = doc_by_id.get(doc_id) if doc_id else None

        old_filer_name = hyp.get("evidence_filer_name")
        old_doc_type = hyp.get("evidence_doc_type")
        old_impact_kind = hyp.get("impact_kind")

        if edinet_companies is None:
            new_filer_name = None
            new_doc_type = None
            new_role = "mentioned"
            new_impact_kind = None
            new_impact_kind_source = "doclist_unavailable"
        elif record is not None:
            new_filer_name = record.get("filer_name")
            new_doc_type = record.get("doc_description")
            filer_name_normalized = normalize_filer_name_for_compare(new_filer_name)
            new_role = "filer_self" if (company_name and filer_name_normalized and company_name == filer_name_normalized) else "mentioned"
            doc_type_code = record.get("doc_type_code")
            if doc_type_code in EDINET_DOC_TYPE_IMPACT_KIND:
                new_impact_kind = EDINET_DOC_TYPE_IMPACT_KIND[doc_type_code]
                new_impact_kind_source = "edinet_doctype"
            else:
                new_impact_kind = None
                new_impact_kind_source = "edinet_doctype_unmapped"
                counts["impact_kind_undetermined_by_doc_type"][doc_type_code] = (
                    counts["impact_kind_undetermined_by_doc_type"].get(doc_type_code, 0) + 1
                )
        else:
            new_filer_name = None
            new_doc_type = None
            new_role = "mentioned"
            new_impact_kind = None
            new_impact_kind_source = "edinet_doc_not_found" if doc_id is not None else "no_edinet_doc"

        hyp["evidence_filer_name"] = new_filer_name
        hyp["evidence_doc_type"] = new_doc_type
        hyp["evidence_role"] = new_role
        hyp["impact_kind"] = new_impact_kind
        hyp["impact_kind_source"] = new_impact_kind_source

        # 検査20相当のTOB例外(condition b): 買い付ける側が提出者のため対象会社はmentionedに
        # なるが、機械が書類種別からprice_statedと決めた場合に限り自動対象に含める。AIが
        # impact_kindにprice_statedと書いただけでは対象にならない(impact_kind_sourceの
        # 条件が必ず付く)。
        cond_a = new_impact_kind in ("price_stated", "amount_stated") and new_role == "filer_self"
        cond_b = new_impact_kind == "price_stated" and new_impact_kind_source == "edinet_doctype"
        hyp["auto_check_target"] = bool(cond_a or cond_b)

        # 改修27-1(4-10): tob_sideも機械で決める(AIは書かない)。
        hyp["tob_side"] = compute_tob_side(hyp, record, new_role, codelist_rows, counts)

        if old_filer_name != new_filer_name:
            counts["evidence_filer_name_overridden"] += 1
        if old_doc_type != new_doc_type:
            counts["evidence_doc_type_overridden"] += 1
        if old_impact_kind != new_impact_kind:
            counts["impact_kind_overridden"] += 1
        counts["impact_kind_source_counts"][new_impact_kind_source] = (
            counts["impact_kind_source_counts"].get(new_impact_kind_source, 0) + 1
        )

    return counts


def check_ticker_fields(hyp, edinet_companies):
    """検査13: ticker/ticker_sourceの確認。次のいずれかに当たったら不合格(None以外を返す)。
      1. ticker が null・空、または証券コードの形(edinet_fetch.is_valid_ticker、英字混在を含む)
         に合わない
      2. ticker_source が null・空
      3. ticker_source が edinet_seccode の仮説について、EDINET書類一覧が読める場合に、
         company_name と ticker の組がその一覧に見つからない。
         (ticker_source が edinet_codelist の仮説はここでは対象外。検査31
         (check_hypothesis_ticker_match)がコードリストとの突き合わせを担当する。
         EDINET書類一覧が読めない(edinet_companiesがNone)場合も、この条件は適用しない)
      4. links.price_history のURL「https://finance.yahoo.co.jp/quote/{証券コード}.T/history」の
         /quote/ と .T の間の文字列が ticker と違う。その形に合わないURLは比較せず飛ばす。
         (ticker_source の値や edinet_companies の有無に関係なく、常に適用する。仮説自身の
         2つの値を比べるだけで、EDINETのデータを必要としないため)
    証券コードの形の判定はedinet_fetch.is_valid_ticker()を使う(derive_ticker()と同じ判定を
    2か所に書かないため)。"""
    ticker = hyp.get("ticker")
    if not (isinstance(ticker, str) and edinet_fetch.is_valid_ticker(ticker)):
        return "ticker_missing"

    if not hyp.get("ticker_source"):
        return "ticker_source_missing"

    if hyp.get("ticker_source") == "edinet_seccode" and edinet_companies is not None:
        company_name = normalize_filer_name_for_compare(hyp.get("company_name"))
        match = None
        for c in edinet_companies:
            if normalize_filer_name_for_compare(c.get("filer_name")) == company_name:
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


def run_check_hypothesis_evidence(hyps, sources_by_id, cache_dir, codelist_rows=None):
    """evidence_grade が primary の仮説だけを検査11にかけ、不合格なら仮説を削除する
    (改修27-2第5回・S5。それまでは reported への格下げだった)。文字コードで読めない場合も
    削除する(D4。理由は分けて記録)。reported・inferredの仮説は検査11の対象外で、そのまま残す。

    codelist_rows(コードリストの行。Noneなら読めない日)から長い社名の候補を作り、
    「より長い別の社名の一部としてしか出ていない」会社を一致としない。Noneのときは
    その判定だけを飛ばし、飛ばした件数(longer_name_check_skipped)を返す。

    戻り値: {"kept": 残す仮説のリスト(元の順),
             "removed": [{"hypothesis_id", "company_name", "reason"}, ...],
             "name_match_stage": {"raw": n, "nfkc": n, "match_name": n}(合格したprimaryの、最初に
                                 見つかった段階ごとの件数),
             "longer_name_check_skipped": 長い社名の判定を飛ばして検査した件数}。"""
    longer_name_index = build_longer_name_index(codelist_rows)
    generic_words = pick_industry_companies.load_generic_words()
    kept = []
    removed = []
    stage_counts = {stage: 0 for stage in NAME_MATCH_STAGES}
    skipped = 0
    for hyp in hyps:
        if hyp.get("evidence_grade") != "primary":
            kept.append(hyp)
            continue
        if longer_name_index is None:
            skipped += 1
        result = check_evidence_source_ref_detail(hyp, sources_by_id, cache_dir, longer_name_index, generic_words)
        if result["reason"]:
            removed.append({
                "hypothesis_id": hyp.get("hypothesis_id"),
                "company_name": hyp.get("company_name"),
                "reason": result["reason"],
            })
            continue
        stage_counts[result["stage"]] += 1
        kept.append(hyp)
    return {
        "kept": kept, "removed": removed, "name_match_stage": stage_counts,
        "longer_name_check_skipped": skipped,
    }


def first_business_day_after(business_days, date_str):
    """business_days(昇順)の中から、date_strより後(date_str自身は含まない)で
    最初の営業日を返す。無ければNone。first_business_day_on_or_after()と違い、
    date_strそのものは対象に含めない(>であって>=ではない)。"""
    for day in business_days:
        if day > date_str:
            return day
    return None


def check_hypothesis_listed(hyp, codelist_rows):
    """検査21(上段): ticker_sourceがedinet_codelistなのに、コードリスト上その会社が
    「上場」で証券コードありの行として見つからない場合は不合格(upper_not_listed)。

    改修27-2第7回(S8): コードリストが読めない(codelist_rowsがNone)日は、上場かどうかを
    確かめられないので、ticker_sourceがedinet_codelistの上段の会社を削除する
    (理由はupper_codelist_unavailable。upper_not_listedとは分けて記録する。
    codelist_unavailableは今までどおりverificationに記録する)。
    ticker_sourceがedinet_seccodeなどの会社は、コードリストを使わないので消さない。
    検査21(この検査)は検査31(check_hypothesis_ticker_match)より先に呼ばれるため、読めない日は
    ここで先に削除され、検査31の「適用しない」に届くのは上段のedinet_codelist以外の会社だけ。"""
    if hyp.get("ticker_source") != "edinet_codelist":
        return None
    if codelist_rows is None:
        return "upper_codelist_unavailable"
    match = edinet_codelist.find_company_by_name(hyp.get("company_name"), codelist_rows, [])
    if match is None:
        return "upper_not_listed"
    return None


def check_hypothesis_impact_reason(hyp):
    """検査23: impact_kindがprice_statedまたはamount_statedなのに、impact_reasonが
    空(null・空文字・空白のみ)なら不合格。impact_kindがfact_only・nullの仮説は
    impact_reasonが空でも正常なので対象外。"""
    if hyp.get("impact_kind") not in ("price_stated", "amount_stated"):
        return None
    impact_reason = hyp.get("impact_reason")
    if not isinstance(impact_reason, str) or not impact_reason.strip():
        return "impact_reason_missing"
    return None


def check_hypothesis_relation_text_number(hyp):
    """検査26(上段専用。下段は対象外): relation_textに半角数字が1文字でも含まれて
    いたら不合格。unicodedata.normalize("NFKC", ...)で全角数字を半角に揃えたうえで
    判定する。漢数字(一・二・三…)は対象にしない(「一部の製品」「第一種」のような
    普通の日本語まで落ちてしまうため)。
    改修31第1回(3-7): NFKCでそろえたrelation_textから、同じくNFKCでそろえたcompany_nameを取り除いてから
    数字を探す(「株式会社レオパレス２１は…」のように、社名の中の数字では消さない)。company_nameが
    空・文字でないときは、取り除かずに今までどおり判定する。"""
    relation_text = hyp.get("relation_text") or ""
    normalized = unicodedata.normalize("NFKC", relation_text)
    company_name = hyp.get("company_name")
    if isinstance(company_name, str) and company_name.strip():
        normalized = normalized.replace(unicodedata.normalize("NFKC", company_name), "")
    if re.search(r"[0-9]", normalized):
        return "relation_text_has_number"
    return None


def check_hypothesis_ticker_match(hyp, codelist_rows):
    """検査31(上段): tickerが、コードリスト上の同じ会社の証券コードと一致しなければ
    不合格。対象はticker_sourceがedinet_codelistの仮説だけ(edinet_seccode由来の仮説は
    検査13の条件3が同じ照合をしているため対象外)。コードリストが読めない場合は
    検査21と同じく適用しない。"""
    if hyp.get("ticker_source") != "edinet_codelist":
        return None
    if codelist_rows is None:
        return None
    match = edinet_codelist.find_company_by_name(hyp.get("company_name"), codelist_rows, [])
    if match is None or match.get("ticker") != hyp.get("ticker"):
        return "upper_ticker_mismatch"
    return None


def check_hypothesis_baseline(hyp, edition, business_days, extra_counts):
    """検査32: added_byがmanualでない仮説について、baseline_price_type/baseline_dateが
    号のslotから機械的に決まる値と一致するか確かめる。
    (a) baseline_price_type: morning→open, noon→observed, evening→next_open
    (b) baseline_date: morning/noon→号の日付、evening→号の日付より後の最初の営業日
    営業日一覧が空、またはeveningで期待日を求められない場合は(b)だけを飛ばし、
    飛ばした件数をextra_counts["baseline_date_check_skipped"]に数える。"""
    if hyp.get("added_by") == "manual":
        return None

    slot = edition.get("slot")
    expected_type = {"morning": "open", "noon": "observed", "evening": "next_open"}.get(slot)
    if hyp.get("baseline_price_type") != expected_type:
        return "baseline_type_mismatch"

    if slot in ("morning", "noon"):
        expected_date = edition.get("date")
    elif slot == "evening":
        expected_date = first_business_day_after(business_days, edition.get("date"))
    else:
        expected_date = None

    if expected_date is None:
        extra_counts["baseline_date_check_skipped"] += 1
        return None

    if hyp.get("baseline_date") != expected_date:
        return "baseline_date_mismatch"
    return None


def check_hypothesis_evidence_source_ref(hyp, sources_by_id):
    """検査34: evidence_source_refが空でないのに、edition["sources"]のsource_idの
    どれとも一致しなければ不合格。evidence_source_refがnull・空の仮説は対象外
    (evidence_gradeがprimaryでなければnullで正常な値のため)。"""
    ref = hyp.get("evidence_source_ref")
    if not ref:
        return None
    if ref not in sources_by_id:
        return "evidence_source_ref_not_found"
    return None


def apply_primary_evidence_line_rule(hyp, line_marks, line_refs):
    """改修31第1回(3-1、案1): primaryの会社は、line_idsの中に「確定した印がsource_number_matchで、
    かつsource_refがevidence_source_refと同じ行」が1行以上あるときだけ残す(無ければ理由
    primary_no_verified_evidence_lineを返す)。残す会社のline_idsからは、確定した印が
    source_number_matchでない行を外す(別の出典の行でも、印がsource_number_matchなら外さない)。
    primary以外の会社には何もしない。
    戻り値: (理由 or None, 外した行のリスト[{"line_id", "mark"}])。外した行は、会社を残すときだけ
    hyp["line_ids"]から実際に取り除く。"""
    if hyp.get("evidence_grade") != "primary":
        return None, []
    evidence_ref = hyp.get("evidence_source_ref")
    hyp_line_ids = hyp.get("line_ids") or []
    has_evidence_line = isinstance(evidence_ref, str) and bool(evidence_ref) and any(
        line_marks.get(lid) == "source_number_match" and line_refs.get(lid) == evidence_ref
        for lid in hyp_line_ids
    )
    if not has_evidence_line:
        return "primary_no_verified_evidence_line", []
    removed = [
        {"line_id": lid, "mark": line_marks.get(lid)}
        for lid in hyp_line_ids if line_marks.get(lid) != "source_number_match"
    ]
    if removed:
        hyp["line_ids"] = [lid for lid in hyp_line_ids if line_marks.get(lid) == "source_number_match"]
    return None, removed


def _line_refs_from_edition(edition):
    """{行ID: source_ref}。editionに行が無ければ空(check_hypothesis()を単体で呼ぶテスト向け)。"""
    if not isinstance(edition, dict) or not isinstance(edition.get("sections"), list):
        return {}
    return {line.get("line_id"): line.get("source_ref") for _s, _a, line in iter_lines(edition)}


def check_hypothesis(hyp, edition, line_ids, business_days, ng_words, sources_by_id, cache_dir,
                      edinet_companies, codelist_rows, extra_counts, line_refs=None):
    """line_ids: {行ID: 確定した印}。line_refs: {行ID: source_ref}(省略したらeditionの行から作る)。"""
    ticker_reason = check_ticker_fields(hyp, edinet_companies)
    if ticker_reason:
        return ticker_reason

    for field in REQUIRED_HYPOTHESIS_FIELDS:
        value = hyp.get(field)
        if value is None or value == "":
            return "missing_field"

    # 改修27-2第8回(S9): line_idsが空・紙面に無い行IDを含む(line_id_not_found)の判定は、
    # 検査37(run_check37)に移した(同じ条件を2か所で判定しないため)。ここには、検査37を
    # 通った仮説だけが来る。
    # 改修31第1回(3-1、案1): primaryの会社の根拠の行の規則。今までのprimary_requires_verified_line
    # (1行でもunverifiedなら消す)を置き換えた。検査37より前に置かないこと(前に置くと、検査で
    # 落とされた行・紙面に元から無い行IDを先に外してしまい、検査37が素通りになる)。
    if line_refs is None:
        line_refs = _line_refs_from_edition(edition)
    primary_reason, trimmed = apply_primary_evidence_line_rule(hyp, line_ids, line_refs)
    if primary_reason:
        return primary_reason
    if trimmed:
        extra_counts.setdefault("primary_line_ids_trimmed", []).append({
            "hypothesis_id": hyp.get("hypothesis_id"), "company_name": hyp.get("company_name"),
            "removed": trimmed,
        })

    # 改修27-1(4-5): horizon_business_days・deadline_dateはapply_observation_window()が
    # 機械で必ず埋めるため、ここでは値を作り直さない。会社を消すのはdeadline_dateが
    # 計算できなかった場合(null)だけで、それ以外は検査17を「別の書き方
    # (recount_deadline_by_stepping)で数え直し、ずれたら記録するだけ」の検算に変えた
    # (会社は消さない)。
    horizon = hyp.get("horizon_business_days")
    if not isinstance(horizon, int) or hyp.get("deadline_date") is None:
        return "deadline_date_mismatch"

    # 改修27-1第2回: ここで起算日が求められない状況は、apply_observation_window()が
    # 同じcompute_deadline_base_date()で先に判定済みのはず(その場合はdeadline_dateが
    # nullになり、上のreturnで既に処理されている)。通常の実行では、この
    # deadline_base_date is Noneの分岐には到達しない。それでも到達した場合(検査32の
    # baseline_date_check_skippedとは意味が違う不具合として)に備えて、専用のキー
    # (horizon_recount_skipped)に分けて記録する。baseline_date_check_skippedを
    # 混ぜて使わないこと(検査17の検算専用のキーと検査32の記録を混同しないため。
    # 第2回でこの取り違えを直した)。
    deadline_base_date, _base_reason = compute_deadline_base_date(hyp, business_days)
    if deadline_base_date is None:
        extra_counts["horizon_recount_skipped"] = extra_counts.get("horizon_recount_skipped", 0) + 1
    else:
        recount = recount_deadline_by_stepping(business_days, deadline_base_date, horizon)
        if recount != hyp.get("deadline_date"):
            extra_counts["horizon_recount_mismatch"] = extra_counts.get("horizon_recount_mismatch", 0) + 1

    relation_text = hyp.get("relation_text", "")
    for word in ng_words:
        if word in relation_text:
            return "relation_text_recommendation"
    for banned in ("プラス", "マイナス", "好材料", "悪材料"):
        if banned in relation_text:
            return "relation_text_conclusive_word"

    # 要件定義書v12 5.6の順序(9→11→12→13→17→21→23→26→31→32→34)に合わせて、
    # 新しい検査21・23・26・31・32・34をこの順で追加する(タスク16-2c-1)。
    listed_reason = check_hypothesis_listed(hyp, codelist_rows)
    if listed_reason:
        return listed_reason

    impact_reason_reason = check_hypothesis_impact_reason(hyp)
    if impact_reason_reason:
        return impact_reason_reason

    relation_number_reason = check_hypothesis_relation_text_number(hyp)
    if relation_number_reason:
        return relation_number_reason

    ticker_match_reason = check_hypothesis_ticker_match(hyp, codelist_rows)
    if ticker_match_reason:
        return ticker_match_reason

    baseline_reason = check_hypothesis_baseline(hyp, edition, business_days, extra_counts)
    if baseline_reason:
        return baseline_reason

    evidence_ref_reason = check_hypothesis_evidence_source_ref(hyp, sources_by_id)
    if evidence_ref_reason:
        return evidence_ref_reason

    return None


LINE_FACT_MARKS = ("source_number_match", "reported_unverified")
CHECK37_REASONS = (
    "article_not_found", "line_ids_empty", "line_id_never_existed",
    "line_id_removed_by_check", "line_not_in_article", "no_fact_line", "primary_ref_mismatch",
)
LINE_DROP_CHECK_NAMES = {
    "stop_words": "check7_stop_words",
    "published_date_not_found": "check36_published_date_not_found",
    "published_timed_date_not_found": "check36_published_timed_date_not_found",
    "stale_source": "check10_stale_or_unknown_published_at",
}
# 改修31第1回(3-2、案A): 検査37で、仮説のline_idsから外すだけにする(会社は消さない)検査。
# 日付・鮮度の検査で落とされた行。停止語(check7_stop_words)で落とされた行と、どの検査で
# 落ちたか分からない行("unknown")は、今までどおりline_id_removed_by_checkで会社を消す。
CHECK37_TRIMMABLE_DROP_CHECKS = frozenset({
    LINE_DROP_CHECK_NAMES["published_date_not_found"],
    LINE_DROP_CHECK_NAMES["published_timed_date_not_found"],
    LINE_DROP_CHECK_NAMES["stale_source"],
})


def line_id_set(edition):
    """紙面にいま載っている行のline_idの集合。"""
    return {line.get("line_id") for _s, _a, line in iter_lines(edition)}


def record_dropped_lines(edition, known_line_ids, dropped_by, check_name):
    """改修27-2第8回(S9): 行を落とす検査(検査7・検査36・検査10)の直後に呼ぶ。known_line_ids
    (まだ紙面にあると分かっている行ID。更新される)のうち、いま紙面から無くなった行IDを、
    どの検査で落ちたか(check_name)とともにdropped_byに記録する。検査37が、仮説のline_idsの
    「元から無い行ID」と「検査で落とされた行ID」を区別するために使う。"""
    now = line_id_set(edition)
    newly_dropped = known_line_ids - now
    for line_id in newly_dropped:
        dropped_by[line_id] = check_name
    known_line_ids -= newly_dropped
    return newly_dropped


def check37_trimmed_line_ids(hyp, original_line_ids, line_marks, dropped_by):
    """改修31第1回(3-2): 仮説のline_idsのうち、日付・鮮度の検査(CHECK37_TRIMMABLE_DROP_CHECKS)で
    紙面から落とされた行。戻り値: {行ID: 落とした検査の名前}(line_idsの順)。"""
    raw_line_ids = hyp.get("line_ids")
    trimmed = {}
    for lid in raw_line_ids if isinstance(raw_line_ids, list) else []:
        if (isinstance(lid, str) and lid in original_line_ids and lid not in line_marks
                and dropped_by.get(lid) in CHECK37_TRIMMABLE_DROP_CHECKS):
            trimmed[lid] = dropped_by[lid]
    return trimmed


def check37_reasons(hyp, article_ids, line_marks, line_refs, original_line_ids, dropped_by, line_articles):
    """検査37(改修27-2第8回・S9): 上段の仮説の根拠が、その記事の行と出典から出ているか。
    次のどれかに当たる理由をすべて返す(空なら合格)。行の検査がすべて終わった後の状態で判定する。
      article_not_found        : article_idが空・null、または紙面に実在しない
      line_ids_empty           : line_idsが空
      line_id_never_existed    : line_idsに、AIが書いた紙面に元から無い行IDが1つでもある
      line_id_removed_by_check : line_idsに、検査で落とされた行IDが1つでもある(どの検査かも記録)
      line_not_in_article      : article_idが実在し、かつline_idsのうちいま紙面に残っている行に、
                                 その記事以外の記事の行が1つでもある(当たった行IDと、その行が
                                 実際に属している記事IDを記録。article_not_foundのときは判定しない)
      no_fact_line             : line_idsの行の確定したmarkが、どれも事実系
                                 (source_number_match・reported_unverified)でない
      primary_ref_mismatch     : evidence_gradeがprimaryなのに、evidence_source_refが、line_idsの
                                 どの行のsource_refにも含まれない
    line_articles: {行ID: その行が載っている記事IDの集合}(run_check37が作る)。同じ行IDが2つ以上の
    記事に載っている場合は、そのどれか1つが仮説の記事なら「その記事の行」とみなす。
    no_fact_line・primary_ref_mismatchは、line_idsの行のうちいま紙面に残っているものだけで判定し、
    1つも残っていないときは判定しない(その場合の原因は上の3つの理由で記録済みのため)。
    改修31第1回(3-2、案A): 日付・鮮度の検査(CHECK37_TRIMMABLE_DROP_CHECKS)で落とされた行は、
    line_id_removed_by_checkにせず、line_idsから外すだけにする(外す行はcheck37_trimmed_line_ids()。
    実際に外すのはrun_check37)。外した後のline_idsでline_not_in_article・no_fact_line・
    primary_ref_mismatchを判定する。外した結果line_idsが空になったら、line_ids_emptyに
    "emptied_by_check_trim": True と外した行・検査の名前を付けて返す。
    戻り値: [{"reason": 理由, ...詳細}, ...](CHECK37_REASONSの順)。"""
    reasons = []
    article_id = hyp.get("article_id")
    if not isinstance(article_id, str) or not article_id or article_id not in article_ids:
        reasons.append({"reason": "article_not_found", "article_id": article_id})

    raw_line_ids = hyp.get("line_ids")
    line_ids = raw_line_ids if isinstance(raw_line_ids, list) else []
    if not line_ids:
        reasons.append({"reason": "line_ids_empty"})
        return reasons

    def hashable(value):
        return isinstance(value, str)

    trimmed = check37_trimmed_line_ids(hyp, original_line_ids, line_marks, dropped_by)
    remaining = [l for l in line_ids if not (hashable(l) and l in trimmed)]
    if not remaining:
        reasons.append({
            "reason": "line_ids_empty", "emptied_by_check_trim": True,
            "line_ids": list(trimmed), "checks": dict(trimmed),
        })
        return reasons

    never_existed = [l for l in remaining if not hashable(l) or l not in original_line_ids]
    removed = [l for l in remaining if hashable(l) and l in original_line_ids and l not in line_marks]
    if never_existed:
        reasons.append({"reason": "line_id_never_existed", "line_ids": never_existed})
    if removed:
        reasons.append({
            "reason": "line_id_removed_by_check", "line_ids": removed,
            "checks": {l: dropped_by.get(l, "unknown") for l in removed},
        })
    surviving = [l for l in remaining if hashable(l) and l in line_marks]
    if surviving and isinstance(article_id, str) and article_id in article_ids:   # article_not_foundのときは判定しない(article_idが文字でなくても落ちない)
        elsewhere = [l for l in surviving if article_id not in line_articles.get(l, set())]
        if elsewhere:
            reasons.append({
                "reason": "line_not_in_article", "article_id": article_id, "line_ids": elsewhere,
                "actual_article_ids": {l: sorted(line_articles.get(l, set())) for l in elsewhere},
            })
    if surviving:
        if not any(line_marks.get(l) in LINE_FACT_MARKS for l in surviving):
            reasons.append({"reason": "no_fact_line", "line_ids": surviving})
        if hyp.get("evidence_grade") == "primary":
            source_refs = {line_refs.get(l) for l in surviving if line_refs.get(l)}
            evidence_ref = hyp.get("evidence_source_ref")
            if not isinstance(evidence_ref, str) or evidence_ref not in source_refs:   # 文字でない値でも落ちない
                reasons.append({
                    "reason": "primary_ref_mismatch", "evidence_source_ref": hyp.get("evidence_source_ref"),
                    "line_ids": surviving,
                })
    return reasons


def run_check37(hyps, edition, original_line_ids, dropped_by):
    """検査37を上段の仮説すべてにかけ、1つでも理由に当たった仮説を削除する。複数の理由に当たる仮説は、
    すべての理由を記録し、削除は1件と数える。
    改修31第1回(3-2): 残す仮説のline_idsから、日付・鮮度の検査で落とされた行を実際に取り除き、
    "trimmed"に記録する。他の理由で消える仮説にも、外すだけの行があれば"line_ids_trimmed_by_check"
    ({行ID: 検査の名前})を書き残す。
    戻り値: {"kept": 残す仮説, "removed": [{"hypothesis_id", "company_name", "article_id", "line_ids",
             "reasons": [check37_reasons()の各理由], ("line_ids_trimmed_by_check")}, ...],
             "trimmed": [{"hypothesis_id", "company_name", "removed": [{"line_id", "check"}]}, ...]}。"""
    article_ids = {a.get("article_id") for _s, a, _l in iter_lines_and_empty_articles(edition)}
    line_marks = {}
    line_refs = {}
    line_articles = {}
    for _section, article, line in iter_lines(edition):
        line_marks[line.get("line_id")] = line.get("mark")
        line_refs[line.get("line_id")] = line.get("source_ref")
        line_articles.setdefault(line.get("line_id"), set()).add(article.get("article_id"))
    kept = []
    removed = []
    trimmed_records = []
    for hyp in hyps:
        reasons = check37_reasons(hyp, article_ids, line_marks, line_refs, original_line_ids, dropped_by, line_articles)
        trimmed = check37_trimmed_line_ids(hyp, original_line_ids, line_marks, dropped_by)
        if reasons:
            record = {
                "hypothesis_id": hyp.get("hypothesis_id"), "company_name": hyp.get("company_name"),
                "article_id": hyp.get("article_id"), "line_ids": hyp.get("line_ids"), "reasons": reasons,
            }
            if trimmed:
                # 改修31第1回(3-2): 他の理由で消える会社でも、日付・鮮度の検査で落とされた行(外すだけの行)を書き残す。
                record["line_ids_trimmed_by_check"] = dict(trimmed)
            removed.append(record)
        else:
            if trimmed:
                hyp["line_ids"] = [l for l in hyp["line_ids"] if l not in trimmed]
                trimmed_records.append({
                    "hypothesis_id": hyp.get("hypothesis_id"), "company_name": hyp.get("company_name"),
                    "removed": [{"line_id": l, "check": c} for l, c in trimmed.items()],
                })
            kept.append(hyp)
    return {"kept": kept, "removed": removed, "trimmed": trimmed_records}


def iter_lines_and_empty_articles(edition):
    """行が0件の記事も含めて、(section, article, None)を返す(article_idの実在確認用)。"""
    for section in edition.get("sections", []):
        for article in section.get("articles", []):
            yield section, article, None


def run_hypothesis_checks(hypotheses_doc, edition, business_days, ng_words, cache_dir, edinet_companies, codelist_rows,
                          line_drop_info=None):
    """line_drop_info: 検査37用。{"original_line_ids": AIが書いた紙面の行ID全部, "dropped_by":
    {行ID: 落とした検査の名前}}。省略したときは、いま紙面にある行IDだけを「元からあった行」とみなす
    (検査で落とされた行は無かったことになる)。"""
    if line_drop_info is None:
        line_drop_info = {"original_line_ids": line_id_set(edition), "dropped_by": {}}
    line_ids = {}
    line_refs = {}
    for section, article, line in iter_lines(edition):
        line_ids[line.get("line_id")] = line.get("mark")
        line_refs[line.get("line_id")] = line.get("source_ref")

    hyps = hypotheses_doc.get("hypotheses", [])
    reasons = {}
    kept = []
    # 修正4・5・12(a): コードリストが読めなかった場合、検査21・31は適用しない
    # (企業欄が丸ごと削除されるのを避けるため)。読めたかどうかはverificationに記録する。
    # edinet_doclist_unavailableも同様に、hypotheses自体が空になる号(市場休場・遅延号)
    # でもEDINET書類一覧が読めたかどうかをそのまま記録する。
    extra_counts = {
        "codelist_unavailable": codelist_rows is None,
        "baseline_date_check_skipped": 0,
        "evidence_filer_name_overridden": 0,
        "evidence_doc_type_overridden": 0,
        "impact_kind_overridden": 0,
        "impact_kind_source_counts": {},
        "impact_kind_undetermined_by_doc_type": {},
        "edinet_url_unparsed": 0,
        "edinet_doclist_unavailable": edinet_companies is None,
        # 改修27-1(4-5): horizon_business_days/deadline_dateを機械で埋めた件数・
        # 検算(検査17)がずれた件数(記録専用)。
        "horizon_overridden": 0,
        "deadline_uncomputable": 0,
        "horizon_recount_mismatch": 0,
        # 改修27-1第2回: 検査17の検算で起算日が求められなかった件数(検査32の
        # baseline_date_check_skippedとは別のキー。通常は0のまま)。
        "horizon_recount_skipped": 0,
        # 改修27-1(4-10): tob_sideの決定でsubjectEdinetCodeが取れず、今の規則
        # (mentioned→target)に倒した件数。
        "tob_side_subject_code_missing": 0,
        # 改修27-2第5回(S5): 検査11で削除した会社(社名・理由)、合格した会社の見つかった段階の件数、
        # コードリストが読めず長い社名の判定を飛ばした件数。市場休場などで仮説を全件消す号でも
        # キーがそろうよう、空・0で始める。
        "check11_removed": [],
        "name_match_stage": {stage: 0 for stage in NAME_MATCH_STAGES},
        "check11_longer_name_check_skipped": 0,
        # 改修27-2第8回(S9): 検査37で削除した会社(社名・理由・記事ID・行ID)。
        "check37_removed": [],
        # 改修31第1回(3-2): 検査37で、日付・鮮度の検査で落とされた行をline_idsから外した会社。
        "check37_line_ids_trimmed": [],
        # 改修31第1回(3-1): 案1で、印がsource_number_matchでない行をline_idsから外したprimaryの会社。
        "primary_line_ids_trimmed": [],
    }

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
        return len(hyps), reasons, extra_counts

    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", [])}
    # 改修27-2第5回(S5): 検査11は格下げではなく削除。削除した会社は、理由ごとにreasonsへ
    # 数え、社名と理由をcheck11_removedに記録する。
    check11 = run_check_hypothesis_evidence(hyps, sources_by_id, cache_dir, codelist_rows)
    hyps = check11["kept"]
    for item in check11["removed"]:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    extra_counts["check11_removed"] = check11["removed"]
    extra_counts["name_match_stage"] = check11["name_match_stage"]
    extra_counts["check11_longer_name_check_skipped"] = check11["longer_name_check_skipped"]

    # 改修27-2第8回(S9): 検査37(上段の会社の根拠の行・記事・出典)。行を落とす検査がすべて終わった
    # 後(この関数が呼ばれる時点)の紙面で判定する。複数の理由に当たる仮説は、すべての理由を
    # check37_removedに記録し、削除は1件と数える(reasonsには最初の理由で1件だけ数える)。
    check37 = run_check37(hyps, edition, set(line_drop_info["original_line_ids"]), line_drop_info["dropped_by"])
    hyps = check37["kept"]
    for item in check37["removed"]:
        first = item["reasons"][0]["reason"]
        reasons[first] = reasons.get(first, 0) + 1
    extra_counts["check37_removed"] = check37["removed"]
    extra_counts["check37_line_ids_trimmed"] = check37["trimmed"]

    # 修正2・3(要件定義書v12 3.4(2)・5.4)、および2026年9月21日の追加指示: evidence_filer_name/
    # evidence_doc_type/evidence_role/impact_kind/impact_kind_source/auto_check_targetはAIには
    # 書かせず、出典URLの書類管理番号からEDINET書類一覧を引いてスクリプトが確定する。
    # 検査23(impact_kind)などがこの結果を見るため、check_hypothesis()の
    # ループより必ず先に実行すること。
    evidence_counts = apply_edinet_evidence(hyps, sources_by_id, edinet_companies, codelist_rows)
    extra_counts["evidence_filer_name_overridden"] = evidence_counts["evidence_filer_name_overridden"]
    extra_counts["evidence_doc_type_overridden"] = evidence_counts["evidence_doc_type_overridden"]
    extra_counts["impact_kind_overridden"] = evidence_counts["impact_kind_overridden"]
    extra_counts["impact_kind_source_counts"] = evidence_counts["impact_kind_source_counts"]
    extra_counts["impact_kind_undetermined_by_doc_type"] = evidence_counts["impact_kind_undetermined_by_doc_type"]
    extra_counts["edinet_url_unparsed"] = evidence_counts["edinet_url_unparsed"]
    extra_counts["tob_side_subject_code_missing"] = evidence_counts["tob_side_subject_code_missing"]

    # 改修27-1(4-5): horizon_business_days・deadline_dateも、impact_kindが確定した後で
    # 機械が埋める(apply_edinet_evidence()の直後、check_hypothesis()のループより前)。
    window_counts = apply_observation_window(hyps, business_days)
    extra_counts["horizon_overridden"] = window_counts["horizon_overridden"]
    extra_counts["deadline_uncomputable"] = window_counts["deadline_uncomputable"]

    for hyp in hyps:
        reason = check_hypothesis(
            hyp, edition, line_ids, business_days, ng_words, sources_by_id, cache_dir,
            edinet_companies, codelist_rows, extra_counts, line_refs=line_refs,
        )
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
    return total_violations, reasons, extra_counts


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


def hypothesis_ticker_doc_key(hyp, sources_by_id):
    """改修27-2第9回: 号をまたぐ重複の比べる鍵(ticker, 書類管理番号)。tickerが文字で、
    evidence_source_refの指す出典のURLから書類管理番号が取れる仮説だけ。それ以外はNone。"""
    ticker = hyp.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        return None
    ref = hyp.get("evidence_source_ref")
    source = sources_by_id.get(ref) if isinstance(ref, str) else None
    url = source.get("url") if isinstance(source, dict) else None
    doc_id = extract_edinet_doc_id(url) if isinstance(url, str) else None
    if doc_id is None:
        return None
    return (ticker, doc_id)


# 改修27-2第9回の2回目(要件3.1): 続報の判定。
HUB_URLS_FILENAME = "hub_urls.csv"
FOLLOWUP_TRACKING_PARAM_PREFIX = "utm_"
FOLLOWUP_TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "n_cid"}
FOLLOWUP_HEADLINE_WORD = "続報"
EDINET_VIEW_HOST = "disclosure2.edinet-fsa.go.jp"
EDINET_VIEW_PATH = "/wzek0040.aspx"
_EDINET_VIEW_DOC_ID_RE = re.compile(r"^([A-Za-z0-9]+)")


def extract_edinet_view_doc_id(url):
    """改修27-2第9回の2回目: 読者向けの閲覧画面のURL(WZEK0040.aspx?{書類管理番号})から
    書類管理番号を取り出す。「?」の直後の英数字の続きだけを取る(後ろの=や,は無視する。
    実例にWZEK0040.aspx?S100VTPA=の形がある)。形が違えばNone。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or (parts.hostname or "") != EDINET_VIEW_HOST:
        return None
    if parts.path.lower() != EDINET_VIEW_PATH:
        return None
    m = _EDINET_VIEW_DOC_ID_RE.match(parts.query)
    return m.group(1) if m else None


def followup_doc_id(url):
    """続報の判定で使う書類管理番号。api/v2/documents/{番号}の形(extract_edinet_doc_id。
    今の動きのまま)と、WZEK0040.aspx?{番号}の形の両方から取り出す。"""
    return extract_edinet_doc_id(url) or extract_edinet_view_doc_id(url)


def normalize_followup_url(url, keep_query=True):
    """改修27-2第9回の2回目: 続報の判定のためにURLをそろえる。http→https、ホスト名を小文字に、
    「#」以降を外す、パスの末尾の「/」を外す(パスが「/」だけのときを除く)。「?」以降は、
    追跡用の値(名前がutm_で始まるもの、fbclid・gclid・yclid・n_cid)だけを外し、残りは
    名前順に並べて残す(e-Statのように「?」以降で資料を区別するサイトがあるため)。
    keep_query=Falseなら「?」以降をすべて外す(入口ページの表と比べるとき)。
    URLとして読めなければNone。"""
    try:
        parts = urlsplit(url.strip())
        pairs = parse_qsl(parts.query, keep_blank_values=True) if keep_query else []
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme == "http":
        scheme = "https"
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    kept = [
        (name, value) for name, value in pairs
        if not name.lower().startswith(FOLLOWUP_TRACKING_PARAM_PREFIX)
        and name.lower() not in FOLLOWUP_TRACKING_PARAMS
    ]
    return urlunsplit((scheme, parts.netloc.lower(), path, urlencode(sorted(kept)), ""))


def load_hub_urls(path):
    """改修27-2第9回の2回目: scripts/hub_urls.csv(入口ページの表。1列目url・2列目note)を読み、
    「?」以降を除いてそろえたURLの集合を返す。source_policy.csvと同じく、表そのものが読めない・
    URLとして読めない行がある場合は号の保存を止める(設定ミスに気づけなくなるのを防ぐため)。"""
    path = Path(path)
    if not path.is_file():
        raise EditionInvalid(f"scripts/{HUB_URLS_FILENAME} が読めません: ファイルがありません。")
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise EditionInvalid(f"scripts/{HUB_URLS_FILENAME} が読めません: {e}")
    hubs = set()
    for number, row in enumerate(rows, start=2):
        url = (row.get("url") or "").strip()
        normalized = normalize_followup_url(url, keep_query=False) if url else None
        if not normalized or not urlsplit(normalized).netloc:
            raise EditionInvalid(f"scripts/{HUB_URLS_FILENAME} の{number}行目のurlが読めません: {url!r}")
        hubs.add(normalized)
    return hubs


def followup_source_key(source, hub_urls):
    """改修27-2第9回の2回目: 1つの出典の、続報の判定で比べる鍵を返す。
      ・書類管理番号が取れる → ("doc", 番号)
      ・取れない → 入口ページ(書類一覧のsource_id・パスが「/」だけか空・hub_urls.csvに載ったURL)
        ならNone(比べない)。入口ページでなければ ("url", そろえたURL)
    URLが空・文字でない・URLとして読めない出典もNone。"""
    url = source.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    doc_id = followup_doc_id(url)
    if doc_id is not None:
        return ("doc", doc_id)
    if source.get("source_id") in EDINET_DOCLIST_SOURCE_IDS:
        return None
    try:
        path = urlsplit(url.strip()).path
    except ValueError:
        return None
    if path in ("", "/"):
        return None
    if normalize_followup_url(url, keep_query=False) in hub_urls:
        return None
    normalized = normalize_followup_url(url)
    return ("url", normalized) if normalized else None


def followup_article_keys(edition, hub_urls):
    """紙面の記事ごとに、行のsource_refが指す出典の鍵を集める。
    戻り値: [(記事, {鍵: その鍵になった生のURLの集合})](紙面の記事の順)。"""
    sources_by_id = {
        s.get("source_id"): s for s in edition.get("sources", [])
        if isinstance(s, dict) and isinstance(s.get("source_id"), str)
    }
    result = []
    for section in edition.get("sections", []):
        for article in section.get("articles", []):
            keys = {}
            for line in article.get("lines", []):
                ref = line.get("source_ref")
                source = sources_by_id.get(ref) if isinstance(ref, str) else None
                key = followup_source_key(source, hub_urls) if source is not None else None
                if key is not None:
                    keys.setdefault(key, set()).add(source["url"])
            result.append((article, keys))
    return result


def apply_followups(edition, recent_loaded, hub_urls):
    """改修27-2第9回の2回目(要件3.1): すべての記事に、機械でfollowupを書く(AIの値は使わない)。
    行を落とす検査(検査7・36・10)がすべて終わった後の紙面で呼ぶこと(残っている行の出典だけを
    比べる)。過去の号は、recent_loaded(load_recent_editions()の結果、古い順)の紙面の記事の行が
    参照する出典。
      ・続報: 比べる鍵が1つでも範囲内の号と重なった記事。first_seen・first_seen_edition_idは
        重なった中で最も古い号の日付とedition_id。has_new_sourceは、比べる鍵のうち範囲内の
        どの号にも無いものが1つでもあれば真
      ・続報でない記事(比べる鍵が1つも無い記事を含む): is_followup偽、他の3つはnull
    戻り値: verificationに書く4つの記録(followup_counts・followup_without_new_source・
    url_normalized_matches・followup_headline_mismatch)。記事は消さない。"""
    past = []
    for item in recent_loaded:
        merged = {}
        for _article, keys in followup_article_keys(item["edition"], hub_urls):
            for key, raws in keys.items():
                merged.setdefault(key, set()).update(raws)
        past.append((item["entry"], merged))

    counts = {"followup": 0, "not_followup": 0}
    without_new_source = []
    normalized_matches = []
    headline_mismatch = []
    for article, keys in followup_article_keys(edition, hub_urls):
        article_id = article.get("article_id")
        first_pos = None
        has_new_source = False
        for key in sorted(keys):
            raws = keys[key]
            found = [(pos, entry, past_keys[key]) for pos, (entry, past_keys) in enumerate(past) if key in past_keys]
            if not found:
                has_new_source = True
                continue
            if first_pos is None or found[0][0] < first_pos:
                first_pos = found[0][0]
            if key[0] == "url":
                past_raws = set().union(*(f[2] for f in found))
                if not (raws & past_raws):
                    normalized_matches.append({
                        "article_id": article_id,
                        "url": sorted(raws)[0],
                        "matched_url": sorted(found[0][2])[0],
                        "edition_id": found[0][1].get("edition_id"),
                    })
        is_followup = first_pos is not None
        if is_followup:
            first_entry = past[first_pos][0]
            article["followup"] = {
                "is_followup": True,
                "first_seen": first_entry.get("date"),
                "first_seen_edition_id": first_entry.get("edition_id"),
                "has_new_source": has_new_source,
            }
            counts["followup"] += 1
            if not has_new_source:
                without_new_source.append(article_id)
        else:
            article["followup"] = {
                "is_followup": False, "first_seen": None, "first_seen_edition_id": None, "has_new_source": None,
            }
            counts["not_followup"] += 1

        headline = article.get("headline")
        headline_has_word = isinstance(headline, str) and FOLLOWUP_HEADLINE_WORD in headline
        if headline_has_word != is_followup:
            headline_mismatch.append({
                "article_id": article_id, "headline_has_word": headline_has_word, "is_followup": is_followup,
            })

    return {
        "followup_counts": counts,
        "followup_without_new_source": {"count": len(without_new_source), "article_ids": without_new_source},
        "url_normalized_matches": {"count": len(normalized_matches), "matches": normalized_matches},
        "followup_headline_mismatch": {"count": len(headline_mismatch), "articles": headline_mismatch},
    }


def apply_cross_edition_duplicates(hyps, sources_by_id, recent_loaded):
    """改修27-2第9回(要件3.4(2)): 上段の仮説(すべての検査と枠の配分の後に残ったもの)を、
    比べる範囲の過去の号の仮説と(ticker, 書類管理番号)で比べる。過去の号の書類管理番号は、
    その過去の号の紙面の出典からevidence_source_refで引く。
    一致すれば、duplicate_ofに一致した中で最も古い号の"edition_id:hypothesis_id"を書き、
    auto_check_targetを偽にする(仮説は削除しない)。一致しなければduplicate_ofはnull、
    auto_check_targetは機械の値のまま。duplicate_ofはAIの値を使わず、すべての仮説に書く。
    recent_loadedはload_recent_editions()の結果(古い順)。
    戻り値: {"count", "duplicates": [{hypothesis_id, company_name, ticker, doc_id, duplicate_of}]}。"""
    past_keys = {}
    for item in recent_loaded:
        if item["hypotheses"] is None:
            continue
        past_sources = item["edition"].get("sources")
        past_sources_by_id = {
            s.get("source_id"): s for s in (past_sources if isinstance(past_sources, list) else [])
            if isinstance(s, dict) and isinstance(s.get("source_id"), str)
        }
        for past_hyp in item["hypotheses"]:
            if not isinstance(past_hyp, dict):
                continue
            key = hypothesis_ticker_doc_key(past_hyp, past_sources_by_id)
            if key is not None and key not in past_keys:
                past_keys[key] = f"{item['entry'].get('edition_id')}:{past_hyp.get('hypothesis_id')}"

    duplicates = []
    for hyp in hyps:
        key = hypothesis_ticker_doc_key(hyp, sources_by_id)
        duplicate_of = past_keys.get(key) if key is not None else None
        hyp["duplicate_of"] = duplicate_of
        if duplicate_of is not None:
            hyp["auto_check_target"] = False
            duplicates.append({
                "hypothesis_id": hyp.get("hypothesis_id"),
                "company_name": hyp.get("company_name"),
                "ticker": key[0],
                "doc_id": key[1],
                "duplicate_of": duplicate_of,
            })
    return {"count": len(duplicates), "duplicates": duplicates}


def print_report(edition_path, stats, stop_hits, watch_hits, dropped_inferences, stale_hits,
                  unknown_published_at_hits, stale_skipped,
                  hypothesis_violations, hypothesis_reasons, number_failure_details, ok,
                  baseline_late=False, ticker_crosscheck="skipped", source_usage_invalid_hits=0,
                  industry_report=None, source_policy_unlisted_domains=None,
                  number_coverage=None, sources_published_at_null=None, rerun_detected=False,
                  published_date_not_found=None, inference_company_names=None,
                  inference_company_name_check_skipped=False, source_body_check=None,
                  excerpt_spans_lines=None, excerpt_line_check_skipped=None,
                  published_timed_date_not_found=None, excerpt_check_log=None,
                  upper_trading_between=None):
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
            "excerpt_spans_lines": "抜き出した文が出典の本文の1行に収まらなかった（2行以上をつないでいた）",
            "number_not_in_excerpt": "数字が抜き出した文の中に見つからなかった",
            "mark_mismatch": "数字が入っているのに「解説」として申告されていた",
            "source_unreadable": "出典ファイルが文字コードの問題で読めなかった",
            "numbers_empty": "数字を1つも書かずに「出典と数字が一致」と申告していた",
            "excerpt_not_allowed": "本文を取得していない出典(quotable以外)からの抜き出しだった(excerptは削除した)",
            "empty_title_or_url": "参照している出典の題名かURLが空だった",
            "reported_without_source": "「報道で見た・未確認」と申告したが、出典の番号が空だった",
            "reported_source_not_snippet": "「報道で見た・未確認」と申告したが、出典が検索結果の断片ではなかった",
            "source_body_not_machine_saved": "出典の本文ファイルが機械(save_source.py)で保存されたものではなかった(記録ファイルが無い)",
            "source_body_mismatch": "出典の本文ファイルの記録(URL・ハッシュ・元のファイル)が食い違った",
        }
        for reason, count in stats["unverified_reasons"].items():
            print(f"  ・{reason_text.get(reason, reason)}: {count}件")

    if number_failure_details:
        print("数字が見つからなかった行:")
        for detail in number_failure_details:
            values = ", ".join(str(n.get("value")) for n in detail["missing_numbers"])
            print(f"  ・{detail['line_id']}: {values}")

    # 改修29: 第2回からこの行の印はunverifiedにしている(理由excerpt_spans_lines)。
    if excerpt_spans_lines is not None:
        print(f"抜き出しが出典の本文の1行に収まらなかった行(印を未確認に下げた): {excerpt_spans_lines['count']}件")
        for line_id in excerpt_spans_lines["line_ids"]:
            print(f"  ・{line_id}")
    if excerpt_line_check_skipped is not None and excerpt_line_check_skipped["count"]:
        print(f"本文を1行に区切れず、1行の判定をしなかった出典: {', '.join(map(str, excerpt_line_check_skipped['source_ids']))}")

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
    print(f"出典が古い行の件数(時刻付きは36時間超、日付だけは号の日付の前日より前か号の日付より後): {stale_hits}")
    print(f"出典の公表時刻が分からず、鮮度を確認できなかったため落とした行の件数: {unknown_published_at_hits}")
    # 改修27-2第4回(S2): 日付だけの出典で、本文に日付が見つからなかった(または本文が読めなかった)もの。
    date_not_found = published_date_not_found or {"count": 0, "source_ids": [], "reasons": {}, "dropped_line_ids": []}
    print(
        f"日付だけの出典で、本文に日付が見つからず日付不明にした出典: {date_not_found['count']}件"
        f"(そのため新しい変化の枠から落とした行: {len(date_not_found['dropped_line_ids'])}行)"
    )
    # 改修31第1回(3-5): 時刻付きの出典(EDINET以外)で、本文に日付が見つからなかったもの。
    timed_not_found = published_timed_date_not_found or {"count": 0, "source_ids": [], "reasons": {}, "dropped_line_ids": []}
    print(
        f"時刻付きの出典(EDINET以外)で、本文に日付が見つからず日付不明にした出典: {timed_not_found['count']}件"
        f"(そのため新しい変化の枠から落とした行: {len(timed_not_found['dropped_line_ids'])}行)"
    )
    print(f"出典の日時が読み取れず判定できなかった行の件数: {stale_skipped}")
    # 改修31第3回(3-1): 提出から株価の基準時点までに取引時間があった上段の会社(記録だけ)。
    if upper_trading_between is not None:
        with_trading = sum(1 for x in upper_trading_between if x["trading_minutes"])
        not_computed = sum(1 for x in upper_trading_between if x["reason"] is not None)
        print(f"提出から基準時点までに取引があった会社 {with_trading}社／計算できなかった会社 {not_computed}社")
    # 改修31第2回(3-4): 抜き出しの事前確認(check_excerpts.py)の実行記録のまとめ(記録だけ)。
    if excerpt_check_log is not None:
        log = excerpt_check_log
        if log["run_count"] == 0:
            print(f"抜き出しの事前確認の実行記録: なし({log['status']})")
        else:
            print(
                f"抜き出しの事前確認の実行記録: {log['run_count']}回(状態: {log['status']}。1回目から消えた行 "
                f"{len(log['removed_line_ids'])}・印が変わった行 {len(log['changed_marks'])}・外れた数字 "
                f"{sum(len(x['values']) for x in log['numbers_removed'])}件。最後の確認の後に紙面が書き換えられた: "
                f"{'はい' if log['edited_after_last_check'] else 'いいえ'})"
            )
    # 修正5: change枠に関係なく、出典そのものでpublished_atが無いものを数える
    # (行は落とさない。既存のunknown_published_at_hitsとは別の集計)。
    pub_null = sources_published_at_null or {"count": 0, "source_ids": []}
    print(f"公表日時(published_at)が書かれていない出典: {pub_null['count']}件")
    print(f"必須項目が空で削除した推論の件数: {dropped_inferences}")
    # 改修27-2第6回(S7): 推論欄に上場会社の名前が入っていたため削除した推論。
    company_removed = inference_company_names or {"count": 0, "removed": []}
    if inference_company_name_check_skipped:
        print("推論欄の会社名の検査: コードリストが読めなかったため行いませんでした")
    else:
        print(f"上場会社の名前が入っていたため削除した推論の件数: {company_removed['count']}")
        for item in company_removed["removed"]:
            for hit in item["hits"]:
                print(f"    ・{item['article_id']}の推論({hit['field']}): {hit['company_name']}(当たった語: {hit['matched_word']})")
    print(f"号の遅延判定(baseline_late): {baseline_late}")
    # 改修28第2回: 出典の本文ファイルが機械で保存されたものか(改修31第1回から、記録ファイルが無い・
    # URLやハッシュが食い違う出典を参照する行の印は下げる。source_body_downgradedを参照)。
    if source_body_check is not None:
        labels = {
            "machine_saved": "機械(save_source.py)で保存され、もう一度文字にした結果も一致",
            "not_machine_saved": "記録ファイルが無い(機械で保存されたものではない)",
            "reconvert_mismatch": "記録ファイル・元のファイル・もう一度文字にした結果のどれかが食い違った",
            "reconvert_skipped": "道具(pdftotext)が無く、もう一度文字にできなかった",
        }
        print("出典の本文ファイルの確認(EDINET以外で本文を引用できる出典):")
        for kind in SOURCE_BODY_CHECK_KINDS:
            entry = source_body_check[kind]
            ids = f"（{'、'.join(entry['source_ids'])}）" if entry["source_ids"] else ""
            print(f"  ・{labels[kind]}: {entry['count']}件{ids}")
        for detail in source_body_check["reconvert_mismatch"]["details"]:
            print(f"    ・{detail['source_id']}: 食い違った項目 {'、'.join(detail['mismatched'])}")
    # 修正4: 本文の数字の個数とnumbersの件数の差(判定には使わない。記録のみ)。
    if number_coverage:
        print(
            f"本文の数字: {number_coverage['text_number_tokens']}個"
            f" / numbersに書かれた数値: {number_coverage['numbers_declared']}件"
            f"(差 {number_coverage['gap']})"
        )
    # 修正8: 再照合であることの記録(判定には使わない)。
    if rerun_detected:
        print("この号は2回目以降の照合です(下段の顔ぶれが1回目と変わることがあります)。")
    print(f"証券コードの突き合わせ(ticker_crosscheck): {ticker_crosscheck}")
    print(f"usageが正しく書かれていない出典の件数(source_usage_invalid_hits): {source_usage_invalid_hits}")
    unlisted = source_policy_unlisted_domains or []
    print(f"出典ポリシー表(source_policy.csv)に無かったドメイン: {'、'.join(unlisted) if unlisted else 'なし'}")

    if hypothesis_reasons:
        print(f"仮説に関する指摘件数: {hypothesis_violations}")
        reason_text = {
            "missing_field": "必須項目が空だった(削除)",
            "field_type_invalid": "仮説の値の型が正しくなかった(文字でもnullでもない値が書かれていた、など。削除。項目名はfield_type_invalid_removedに記録)",
            "article_not_found": "検査37: 仮説の記事ID(article_id)が空、または紙面に無かった(削除。他の理由もcheck37_removedに記録)",
            "line_ids_empty": "検査37: 仮説の根拠の行(line_ids)が空だった、または日付・鮮度の検査で落とされた行を外した結果空になった(削除)",
            "line_id_never_existed": "検査37: 仮説の根拠の行に、AIが書いた紙面に元から無い行IDがあった(削除)",
            "line_id_removed_by_check": "検査37: 仮説の根拠の行が、停止語の検査で落とされていた(削除。日付・鮮度の検査で落とされた行は外すだけ)",
            "line_not_in_article": "検査37: 仮説の根拠の行に、仮説の記事(article_id)以外の記事の行があった(削除)",
            "no_fact_line": "検査37: 仮説の根拠の行が、どれも事実系(出典と数字が一致・出典を明示した未確認)でなかった(削除)",
            "primary_ref_mismatch": "検査37: 根拠が最上位(primary)なのに、根拠の出典が根拠の行の出典に含まれなかった(削除)",
            "primary_no_verified_evidence_line": "根拠が最上位(primary)なのに、根拠の出典で数字が一致した行が1行も無かった(削除)",
            "deadline_date_mismatch": "確認期限の日付が営業日計算と合わなかった(削除)",
            "relation_text_conclusive_word": "断定的な言葉(プラス/マイナス/好材料/悪材料)が入っていた(削除)",
            "relation_text_recommendation": "説明文に推奨表現が入っていた(削除)",
            "market_closed": "市場が休みの号に仮説が入っていた(削除)",
            "baseline_late": "号が遅延していた(baseline_late)ため仮説が入っていた(削除)",
            "too_many_hypotheses": "仮説が上限(5件)を超えていた(削除)",
            "evidence_source_ref_missing": "根拠が最上位(primary)なのに根拠の出典番号が空だった(削除)",
            "evidence_source_not_found": "根拠が最上位(primary)なのに、根拠の出典またはその本文が見つからなかった(削除)",
            "evidence_source_link_only": "根拠が最上位(primary)なのに、根拠の出典はリンクだけの扱い(本文を確かめられない)だった(削除)",
            "evidence_source_unreadable": "根拠の出典ファイルが文字コードの問題で読めなかった(削除)",
            "evidence_company_name_not_found": "根拠が最上位(primary)の自己申告なのに、出典本文に会社名を確認できなかった(削除)",
            "evidence_company_name_only_in_longer_name": "出典本文に会社名は出ているが、より長い別の社名の一部としてしか出ていなかった(削除)",
            "ticker_missing": "証券コードが無い、または証券コードの形に合わなかった(削除)",
            "ticker_source_missing": "証券コードの出典(ticker_source)が空だった(削除)",
            "ticker_mismatch": "証券コードがEDINET書類一覧の記録と一致しなかった(削除)",
            "upper_not_listed": "コードリスト上「上場」として見つからなかった(削除)",
            "upper_codelist_unavailable": "コードリストが読めない日で、証券コードの出典がコードリストの会社は上場かどうかを確かめられなかった(削除)",
        }
        for reason, count in hypothesis_reasons.items():
            print(f"  ・{reason_text.get(reason, reason)}: {count}件")
    elif hypothesis_violations:
        print(f"仮説に関する指摘件数: {hypothesis_violations}")

    if industry_report and "industry_picks_total" not in industry_report:
        # 検査14: 休場日・遅延号のため、下段(企業欄)自体を作らなかった場合。
        print(
            "休場日、または号の遅延のため、下段(業種から選ぶ企業欄)は作りませんでした。"
            f"破棄したindustry_picksの件数: {industry_report['industry_picks_discarded']}件"
        )
    elif industry_report:
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

    if industry_report:
        # 修正3・修正2: どちらも判定には使わない記録専用の件数。休場日・遅延号
        # (下段を作らなかった号)でも0件として出す(対称性のため)。
        legacy = industry_report.get("legacy_substring_rule_dropped", {"count": 0, "names": []})
        generic = industry_report.get("generic_name_dropped", {"count": 0, "names": []})
        legacy_names = f"（{'、'.join(legacy['names'])}）" if legacy["names"] else ""
        generic_names = f"（{'、'.join(generic['names'])}）" if generic["names"] else ""
        print(f"旧規則で本文照合から外した会社: {legacy['count']}件{legacy_names}")
        print(f"一般語辞書で本文照合から外した会社: {generic['count']}件{generic_names}")


def should_abort_rerun(existing_verification, baseline_late):
    """再照合で企業欄が消える結果になるときにTrueを返す。
    既に照合済み(verificationがある)の号で、そのときは遅延していなかった
    (baseline_lateが偽だった)のに、今回の判定が真になった場合が対象。
    この場合は号を書き戻さずに中止する(既存の号を壊さないため)。"""
    if not existing_verification:
        return False
    if existing_verification.get("baseline_late") is not False:
        return False
    return bool(baseline_late)


def remove_edition_corrections(edition):
    """改修31第1回(3-6): 号の最上位のcorrections(訂正の記録)を取り除く。紙面を作るAIが偽の訂正を
    書けないようにするため(運営側の訂正は今後、号とは別のファイルに置く)。過去の号は検査24で
    書き戻されないため、既にある運営側の訂正(9/20夕号)は消えない。
    戻り値: 取り除いた件数(配列なら要素の数、配列でない値なら1、nullやキーが無いなら0)。"""
    if "corrections" not in edition:
        return 0
    value = edition.pop("corrections")
    if isinstance(value, list):
        return len(value)
    return 0 if value is None else 1


# 改修31第1回(3-6): 上段の仮説から取り除く廃止キー。紙面の推論欄(inferences)のfalsifierは
# 名前が同じ別物(必須の項目)なので、こちらは触らない。
DEPRECATED_HYPOTHESIS_KEYS = ("direction", "evidence_excerpt", "falsifier")


def remove_deprecated_hypothesis_keys(hypotheses_doc):
    """改修31第1回(3-6): 仮説ファイルのhypotheses[]の各要素から、廃止キー(DEPRECATED_HYPOTHESIS_KEYS)を
    取り除く。下段(industry_examples)・industry_picks・紙面には触らない。
    戻り値: {キー: 取り除いた仮説の数}。"""
    counts = {key: 0 for key in DEPRECATED_HYPOTHESIS_KEYS}
    for hyp in hypotheses_doc.get("hypotheses") or []:
        if not isinstance(hyp, dict):
            continue
        for key in DEPRECATED_HYPOTHESIS_KEYS:
            if key in hyp:
                del hyp[key]
                counts[key] += 1
    return counts


def override_generated_at(doc, run_at_iso):
    """改修27-1(4-1): generated_atをAIの自己申告から照合スクリプトの実行時刻(run_at_iso、
    日本時間・+09:00付き)へ上書きする。docは紙面JSON・仮説JSONのどちらにも使う共通処理。
    戻り値: 上書きする前にdocに入っていた値(AIの自己申告。nullのこともある。記録専用)。"""
    reported = doc.get("generated_at")
    doc["generated_at"] = run_at_iso
    return reported


def build_first_run_record(run_at, run_at_dt, baseline_late, market_open_reported,
                            source_policy_overwritten, generated_at_raw,
                            hypotheses_generated_at_raw=None):
    """修正2: 初回の照合結果を1回だけ記録する(first_run)。2回目以降の照合では、
    main()がこの中身を一切書き換えない(呼び出さない)。
    この記録は判定には使わない。AIが書けるファイルの中にあるため。

    改修27-1(4-1): 紙面のgenerated_at(generated_at_raw)に加えて、仮説ファイルの
    generated_at(hypotheses_generated_at_raw)も同じ形で記録する。--hypothesesを
    指定しない実行ではhypotheses_generated_at_rawはNoneのまま(parsed=False、
    drift_minutes=Noneになる)。"""
    def _parsed_and_drift(raw):
        parsed_dt = parse_datetime_assume_jst(raw)
        if parsed_dt is None:
            return False, None
        return True, int((run_at_dt - parsed_dt).total_seconds() / 60)

    generated_at_parsed, drift_minutes = _parsed_and_drift(generated_at_raw)
    hyp_generated_at_parsed, hyp_drift_minutes = _parsed_and_drift(hypotheses_generated_at_raw)
    return {
        "run_at": run_at,
        "baseline_late": baseline_late,
        "market_open_reported": market_open_reported,
        "source_policy_overwritten": source_policy_overwritten,
        "generated_at_reported": generated_at_raw,
        "generated_at_parsed": generated_at_parsed,
        "generated_at_drift_minutes": drift_minutes,
        "hypotheses_generated_at_reported": hypotheses_generated_at_raw,
        "hypotheses_generated_at_parsed": hyp_generated_at_parsed,
        "hypotheses_generated_at_drift_minutes": hyp_drift_minutes,
    }


def run_verification(edition_file, hypotheses_file, cache_dir_arg, calendar_dir_arg, run_at_dt):
    """照合の本体(改修27-2第2回でmain()から分けた)。

    run_at_dt(実行時刻、タイムゾーン付きのdatetime)は必須の引数で、省略したら
    現在時刻を使う、という既定値は付けない。コマンドとして実行したとき(main())は
    必ずdt.datetime.now(JST)がここに渡される。コマンドの引数・環境変数・ファイルの
    どれからも時刻を変えられないようにするため、時刻を渡せるのはPythonの中から
    この関数を直接呼ぶとき(セルフテストが固定した時刻で確かめるとき)だけにしている。

    edition_file/hypotheses_file/cache_dir_arg/calendar_dir_argは、コマンドの
    --edition/--hypotheses/--cache/--calendarと同じ意味(hypotheses_fileはNone可)。
    戻り値: 終了コード(0=保存してよい 1=保存してはいけない)。スクリプト自体の
    エラー(終了コード2)は、呼び出し元(__main__)が例外として扱う。"""
    run_at = run_at_dt.isoformat()

    script_dir = Path(__file__).resolve().parent
    ng_words_path = script_dir / "ng_words.txt"
    ng_words_exclude_path = script_dir / "ng_words_exclude.txt"
    source_policy_path = script_dir / "source_policy.csv"

    try:
        edition_path = Path(edition_file)
        try:
            # 改修31第2回: 読んだバイト列のハッシュを、中身を書き換える前に取っておく(実行記録との比べ方に使う)。
            edition_bytes = edition_path.read_bytes()
            edition = json.loads(edition_bytes.decode("utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            raise EditionInvalid(f"紙面JSONを読み込めません: {e}")
        edition_sha256 = hashlib.sha256(edition_bytes).hexdigest()

        # 修正D: 号のファイルを読み込んだ直後に、既存のverification/first_runを
        # 覚えておく。first_runは記録としてだけ引き継ぐ(判定には使わない。
        # AIが書けるファイルの中にある値のため)。existing_verificationは
        # should_abort_rerun()の判定にだけ使う(修正C)。
        # 改修27-2 第8回の追加2: 構造・型の検査を、紙面の値を読むどの処理よりも先に行う
        # (verificationが辞書でないと、下でも止まるため)。
        check_a_structure(edition)
        # 改修31第1回(3-6): 号の最上位のcorrectionsを、構造の検査の直後に取り除く(件数を記録)。
        corrections_removed = remove_edition_corrections(edition)
        existing_verification = edition.get("verification")
        existing_first_run = (existing_verification or {}).get("first_run")
        # 修正8: 記録専用。existing_verificationの有無だけで決まり、実行時刻には
        # 依存しない(compute_rerun_detected()を参照)。
        rerun_detected = compute_rerun_detected(existing_verification)

        check_b_edition_id(edition, edition_path)

        # 改修31第2回(3-4): check_excerpts.pyの実行記録をまとめる(記録だけ)。紙面の中身を書き換える前の状態と、
        # 1回目の確認を比べるため、ここ(構造の検査・edition_idの検査の直後)で行う。
        excerpt_check_log = compute_excerpt_check_log(cache_dir_arg, edition, edition_sha256)

        # 改修27-2第8回(S9): AIが書いた紙面の行ID全部と、検査で行を落とすたびに「どの検査で
        # 落ちたか」を控える(検査37が、元から無い行IDと落とされた行IDを区別するため)。
        original_line_ids = line_id_set(edition)
        known_line_ids = set(original_line_ids)
        dropped_by = {}

        # 改修27-1(4-1): generated_atはAIの自己申告ではなく、照合スクリプトの実行時刻で
        # 上書きする。AIの値(nullを含む)はfirst_run.generated_at_reportedに今までどおり
        # 記録する(上書きする前に控えておく)。
        generated_at_raw = override_generated_at(edition, run_at)

        # 検査24(要件3.5(9)): first_runの有無にかかわらず常に実行する(修正A)。
        # first_runは紙面を作るAIが書けるファイルの中にあるため、その有無で
        # 検査を飛ばすと、AIがfirst_runを自分で書いてこの検査を丸ごと避けられる。
        check_edition_date(edition, run_at_dt)

        # 改修27-2第2回(S14): 実行時刻から期待する時間帯(slot)を出し、号のslotと違えば
        # 号を保存しない(終了コード1)。slot_mismatchは止めるようになった後も、キーを
        # そろえるために記録する(保存される号では常に偽)。
        slot_expected = check_edition_slot(edition, run_at_dt)
        slot_mismatch = edition.get("slot") != slot_expected

        # usage/publisher_typeはAIの自己申告を信用せず、表の値で必ず上書きする。
        source_policy_result = apply_source_policy(edition, source_policy_path)

        # 検査35: market_openはAIの自己申告ではなく営業日カレンダーで確定させる。
        market_open_result = check_market_open(edition, calendar_dir_arg)

        # 改修27-1第2回: business_daysはEDINET書類一覧の「直前の営業日」の特定(4-4)にも
        # 使うため、ここで1回だけ読み込み、仮説の検査(検査17・32等)にも同じものを渡す
        # (同じファイルを2回読む作りにしない)。
        business_days = load_business_days(calendar_dir_arg)

        # 改修27-1(4-4): --cache フォルダの中のSRC-EDINET-LIST.json(号の日付)と
        # SRC-EDINET-LIST-PREV.json(直前の営業日)の両方を読み、合わせて使う。片方が
        # 読めなければ、読めた方だけで続け、読めなかった日付をedinet_doclist_partialに
        # 記録する。
        edinet_companies, edinet_doclist_availability = load_edinet_companies(cache_dir_arg)
        prev_business_day = last_business_day_before(business_days, edition.get("date")) if business_days else None
        edinet_doclist_partial = []
        if not edinet_doclist_availability["today"]:
            edinet_doclist_partial.append(edition.get("date"))
        if not edinet_doclist_availability["prev"]:
            edinet_doclist_partial.append(prev_business_day)
        ticker_crosscheck = "applied" if edinet_companies is not None else "skipped"

        # 改修27-1(4-2・4-3): EDINETの出典のpublished_atを機械で書き込む。検査10(36時間
        # ルール)・検査36(published_atの検算)がpublished_at/published_date_onlyを読むより
        # 前に行う。
        edinet_published_at_overwritten, edinet_other_url_hits = apply_edinet_source_published_at(
            edition, cache_dir_arg, edinet_companies
        )

        # 改修27-2(S1): 全出典のpublished_date_onlyを、published_atの形から機械で書く
        # (AIの自己申告は上書き)。EDINETのpublished_atが確定した後、検査10・検査36より前。
        published_date_only_sources = apply_published_date_only(edition)

        # 改修27-1(4-8): EDINETの個々の書類について、edinet_fetch.pyがどのファイルを
        # 本文に選んだかを記録する(選んだ本文自体はキャッシュに既にある。ここでは
        # 記録を写すだけ)。
        edinet_doc_files = collect_edinet_doc_files(edition, cache_dir_arg)

        # 改修27-1第6回: EDINETの出典のview_url(読者が実際に開けるURL)を機械で
        # 書き込む。apply_source_attribution()がattribution/processing_noteの
        # {url}にこの値を使うため、その前に行うこと。
        edinet_view_url_count = apply_edinet_view_url(edition)

        # 改修27-1(4-6): 出典(sources)と本文の各行のattribution/processing_noteを、
        # ひな形から機械で作る。検査3(run_line_verification内)より前に行うこと
        # (そうしないと、AIがnullを置いた行が検査3で丸ごとmissing_fieldになる)。
        source_attribution_result = apply_source_attribution(edition, source_policy_path)

        # 改修27-1(4-12): AIの書きぶりを測る記録専用のキーは、行が消される前
        # (停止語・出典の鮮度の検査より前)の全件を対象に数える。仮説(hypotheses)側の
        # 分(speculative_word_counts・banned_word_hitsの残り)は、仮説ファイルを
        # 読み込んだ後で別に数える。
        date_only_number_lines = compute_date_only_number_lines(edition)
        self_declared_unverified = compute_self_declared_unverified(edition)
        banned_word_hits = compute_banned_word_hits_edition(edition)

        # 改修27-1(4-11): scripts/recent_headlines.pyの実行結果(印のファイル)を読み、
        # 失敗したかどうかを記録する(号は止めない)。
        recent_headlines_failed = check_recent_headlines_status(
            cache_dir_arg, edition.get("date"), edition.get("slot")
        )

        # 改修27-2第9回: 号をまたいで比べる範囲の号(editions/index.jsonに載った号)を読む。
        # 一覧が読めなければ比べる号は無しとして進め、個々の号のファイルが読めなければ
        # その号だけ飛ばす(どちらも記録するだけで、号は止めない)。
        editions_index = load_editions_index()
        recent_editions_index_unavailable = editions_index is None
        recent_entries = select_recent_editions(
            editions_index or [], business_days, edition.get("date"), edition.get("slot"), edition.get("edition_id"),
        )
        recent_loaded, recent_editions_unreadable = load_recent_editions(recent_entries)

        ng_words = load_ng_words(ng_words_path)
        ng_words_exclude = load_ng_words(ng_words_exclude_path)

        # 検査7: 停止語を含む行はその行だけを削除する(号全体は保存する)。
        stop_hits = check_c_stop_words(edition, ng_words)
        record_dropped_lines(edition, known_line_ids, dropped_by, LINE_DROP_CHECK_NAMES["stop_words"])

        watch_hits = check_watch_proximity(edition, ng_words_exclude)

        # 改修28第2回: 出典の本文ファイルが機械(save_source.py)で保存されたものか。改修31第1回(3-4)で、
        # 行の確定より前に移し、記録ファイルが無い・URLやハッシュが食い違う出典を参照する行の印を
        # 下げるようにした(中身は出典と本文ファイルだけを見るので、移しても記録は変わらない)。
        source_body_check = run_check_source_body(edition, cache_dir_arg)
        body_downgrades = compute_source_body_downgrades(source_body_check)

        stats, number_failure_details = run_line_verification(edition, cache_dir_arg, body_downgrades)
        # 改修27-2(S12): titleかurlが空の出典を参照していた行(印はunverifiedにした)。
        empty_title_or_url_refs = {
            "count": len(stats["empty_title_or_url_line_ids"]),
            "line_ids": stats["empty_title_or_url_line_ids"],
        }
        # 改修28第1回: 出典の無い「報道で見た・未確認」の行(印はunverifiedにした)。
        reported_without_source = {
            "count": len(stats["reported_without_source_line_ids"]),
            "line_ids": stats["reported_without_source_line_ids"],
        }
        # 改修29: 抜き出しが本文の1行に収まらなかった行(第2回から印をunverifiedにした)と、
        # 1行に区切れなかった出典(記録だけ。印は下げない)。
        excerpt_spans_lines = {
            "count": len(stats["excerpt_spans_lines_line_ids"]),
            "line_ids": stats["excerpt_spans_lines_line_ids"],
        }
        excerpt_line_check_skipped = {
            "count": len(stats["excerpt_line_check_skipped_source_ids"]),
            "source_ids": stats["excerpt_line_check_skipped_source_ids"],
        }
        # 改修31第1回(3-3): 出典が検索結果の断片でない「報道で見た・未確認」の行(印はunverifiedにした)。
        reported_source_not_snippet = {
            "count": len(stats["reported_source_not_snippet_line_ids"]),
            "line_ids": stats["reported_source_not_snippet_line_ids"],
        }
        # 改修31第1回(3-4): 本文ファイルの確認で印を下げた行。
        body_by_reason = {}
        for item in stats["source_body_downgraded"]:
            body_by_reason[item["reason"]] = body_by_reason.get(item["reason"], 0) + 1
        source_body_downgraded = {
            "count": len(stats["source_body_downgraded"]),
            "line_ids": [item["line_id"] for item in stats["source_body_downgraded"]],
            "by_reason": body_by_reason,
        }
        source_usage_invalid_hits = count_invalid_source_usages(edition)
        dropped_inferences = run_check_d_inferences(edition)
        # 改修27-2第6回(S7): コードリストは、--hypothesesの有無にかかわらずここで1回だけ読む
        # (推論欄の会社名の検査・上段の検査・下段の検査が同じ結果を使う)。
        codelist_rows, _codelist_date = edinet_codelist.load_codelist()
        inference_company_names = run_check_inference_company_names(
            edition, build_listed_company_matcher(codelist_rows)
        )
        # 改修27-2第4回(S2・S3): 検査36を検査10より先に行う。日付だけの出典は、本文に日付が
        # 見つからなければ日付不明としてchangeの行をここで落とし(検査10に届かないので
        # 二重に数えない)、時刻付きの出典は今までどおり記録だけ(行は落とさない)。
        published_date_not_found = run_check_published_date_only_required(edition, cache_dir_arg)
        record_dropped_lines(edition, known_line_ids, dropped_by, LINE_DROP_CHECK_NAMES["published_date_not_found"])
        # 改修31第1回(3-5): 時刻付きの出典(EDINET以外)も、本文に日付が見つからなければchangeの行を落とす。
        published_timed_date_not_found = run_check_published_timed_date_required(edition, cache_dir_arg)
        record_dropped_lines(edition, known_line_ids, dropped_by, LINE_DROP_CHECK_NAMES["published_timed_date_not_found"])
        published_at_unverified_hits, published_at_unverified_sources = run_check_published_at(edition, cache_dir_arg)
        stale_hits, unknown_published_at_hits, stale_source_hits_by_kind = run_check_e_stale_sources(edition, run_at_dt)
        record_dropped_lines(edition, known_line_ids, dropped_by, LINE_DROP_CHECK_NAMES["stale_source"])
        stale_check_skipped = 0  # run_at_dtは常に読み取れるため、判定を飛ばす理由が無い。

        # 改修27-2第9回の2回目(要件3.1): 続報の判定。行を落とす検査(検査7・36・10)がすべて
        # 終わった後に、今紙面に残っている行の出典で比べる。一覧・過去の号が読めない場合は
        # 比べる号が減るだけで、号は止めない(記録は1回目のrecent_editions_*と同じ)。
        followup_records = apply_followups(edition, recent_loaded, load_hub_urls(script_dir / HUB_URLS_FILENAME))
        # 修正5: 枠(change)に関係なく、出典そのものでpublished_atが無いものを数える。
        sources_published_at_null = count_sources_published_at_null(edition)

        # 検査20: 号の遅延判定。常に今回の実行時刻で判定する(修正B)。first_runの
        # 中の値は判定に使わない(AIが書けるファイルの中にある値のため)。
        baseline_late = run_check_baseline_late(edition, run_at_dt)
        edition["baseline_late"] = baseline_late

        # 修正C: 既に照合済みの号を、遅延の判定が変わる形で照合し直すと、
        # 企業欄が消えてしまう。それを防ぐため、書き戻す前に中止する。
        if should_abort_rerun(existing_verification, baseline_late):
            raise EditionInvalid(
                "この号は既に照合済みです。いま照合し直すと号の遅延判定(baseline_late)が"
                "偽から真に変わり、企業欄(下段)が削除されてしまうため、"
                "何も書き換えずに中止しました"
                f"(実行時刻: {run_at_dt.isoformat()}, slot: {edition.get('slot')})。"
            )

        hypothesis_violations = 0
        hypothesis_reasons = {}
        hypothesis_extra = {
            "codelist_unavailable": False,
            "baseline_date_check_skipped": 0,
            "evidence_filer_name_overridden": 0,
            "evidence_doc_type_overridden": 0,
            "impact_kind_overridden": 0,
            "impact_kind_source_counts": {},
            "impact_kind_undetermined_by_doc_type": {},
            "edinet_url_unparsed": 0,
            "edinet_doclist_unavailable": edinet_companies is None,
            # 改修27-1(4-5): --hypotheses未指定でもキーがそろうよう、既定値を0にしておく。
            "horizon_overridden": 0,
            "deadline_uncomputable": 0,
            "horizon_recount_mismatch": 0,
            "horizon_recount_skipped": 0,
            # 改修27-1(4-10): --hypotheses未指定でもキーがそろうよう、既定値を0にしておく。
            "tob_side_subject_code_missing": 0,
            # 改修27-2第5回(S5): --hypotheses未指定でもキーがそろうよう、既定値にしておく。
            "check11_removed": [],
            "name_match_stage": {stage: 0 for stage in NAME_MATCH_STAGES},
            "check11_longer_name_check_skipped": 0,
            # 改修27-2第8回(S9): --hypotheses未指定でもキーがそろうよう、既定値にしておく。
            "check37_removed": [],
            # 改修31第1回(3-1・3-2): --hypotheses未指定でもキーがそろうよう、既定値にしておく。
            "check37_line_ids_trimmed": [],
            "primary_line_ids_trimmed": [],
        }
        # 改修31第1回(3-6): --hypotheses未指定でもキーがそろうよう、既定値(0件)にしておく。
        deprecated_keys_removed = {key: 0 for key in DEPRECATED_HYPOTHESIS_KEYS}
        hypotheses_doc = None
        field_type_invalid_removed = []
        industry_pick_field_type_invalid_removed = []
        hypotheses_generated_at_raw = None
        industry_report = None
        # 改修27-1(4-12): --hypotheses未指定でもキーがそろうよう、既定値(0件)にしておく。
        speculative_word_counts = {
            "relation_text": {w: 0 for w in SPECULATIVE_WORDS},
            "impact_reason": {w: 0 for w in SPECULATIVE_WORDS},
        }
        # 改修27-2(S13): --hypotheses未指定でもキーがそろうよう、既定値(0件)にしておく。
        reported_relation_text_mismatch = {"count": 0, "hypothesis_ids": []}
        # 改修27-2第9回: --hypotheses未指定でもキーがそろうよう、既定値(0件)にしておく。
        cross_edition_duplicates = {"count": 0, "duplicates": []}
        # 改修31第3回: 記録だけ足すもの(上段・業種の分)。--hypotheses未指定でもキーがそろうよう、既定値にしておく。
        upper_trading_between = []
        reported_name_in_snippet = []
        relation_text_role_mismatch = []
        industry_picks_not_shown = []
        industries_shown_status = "no_hypotheses_file"
        if hypotheses_file:
            try:
                hypotheses_doc = load_json(hypotheses_file)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                raise EditionInvalid(f"仮説JSONを読み込めません: {e}")
            # 改修27-2 第8回の追加2: 仮説の値を読むどの処理よりも先に、型の問題のある仮説だけを削除する
            # (理由field_type_invalid)。仮説ファイル全体の型の問題は保存できない(終了コード1)。
            validate_hypotheses_doc_structure(hypotheses_doc)
            field_type_invalid_removed = remove_type_invalid_hypotheses(hypotheses_doc)
            industry_pick_field_type_invalid_removed = remove_type_invalid_industry_picks(hypotheses_doc)
            # 改修31第1回(3-6): 上段の廃止キー(direction・evidence_excerpt・falsifier)を取り除く(件数を記録)。
            # 紙面の推論欄のfalsifierは触らない。
            deprecated_keys_removed = remove_deprecated_hypothesis_keys(hypotheses_doc)
            # 改修27-1(4-1): 仮説ファイルのgenerated_atも、紙面と同じく実行時刻で上書きする。
            hypotheses_generated_at_raw = override_generated_at(hypotheses_doc, run_at)
            hypotheses_doc["baseline_late"] = baseline_late
            hypotheses_doc["market_open"] = edition["market_open"]
            # 修正12(a): コードリストは上段(hypotheses)・下段(industry_examples)の両方の
            # 検査(21・31)で使う。改修27-2第6回から、読み込みは推論欄の検査の前に1回だけ
            # 行っている(上のcodelist_rows。同じファイルを2回読む作りにしない)。

            # 改修27-1(4-12): 仮説(hypotheses)側の記録専用キーは、検査で仮説が
            # 消される前の全件を対象に数える(run_hypothesis_checks()がhypotheses配列を
            # 絞り込む前に計算すること)。
            speculative_word_counts = compute_speculative_word_counts(hypotheses_doc.get("hypotheses") or [])
            banned_word_hits.extend(compute_banned_word_hits_hyps(hypotheses_doc.get("hypotheses") or []))
            reported_relation_text_mismatch = compute_reported_relation_text_mismatch(
                hypotheses_doc.get("hypotheses") or []
            )

            hypothesis_violations, hypothesis_reasons, hypothesis_extra = run_hypothesis_checks(
                hypotheses_doc, edition, business_days, ng_words, cache_dir_arg, edinet_companies, codelist_rows,
                line_drop_info={"original_line_ids": original_line_ids, "dropped_by": dropped_by},
            )
            if field_type_invalid_removed:
                # 型の問題で先に削除した仮説も、指摘件数と理由に数える。
                hypothesis_violations += len(field_type_invalid_removed)
                hypothesis_reasons["field_type_invalid"] = len(field_type_invalid_removed)

            # 検査14: 休場日(market_openがfalse)、または遅延号(baseline_lateがtrue)の号は、
            # 上段(hypotheses、run_hypothesis_checks側で既に空にしている)だけでなく、
            # 下段(industry_examples)も作らない。
            skip_companies = (edition["market_open"] is False) or baseline_late
            if skip_companies:
                industry_picks_discarded = len(hypotheses_doc.get("industry_picks") or [])
                hypotheses_doc["industry_examples"] = []
                industry_report = {
                    "industry_picks_discarded": industry_picks_discarded,
                    # 修正3・修正2: 下段を作らなかった号でも、作った号との対称性の
                    # ため常にこのキーを出す(0件)。
                    "legacy_substring_rule_dropped": {"count": 0, "names": []},
                    "generic_name_dropped": {"count": 0, "names": []},
                }
            else:
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
                # codelist_rowsは上段の検査を呼ぶ前に読み込み済みのものをそのまま使う
                # (修正12(a): 同じファイルを2回読む作りにしない)。
                kept_examples, lower_reasons, allowed_industries = run_industry_example_checks(
                    hypotheses_doc["industry_examples"], edition, codelist_rows
                )
                hypotheses_doc["industry_examples"] = kept_examples

                # 7. 枠配分の検査29は、上段・下段の個別の検査が終わった後に行う。
                slot_total, slot_reasons = run_slot_allocation(hypotheses_doc, edition)

                industry_report = {
                    # 修正E: 休場日・遅延号で下段を作らなかった場合との対称性のため、
                    # 下段を作った号でも常にこのキーを出す(0件)。
                    "industry_picks_discarded": 0,
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
                    # 修正3: 旧規則4(pick_industry_companies._filter_usable_names)で
                    # 本文照合から外した会社。コードリスト未取得(fatal_error)の場合は
                    # pick_industry_companies.run()側でこのキーを作らないため0件で補う。
                    "legacy_substring_rule_dropped": pick_result.get(
                        "legacy_substring_rule_dropped", {"count": 0, "names": []}
                    ),
                    # 修正2: 一般語辞書(scripts/generic_words.txt、名寄せ規則6)で
                    # 本文照合から外した会社。
                    "generic_name_dropped": pick_result.get(
                        "generic_name_dropped", {"count": 0, "names": []}
                    ),
                }

            # 改修27-2第9回(要件3.4(2)): 号をまたぐ重複。すべての検査と枠の配分(検査29)の後に
            # 残った上段の仮説を、比べる範囲の過去の号の仮説と比べる(削除はしない)。
            cross_edition_duplicates = apply_cross_edition_duplicates(
                hypotheses_doc["hypotheses"],
                {s.get("source_id"): s for s in edition.get("sources", [])},
                recent_loaded,
            )

            # 改修31第3回(3-1・3-4・3-5・3-6): 記録だけ。すべての検査と枠の配分の後に残った上段の会社と、
            # 照合を通った業種を対象にする(会社・行の印は変えない)。
            survivors = hypotheses_doc["hypotheses"]
            survivor_sources = {s.get("source_id"): s for s in edition.get("sources", [])}
            upper_trading_between = compute_upper_trading_between(
                survivors, survivor_sources, edition, business_days, run_at_dt,
            )
            reported_name_in_snippet = compute_reported_name_in_snippet(survivors, survivor_sources, cache_dir_arg)
            relation_text_role_mismatch = compute_relation_text_role_mismatch(survivors)
            skip_reason = None
            if skip_companies:
                skip_reason = "market_closed" if edition["market_open"] is False else "baseline_late"
            industries_shown, industry_picks_not_shown, industries_shown_status = compute_industries_shown(
                hypotheses_doc, edition, codelist_rows, skip_reason,
            )
            hypotheses_doc["industries_shown"] = industries_shown

        # 修正2: first_runが無ければ今回の値で作る。あれば中身を一切書き換えず、
        # そのまま引き継ぐ(2回目以降の照合でAIの初回申告が消えないように)。
        if existing_first_run is not None:
            first_run = existing_first_run
        else:
            first_run = build_first_run_record(
                run_at, run_at_dt, baseline_late,
                market_open_result["reported"], source_policy_result["overwritten"],
                generated_at_raw,
                hypotheses_generated_at_raw=hypotheses_generated_at_raw,
            )

        # 修正4: 号に残っている行(停止語・出典の鮮度の検査が終わった後)で数える。
        number_coverage = compute_number_coverage(edition)
        # 改修27-1(4-12): change_verified_lines_by_sectionも、最終的に号に残っている
        # 行(停止語・出典の鮮度の検査が終わった後)で数える。
        change_verified_lines_by_section = compute_change_verified_lines_by_section(edition)
        # 改修31第3回(3-2・3-3): 最終的に号に残っている行の印で数える(記録だけ)。
        unregistered_numbers = compute_unregistered_numbers(edition)
        short_excerpts = compute_short_excerpts(edition)

        edition["verification"] = {
            "script_version": "2.0.0",
            "run_at": run_at,
            "first_run": first_run,
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
            # 改修27-2第3回(S4): 上の合計(stale_source_hits)の内訳。時刻付きの出典(36時間超)と
            # 日付だけの出典(号の日付の前日より前、または号の日付より後)で落とした行の件数。
            "stale_source_hits_by_kind": stale_source_hits_by_kind,
            "unknown_published_at_hits": unknown_published_at_hits,
            "stale_check_skipped": stale_check_skipped,
            "inference_dropped": dropped_inferences,
            # 改修27-2第6回(S7): 推論欄の4項目に上場会社の名前が入っていたため削除した推論
            # (件数と、記事ID・項目・社名・当たった語)。コードリストが読めない日は検査を行わず、
            # inference_company_name_check_skippedを真にする。
            "inference_company_name_removed": inference_company_names,
            "inference_company_name_check_skipped": codelist_rows is None,
            "hypothesis_violations": hypothesis_violations,
            "unverified_reasons": stats["unverified_reasons"],
            "ticker_crosscheck": ticker_crosscheck,
            # 改修27-1(4-4): EDINET書類一覧2日分のうち、読めなかった日付(記録専用)。
            # 両方読めれば空配列。
            "edinet_doclist_partial": edinet_doclist_partial,
            # 改修27-1(4-2・4-3): EDINETの出典のpublished_atを機械で上書きした件数
            # (個々の書類・一覧そのものの両方を合わせた件数。記録専用)。
            "edinet_published_at_overwritten": edinet_published_at_overwritten,
            # 改修27-1第3回の小さな修正1: 一覧でも個々の書類でもない、EDINETの別の
            # URL(会社の検索ページ等)だった出典の件数(記録専用。published_atは
            # 変えていない)。
            "edinet_other_url_hits": edinet_other_url_hits,
            # 改修27-1(4-8): EDINETの個々の書類ごとに、どのファイルを本文として
            # 選んだか(edinet_fetch.pyが書き出したSRC-xxx.files.jsonを写したもの。
            # 記録専用)。
            "edinet_doc_files": edinet_doc_files,
            # 改修27-1第6回: view_urlを書いた(Noneでない)出典の件数(記録専用)。
            "edinet_view_url_count": edinet_view_url_count,
            # 改修27-1(4-6): 出典・行のattribution/processing_noteをひな形で
            # 上書きした件数(nullから値にした件数も含む)と、ひな形に埋める値が
            # 足りず生成をやめた件数(記録専用)。
            "attribution_overwritten": source_attribution_result["attribution_overwritten"],
            "attribution_generation_skipped": source_attribution_result["attribution_generation_skipped"],
            "baseline_late": baseline_late,
            # 改修27-2第2回(S14): 時間帯がずれていれば号を保存しないため、
            # 保存される号ではslot_mismatchは常に偽(キーをそろえるために残す)。
            "slot_expected": slot_expected,
            "slot_mismatch": slot_mismatch,
            "source_usage_invalid_hits": source_usage_invalid_hits,
            "source_policy_applied": True,
            "source_policy_overwritten": source_policy_result["overwritten"],
            "source_policy_unlisted_domains": source_policy_result["unlisted_domains"],
            "market_open_source": "calendar",
            "market_open_overwritten": market_open_result["overwritten"],
            "market_open_reported": market_open_result["reported"],
            "codelist_unavailable": codelist_rows is None,
            "baseline_date_check_skipped": hypothesis_extra["baseline_date_check_skipped"],
            # 2026年9月21日の追加指示: evidence_filer_name/evidence_doc_type/impact_kindを
            # AIの値からEDINET書類一覧で機械判定した値へ上書きした件数・内訳(記録専用)。
            "evidence_filer_name_overridden": hypothesis_extra["evidence_filer_name_overridden"],
            "evidence_doc_type_overridden": hypothesis_extra["evidence_doc_type_overridden"],
            "impact_kind_overridden": hypothesis_extra["impact_kind_overridden"],
            "impact_kind_source_counts": hypothesis_extra["impact_kind_source_counts"],
            "impact_kind_undetermined_by_doc_type": hypothesis_extra["impact_kind_undetermined_by_doc_type"],
            "edinet_url_unparsed": hypothesis_extra["edinet_url_unparsed"],
            "edinet_doclist_unavailable": hypothesis_extra["edinet_doclist_unavailable"],
            # 改修27-1(4-5): horizon_business_days/deadline_dateを機械で埋めた件数・
            # 計算できなかった件数(記録専用。会社を消すのはdeadline_uncomputableの
            # 場合のみで、その削除は仮説に関する指摘件数の方に出る)。
            "horizon_overridden": hypothesis_extra["horizon_overridden"],
            "deadline_uncomputable": hypothesis_extra["deadline_uncomputable"],
            # 検査17(改修27-1・4-5で検算に変更): 別の書き方で数え直した期限日がずれた件数
            # (判定には使わない。会社は消さない)。
            "horizon_recount_mismatch": hypothesis_extra["horizon_recount_mismatch"],
            # 改修27-1第2回: 検査17の検算で起算日が求められなかった件数(通常は0。
            # 検査32のbaseline_date_check_skippedとは別のキー)。
            "horizon_recount_skipped": hypothesis_extra["horizon_recount_skipped"],
            # 改修27-1(4-10): tob_sideの決定でsubjectEdinetCodeが取れなかった件数(記録専用)。
            "tob_side_subject_code_missing": hypothesis_extra["tob_side_subject_code_missing"],
            # 改修27-2第5回(S5): 検査11(上段のprimaryの会社名が出典本文にあるか)。
            # 削除した会社(社名と理由)・合格した会社の見つかった段階ごとの件数・コードリストが
            # 読めず「より長い別の社名の一部」の判定を飛ばした件数。
            "check11_removed": hypothesis_extra["check11_removed"],
            "name_match_stage": hypothesis_extra["name_match_stage"],
            "check11_longer_name_check_skipped": hypothesis_extra["check11_longer_name_check_skipped"],
            # 改修27-2第8回(S9): 検査37(上段の会社の根拠の行・記事・出典)で削除した会社。
            # 社名・記事ID・行ID・当たった理由すべて(複数の理由に当たれば全部)。
            "check37_removed": hypothesis_extra["check37_removed"],
            # 改修31第1回(3-2): 検査37で、日付・鮮度の検査で落とされた行をline_idsから外した会社
            # (会社は残した。外した行IDと、落とした検査の名前)。
            "check37_line_ids_trimmed": hypothesis_extra["check37_line_ids_trimmed"],
            # 改修31第1回(3-1、案1): primaryの会社のline_idsから、印がsource_number_matchでない行を
            # 外した記録(会社は残した。外した行IDとその行の印)。
            "primary_line_ids_trimmed": hypothesis_extra["primary_line_ids_trimmed"],
            # 改修31第1回(3-6): 号の最上位から取り除いたcorrectionsの件数と、上段の仮説から取り除いた
            # 廃止キーの件数(キーごと)。
            "corrections_removed": corrections_removed,
            "deprecated_keys_removed": deprecated_keys_removed,
            # 改修27-2 第8回の追加2: 値の型の問題(文字でもnullでもない、など)で先に削除した上段の仮説
            # (位置・hypothesis_id・項目名)と、取り除いた業種の指定(位置・項目名)。
            "field_type_invalid_removed": field_type_invalid_removed,
            "industry_pick_field_type_invalid_removed": industry_pick_field_type_invalid_removed,
            # 改修27-2第4回(S2): 日付だけの出典で、本文に日付が見つからず(または本文が読めず)
            # 日付不明にした出典と、そのためにchangeの枠から落とした行。
            "published_date_not_found": published_date_not_found,
            # 改修31第1回(3-5): 時刻付きの出典(EDINET以外)で、本文に日付(前日を含む候補)が見つからず
            # (または本文が無い・読めず)日付不明にした出典と、そのためにchangeの枠から落とした行。
            "published_timed_date_not_found": published_timed_date_not_found,
            "published_at_unverified_hits": published_at_unverified_hits,
            "published_at_unverified_sources": published_at_unverified_sources,
            # 修正5: 記録専用(判定には使わない)。既存のunknown_published_at_hitsは変えない。
            "sources_published_at_null": sources_published_at_null,
            # 修正4: 記録専用(判定には使わない)。
            "number_coverage": number_coverage,
            # 修正8: 記録専用(判定には使わない)。
            "rerun_detected": rerun_detected,
            # 改修27-1(4-12): AIの書きぶりを測る記録専用のキー(会社も行も消さない)。
            "date_only_number_lines": date_only_number_lines,
            "self_declared_unverified": self_declared_unverified,
            "banned_word_hits": banned_word_hits,
            "speculative_word_counts": speculative_word_counts,
            "change_verified_lines_by_section": change_verified_lines_by_section,
            # 改修27-1(4-11): scripts/recent_headlines.pyがこの号のために正しく
            # 実行されたか(記録専用。号は止めない)。
            "recent_headlines_failed": recent_headlines_failed,
            # 改修27-2(S1): published_atが日付だけ(時刻なし)の出典(機械が判定)。
            "published_date_only_sources": published_date_only_sources,
            # 改修27-2(S12): titleかurlが空の出典を参照していたため印をunverifiedにした行。
            "empty_title_or_url_refs": empty_title_or_url_refs,
            # 改修28第1回: 出典の無い「報道で見た・未確認」の行(印をunverifiedにした)。
            "reported_without_source": reported_without_source,
            # 改修29: 抜き出しが出典の本文のどの1行にも収まらなかった(2行以上をつないでいた)行
            # (第2回から印をunverifiedにした)と、本文を1行に区切れず1行の判定をしなかった出典
            # (記録専用。印は下げない)。
            "excerpt_spans_lines": excerpt_spans_lines,
            "excerpt_line_check_skipped": excerpt_line_check_skipped,
            # 改修31第2回(3-4): check_excerpts.pyの実行記録(キャッシュのCHECK-EXCERPTS-{edition_id}.jsonl)のまとめ
            # (記録専用。実行回数・1回目の確認から消えた行・印が変わった行・外れた数字・最後の確認の後に
            # 紙面が書き換えられたか。印・会社・終了コードは変えない)。
            "excerpt_check_log": excerpt_check_log,
            # 改修31第3回: 記録だけ足すもの(会社も行も消さない。印・会社・終了コードは変えない)。
            # 3-1 上段の会社ごとの、書類の提出から株価の基準時点までにはさまった取引時間(分)。
            "upper_trading_between": upper_trading_between,
            # 3-2 確定した印がsource_number_matchの行の、textにあってnumbersに登録していない数字
            # (年・月・日・時・分の直前の数字は除く)。
            "unregistered_numbers": unregistered_numbers,
            # 3-3 数字・記号・空白を除いた文字が3文字以下の抜き出し。
            "short_excerpts": short_excerpts,
            # 3-4 報道由来(reported)の上段の会社の名前が、根拠の出典の本文にあるか。
            "reported_name_in_snippet": reported_name_in_snippet,
            # 3-5 relation_textの言葉(自ら提出・買付者・公開買付けの対象)と、機械が決めた立場の食い違い。
            "relation_text_role_mismatch": relation_text_role_mismatch,
            # 3-6 画面に「関係しそうな業種」として出す業種の一覧(仮説ファイルのindustries_shown)に入れなかった
            # 業種の指定と理由、一覧の状態(ok・market_closed・baseline_late・codelist_unavailable)。
            "industry_picks_not_shown": industry_picks_not_shown,
            "industries_shown_status": industries_shown_status,
            # 改修28第2回: EDINET以外のquotableの出典で本文ファイルがあるものを、機械で保存されたもの
            # (machine_saved)・記録ファイルが無いもの(not_machine_saved)・食い違ったもの
            # (reconvert_mismatch、detailsに食い違った項目)・道具が無くて確かめられなかったもの
            # (reconvert_skipped)に分けた記録。改修31第1回(3-4)から、not_machine_savedと、reconvert_mismatchの
            # うちURL・ハッシュ・元のファイル・記録ファイルが食い違った出典は、参照する行の印を下げる。
            "source_body_check": source_body_check,
            # 改修31第1回(3-4): 上の確認で、記録ファイルが無い(source_body_not_machine_saved)・URLやハッシュが
            # 食い違った(source_body_mismatch)出典を参照していたため、印をunverifiedにした行。
            "source_body_downgraded": source_body_downgraded,
            # 改修31第1回(3-3): 出典が検索結果の断片でない「報道で見た・未確認」の行(印をunverifiedにした)。
            "reported_source_not_snippet": reported_source_not_snippet,
            # 改修27-2(S13): reportedの上段の会社のうち、relation_textが定型文と違うもの
            # (記録専用。会社は消さない)。
            "reported_relation_text_mismatch": reported_relation_text_mismatch,
            # 改修27-2第9回: 号をまたいで比べる範囲の号について、一覧(editions/index.json)が
            # 読めなかったか・読めなかった号(edition_idと、紙面・仮説のどちらか)(記録専用)。
            "recent_editions_index_unavailable": recent_editions_index_unavailable,
            "recent_editions_unreadable": recent_editions_unreadable,
            # 改修27-2第9回(要件3.4(2)): 範囲内の過去の号と(ticker, 書類管理番号)が一致した
            # 上段の仮説(duplicate_ofを書き、auto_check_targetを偽にした。削除はしない)。
            "cross_edition_duplicates": cross_edition_duplicates,
            # 改修27-2第9回の2回目(要件3.1): 続報の判定の記録(記事は消さない)。続報・続報でない
            # 記事の件数、新しい出典の無い続報、URLをそろえて初めて一致した組、見出しの「続報」と
            # 機械の判定の食い違い(記録だけ)。
            "followup_counts": followup_records["followup_counts"],
            "followup_without_new_source": followup_records["followup_without_new_source"],
            "url_normalized_matches": followup_records["url_normalized_matches"],
            "followup_headline_mismatch": followup_records["followup_headline_mismatch"],
        }
        if industry_report is not None:
            edition["verification"].update(industry_report)

        with open(edition_path, "w", encoding="utf-8") as f:
            json.dump(edition, f, ensure_ascii=False, indent=1)

        if hypotheses_file:
            with open(hypotheses_file, "w", encoding="utf-8") as f:
                json.dump(hypotheses_doc, f, ensure_ascii=False, indent=1)

        print_report(
            edition_file, stats, stop_hits, watch_hits, dropped_inferences, stale_hits,
            unknown_published_at_hits, stale_check_skipped,
            hypothesis_violations, hypothesis_reasons, number_failure_details, ok=True,
            baseline_late=baseline_late, ticker_crosscheck=ticker_crosscheck,
            source_usage_invalid_hits=source_usage_invalid_hits,
            industry_report=industry_report,
            source_policy_unlisted_domains=source_policy_result["unlisted_domains"],
            number_coverage=number_coverage,
            sources_published_at_null=sources_published_at_null,
            rerun_detected=rerun_detected,
            published_date_not_found=published_date_not_found,
            inference_company_names=inference_company_names,
            inference_company_name_check_skipped=codelist_rows is None,
            source_body_check=source_body_check,
            excerpt_spans_lines=excerpt_spans_lines,
            excerpt_line_check_skipped=excerpt_line_check_skipped,
            published_timed_date_not_found=published_timed_date_not_found,
            excerpt_check_log=excerpt_check_log,
            upper_trading_between=upper_trading_between,
        )
        return 0

    except EditionInvalid as e:
        print("=" * 60)
        print(f"照合結果: {edition_file}")
        print("=" * 60)
        print(f"→ この号は保存できません。理由: {e}")
        return 1


def main():
    """コマンドとしての入口。実行時刻は必ずここで現在時刻(日本時間)を取り、
    run_verification()に渡すだけにする(引数・環境変数・ファイルから時刻を
    受け取る経路は作らない)。コマンドの引数と出力の形は改修27-2第2回の前と同じ。"""
    run_at_dt = dt.datetime.now(JST)

    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", required=True)
    parser.add_argument("--hypotheses")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--calendar", required=True)
    args = parser.parse_args()

    return run_verification(args.edition, args.hypotheses, args.cache, args.calendar, run_at_dt)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
