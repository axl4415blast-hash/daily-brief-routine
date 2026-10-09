"""改修29第1回: 抜き出しを照合の前に確かめるコマンド(紙面と仮説のファイルは書き換えない。改修31第2回から、
キャッシュの中の実行記録にだけ、1回ごとに1行書き足す)。

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
      [--hypotheses hypotheses/{日付}-{時間帯}.json]
  python3 scripts/check_excerpts.py --edition editions/{日付}/{時間帯}.json --show-lines SRC-xxx [--grep 文字]
      (機械が区切った本文の「1行」を、1始まりの番号付きで表示する。抜き出しの確認も実行記録もしない)

実行記録(改修31第2回): 抜き出しの確認のたびに、キャッシュ(--cache-dir)の CHECK-EXCERPTS-{edition_id}.jsonl に
1回分(実行時刻・紙面のハッシュ・全行の行ID/申告した印/数字/状態・件数・終了コード)を1行書き足す(上書きしない)。
照合(verify_edition.py)がこれを読んで、実行回数・消えた行・印が変わった行・外れた数字を記録する。
edition_idが{日付}-{morning|noon|evening}の形でなければ、記録は書かない。

--hypotheses を渡すと、紙面に無い行IDを指している上段の会社(line_ids)・業種の指定(industry_line_ids)も表示する
(1件でもあれば終了コード1。直し方: 消した行のIDを外す。残りの行IDは振り直さない)。

終了コード: 0=すべてok、1=ok以外が1つ以上、2=スクリプト自体のエラー。
"""
import argparse
import datetime as dt
import hashlib
import json
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


SHOW_LINES_DEFAULT_MAX = 200
HYP_FIX_HINT = "直し方：消した行のIDを line_ids・industry_line_ids から外す。残りの行IDは振り直さない"


def find_missing_line_refs(hypotheses_path, edition):
    """改修31第2回(3-3): 仮説ファイルの上段の会社(line_ids)・業種の指定(industry_line_ids)のうち、紙面に無い
    行IDを指しているものを探す。戻り値: {"hypotheses": [{hypothesis_id, company_name, line_ids}],
    "industry_picks": [{article_id, industry, line_ids}]}(line_idsは紙面に無い行ID)。"""
    doc = ve.load_json(hypotheses_path)
    known = ve.line_id_set(edition)

    def missing(ids):
        return [l for l in ids if not isinstance(l, str) or l not in known] if isinstance(ids, list) else []

    result = {"hypotheses": [], "industry_picks": []}
    for hyp in doc.get("hypotheses") or [] if isinstance(doc, dict) else []:
        if isinstance(hyp, dict) and missing(hyp.get("line_ids")):
            result["hypotheses"].append({
                "hypothesis_id": hyp.get("hypothesis_id"), "company_name": hyp.get("company_name"),
                "line_ids": missing(hyp.get("line_ids")),
            })
    for pick in doc.get("industry_picks") or [] if isinstance(doc, dict) else []:
        if isinstance(pick, dict) and missing(pick.get("industry_line_ids")):
            result["industry_picks"].append({
                "article_id": pick.get("article_id"), "industry": pick.get("industry"),
                "line_ids": missing(pick.get("industry_line_ids")),
            })
    return result


