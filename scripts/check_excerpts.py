"""改修29第1回: 抜き出しを照合の前に確かめるコマンド(ファイルを1つも書き換えない)。

紙面を作るAIが、照合(verify_edition.py)の前に自分で実行して、抜き出しを直すための道具。
紙面のうち claimed_mark が source_number_match の行すべてについて、照合と同じ関数
(verify_edition.verify_excerpt_against_source: ハッシュ確認・抜き出しの照合・1行の検査・
数字の確認)で調べ、1行ごとに次のどれかを表示する。

  ok                 1行に収まり、数字もすべて見つかった
  spans_lines        どの1行にも収まらない(抜き出しにつながっていた本文の行を前後とも表示する。
                     改修29第2回から、照合ではこの行の印を未確認に下げる)
  excerpt_not_found  抜き出しが出典の本文に見つからない
  number_missing     数字が見つからない(見つからなかった数字を表示する)
  skipped            調べられなかった(本文が無い・ハッシュ不一致・区切れない等。理由も表示する)
  body_check_failed  改修31第1回: 出典の本文ファイルが機械で保存されたものと確かめられない(記録ファイルが
                     無い・URLやハッシュが食い違う)。照合ではこの行の印を未確認に下げる。
                     直し方: scripts/save_source.py で本文を保存し直す
  reported_source_not_snippet
                     改修31第1回: claimed_mark が reported_unverified の行で、出典が検索結果の断片
                     (fetch_method が websearch_snippet、かつ usage が snippet_only)でない。照合ではこの行の
                     印を未確認に下げる。直し方: 検索結果の断片を出典にするか、報道の印(reported_unverified)をやめる

reported_unverified の行は、出典が検索結果の断片でないものだけを表示する(断片の行は表示しない)。

表示する本文の行は、抜き出しの確認に要る行だけにする(本文を丸ごと出さない)。
出典の usage・publisher_type は、照合と同じく source_policy.csv の値で決めてから調べる(紙面には書き戻さない)。紙面を作るAIは usage に null を置くため。

使い方:
  python3 scripts/check_excerpts.py --edition editions/{日付}/{時間帯}.json [--cache-dir .cache/sources]

終了コード: 0=すべてok、1=ok以外が1つ以上、2=スクリプト自体のエラー。
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_edition as ve

DEFAULT_CACHE_DIR = ".cache/sources"
STATUSES = (
    "ok", "spans_lines", "excerpt_not_found", "number_missing", "skipped",
    "body_check_failed", "reported_source_not_snippet",
)
# 1行を表示するときの長さの上限(長い段落や表の行は、抜き出しの周りだけを出す)。
SHOW_CHARS = 120
# またがった行がこれより多いときは、最初と最後の2行ずつだけを出す。
SHOW_SPAN_LINES = 4

SKIP_REASON_TEXT = {
    "no_source_ref": "出典の番号(source_ref)が空",
    "source_ref_not_found": "出典の番号が出典の一覧(sources)に無い",
    "not_quotable": "抜き出しを付けられない出典(scripts/source_policy.csv で quotable でないドメイン、または表に無いドメイン)",
    "no_excerpt": "抜き出し(excerpt)が空",
    "numbers_empty": "数字(numbers)が1つも無い",
    "numbers_malformed": "数字(numbers)の形が正しくない(一覧の中が{\"value\": …}の形でない)",
    "source_unfetchable": "出典の本文のファイルが手元に無い",
    "hash_missing": "出典の本文のハッシュが記録されていない",
    "hash_mismatch": "手元の本文が、記録されたハッシュと一致しない",
    "source_unreadable": "出典の本文が文字コードの問題で読めない",
    "join_mismatch": "本文を1行に区切れない(区切りをつなげ直すと本文と一致しない)",
    "raw_html_unreadable": "本文を1行に区切れない(元のHTMLが読めない)",
}

# 改修31第1回: body_check_failedの理由(照合のverify_edition.compute_source_body_downgrades()の理由)。
BODY_CHECK_REASON_TEXT = {
    "source_body_not_machine_saved": "本文の記録ファイル(.meta.json)が無い(機械で保存されたものではない)",
    "source_body_mismatch": "本文の記録ファイル・元のファイル・URL・ハッシュのどれかが食い違う",
}


_DISPLAY_WS_RE = re.compile(r"[\t\r\n]")
_DISPLAY_LONG_SPACE_RE = re.compile(r" {3,}")


def _for_display(text):
    """表示用に、タブ・改行を空白にし、3つ以上続く空白を2つにまとめる(空白1つと2つ以上の
    違いは数字の切り出しに効くため、その違いは残す)。前後の空白は除く。"""
    return _DISPLAY_LONG_SPACE_RE.sub("  ", _DISPLAY_WS_RE.sub(" ", text)).strip()


def _shorten(text, keep="head"):
    """1行を表示用に短くする(keep="head"なら先頭、"tail"なら末尾を残す)。"""
    text = _for_display(text)
    if len(text) <= SHOW_CHARS:
        return text
    if keep == "tail":
        return "…" + text[-SHOW_CHARS:]
    return text[:SHOW_CHARS] + "…"


def _around_excerpt(seg, excerpt_norm):
    """1行のうち、抜き出しが当たった所の前後だけを返す。"""
    seg_norm, seg_idx = ve._map_line_chars(seg, keep_space=False)
    k = seg_norm.find(excerpt_norm)
    if k < 0 or not excerpt_norm:
        return _shorten(seg)
    first = seg_idx[k]
    last = seg_idx[k + len(excerpt_norm) - 1]
    margin = max(0, (SHOW_CHARS - (last - first + 1)) // 2)
    start = max(0, first - margin)
    end = min(len(seg), last + 1 + margin)
    shown = _for_display(seg[start:end])
    return ("…" if start > 0 else "") + shown + ("…" if end < len(seg) else "")


def _number_values(numbers):
    return ", ".join(str(n.get("value")) for n in numbers)


def check_line(line, sources_by_id, cache_dir, body_downgrades=None):
    """1行を調べる。body_downgrades: {source_id: 理由}(照合と同じ、印を下げる出典)。
    戻り値: (状態, 表示する詳しい行のリスト)。"""
    body_downgrades = body_downgrades or {}
    source_ref = line.get("source_ref")
    excerpt = line.get("excerpt")
    numbers = line.get("numbers") or []
    if ve._is_blank(source_ref):
        return "skipped", [f"理由: {SKIP_REASON_TEXT['no_source_ref']}"]
    if source_ref not in sources_by_id:
        return "skipped", [f"理由: {SKIP_REASON_TEXT['source_ref_not_found']}({source_ref})"]
    source = sources_by_id[source_ref]
    if source.get("usage") != "quotable":
        return "skipped", [f"理由: {SKIP_REASON_TEXT['not_quotable']}"]
    if source_ref in body_downgrades:
        # 改修31第1回: 照合はこの出典を参照する行の印を、抜き出しの結果にかかわらず下げる(skippedには入れない)。
        reason = body_downgrades[source_ref]
        return "body_check_failed", [
            f"理由: {BODY_CHECK_REASON_TEXT.get(reason, reason)}",
            "(照合ではこの行の印を未確認に下げる。scripts/save_source.py で本文を保存し直す)",
        ]
    if ve._is_blank(excerpt):
        return "skipped", [f"理由: {SKIP_REASON_TEXT['no_excerpt']}"]
    if not numbers:
        return "skipped", [f"理由: {SKIP_REASON_TEXT['numbers_empty']}"]
    if not isinstance(numbers, list) or not all(isinstance(n, dict) for n in numbers):
        return "skipped", [f"理由: {SKIP_REASON_TEXT['numbers_malformed']}"]

    line_check = {}
    mark, reason, missing = ve.verify_excerpt_against_source(
        source, source_ref, excerpt, numbers, cache_dir, line_check
    )
    if reason == "excerpt_not_found":
        return "excerpt_not_found", [f"抜き出し: {_shorten(excerpt)}"]
    if "fits" not in line_check:
        # ハッシュ確認・本文の読み込みより先で止まった(1行の検査まで届かなかった)。
        return "skipped", [f"理由: {SKIP_REASON_TEXT.get(reason, reason)}"]

    excerpt_norm = ve.normalize_text(excerpt)
    lines = line_check["lines"]
    if line_check["fits"] is None:
        detail = [f"理由: {SKIP_REASON_TEXT.get(line_check['skipped_reason'], line_check['skipped_reason'])}"]
        if missing:
            detail.append(f"(参考)今までどおりの照合で見つからなかった数字: {_number_values(missing)}")
        return "skipped", detail

    if line_check["fits"] is False:
        detail = ["抜き出しにつながっていた本文の行:"]
        touched = ve.find_spanned_lines(lines, excerpt_norm)
        if not touched:
            detail.append("  (つながっていた行を特定できなかった)")
        else:
            shown = touched
            if len(touched) > SHOW_SPAN_LINES:
                shown = touched[:2] + [None] + touched[-2:]
            for pos, i in enumerate(shown):
                if i is None:
                    detail.append(f"  (…{len(touched) - 4}行省略…)")
                    continue
                keep = "tail" if pos == 0 and len(touched) > 1 else "head"
                detail.append(f"  本文の{i + 1}行目: {_shorten(lines[i], keep)}")
        # 改修29第2回: 1行に収まらない抜き出しは照合で印を下げる(excerpt_spans_lines)ため、数字は確かめない。
        detail.append("(照合ではこの行の印を未確認に下げる。抜き出しを本文の1行に縮めるか、紙面の行を分ける)")
        return "spans_lines", detail

    if missing:
        hit = line_check["hit_lines"][0]
        return "number_missing", [
            f"見つからなかった数字: {_number_values(missing)}",
            f"当たった本文の{hit + 1}行目: {_around_excerpt(lines[hit], excerpt_norm)}",
        ]
    return "ok", []


def check_reported_line(line, sources_by_id):
    """改修31第1回: claimed_markがreported_unverifiedの行のうち、出典が一覧にあって検索結果の断片でない
    行だけをreported_source_not_snippetとして返す(照合のverify_line()と同じ条件)。それ以外(断片の行・
    出典が空・一覧に無い行)は(None, [])を返し、表示しない。"""
    source_ref = line.get("source_ref")
    if ve._is_blank(source_ref) or source_ref not in sources_by_id:
        return None, []
    source = sources_by_id[source_ref]
    if ve.is_search_snippet_source(source):
        return None, []
    return "reported_source_not_snippet", [
        f"出典の取得方法(fetch_method): {source.get('fetch_method')}、扱い(usage): {source.get('usage')}",
        "(照合ではこの行の印を未確認に下げる。検索結果の断片を出典にするか、報道の印をやめる)",
    ]


def run(edition_path, cache_dir, out=sys.stdout):
    """紙面を読み、source_number_matchの行をすべて調べて表示する(改修31第1回から、出典が検索結果の
    断片でないreported_unverifiedの行も表示する)。戻り値: 終了コード(0か1)。"""
    edition = ve.load_json(edition_path)
    # 照合(verify_edition.run_verification)と同じ表・同じ関数で usage・publisher_type を決める(メモリの中だけ。紙面は書き換えない)。
    ve.apply_source_policy(edition, Path(ve.__file__).resolve().parent / "source_policy.csv")
    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", []) if isinstance(s, dict)}
    counts = {status: 0 for status in STATUSES}
    # 改修31第1回: 照合と同じ関数で、本文ファイルの確認で印を下げる出典を決める(ファイルは書き換えない)。
    body_downgrades = ve.compute_source_body_downgrades(ve.run_check_source_body(edition, cache_dir))
    print(f"抜き出しの事前確認: {edition_path}(出典の本文: {cache_dir})", file=out)
    for _section, _article, line in ve.iter_lines(edition):
        claimed = line.get("claimed_mark")
        if claimed == "reported_unverified":
            status, detail = check_reported_line(line, sources_by_id)
            if status is None:
                continue
        elif claimed != "source_number_match":
            continue
        else:
            status, detail = check_line(line, sources_by_id, cache_dir, body_downgrades)
        counts[status] += 1
        print(f"{line.get('line_id')}  {status}  ({line.get('source_ref')})", file=out)
        for d in detail:
            print(f"    {d}", file=out)
    total = sum(counts.values())
    print("-" * 60, file=out)
    print(f"合計 {total}行: " + "、".join(f"{s} {counts[s]}" for s in STATUSES), file=out)
    return 0 if counts["ok"] == total else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="抜き出しを照合の前に確かめる(ファイルは書き換えない)")
    parser.add_argument("--edition", required=True)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    args = parser.parse_args(argv)
    return run(args.edition, args.cache_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