def append_run_record(log_path, record):
    """実行記録に1回分を1行書き足す(上書きしない)。直前の行が改行で終わっていなければ、先に改行を足す。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if log_path.is_file() and log_path.stat().st_size > 0:
        with open(log_path, "rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                prefix = "\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(prefix + json.dumps(record, ensure_ascii=False) + "\n")


def run(edition_path, cache_dir, out=sys.stdout, hypotheses_path=None):
    """紙面を読み、source_number_matchの行をすべて調べて表示する(改修31第1回から、出典が検索結果の
    断片でないreported_unverifiedの行も表示する)。改修31第2回から、調べた結果を実行記録に書き足し、
    hypotheses_pathがあれば紙面に無い行IDを指す会社・業種も表示する。戻り値: 終了コード(0か1)。"""
    edition_bytes = Path(edition_path).read_bytes()
    edition = json.loads(edition_bytes.decode("utf-8"))
    # 実行記録に残す行の一覧は、出典の表を当てる前の紙面から作る(表を当てても行の中身は変わらない)。
    snapshot = ve.excerpt_log_line_snapshot(edition)
    # 照合(verify_edition.run_verification)と同じ表・同じ関数で usage・publisher_type を決める(メモリの中だけ。紙面は書き換えない)。
    ve.apply_source_policy(edition, Path(ve.__file__).resolve().parent / "source_policy.csv")
    sources_by_id = {s.get("source_id"): s for s in edition.get("sources", []) if isinstance(s, dict)}
    counts = {status: 0 for status in STATUSES}
    status_by_line = {}
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
        status_by_line.setdefault(line.get("line_id"), status)
        print(f"{line.get('line_id')}  {status}  ({line.get('source_ref')})", file=out)
        for d in detail:
            print(f"    {d}", file=out)
    total = sum(counts.values())
    print("-" * 60, file=out)
    print(f"合計 {total}行: " + "、".join(f"{s} {counts[s]}" for s in STATUSES), file=out)

    missing_refs = None
    missing_total = 0
    if hypotheses_path is not None:
        missing_refs = find_missing_line_refs(hypotheses_path, edition)
        missing_total = len(missing_refs["hypotheses"]) + len(missing_refs["industry_picks"])
        print(f"紙面に無い行IDを指している会社・業種の確認: {hypotheses_path}", file=out)
        if missing_total:
            print(f"    {HYP_FIX_HINT}", file=out)
        for item in missing_refs["hypotheses"]:
            print(f"    上段の会社 {item['hypothesis_id']}({item['company_name']}): 紙面に無い行ID {', '.join(map(str, item['line_ids']))}", file=out)
        for item in missing_refs["industry_picks"]:
            print(f"    業種の指定 {item['article_id']}({item['industry']}): 紙面に無い行ID {', '.join(map(str, item['line_ids']))}", file=out)
        print(
            f"紙面に無い行IDを指している会社 {len(missing_refs['hypotheses'])}件・業種 {len(missing_refs['industry_picks'])}件",
            file=out,
        )

    exit_code = 0 if counts["ok"] == total and missing_total == 0 else 1

    # 実行記録(改修31第2回): この号の記録に、1回分を1行書き足す。
    edition_id = edition.get("edition_id")
    log_path = ve.excerpt_log_path(cache_dir, edition_id)
    if log_path is None:
        print("実行記録：edition_idが{日付}-{morning|noon|evening}の形でないため、記録は書きません", file=out)
    else:
        previous = len(ve.read_excerpt_log(cache_dir, edition_id)["records"])
        record = {
            "edition_id": edition_id,
            "run_at": dt.datetime.now(ve.JST).isoformat(),
            "edition_sha256": hashlib.sha256(edition_bytes).hexdigest(),
            "script_version": ve.EXCERPT_LOG_SCRIPT_VERSION,
            "lines": [dict(item, status=status_by_line.get(item["line_id"])) for item in snapshot],
            "counts": counts,
            "missing_line_refs": missing_refs,
            "exit_code": exit_code,
        }
        append_run_record(log_path, record)
        print(f"実行記録：この号の{previous + 1}回目({log_path})", file=out)
    return exit_code


def show_lines(edition_path, cache_dir, source_id, grep=None, out=sys.stdout):
    """改修31第2回(3-2): 出典の本文を機械が「1行」に区切った結果を、1始まりの番号付きで表示する
    (ふだんの表示の「本文の{n}行目」と同じ数え方)。抜き出しの確認も実行記録もしない。
    戻り値: 終了コード(0=表示した、1=区切れない・出典や本文が無い)。"""
    edition = ve.load_json(edition_path)
    source = next((s for s in edition.get("sources", []) if isinstance(s, dict) and s.get("source_id") == source_id), None)
    if source is None:
        print(f"出典 {source_id} が紙面の出典の一覧(sources)に見つかりません", file=out)
        return 1
    cache_path = Path(cache_dir) / f"{source_id}.txt"
    if not cache_path.is_file():
        print(f"{source_id}: {SKIP_REASON_TEXT['source_unfetchable']}", file=out)
        return 1
    lines, reason = ve.split_source_lines(cache_path, source)
    if lines is None:
        print(f"{source_id}: {SKIP_REASON_TEXT.get(reason) or f'本文を1行に区切れない({reason})'}", file=out)
        return 1
    print(f"{source_id} の本文の「1行」(機械の区切り。全{len(lines)}行。空の行は表示しない)", file=out)
    if grep:
        needle = ve.normalize_text(grep)
        shown = [i for i, seg in enumerate(lines) if needle and needle in ve.normalize_text(seg)]
        limit = None
    else:
        shown = [i for i, seg in enumerate(lines) if _for_display(seg)]
        limit = SHOW_LINES_DEFAULT_MAX
    for i in shown[:limit]:
        print(f"本文の{i + 1}行目: {_for_display(lines[i])}", file=out)
    if limit is not None and len(shown) > limit:
        print(f"残り{len(shown) - limit}行。--grep で絞ってください", file=out)
    if grep:
        print(f"「{grep}」を含む行: {len(shown)}行", file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="抜き出しを照合の前に確かめる(紙面・仮説のファイルは書き換えない)")
    parser.add_argument("--edition", required=True)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--hypotheses", help="仮説ファイル。紙面に無い行IDを指している会社・業種も表示する")
    parser.add_argument("--show-lines", metavar="SRC-xxx", help="この出典の本文を、機械の「1行」の番号付きで表示する")
    parser.add_argument("--grep", help="--show-lines のとき、この文字を含む行だけを表示する")
    args = parser.parse_args(argv)
    if args.show_lines:
        return show_lines(args.edition, args.cache_dir, args.show_lines, args.grep)
    return run(args.edition, args.cache_dir, hypotheses_path=args.hypotheses)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
