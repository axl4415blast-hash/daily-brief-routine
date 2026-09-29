"""verify_edition.py の主要な判定が今まで通り動くことを固定するためのテスト。

既存の挙動(検査9・検査11・停止/注意のキー分割・出典本文の文字コード対応)に加え、
今回追加した検査13・15・16・20(検査12は改修27-2で廃止)についても、正例(反応してほしい例)と
負例(反応してほしくない例)の両方を確かめる。その場で一時ファイルを作って確かめ、
テスト中に作った一時ファイル・一時フォルダはテストの終わりに消し、リポジトリには
残さない。

scripts/testdata は実データのフィクスチャだが、verify_edition.py の main() は
検査結果を紙面JSON・仮説JSONへ書き戻すため、直接そのパスを指定して実行すると
scripts/testdata の中身が書き換わってしまう(過去に2回、この事故で「古い行を
落とす検査」のテストデータ自体から古い行が消えた)。そのため、scripts/testdata を
使うテスト(test_testdata_copy_integration)は必ず一時フォルダにコピーしてから、
コピーの方に対して検査を走らせる。それ以外のテストは実データを使わず、その場で
作った架空の会社名(テスト物産、テスト電機など)によるフィクスチャだけを使う。

使い方:
  python3 scripts/selftest_checks.py

1件でも期待と異なれば、終了コード1で終わる。
"""
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edinet_fetch
import edinet_codelist as ec
import verify_edition as ve
import pick_industry_companies as pic
import build_index
import recent_headlines as rh

REPO_ROOT = Path(__file__).resolve().parent.parent

_MISSING = object()  # 辞書にキー自体が無い場合を表す番人(hyp.get()がNoneを返す場合と区別するため)

results = []


def check(name, actual, expected):
    ok = actual == expected
    results.append((name, ok, actual, expected))


def write(dirpath, filename, data):
    """dirpath配下にfilenameを作る。dataがbytesならそのまま、strならutf-8で書く。"""
    p = Path(dirpath) / filename
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8")
    return p


def test_read_source_text():
    """1. 出典本文の文字コード対応(read_source_text)の正例・負例。"""
    name = "テスト物産"
    with tempfile.TemporaryDirectory() as d:
        check(
            "文字コード/UTF-8(BOM無し)のファイルを読める",
            ve.read_source_text(write(d, "utf8.txt", name.encode("utf-8"))),
            name,
        )
        check(
            "文字コード/UTF-8(BOM付き)のファイルを読める",
            ve.read_source_text(write(d, "utf8bom.txt", b"\xef\xbb\xbf" + name.encode("utf-8"))),
            name,
        )
        check(
            "文字コード/UTF-16LE(BOM付き。EDINETのCSVと同じ形)のファイルを読める",
            ve.read_source_text(write(d, "utf16le.txt", b"\xff\xfe" + name.encode("utf-16-le"))),
            name,
        )
        check(
            "文字コード/cp932(Windowsの日本語)のファイルを読める",
            ve.read_source_text(write(d, "cp932.txt", name.encode("cp932"))),
            name,
        )
        check(
            "文字コード/どの候補でも読めない壊れたファイルはNoneを返す(例外を投げない)",
            ve.read_source_text(write(d, "broken.txt", b"\x83\xff\x00\x81")),
            None,
        )
        check(
            "文字コード/ファイルが存在しない場合もNoneを返す",
            ve.read_source_text(Path(d) / "not_exist.txt"),
            None,
        )


def test_check_evidence_source_ref():
    """検査11(primaryの自己申告を機械で確かめる)の正例・負例。文字コード対応も含む。"""
    with tempfile.TemporaryDirectory() as d:
        write(d, "SRC-OK.txt", "テスト物産株式会社の開示書類。".encode("utf-8"))
        write(d, "SRC-NG.txt", "無関係な会社Xの開示書類。".encode("utf-8"))
        write(d, "SRC-UTF16.txt", b"\xff\xfe" + "テスト物産の開示書類。".encode("utf-16-le"))
        write(d, "SRC-BROKEN.txt", b"\x83\xff\x00\x81")

        sources = {
            "SRC-OK": {"source_id": "SRC-OK", "usage": "quotable"},
            "SRC-NG": {"source_id": "SRC-NG", "usage": "quotable"},
            "SRC-UTF16": {"source_id": "SRC-UTF16", "usage": "quotable"},
            "SRC-BROKEN": {"source_id": "SRC-BROKEN", "usage": "quotable"},
            "SRC-LINKONLY": {"source_id": "SRC-LINKONLY", "usage": "link_only"},
        }

        check(
            "検査11/正例: 出典本文に会社名があれば合格(Noneが返る)",
            ve.check_evidence_source_ref(
                {"company_name": "テスト物産", "evidence_source_ref": "SRC-OK"}, sources, d
            ),
            None,
        )
        check(
            "検査11/負例: 出典本文に会社名が無ければ不合格",
            ve.check_evidence_source_ref(
                {"company_name": "テスト物産", "evidence_source_ref": "SRC-NG"}, sources, d
            ),
            "evidence_company_name_not_found",
        )
        check(
            "検査11/正例: UTF-16LEの出典本文でも会社名を確認できる",
            ve.check_evidence_source_ref(
                {"company_name": "テスト物産", "evidence_source_ref": "SRC-UTF16"}, sources, d
            ),
            None,
        )
        check(
            "検査11/負例: evidence_source_refが無い",
            ve.check_evidence_source_ref({"company_name": "テスト物産", "evidence_source_ref": None}, sources, d),
            "evidence_source_ref_missing",
        )
        check(
            "検査11/負例: sourcesに存在しない参照",
            ve.check_evidence_source_ref(
                {"company_name": "テスト物産", "evidence_source_ref": "SRC-NOPE"}, sources, d
            ),
            "evidence_source_not_found",
        )
        check(
            "検査11/負例: usageがlink_onlyの出典は不合格",
            ve.check_evidence_source_ref(
                {"company_name": "テスト物産", "evidence_source_ref": "SRC-LINKONLY"}, sources, d
            ),
            "evidence_source_link_only",
        )
        check(
            "検査11/負例(文字コード): 出典本文がどの候補でも読めなければ"
            "evidence_source_unreadable(evidence_source_not_foundとは区別する)",
            ve.check_evidence_source_ref(
                {"company_name": "テスト物産", "evidence_source_ref": "SRC-BROKEN"}, sources, d
            ),
            "evidence_source_unreadable",
        )


def test_verify_line_source_unreadable():
    """既存の数字照合(verify_line)でも、出典本文が読めない場合はunverified/source_unreadableへ
    格下げされることを確かめる(検査11と同じ弱点への対処)。"""
    import hashlib

    with tempfile.TemporaryDirectory() as d:
        broken_bytes = b"\x83\xff\x00\x81"
        write(d, "SRC-BROKEN.txt", broken_bytes)
        source = {
            "source_id": "SRC-BROKEN",
            "content_sha256": hashlib.sha256(broken_bytes).hexdigest(),
            "usage": "quotable",
        }
        line = {
            "claimed_mark": "source_number_match",
            "numbers": [{"value": 1}],
            "source_ref": "SRC-BROKEN",
            "excerpt": "何か",
            "attribution": "出典：テスト",
            "processing_note": "テスト用の注記",
        }
        mark, reason, _ = ve.verify_line(line, {"SRC-BROKEN": source}, d)
        check("既存関数/出典本文が読めない場合はunverifiedへ格下げ", mark, "unverified")
        check("既存関数/理由はsource_unreadable(excerpt_not_foundと区別する)", reason, "source_unreadable")


def test_check_source_ref_not_found():
    """タスク16-2c-1 修正10(検査33): source_refが空でないのにsources一覧に見つからない
    場合、印はunverified・理由はsource_ref_not_foundになる。対象はclaimed_markの種類を
    問わず本文の行すべて(source_number_matchに限らない)。既存のsource_unfetchable
    (sourcesには載っているがキャッシュに本文が無い)とは意味が分かれることも確かめる。"""
    sources_by_id = {"SRC-OK": {"source_id": "SRC-OK", "usage": "quotable", "content_sha256": "x"}}

    # --- 正例(反応してほしい: 不合格になる。3件) ---
    line_missing_ref = {
        "claimed_mark": "source_number_match", "numbers": [{"value": 1}],
        "source_ref": "SRC-MISSING", "excerpt": "何か", "attribution": "出典：テスト", "processing_note": "注記",
    }
    mark, reason, _ = ve.verify_line(line_missing_ref, sources_by_id, ".")
    check("検査33/正例1: source_number_matchでsource_refが見つからなければ不合格", mark, "unverified")
    check("検査33/正例1: 理由はsource_ref_not_found", reason, "source_ref_not_found")

    line_reported_missing_ref = {"claimed_mark": "reported_unverified", "source_ref": "SRC-MISSING"}
    mark2, reason2, _ = ve.verify_line(line_reported_missing_ref, sources_by_id, ".")
    check("検査33/正例2: reported_unverifiedの行でもsource_refが見つからなければ対象になる", mark2, "unverified")
    check("検査33/正例2: 理由はsource_ref_not_found", reason2, "source_ref_not_found")

    line_explainer_missing_ref = {"claimed_mark": "explainer", "numbers": [], "source_ref": "SRC-MISSING"}
    mark3, reason3, _ = ve.verify_line(line_explainer_missing_ref, sources_by_id, ".")
    check("検査33/正例3: explainerの行でもsource_refが見つからなければ対象になる", mark3, "unverified")
    check("検査33/正例3: 理由はsource_ref_not_found", reason3, "source_ref_not_found")

    # --- 負例(反応してほしくない例。5件以上) ---
    line_explainer_null_ref = {"claimed_mark": "explainer", "numbers": [], "source_ref": None}
    mark4, reason4, _ = ve.verify_line(line_explainer_null_ref, sources_by_id, ".")
    check(
        "検査33/負例1: source_refがnullの解説行は従来どおりexplainerのまま",
        (mark4, reason4), ("explainer", None),
    )

    line_empty_ref = {"claimed_mark": "explainer", "numbers": [], "source_ref": ""}
    mark5, reason5, _ = ve.verify_line(line_empty_ref, sources_by_id, ".")
    check("検査33/負例2: source_refが空文字の行も対象外(explainerのまま)", mark5, "explainer")

    line_found_reported = {"claimed_mark": "reported_unverified", "source_ref": "SRC-OK"}
    mark6, reason6, _ = ve.verify_line(line_found_reported, sources_by_id, ".")
    check("検査33/負例3: source_refが見つかれば従来どおりreported_unverifiedになる", mark6, "reported_unverified")

    line_found_no_cache = {
        "claimed_mark": "source_number_match", "numbers": [{"value": 1}],
        "source_ref": "SRC-OK", "excerpt": "何か", "attribution": "出典：テスト", "processing_note": "注記",
    }
    mark7, reason7, _ = ve.verify_line(line_found_no_cache, sources_by_id, "/nonexistent-cache-dir")
    check(
        "検査33/負例4: source_refは見つかるがキャッシュが無い場合はsource_unfetchable"
        "(source_ref_not_foundとは区別される)",
        (mark7, reason7), ("unverified", "source_unfetchable"),
    )

    line_no_key = {"claimed_mark": "explainer", "numbers": []}
    mark8, reason8, _ = ve.verify_line(line_no_key, sources_by_id, ".")
    check("検査33/負例5: source_refキー自体が無い行も対象外(explainerのまま)", mark8, "explainer")


def test_check_published_at():
    """タスク16-2c-1 修正11(検査36): 出典のpublished_at(発表日)が本文中にどれかの
    書き方(要件定義書v12 13章の6通り)で見つかるかどうかを確かめる。7日間は記録だけで
    行は一切落とさない(markは変更しない)。"""
    with tempfile.TemporaryDirectory() as d:
        write(d, "SRC-ISO.txt", "本文の先頭。2026-09-18に発表した。")
        write(d, "SRC-SLASH0.txt", "本文の先頭。2026/09/18に発表した。")
        write(d, "SRC-SLASH1.txt", "本文の先頭。2026/9/18に発表した。")
        write(d, "SRC-KANJI.txt", "本文の先頭。2026年9月18日に発表した。")
        write(d, "SRC-REIWA.txt", "本文の先頭。令和8年9月18日に発表した。")
        write(d, "SRC-NOYEAR.txt", "本文の先頭。9月18日に発表した。")
        write(d, "SRC-NOTFOUND.txt", "本文の先頭。まったく違う日付が書かれている。")

        def edition_for(source_ids, published_at="2026-09-18T10:00:00+09:00"):
            sources = [{"source_id": sid, "published_at": published_at} for sid in source_ids]
            lines = [{"line_id": f"L-{i}", "source_ref": sid} for i, sid in enumerate(source_ids)]
            return {
                "sources": sources,
                "sections": [{"section_id": "change", "articles": [{"lines": lines}]}],
            }

        # --- 正例(反応してほしい: 確認できない出典として記録される) ---
        edition_fail = edition_for(["SRC-NOTFOUND"])
        hits, srcs = ve.run_check_published_at(edition_fail, d)
        check("検査36/正例: 本文にどの書き方も無ければ確認できなかった出典として記録される", srcs, ["SRC-NOTFOUND"])
        check("検査36/正例: 参照している行数(1行)がhitsに数えられる", hits, 1)
        check(
            "検査36/正例: 行自体は落とされない(markは変更しない。記録するだけ)",
            len(edition_fail["sections"][0]["articles"][0]["lines"]), 1,
        )

        # --- 負例(反応してほしくない例。6つの書き方それぞれ) ---
        for label, sid in [
            ("2026-09-18", "SRC-ISO"), ("2026/09/18", "SRC-SLASH0"),
            ("2026/9/18", "SRC-SLASH1"), ("2026年9月18日", "SRC-KANJI"),
            ("令和8年9月18日", "SRC-REIWA"), ("9月18日", "SRC-NOYEAR"),
        ]:
            edition_ok = edition_for([sid])
            hits_ok, srcs_ok = ve.run_check_published_at(edition_ok, d)
            check(f"検査36/負例(書き方:{label}): 本文にあれば確認できたとみなされ記録されない", srcs_ok, [])
            check(f"検査36/負例(書き方:{label}): hitsも0", hits_ok, 0)

        # published_atがnullの出典は対象外(件数に入らない)
        edition_null_pub = edition_for(["SRC-NOTFOUND"], published_at=None)
        hits_null, srcs_null = ve.run_check_published_at(edition_null_pub, d)
        check(
            "検査36/負例8: published_atがnullの出典は対象外なので件数に入らない",
            (hits_null, srcs_null), (0, []),
        )

        # キャッシュ自体が無い出典も対象外
        edition_no_cache = edition_for(["SRC-NOCACHE"])
        hits_nocache, srcs_nocache = ve.run_check_published_at(edition_no_cache, d)
        check(
            "検査36/負例9: キャッシュに本文のファイルが無い出典も対象外",
            (hits_nocache, srcs_nocache), (0, []),
        )


def test_stale_sources():
    """検査9(36時間ルール)。基準時刻はrun_at_dt(スクリプトの実行時刻)。
    公表時刻が古い行/わからない行を、どちらも件数を分けて落とすことを確かめる。"""
    run_at_dt = dt.datetime.fromisoformat("2026-09-24T08:14:32+09:00")

    def make_edition(published_at):
        return {
            "sections": [
                {
                    "section_id": "change",
                    "articles": [
                        {
                            "lines": [
                                {"line_id": "X-01", "source_ref": "SRC-X"},
                            ]
                        }
                    ],
                }
            ],
            "sources": [{"source_id": "SRC-X", "published_at": published_at}],
        }

    fresh = make_edition("2026-09-23T08:00:00+09:00")
    stale, unknown, _ = ve.run_check_e_stale_sources(fresh, run_at_dt)
    check("検査9/正例: 36時間以内なら落とさない(stale=0)", stale, 0)
    check("検査9/正例: 36時間以内なら行が残る", len(fresh["sections"][0]["articles"][0]["lines"]), 1)

    old = make_edition("2026-09-17T08:50:00+09:00")
    stale, unknown, _ = ve.run_check_e_stale_sources(old, run_at_dt)
    check("検査9/負例: 36時間より古い場合はstale_source_hitsが増える", stale, 1)
    check("検査9/負例: 36時間より古い場合はunknown_published_at_hitsは増えない", unknown, 0)
    check("検査9/負例: 36時間より古い行は落とされる", len(old["sections"][0]["articles"][0]["lines"]), 0)

    unknown_pub = make_edition(None)
    stale, unknown, _ = ve.run_check_e_stale_sources(unknown_pub, run_at_dt)
    check("検査9/負例: 公表時刻がnullの場合はstale_source_hitsは増えない", stale, 0)
    check("検査9/負例: 公表時刻がnullの場合はunknown_published_at_hitsが増える", unknown, 1)
    check("検査9/負例: 公表時刻がnullの行も落とされる", len(unknown_pub["sections"][0]["articles"][0]["lines"]), 0)


def test_stop_and_watch_split():
    """停止(検査C)/注意(近接ルール)のキー分割の正例・負例。"""
    stop_edition = {
        "sections": [
            {
                "section_id": "test",
                "articles": [
                    {"lines": [{"line_id": "S-01", "text": "この銘柄は買い時だ。"}]}
                ],
            }
        ]
    }
    hits = ve.check_c_stop_words(stop_edition, ["買い時"])
    check("停止/正例: 停止語を含む行を検出する", len(hits), 1)

    stop_edition_clean = {
        "sections": [
            {
                "section_id": "test",
                "articles": [
                    {"lines": [{"line_id": "S-02", "text": "輸出額は前年同月比で増えた。"}]}
                ],
            }
        ]
    }
    hits = ve.check_c_stop_words(stop_edition_clean, ["買い時"])
    check("停止/負例: 停止語を含まない行は検出しない", len(hits), 0)

    watch_edition = {
        "sections": [
            {
                "section_id": "test",
                "articles": [
                    {"lines": [{"line_id": "W-01", "text": "株価はこの先も伸びるとみて、買っておきたい。"}]}
                ],
            }
        ]
    }
    hits = ve.check_watch_proximity(watch_edition, [])
    check("注意/正例: 「株価」の近くに「買/売」があれば検出する", len(hits), 1)

    watch_edition_excluded = {
        "sections": [
            {
                "section_id": "test",
                "articles": [
                    {"lines": [{"line_id": "W-02", "text": "トヨタ自動車株式会社の売上高は増えた。"}]}
                ],
            }
        ]
    }
    hits = ve.check_watch_proximity(watch_edition_excluded, ["株式会社", "売上高"])
    check("注意/負例: 除外語(株式会社・売上高)は近接ルールに引っかけない", len(hits), 0)


def test_check_ticker_fields():
    """検査13(ticker/ticker_sourceの確認)の正例・負例。証券コードは実在しない9999/9998。

    2026年9月21日の追加指示により、条件3(EDINET一覧との突き合わせ)は
    ticker_sourceがedinet_seccodeの仮説にだけ適用するようになった(edinet_codelist
    経路は検査31が担当する)。そのため、条件3を確かめる基本ケースはticker_sourceを
    edinet_seccodeにする。また、links.price_historyとtickerの突き合わせは条件3の
    外に出したため、ticker_source・edinet_companiesの有無に関係なく常に働くことを
    別途確かめる。"""
    edinet_companies = [
        {"filer_name": "テスト物産", "ticker": "9999"},
        {"filer_name": "テスト電機", "ticker": "9998"},
    ]

    def base(**overrides):
        hyp = {
            "company_name": "テスト物産",
            "ticker": "9999",
            "ticker_source": "edinet_seccode",
        }
        hyp.update(overrides)
        return hyp

    # --- 正例(反応してほしい: 理由の文字列が返る=不合格) ---
    check(
        "検査13/正例: tickerがnullなら不合格",
        ve.check_ticker_fields(base(ticker=None), edinet_companies),
        "ticker_missing",
    )
    check(
        "検査13/正例: tickerが4桁の数字でなければ不合格",
        ve.check_ticker_fields(base(ticker="99999"), edinet_companies),
        "ticker_missing",
    )
    check(
        "検査13/正例: ticker_sourceが空なら不合格",
        ve.check_ticker_fields(base(ticker_source=""), edinet_companies),
        "ticker_source_missing",
    )
    check(
        "検査13/正例: ticker_sourceがedinet_seccodeでEDINET一覧のtickerと食い違えば不合格",
        ve.check_ticker_fields(base(ticker="9998"), edinet_companies),
        "ticker_mismatch",
    )
    check(
        "検査13/正例: ticker_sourceがedinet_seccodeでEDINET一覧に会社名が見つからなければ不合格(#11)",
        ve.check_ticker_fields(base(company_name="テスト建設"), edinet_companies),
        "ticker_mismatch",
    )
    check(
        "検査13/正例: links.price_historyのURL(/quote/と.Tの間)に違う証券コードが入っていれば不合格",
        ve.check_ticker_fields(
            base(links={"price_history": "https://finance.yahoo.co.jp/quote/9998.T/history"}), edinet_companies
        ),
        "ticker_mismatch",
    )
    check(
        "検査13/正例(#12): links.price_historyの食い違いは、ticker_sourceがedinet_codelistでも不合格",
        ve.check_ticker_fields(
            base(ticker_source="edinet_codelist",
                 links={"price_history": "https://finance.yahoo.co.jp/quote/9998.T/history"}),
            edinet_companies,
        ),
        "ticker_mismatch",
    )
    check(
        "検査13/正例(#12): links.price_historyの食い違いは、EDINET一覧が読めない(None)場合でも不合格",
        ve.check_ticker_fields(
            base(links={"price_history": "https://finance.yahoo.co.jp/quote/9998.T/history"}), None
        ),
        "ticker_mismatch",
    )

    # --- 負例(反応してほしくない: Noneが返る=合格) ---
    check(
        "検査13/負例: 正しい4桁のtickerでEDINET一覧と一致すれば合格",
        ve.check_ticker_fields(base(), edinet_companies),
        None,
    )
    check(
        "検査13/負例: EDINET一覧のファイルが無い場合(None)は条件3を適用せず合格",
        ve.check_ticker_fields(base(ticker="1234"), None),
        None,
    )
    check(
        "検査13/負例: 会社名に前後の全角空白が入っているだけなら一覧と一致して合格",
        ve.check_ticker_fields(base(company_name="　テスト物産　"), edinet_companies),
        None,
    )
    check(
        "検査13/負例: linksが無くても合格(参照するURLが無い)",
        ve.check_ticker_fields(base(links=None), edinet_companies),
        None,
    )
    check(
        "検査13/負例: links.price_historyが/quote/{コード}.Tの形でなければ比較せず合格",
        ve.check_ticker_fields(
            base(links={"price_history": "https://example.test/chart?name=test"}), edinet_companies
        ),
        None,
    )
    check(
        "検査13/負例(★穴をふさぐ本題・#10): ticker_sourceがedinet_codelistの仮説は、"
        "EDINET一覧に社名が無くても検査13の条件3では削除されない(検査31が担当するため)",
        ve.check_ticker_fields(base(ticker_source="edinet_codelist", company_name="テスト建設"), edinet_companies),
        None,
    )
    check(
        "検査13/負例(#10): ticker_sourceがedinet_codelistなら、EDINET一覧のtickerと"
        "食い違っていても条件3では削除されない",
        ve.check_ticker_fields(base(ticker_source="edinet_codelist", ticker="9998"), edinet_companies),
        None,
    )

    # --- 英字入りの証券コード(2024年1月以降の新規上場を想定) ---
    edinet_companies_with_letter = edinet_companies + [
        {"filer_name": "テスト新興", "ticker": "130A"},
    ]

    def base_letter(**overrides):
        hyp = {
            "company_name": "テスト新興",
            "ticker": "130A",
            "ticker_source": "edinet_seccode",
        }
        hyp.update(overrides)
        return hyp

    check(
        "検査13/負例(英字コード): tickerが130AでEDINET一覧にも130A(元の証券コードは130A0)の会社がいれば合格",
        ve.check_ticker_fields(base_letter(), edinet_companies_with_letter),
        None,
    )
    check(
        "検査13/負例(英字コード): links.price_historyがhttps://finance.yahoo.co.jp/quote/130A.T/historyでも合格",
        ve.check_ticker_fields(
            base_letter(links={"price_history": "https://finance.yahoo.co.jp/quote/130A.T/history"}),
            edinet_companies_with_letter,
        ),
        None,
    )
    check(
        "検査13/正例(英字コード): links.price_historyが別会社の証券コード(9730)を指していれば不合格",
        ve.check_ticker_fields(
            base_letter(links={"price_history": "https://finance.yahoo.co.jp/quote/9730.T/history"}),
            edinet_companies_with_letter,
        ),
        "ticker_mismatch",
    )


def test_derive_ticker_and_is_valid_ticker():
    """証券コードの形(edinet_fetch.derive_ticker / is_valid_ticker)の正例・負例。
    2024年1月4日以降に新規上場承認を受けた株式は、証券コードの2桁目・4桁目のいずれか、
    または両方に英大文字(B・E・I・O・Q・V・Zを除く19文字)が入る場合がある。"""

    # --- 反応してほしい例(通るべき) ---
    check("derive_ticker/正例: 数字だけの5桁('12340')は'1234'になる", edinet_fetch.derive_ticker("12340"), ("1234", None))
    check("derive_ticker/正例: 4桁目が英字('130A0')は'130A'になる", edinet_fetch.derive_ticker("130A0"), ("130A", None))
    check("derive_ticker/正例: 2桁目が英字('2A460')は'2A46'になる", edinet_fetch.derive_ticker("2A460"), ("2A46", None))
    check("derive_ticker/正例: 2桁目・4桁目とも英字('8A9A0')は'8A9A'になる", edinet_fetch.derive_ticker("8A9A0"), ("8A9A", None))
    check("derive_ticker/正例: 数字だけの5桁('99990')は'9999'になる", edinet_fetch.derive_ticker("99990"), ("9999", None))

    check("is_valid_ticker/正例: '1234'は正しい形", edinet_fetch.is_valid_ticker("1234"), True)
    check("is_valid_ticker/正例: '130A'(4桁目が英字)は正しい形", edinet_fetch.is_valid_ticker("130A"), True)
    check("is_valid_ticker/正例: '2A46'(2桁目が英字)は正しい形", edinet_fetch.is_valid_ticker("2A46"), True)
    check("is_valid_ticker/正例: '8A9A'(2桁目・4桁目とも英字)は正しい形", edinet_fetch.is_valid_ticker("8A9A"), True)
    check("is_valid_ticker/正例: '9999'は正しい形", edinet_fetch.is_valid_ticker("9999"), True)

    # --- 反応してほしくない例(落ちるべき) ---
    check("derive_ticker/負例: Noneはsec_code_null", edinet_fetch.derive_ticker(None), (None, "sec_code_null"))
    check("derive_ticker/負例: 空文字はsec_code_empty", edinet_fetch.derive_ticker(""), (None, "sec_code_empty"))
    check("derive_ticker/負例: 4文字('1234')はsec_code_not_5chars", edinet_fetch.derive_ticker("1234"), (None, "sec_code_not_5chars"))
    check(
        "derive_ticker/負例: 末尾が0でない('12345')はsec_code_not_ending_zero",
        edinet_fetch.derive_ticker("12345"), (None, "sec_code_not_ending_zero"),
    )
    check(
        "derive_ticker/負例: 使われない英字('1B3A0'のB)はsec_code_unexpected_format",
        edinet_fetch.derive_ticker("1B3A0"), (None, "sec_code_unexpected_format"),
    )
    check(
        "derive_ticker/負例: 小文字('130a0')はsec_code_unexpected_format(大文字に直して通さない)",
        edinet_fetch.derive_ticker("130a0"), (None, "sec_code_unexpected_format"),
    )
    check(
        "derive_ticker/負例: 1桁目が英字('A1230')はsec_code_unexpected_format",
        edinet_fetch.derive_ticker("A1230"), (None, "sec_code_unexpected_format"),
    )

    check("is_valid_ticker/負例: Noneは正しい形でない", edinet_fetch.is_valid_ticker(None), False)
    check("is_valid_ticker/負例: 空文字は正しい形でない", edinet_fetch.is_valid_ticker(""), False)
    check("is_valid_ticker/負例: 4文字でない('12345')は正しい形でない", edinet_fetch.is_valid_ticker("12345"), False)
    check("is_valid_ticker/負例: 使われない英字('1B3A'のB)は正しい形でない", edinet_fetch.is_valid_ticker("1B3A"), False)
    check("is_valid_ticker/負例: 小文字('130a')は正しい形でない", edinet_fetch.is_valid_ticker("130a"), False)
    check("is_valid_ticker/負例: 1桁目が英字('A123')は正しい形でない", edinet_fetch.is_valid_ticker("A123"), False)


def test_check_numbers_empty():
    """検査15(claimed_markがsource_number_matchなのにnumbersが空)の正例・負例。"""
    sources = {}

    # --- 正例(反応してほしい: unverified/numbers_emptyになる) ---
    line_empty = {"claimed_mark": "source_number_match", "numbers": []}
    mark, reason, _ = ve.verify_line(line_empty, sources, "/nonexistent")
    check("検査15/正例: numbersが空配列ならunverifiedになる", mark, "unverified")
    check("検査15/正例: numbersが空配列なら理由はnumbers_empty", reason, "numbers_empty")

    line_missing = {"claimed_mark": "source_number_match"}
    mark, reason, _ = ve.verify_line(line_missing, sources, "/nonexistent")
    check("検査15/正例: numbersキー自体が無くてもnumbers_emptyになる", (mark, reason), ("unverified", "numbers_empty"))

    # --- 負例(反応してほしくない) ---
    line_explainer = {"claimed_mark": "explainer", "numbers": []}
    mark, reason, _ = ve.verify_line(line_explainer, sources, "/nonexistent")
    check("検査15/負例: explainerでnumbersが空でもnumbers_emptyにならない(explainerのまま合格)", mark, "explainer")

    line_reported = {"claimed_mark": "reported_unverified", "numbers": []}
    mark, reason, _ = ve.verify_line(line_reported, sources, "/nonexistent")
    check(
        "検査15/負例: reported_unverifiedでnumbersが空でもnumbers_emptyにならない",
        mark, "reported_unverified",
    )

    line_explainer_with_numbers = {"claimed_mark": "explainer", "numbers": [{"value": 1}]}
    mark, reason, _ = ve.verify_line(line_explainer_with_numbers, sources, "/nonexistent")
    check(
        "検査15/負例: explainerなのにnumbersがあるのはmark_mismatchであってnumbers_emptyではない",
        reason, "mark_mismatch",
    )

    line_numbers_but_missing_fields = {
        "claimed_mark": "source_number_match", "numbers": [{"value": 1}],
    }
    mark, reason, _ = ve.verify_line(line_numbers_but_missing_fields, sources, "/nonexistent")
    check(
        "検査15/負例: numbersはあってもsource_ref等が空ならmissing_fieldであってnumbers_emptyではない",
        reason, "missing_field",
    )

    with tempfile.TemporaryDirectory() as d:
        body = "テスト物産の輸出額は120億円だった。"
        write(d, "SRC-N.txt", body.encode("utf-8"))
        import hashlib
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        sources_full = {"SRC-N": {"source_id": "SRC-N", "content_sha256": digest, "usage": "quotable"}}
        line_full = {
            "claimed_mark": "source_number_match",
            "numbers": [{"value": 120}],
            "source_ref": "SRC-N",
            "excerpt": "輸出額は120億円だった",
            "attribution": "出典：テスト",
            "processing_note": "テスト用の注記",
        }
        mark, reason, _ = ve.verify_line(line_full, sources_full, d)
        check(
            "検査15/負例: numbersが1件以上あり他の条件も満たせばsource_number_matchで合格",
            (mark, reason), ("source_number_match", None),
        )


def test_check_excerpt_not_allowed():
    """検査16(usageがquotableでない、またはusageが正しく書かれていない出典を参照する行に
    excerptがある)の正例・負例。usageが無い・null・想定外の値の出典は、以前は検査の対象外
    (穴)だったが、今回の修正で「quotableではないもの」として扱うようにした。"""
    sources = {
        "SRC-SNIPPET": {"source_id": "SRC-SNIPPET", "usage": "snippet_only"},
        "SRC-LINK": {"source_id": "SRC-LINK", "usage": "link_only"},
        "SRC-QUOTABLE": {"source_id": "SRC-QUOTABLE", "usage": "quotable"},
        "SRC-NOUSAGE": {"source_id": "SRC-NOUSAGE"},
        "SRC-NULLUSAGE": {"source_id": "SRC-NULLUSAGE", "usage": None},
        "SRC-BADUSAGE": {"source_id": "SRC-BADUSAGE", "usage": "quotable "},
    }

    # --- 正例(反応してほしい: excerptがnullに書き換わりunverified/excerpt_not_allowedになる) ---
    line1 = {
        "claimed_mark": "reported_unverified", "numbers": [],
        "source_ref": "SRC-SNIPPET", "excerpt": "何かの抜き出し",
    }
    mark, reason, _ = ve.verify_line(line1, sources, "/nonexistent")
    check("検査16/正例: usageがsnippet_onlyの出典を参照しexcerptがあれば不合格", (mark, reason), ("unverified", "excerpt_not_allowed"))
    check("検査16/正例: 不合格になった行のexcerptはNoneに書き換わる", line1["excerpt"], None)

    line2 = {
        "claimed_mark": "source_number_match", "numbers": [{"value": 1}],
        "source_ref": "SRC-LINK", "excerpt": "何かの抜き出し",
        "attribution": "出典：テスト", "processing_note": "注記",
    }
    mark, reason, _ = ve.verify_line(line2, sources, "/nonexistent")
    check("検査16/正例: usageがlink_onlyの出典を参照しexcerptがあれば不合格", (mark, reason), ("unverified", "excerpt_not_allowed"))

    line_nousage_key = {
        "claimed_mark": "reported_unverified", "numbers": [],
        "source_ref": "SRC-NOUSAGE", "excerpt": "何かの抜き出し",
    }
    mark, reason, _ = ve.verify_line(line_nousage_key, sources, "/nonexistent")
    check(
        "検査16/正例: usageのキーが無い出典を参照しexcerptがあれば不合格(以前は穴だった)",
        (mark, reason), ("unverified", "excerpt_not_allowed"),
    )

    line_nullusage = {
        "claimed_mark": "reported_unverified", "numbers": [],
        "source_ref": "SRC-NULLUSAGE", "excerpt": "何かの抜き出し",
    }
    mark, reason, _ = ve.verify_line(line_nullusage, sources, "/nonexistent")
    check(
        "検査16/正例: usageがnullの出典を参照しexcerptがあれば不合格(以前は穴だった)",
        (mark, reason), ("unverified", "excerpt_not_allowed"),
    )

    line_badusage = {
        "claimed_mark": "reported_unverified", "numbers": [],
        "source_ref": "SRC-BADUSAGE", "excerpt": "何かの抜き出し",
    }
    mark, reason, _ = ve.verify_line(line_badusage, sources, "/nonexistent")
    check(
        "検査16/正例: usageが想定外の値(quotableに余計な空白)の出典を参照しexcerptがあれば不合格",
        (mark, reason), ("unverified", "excerpt_not_allowed"),
    )

    # --- 負例(反応してほしくない) ---
    with tempfile.TemporaryDirectory() as d:
        body = "テスト物産の売上高は増えた。"
        write(d, "SRC-QUOTABLE.txt", body.encode("utf-8"))
        import hashlib
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        sources_ok = dict(sources)
        sources_ok["SRC-QUOTABLE"] = {"source_id": "SRC-QUOTABLE", "usage": "quotable", "content_sha256": digest}
        line_ok = {
            "claimed_mark": "reported_unverified", "numbers": [],
            "source_ref": "SRC-QUOTABLE", "excerpt": "売上高は増えた",
        }
        mark, reason, _ = ve.verify_line(line_ok, sources_ok, d)
        check(
            "検査16/負例: usageがquotableの出典を参照する行はexcerptがあってもexcerpt_not_allowedにならない",
            reason != "excerpt_not_allowed", True,
        )

    line_no_excerpt = {
        "claimed_mark": "explainer", "numbers": [],
        "source_ref": "SRC-SNIPPET", "excerpt": None,
    }
    mark, reason, _ = ve.verify_line(line_no_excerpt, sources, "/nonexistent")
    check("検査16/負例: excerptが無ければsnippet_onlyの出典を参照していてもexcerpt_not_allowedにならない", mark, "explainer")

    line_link_no_excerpt = {
        "claimed_mark": "explainer", "numbers": [],
        "source_ref": "SRC-LINK", "excerpt": None,
    }
    mark, reason, _ = ve.verify_line(line_link_no_excerpt, sources, "/nonexistent")
    check("検査16/負例: excerptが無ければlink_onlyの出典を参照していてもexcerpt_not_allowedにならない", mark, "explainer")

    line_unknown_ref = {
        "claimed_mark": "reported_unverified", "numbers": [],
        "source_ref": "SRC-NOT-IN-LIST", "excerpt": "何か",
    }
    mark, reason, _ = ve.verify_line(line_unknown_ref, sources, "/nonexistent")
    check(
        "検査16/負例: source_refがsources一覧に無い場合はexcerpt_not_allowedにならない(該当する出典が特定できないため)",
        reason != "excerpt_not_allowed", True,
    )

    line_nousage_no_excerpt = {
        "claimed_mark": "explainer", "numbers": [],
        "source_ref": "SRC-NOUSAGE", "excerpt": None,
    }
    mark, reason, _ = ve.verify_line(line_nousage_no_excerpt, sources, "/nonexistent")
    check(
        "検査16/負例: usageが記録されていない出典でもexcerptがNoneならexcerpt_not_allowedにならない(これは通るべき)",
        mark, "explainer",
    )

    line_explainer_quotable = {
        "claimed_mark": "explainer", "numbers": [],
        "source_ref": "SRC-QUOTABLE", "excerpt": None,
    }
    mark, reason, _ = ve.verify_line(line_explainer_quotable, sources, "/nonexistent")
    check("検査16/負例: excerptがNoneのexplainer行はquotable以外の出典でも合格のまま", mark, "explainer")


def test_count_invalid_source_usages():
    """source_usage_invalid_hits(usageが正しく書かれていない出典の数)の正例・負例。"""
    edition_mixed = {
        "sources": [
            {"source_id": "SRC-1", "usage": "quotable"},
            {"source_id": "SRC-2", "usage": "link_only"},
            {"source_id": "SRC-3", "usage": "snippet_only"},
            {"source_id": "SRC-4"},
            {"source_id": "SRC-5", "usage": None},
            {"source_id": "SRC-6", "usage": ""},
            {"source_id": "SRC-7", "usage": "quotable "},
        ]
    }
    check(
        "source_usage_invalid_hits/正例: usage無し・null・空文字・想定外の値の4件を数える",
        ve.count_invalid_source_usages(edition_mixed), 4,
    )

    edition_all_valid = {
        "sources": [
            {"source_id": "SRC-1", "usage": "quotable"},
            {"source_id": "SRC-2", "usage": "link_only"},
            {"source_id": "SRC-3", "usage": "snippet_only"},
        ]
    }
    check(
        "source_usage_invalid_hits/負例: 3種類とも正しいusageなら0件",
        ve.count_invalid_source_usages(edition_all_valid), 0,
    )

    edition_no_sources = {"sources": []}
    check(
        "source_usage_invalid_hits/負例: sourcesが空配列なら0件",
        ve.count_invalid_source_usages(edition_no_sources), 0,
    )


def test_check_baseline_late():
    """検査20(号の遅延判定)の正例・負例。修正1により、判定基準はgenerated_at(AIの自己申告)
    ではなくrun_at_dt(スクリプトの実行時刻)になった。さらに修正1(タスク16-2c-1)により、
    昼号(noon)は時刻の制限が無くなり、実行時刻にかかわらず常にFalseになった
    (朝号(morning)の8:50判定はそのまま残る)。"""
    def run_at(iso):
        return dt.datetime.fromisoformat(iso)

    def edition(slot, generated_at="dummy"):
        return {"slot": slot, "generated_at": generated_at}

    # --- 正例(反応してほしい: Trueになる) ---
    check(
        "検査20/正例: 朝号(morning)で実行時刻08:51はbaseline_late",
        ve.run_check_baseline_late(edition("morning"), run_at("2026-09-24T08:51:00+09:00")),
        True,
    )
    check(
        "検査20/正例: 朝号(morning)で実行時刻09:20はbaseline_late",
        ve.run_check_baseline_late(edition("morning"), run_at("2026-09-24T09:20:00+09:00")),
        True,
    )

    # --- 負例(反応してほしくない例。5件以上) ---
    check(
        "検査20/負例1: 朝号で実行時刻08:49はbaseline_lateにならない",
        ve.run_check_baseline_late(edition("morning"), run_at("2026-09-24T08:49:00+09:00")),
        False,
    )
    check(
        "検査20/負例2: 朝号で実行時刻08:50ちょうどはbaseline_lateにならない"
        "(「過ぎている」なので同時刻は通す)",
        ve.run_check_baseline_late(edition("morning"), run_at("2026-09-24T08:50:00+09:00")),
        False,
    )
    check(
        "検査20/負例3: 昼号で実行時刻13:05はbaseline_lateにならない",
        ve.run_check_baseline_late(edition("noon"), run_at("2026-09-24T13:05:00+09:00")),
        False,
    )
    check(
        "検査20/負例4: 夕方号(evening)は実行時刻23:00でもbaseline_lateにならない(常に判定しない)",
        ve.run_check_baseline_late(edition("evening"), run_at("2026-09-24T23:00:00+09:00")),
        False,
    )
    check(
        "検査20/負例5: generated_atに'8:45'(門限前を装った値)と書かれていても、"
        "実行時刻が09:20ならbaseline_lateになる(AIの申告に影響されない)",
        ve.run_check_baseline_late(edition("morning", generated_at="8:45"), run_at("2026-09-24T09:20:00+09:00")),
        True,
    )
    check(
        "検査20/負例6: generated_atが空文字でも、実行時刻(08:49)で正しくFalseと判定される",
        ve.run_check_baseline_late(edition("morning", generated_at=""), run_at("2026-09-24T08:49:00+09:00")),
        False,
    )
    check(
        "検査20/負例7: generated_atが壊れた文字列でも、実行時刻(09:20)で正しくTrueと判定される",
        ve.run_check_baseline_late(edition("morning", generated_at="not-a-datetime"), run_at("2026-09-24T09:20:00+09:00")),
        True,
    )
    check(
        "検査20/負例8(修正1): 昼号は実行時刻が15:00でもbaseline_lateにならない"
        "(昼号の基準は「読んだ時点の株価」であり、時刻の制限が無いため)",
        ve.run_check_baseline_late(edition("noon"), run_at("2026-09-24T15:00:00+09:00")),
        False,
    )
    check(
        "検査20/負例9(修正1): 昼号は実行時刻が23:59でもbaseline_lateにならない",
        ve.run_check_baseline_late(edition("noon"), run_at("2026-09-24T23:59:00+09:00")),
        False,
    )


def test_stop_words_remove_line_not_whole_edition():
    """検査7(停止語)が号全体ではなく該当行だけを削除することを確かめる。"""
    edition = {
        "sections": [
            {
                "section_id": "test",
                "articles": [
                    {
                        "article_id": "ART-STOP",
                        "lines": [
                            {"line_id": "S-01", "text": "この銘柄は買い時だ。", "claimed_mark": "explainer", "numbers": []},
                            {"line_id": "S-02", "text": "輸出額は前年同月比で増えた。", "claimed_mark": "explainer", "numbers": []},
                        ],
                    }
                ],
            }
        ]
    }
    hits = ve.check_c_stop_words(edition, ["買い時"])
    check("検査7/停止語を含む行は削除される(削除件数1)", len(hits), 1)
    remaining_ids = [line["line_id"] for line in edition["sections"][0]["articles"][0]["lines"]]
    check("検査7/停止語を含まない行は残る", remaining_ids, ["S-02"])


def _pic_candidate(name, edinet_code, ticker, capital, industry="テスト業"):
    return {
        "company_name": name, "edinet_code": edinet_code, "ticker": ticker,
        "industry": industry, "capital_million": capital, "retrieved_date": "2026-09-19",
    }


def test_pick_industry_companies_matching():
    """pick_industry_companies.pyの本文照合(3.3)の正例・負例。
    実データ(コードリスト)には依存せず、その場で作った架空の会社名だけを使う。"""

    # --- 負例1: 'NTTデータ'の中の'NTT'を当ててはいけない(登録されていない
    # 長い固有名詞の一部を、短い照合名の一致とみなさない) ---
    candidates = [_pic_candidate("ＮＴＴ株式会社", "E-NTT", "9432", 1000)]
    article = {"text_blob": "NTTデータの決算が発表された。"}
    selected = pic.select_companies_for_pick(candidates, article, {}, set(), 1)
    check(
        "会社選定/負例1: 'NTTデータ'の本文で'NTT'はmentioned_in_textにならない",
        selected[0]["selection_rule"], "capital_rank",
    )

    # --- 負例2: '近鉄百貨店'の中の'近鉄'(エイリアス経由)を当ててはいけない ---
    candidates = [_pic_candidate("近鉄グループホールディングス株式会社", "E-KINTETSU", "9041", 1000)]
    aliases = {"E-KINTETSU": [{"news_name": "近鉄", "entity_relation": "parent"}]}
    article = {"text_blob": "近鉄百貨店が新装開店した。"}
    selected = pic.select_companies_for_pick(candidates, article, aliases, set(), 1)
    check(
        "会社選定/負例2: '近鉄百貨店'の本文で'近鉄'(エイリアス)はmentioned_in_textにならない",
        selected[0]["selection_rule"], "capital_rank",
    )

    # --- 負例3: 'ソフトバンクグループ'の中の'ソフトバンク'を当ててはいけない
    # ('ソフトバンクグループ'自体は候補に無い状況でも、境界の確認だけで弾けること) ---
    candidates = [_pic_candidate("ソフトバンク株式会社", "E-SB", "9434", 1000)]
    article = {"text_blob": "ソフトバンクグループが発表した。"}
    selected = pic.select_companies_for_pick(candidates, article, {}, set(), 1)
    check(
        "会社選定/負例3: 'ソフトバンクグループ'の本文で'ソフトバンク'はmentioned_in_textにならない",
        selected[0]["selection_rule"], "capital_rank",
    )

    # --- 負例4: 別の記事の行に社名があっても、その記事の企業欄には入らない ---
    edition = {
        "sections": [{
            "section_id": "s", "articles": [
                {
                    "article_id": "ART-A", "headline": "無関係な見出し",
                    "lines": [{"line_id": "A-01", "text": "この記事はまったく別の話題を扱っている。"}],
                },
                {
                    "article_id": "ART-B", "headline": "テスト物産の記事",
                    "lines": [{"line_id": "B-01", "text": "テスト物産は新工場を稼働させた。"}],
                },
            ],
        }],
    }
    articles_by_id = pic.index_articles(edition)
    candidates = [_pic_candidate("テスト物産株式会社", "E-BUSSAN", "9001", 1000)]
    selected_a = pic.select_companies_for_pick(candidates, articles_by_id["ART-A"], {}, set(), 1)
    selected_b = pic.select_companies_for_pick(candidates, articles_by_id["ART-B"], {}, set(), 1)
    check(
        "会社選定/負例4: 別記事(ART-B)にしか無い社名は、ART-Aの企業欄ではmentioned_in_textにならない",
        selected_a[0]["selection_rule"], "capital_rank",
    )
    check(
        "会社選定/負例4: 社名がある記事(ART-B)自身ではmentioned_in_textになる",
        selected_b[0]["selection_rule"], "mentioned_in_text",
    )

    # --- 負例5: 業種が「サービス業」なら1社も出ない(飛ばす) ---
    edition_service = {
        "sections": [{
            "section_id": "s", "articles": [
                {"article_id": "ART-S", "headline": "見出し", "lines": [{"line_id": "S-01", "text": "本文。"}]},
            ],
        }],
    }
    articles_service = pic.index_articles(edition_service)
    pick_service = {"article_id": "ART-S", "event_id": "EVT-S", "industry": "サービス業", "industry_line_ids": ["S-01"]}
    ok, reason, industry_name, industry_result = pic.validate_pick(pick_service, articles_service, {})
    check("会社選定/負例5: 業種が'サービス業'なら不合格になる(ok=False)", ok, False)
    check("会社選定/負例5: 業種が'サービス業'なら1社も選ばれない(industry_resultはNone)", industry_result, None)

    # --- 負例6: industry_line_idsが空なら1社も出ない ---
    pick_empty_lines = {"article_id": "ART-S", "event_id": "EVT-S", "industry": "電気機器", "industry_line_ids": []}
    ok, reason, industry_name, industry_result = pic.validate_pick(pick_empty_lines, articles_service, {})
    check("会社選定/負例6: industry_line_idsが空なら不合格になる", ok, False)
    check("会社選定/負例6: 理由は'industry_line_idsが空'", reason, "industry_line_idsが空")

    # --- 負例7: industry_picksに会社名らしき項目が書かれていたら1社も出ない ---
    pick_with_company = {
        "article_id": "ART-S", "event_id": "EVT-S", "industry": "電気機器",
        "industry_line_ids": ["S-01"], "company_name": "何かの会社",
    }
    ok, reason, industry_name, industry_result = pic.validate_pick(pick_with_company, articles_service, {})
    check("会社選定/負例7: company_nameが書かれていたら不合格になる", ok, False)
    check("会社選定/負例7: 理由は'industry_picksに会社名らしき項目が書かれている'", reason, "industry_picksに会社名らしき項目が書かれている")

    # --- 負例8: '三井物産'の一部になっている'三井'は、離れた位置に単独で
    # 出ていても(他社名の一部になっている名前は使わないため)当ててはいけない ---
    candidates = [
        _pic_candidate("三井物産株式会社", "E-MITSUI-B", "8031", 5000),
        _pic_candidate("三井株式会社", "E-MITSUI", "9999", 3000),
    ]
    article = {"text_blob": "三井物産が新規事業を発表した。三井は単独でも別の事業を進めている。"}
    selected = pic.select_companies_for_pick(candidates, article, {}, set(), 2)
    by_name = {s["candidate"]["company_name"]: s["selection_rule"] for s in selected}
    check(
        "会社選定/負例8: '三井物産'はmentioned_in_textになる",
        by_name.get("三井物産株式会社"), "mentioned_in_text",
    )
    check(
        "会社選定/負例8: '三井'は他社名('三井物産')の一部のため、離れた位置の単独出現でもmentioned_in_textにならない",
        by_name.get("三井株式会社"), "capital_rank",
    )

    # --- 負例9: '兼松エレクトロニクス'が本文にあるとき、その一部である短い
    # '兼松'は当てない(最長一致で長い名前が先に当たるため) ---
    candidates = [
        _pic_candidate("兼松株式会社", "E-KANEMATSU", "8020", 5000),
        _pic_candidate("兼松エレクトロニクス株式会社", "E-KANEMATSU-E", "9999", 2000),
    ]
    article = {"text_blob": "兼松エレクトロニクスが新製品を発表した。"}
    selected = pic.select_companies_for_pick(candidates, article, {}, set(), 2)
    by_name = {s["candidate"]["company_name"]: s["selection_rule"] for s in selected}
    check(
        "会社選定/負例9: '兼松エレクトロニクス'はmentioned_in_textになる",
        by_name.get("兼松エレクトロニクス株式会社"), "mentioned_in_text",
    )
    check(
        "会社選定/負例9: 短い'兼松'は最長一致で長い名前に先を越されてmentioned_in_textにならない",
        by_name.get("兼松株式会社"), "capital_rank",
    )

    # --- 正例1: 本文に社名がそのまま出ていればmentioned_in_textで選ばれる ---
    candidates = [
        _pic_candidate("テスト物産株式会社", "E-P1A", "9001", 1000),
        _pic_candidate("テスト電機株式会社", "E-P1B", "9002", 2000),
    ]
    article = {"text_blob": "テスト物産は18日、新工場を稼働させた。"}
    selected = pic.select_companies_for_pick(candidates, article, {}, set(), 2)
    check(
        "会社選定/正例1: 本文にある'テスト物産'がmentioned_in_textで先に選ばれる",
        [s["selection_rule"] for s in selected], ["mentioned_in_text", "capital_rank"],
    )
    check("会社選定/正例1: mentioned_in_textの会社は'テスト物産株式会社'", selected[0]["candidate"]["company_name"], "テスト物産株式会社")

    # --- 正例2: 本文に社名が無ければ資本金の多い順(capital_rank)で選ばれる ---
    article_no_mention = {"text_blob": "この記事には関係する会社名が出てこない。"}
    selected = pic.select_companies_for_pick(candidates, article_no_mention, {}, set(), 1)
    check("会社選定/正例2: 本文に社名が無ければcapital_rankになる", selected[0]["selection_rule"], "capital_rank")
    check("会社選定/正例2: 資本金の多い'テスト電機'が選ばれる", selected[0]["candidate"]["company_name"], "テスト電機株式会社")

    # --- 正例3: aliases.csv相当のエイリアス経由での一致(3.5) ---
    candidates = [_pic_candidate("テストフィナンシャルグループ株式会社", "E-P3", "9003", 5000)]
    aliases = {"E-P3": [{"news_name": "テスト銀行", "entity_relation": "parent"}]}
    article = {"text_blob": "テスト銀行は本日、新支店を開設した。"}
    selected = pic.select_companies_for_pick(candidates, article, aliases, set(), 1)
    check("会社選定/正例3: エイリアス'テスト銀行'でmentioned_in_textになる", selected[0]["selection_rule"], "mentioned_in_text")
    check("会社選定/正例3: news_entityはエイリアスの呼び名'テスト銀行'", selected[0]["news_entity"], "テスト銀行")
    check("会社選定/正例3: entity_relationはエイリアスの'parent'", selected[0]["entity_relation"], "parent")
    check(
        "会社選定/正例3: company_nameはコードリストの正式名'テストフィナンシャルグループ株式会社'",
        selected[0]["candidate"]["company_name"], "テストフィナンシャルグループ株式会社",
    )

    # --- 正例4: 正式名がそのまま出ていた場合はnews_entity=null, entity_relation=same ---
    article_official = {"text_blob": "テストフィナンシャルグループは本日、決算を発表した。"}
    selected = pic.select_companies_for_pick(candidates, article_official, aliases, set(), 1)
    check("会社選定/正例4: 正式名の一致ではnews_entityがNone", selected[0]["news_entity"], None)
    check("会社選定/正例4: 正式名の一致ではentity_relationが'same'", selected[0]["entity_relation"], "same")

    # --- 一般語の負例(8.7): 短い社名・一般的な語が普通の文章に誤反応しないこと ---
    candidates_taisei = [_pic_candidate("大成株式会社", "E-GEN-1", "9021", 1000)]
    article_taisei = {"text_blob": "今年は大成長を遂げた分野が多い。"}
    selected = pic.select_companies_for_pick(candidates_taisei, article_taisei, {}, set(), 1)
    check("一般語の負例/「大成長」は架空社名「大成」に反応しない", selected[0]["selection_rule"], "capital_rank")

    candidates_maeda = [_pic_candidate("前田株式会社", "E-GEN-2", "9022", 1000)]
    article_maeda = {"text_blob": "前田氏がコメントを発表した。"}
    selected = pic.select_companies_for_pick(candidates_maeda, article_maeda, {}, set(), 1)
    check("一般語の負例/「前田氏」は架空社名「前田」に反応しない", selected[0]["selection_rule"], "capital_rank")

    candidates_oji = [_pic_candidate("王子株式会社", "E-GEN-3", "9023", 1000)]
    article_oji = {"text_blob": "王子駅前が再開発される。"}
    selected = pic.select_companies_for_pick(candidates_oji, article_oji, {}, set(), 1)
    check("一般語の負例/「王子駅」は架空社名「王子」に反応しない", selected[0]["selection_rule"], "capital_rank")

    # --- 正例5(決定性): 資本金が同じ会社が並んだときはEDINETコードの昇順になる ---
    candidates_tie = [
        _pic_candidate("テストA株式会社", "E-Z999", "9911", 1000),
        _pic_candidate("テストB株式会社", "E-A001", "9912", 1000),
    ]
    article_tie = {"text_blob": "この記事には関係する会社名が出てこない。"}
    selected = pic.select_companies_for_pick(candidates_tie, article_tie, {}, set(), 2)
    check(
        "会社選定/正例5: 資本金が同じならEDINETコードの昇順(E-A001が先)になる",
        [s["candidate"]["edinet_code"] for s in selected], ["E-A001", "E-Z999"],
    )


def test_generic_words_dictionary():
    """修正2(名寄せ規則6): scripts/generic_words.txt による除外の正例・負例。
    実際のファイル(scripts/generic_words.txt)を使う。テスト実行はリポジトリの
    ルートから行う前提(python3 scripts/selftest_checks.py)。"""

    # --- 正例1: 辞書に「コア」があるとき、本文「コアの上昇率は…」でその
    # 照合名の会社がmentioned_in_textにならない ---
    candidates = [_pic_candidate("コア株式会社", "E-GW-CORE", "1001", 1000)]
    article = {"text_blob": "コアの上昇率は前年比で上昇した。"}
    selected = pic.select_companies_for_pick(candidates, article, {}, set(), 1)
    check(
        "一般語辞書/正例1: 辞書の'コア'は本文'コアの上昇率'でmentioned_in_textにならない",
        selected[0]["selection_rule"], "capital_rank",
    )

    # --- 正例2: 辞書に無い普通の会社名(ゆうちょ銀行)は、これまでどおり本文照合で選ばれる ---
    candidates_yucho = [_pic_candidate("ゆうちょ銀行", "E-GW-YUCHO", "1002", 1000)]
    article_yucho = {"text_blob": "ゆうちょ銀行は新サービスを開始した。"}
    selected_yucho = pic.select_companies_for_pick(candidates_yucho, article_yucho, {}, set(), 1)
    check(
        "一般語辞書/正例2: 辞書に無い'ゆうちょ銀行'はこれまでどおり本文照合で選ばれる",
        selected_yucho[0]["selection_rule"], "mentioned_in_text",
    )

    # --- 正例3: 別名(aliases)側の照合名が辞書に載っている場合も除外される ---
    candidates_alias = [_pic_candidate("テスト持株株式会社", "E-GW-ALIAS", "1003", 1000)]
    aliases_alias = {"E-GW-ALIAS": [{"news_name": "コア", "entity_relation": "parent"}]}
    article_alias = {"text_blob": "コアの説明が本日あった。"}
    selected_alias = pic.select_companies_for_pick(candidates_alias, article_alias, aliases_alias, set(), 1)
    check(
        "一般語辞書/正例3: エイリアス側の照合名'コア'が辞書にあれば除外される",
        selected_alias[0]["selection_rule"], "capital_rank",
    )

    # --- 正例4: 辞書ファイルが無くても異常終了せず、除外0件で動く ---
    original_path = pic.GENERIC_WORDS_PATH
    try:
        pic.GENERIC_WORDS_PATH = Path("scripts/generic_words_does_not_exist.txt")
        words = pic.load_generic_words()
        check("一般語辞書/正例4: 辞書ファイルが無くても例外にならず、除外0件(空集合)になる", words, set())
    finally:
        pic.GENERIC_WORDS_PATH = original_path

    # --- 負例5: 部分一致では除外しない(辞書'コア'で、照合名'コアラ'の会社は消えない) ---
    candidates_koala = [_pic_candidate("コアラ株式会社", "E-GW-KOALA", "1004", 1000)]
    article_koala = {"text_blob": "コアラの生態調査が行われた。"}
    selected_koala = pic.select_companies_for_pick(candidates_koala, article_koala, {}, set(), 1)
    check(
        "一般語辞書/負例5: 部分一致(辞書'コア'/照合名'コアラ')では除外しない",
        selected_koala[0]["selection_rule"], "mentioned_in_text",
    )

    # --- 正例6: 半角カタカナ等でも、正規化後に一致すれば除外される ---
    candidates_halfwidth = [_pic_candidate("ｺｱ株式会社", "E-GW-HW", "1005", 1000)]
    article_halfwidth = {"text_blob": "コアの説明が本日あった。"}
    selected_halfwidth = pic.select_companies_for_pick(candidates_halfwidth, article_halfwidth, {}, set(), 1)
    check(
        "一般語辞書/正例6: 半角カタカナ'ｺｱ'も正規化後は'コア'と一致して除外される",
        selected_halfwidth[0]["selection_rule"], "capital_rank",
    )

    # --- 正例7: 辞書で除外された会社も、資本金順では選ばれる(消えない) ---
    candidates_both = [
        _pic_candidate("コア株式会社", "E-GW-CORE2", "1006", 3000),
        _pic_candidate("テスト電機株式会社", "E-GW-OTHER", "1007", 1000),
    ]
    article_both = {"text_blob": "この記事には関係する会社名が出てこない。"}
    selected_both = pic.select_companies_for_pick(candidates_both, article_both, {}, set(), 2)
    check("一般語辞書/正例7: 辞書で除外された会社も候補には残り、2社とも選ばれる", len(selected_both), 2)
    by_code = {s["candidate"]["edinet_code"]: s["selection_rule"] for s in selected_both}
    check(
        "一般語辞書/正例7: 辞書除外された'コア株式会社'は資本金順(capital_rank)で選ばれる",
        by_code.get("E-GW-CORE2"), "capital_rank",
    )


def test_v12_generic_word_negative_examples():
    """v12 3.4(5)が名指しした一般語の負例5件(依頼書16-2c-2 第8節)。

    当初の指示文は「5件とも_is_word_forming(直後がひらがなでなければ一致を
    認めない仕組み)が防いでいる」としていたが、実在の正式社名で検算した
    結果、防いでいるのは_is_word_formingではなく『照合名(正式な社名)その
    ものが本文に一部としてすら現れない』ことだと判明した(依頼者確認済み・
    指示文を訂正)。そのため、5件は実在の正式社名で「当たらないこと」を
    確かめる形にする。

    あわせて、架空の短い照合名('日本'/'東京'という2文字だけの会社)を使うと
    _is_word_formingは実際には防げない(直後が助詞'の'のときは境界とみなし、
    一致を許してしまう)ことを示し、その穴を一般語辞書(コア等と同じ仕組み)で
    塞いだことを確かめる。"""

    # --- v12の5件: 実在の正式社名では、どれも本文に当たらない ---
    real_cases = [
        ("大成建設株式会社", "E-V12-1", "今年は大成長を遂げた分野が多い。", "大成建設"),
        ("前田建設工業株式会社", "E-V12-2", "前田氏がコメントを発表した。", "前田建設工業"),
        ("王子ホールディングス株式会社", "E-V12-3", "王子駅前が再開発される。", "王子ホールディングス"),
        ("日本製鉄株式会社", "E-V12-4", "日本の鉄鋼業界は変化している。", "日本製鉄"),
        ("東京電力ホールディングス株式会社", "E-V12-5", "東京の電力需要が増えている。", "東京電力ホールディングス"),
    ]
    for company_name, edinet_code, text, label in real_cases:
        candidates = [_pic_candidate(company_name, edinet_code, "0000", 1000)]
        article = {"text_blob": text}
        selected = pic.select_companies_for_pick(candidates, article, {}, set(), 1)
        check(
            f"v12一般語の負例/実在社名'{label}'は本文'{text}'にmentioned_in_textとして当たらない",
            selected[0]["selection_rule"], "capital_rank",
        )

    # --- 一般語辞書が塞いだ穴: 照合名が'日本'/'東京'そのものの会社は、
    # 辞書に載っているため本文照合から外れる。ただし資本金順では選ばれる。 ---
    for word, text, label in [
        ("日本", "日本の鉄鋼業界は変化している。", "'日本'"),
        ("東京", "東京の電力需要が増えている。", "'東京'"),
    ]:
        candidates = [_pic_candidate(f"{word}株式会社", f"E-V12-GW-{word}", "0000", 1000)]
        article = {"text_blob": text}
        selected = pic.select_companies_for_pick(candidates, article, {}, set(), 1)
        check(
            f"v12一般語の負例/照合名が{label}そのものの会社は辞書に載っているため本文照合から外れる",
            selected[0]["selection_rule"], "capital_rank",
        )
        check(
            f"v12一般語の負例/{label}の会社は除外されても資本金順では選ばれる(候補から消えない)",
            len(selected), 1,
        )


def test_pick_industry_companies_relation_and_ticker():
    """作業A: 定型文2種類化・entity_relationの統一(same/parent)・ticker_sourceの正例・負例。
    edinet_codelist.get_companies_by_industryを差し替えて、コードリスト実データに
    依存せず架空の会社(テスト物産・テスト電機など)だけで確かめる。"""
    original = pic.edinet_codelist.get_companies_by_industry

    def fake_get_companies_by_industry(industry, limit=10**9):
        return {
            "attribution": "テスト出典",
            "processing_note": "テスト注記",
            "companies": [
                {
                    "company_name": "テスト物産株式会社", "edinet_code": "E-REL-1",
                    "ticker": "9001", "industry": "テスト業種", "capital_million": 1000,
                    "retrieved_date": "2026-09-19",
                },
                {
                    "company_name": "テスト電機株式会社", "edinet_code": "E-REL-2",
                    "ticker": "9002", "industry": "テスト業種", "capital_million": 2000,
                    "retrieved_date": "2026-09-19",
                },
            ],
        }

    pic.edinet_codelist.get_companies_by_industry = fake_get_companies_by_industry
    try:
        edition = {
            "sections": [{
                "section_id": "s", "articles": [
                    {
                        "article_id": "ART-MENTION", "headline": "テスト物産の記事",
                        "lines": [{"line_id": "L-01", "text": "テスト物産は新工場を稼働させた。"}],
                    },
                    {
                        "article_id": "ART-NOMENTION", "headline": "無関係の記事",
                        "lines": [{"line_id": "L-02", "text": "この記事には関係する会社名が出てこない。"}],
                    },
                ],
            }],
        }
        articles_by_id = pic.index_articles(edition)

        # --- 正例: mentioned_in_textの会社にRELATION_TEXT_MENTIONEDが完全一致で入る ---
        hyp_mention = {
            "edition_id": "2026-09-24-test", "hypotheses": [],
            "industry_picks": [
                {"article_id": "ART-MENTION", "event_id": "EVT-1", "industry": "テスト業種", "industry_line_ids": ["L-01"]},
            ],
        }
        result = pic.run(hyp_mention, articles_by_id, {})
        mentioned_examples = [e for e in result["examples"] if e["selection_rule"] == "mentioned_in_text"]
        check("作業A/正例: mentioned_in_textの会社が1社選ばれる", len(mentioned_examples), 1)
        check(
            "作業A/正例: mentioned_in_textの定型文がRELATION_TEXT_MENTIONEDと完全一致する",
            mentioned_examples[0]["relation_text"],
            pic.RELATION_TEXT_MENTIONED.format(industry="テスト業種"),
        )
        check("作業A/正例: ticker_sourceが'edinet_codelist'", mentioned_examples[0]["ticker_source"], "edinet_codelist")
        check("作業A/正例: 正式名一致のentity_relationは'same'", mentioned_examples[0]["entity_relation"], "same")

        # --- 正例: capital_rankの会社にRELATION_TEXT_CAPITALが完全一致で入る ---
        hyp_no_mention = {
            "edition_id": "2026-09-24-test", "hypotheses": [],
            "industry_picks": [
                {"article_id": "ART-NOMENTION", "event_id": "EVT-2", "industry": "テスト業種", "industry_line_ids": ["L-02"]},
            ],
        }
        result2 = pic.run(hyp_no_mention, articles_by_id, {})
        capital_examples = [e for e in result2["examples"] if e["selection_rule"] == "capital_rank"]
        # 下段の枠は2社(上段0件なのでmin(2, 5-0)=2)あり、本文一致が無いのでどちらの候補も
        # capital_rankで選ばれる。資本金の多い順に並ぶため、[0]は資本金2000のテスト電機。
        check("作業A/正例: capital_rankの会社が2社選ばれる(下段の枠2)", len(capital_examples), 2)
        check(
            "作業A/正例: capital_rankの定型文がRELATION_TEXT_CAPITALと完全一致する",
            capital_examples[0]["relation_text"],
            pic.RELATION_TEXT_CAPITAL.format(industry="テスト業種"),
        )
        check("作業A/正例: capital_rankのentity_relationも'same'", capital_examples[0]["entity_relation"], "same")
        check("作業A/正例: capital_rankのticker_sourceも'edinet_codelist'", capital_examples[0]["ticker_source"], "edinet_codelist")

        # --- 正例: 業種名が変わっても、定型文の他の文字は1字も変わらない ---
        text_a = pic.RELATION_TEXT_MENTIONED.format(industry="銀行業")
        text_b = pic.RELATION_TEXT_MENTIONED.format(industry="電気機器")
        check(
            "作業A/正例: 業種名を差し替えても定型文の他の文字は同じ({industry}部分だけ差し替わる)",
            text_a.replace("銀行業", "電気機器"), text_b,
        )
    finally:
        pic.edinet_codelist.get_companies_by_industry = original

    # --- entity_relationの統一: エイリアスの'self'は'same'に読み替える ---
    candidates_self = [_pic_candidate("テスト銀行株式会社", "E-SELF-1", "9010", 5000)]
    aliases_self = {"E-SELF-1": [{"news_name": "テスト銀行だけ", "entity_relation": "self"}]}
    article_self = {"text_blob": "テスト銀行だけが発表した。"}
    selected = pic.select_companies_for_pick(candidates_self, article_self, aliases_self, set(), 1)
    check("作業A/正例: エイリアスのentity_relation'self'は'same'に読み替わる", selected[0]["entity_relation"], "same")

    # --- entity_relationがsame/parent以外なら、そのエイリアス名は本文照合に使わない ---
    candidates_bad = [_pic_candidate("テスト鉱業株式会社", "E-BAD-1", "9011", 5000)]
    aliases_bad = {"E-BAD-1": [{"news_name": "TKGアルファ", "entity_relation": "affiliate"}]}
    article_bad = {"text_blob": "TKGアルファが新製品を発表した。"}
    selected_bad = pic.select_companies_for_pick(candidates_bad, article_bad, aliases_bad, set(), 1)
    check(
        "作業A/負例: entity_relationがsame/parent以外のエイリアスは本文照合に使われない(capital_rankになる)",
        selected_bad[0]["selection_rule"], "capital_rank",
    )


def test_pick_industry_companies_excluded_tickers():
    """作業B(8.5・規則6): 上段(hypotheses)に残っている会社のtickerを、下段の候補から
    除くこと。edinet_codelist.get_companies_by_industryを差し替えて、架空の会社
    (テスト重複・テスト非重複)だけで確かめる。"""
    original = pic.edinet_codelist.get_companies_by_industry

    def fake_get_companies_by_industry(industry, limit=10**9):
        return {
            "attribution": "テスト出典", "processing_note": "テスト注記",
            "companies": [
                {
                    "company_name": "テスト重複株式会社", "edinet_code": "E-DUP-1",
                    "ticker": "1234", "industry": "テスト業種", "capital_million": 1000,
                    "retrieved_date": "2026-09-19",
                },
                {
                    "company_name": "テスト非重複株式会社", "edinet_code": "E-DUP-2",
                    "ticker": "5678", "industry": "テスト業種", "capital_million": 500,
                    "retrieved_date": "2026-09-19",
                },
            ],
        }

    pic.edinet_codelist.get_companies_by_industry = fake_get_companies_by_industry
    try:
        edition = {
            "sections": [{
                "section_id": "s", "articles": [
                    {
                        "article_id": "ART-EXCL", "headline": "見出し",
                        "lines": [{"line_id": "L-01", "text": "この記事には関係する会社名が出てこない。"}],
                    },
                ],
            }],
        }
        articles_by_id = pic.index_articles(edition)
        hyp_doc = {
            "edition_id": "2026-09-24-test", "hypotheses": [],
            "industry_picks": [
                {"article_id": "ART-EXCL", "event_id": "EVT-EXCL", "industry": "テスト業種", "industry_line_ids": ["L-01"]},
            ],
        }

        # --- 正例: 上段に残っているticker'1234'の会社は、下段の候補から除かれる ---
        result_excluded = pic.run(hyp_doc, articles_by_id, {}, {"1234"})
        tickers_excluded = {e["ticker"] for e in result_excluded["examples"]}
        check("作業B/8.5(規則6)正例: 上段に残るticker'1234'の会社は下段に出ない", "1234" in tickers_excluded, False)
        check("作業B/8.5(規則6)正例: 除外後も他の候補(ticker'5678')は下段に出る", "5678" in tickers_excluded, True)

        # --- 負例: 上段の会社が検査で削除された(除外集合が空)場合、下段の候補に戻る ---
        result_not_excluded = pic.run(hyp_doc, articles_by_id, {}, set())
        tickers_not_excluded = {e["ticker"] for e in result_not_excluded["examples"]}
        check(
            "作業B/8.5(規則6)負例: 上段から削除された会社(除外集合が空)は下段の候補に戻る",
            "1234" in tickers_not_excluded, True,
        )
    finally:
        pic.edinet_codelist.get_companies_by_industry = original


def test_dropped_names_records():
    """修正3(a)・修正2: legacy_substring_rule_dropped(旧規則4)と
    generic_name_dropped(一般語辞書)の記録が正しく件数・会社名を持つこと。
    両者は原因が別なので混ぜない(それぞれ別のキーに入る)ことも確かめる。"""
    original = pic.edinet_codelist.get_companies_by_industry

    # --- 正例8: 包含関係のある会社が無い号では0件かつ空配列 ---
    def fake_no_overlap(industry, limit=10**9):
        return {
            "attribution": "テスト出典", "processing_note": "テスト注記",
            "companies": [
                {
                    "company_name": "テスト物産株式会社", "edinet_code": "E-DROP-1",
                    "ticker": "9001", "industry": "テスト業種", "capital_million": 1000,
                    "retrieved_date": "2026-09-19",
                },
            ],
        }

    pic.edinet_codelist.get_companies_by_industry = fake_no_overlap
    try:
        edition = {
            "sections": [{
                "section_id": "s", "articles": [
                    {
                        "article_id": "ART-NOOVERLAP", "headline": "見出し",
                        "lines": [{"line_id": "L-01", "text": "この記事には関係する会社名が出てこない。"}],
                    },
                ],
            }],
        }
        articles_by_id = pic.index_articles(edition)
        hyp_doc = {
            "edition_id": "2026-09-24-test", "hypotheses": [],
            "industry_picks": [
                {"article_id": "ART-NOOVERLAP", "event_id": "EVT-1", "industry": "テスト業種", "industry_line_ids": ["L-01"]},
            ],
        }
        result = pic.run(hyp_doc, articles_by_id, {})
        check(
            "記録/正例8: 包含関係のある会社が無い号ではlegacy_substring_rule_droppedが0件",
            result["legacy_substring_rule_dropped"], {"count": 0, "names": []},
        )
        check(
            "記録/正例8: 一般語に当たる会社が無い号ではgeneric_name_droppedも0件",
            result["generic_name_dropped"], {"count": 0, "names": []},
        )
    finally:
        pic.edinet_codelist.get_companies_by_industry = original

    # --- 正例9: 包含関係がある場合に件数と社名が入る(三井物産/三井) ---
    def fake_overlap(industry, limit=10**9):
        return {
            "attribution": "テスト出典", "processing_note": "テスト注記",
            "companies": [
                {
                    "company_name": "三井物産株式会社", "edinet_code": "E-DROP-MITSUI-B",
                    "ticker": "8031", "industry": "テスト業種", "capital_million": 5000,
                    "retrieved_date": "2026-09-19",
                },
                {
                    "company_name": "三井株式会社", "edinet_code": "E-DROP-MITSUI",
                    "ticker": "9999", "industry": "テスト業種", "capital_million": 3000,
                    "retrieved_date": "2026-09-19",
                },
            ],
        }

    pic.edinet_codelist.get_companies_by_industry = fake_overlap
    try:
        edition = {
            "sections": [{
                "section_id": "s", "articles": [
                    {
                        "article_id": "ART-OVERLAP", "headline": "見出し",
                        "lines": [{
                            "line_id": "L-01",
                            "text": "三井物産が新規事業を発表した。三井は単独でも別の事業を進めている。",
                        }],
                    },
                ],
            }],
        }
        articles_by_id = pic.index_articles(edition)
        hyp_doc = {
            "edition_id": "2026-09-24-test", "hypotheses": [],
            "industry_picks": [
                {"article_id": "ART-OVERLAP", "event_id": "EVT-1", "industry": "テスト業種", "industry_line_ids": ["L-01"]},
            ],
        }
        result = pic.run(hyp_doc, articles_by_id, {})
        check(
            "記録/正例9: 包含関係のある会社(三井)がlegacy_substring_rule_droppedに1件記録される",
            result["legacy_substring_rule_dropped"]["count"], 1,
        )
        check(
            "記録/正例9: legacy_substring_rule_droppedの会社名は'三井株式会社'",
            result["legacy_substring_rule_dropped"]["names"], ["三井株式会社"],
        )
        check(
            "記録/正例9: 一般語辞書とは原因が別のためgeneric_name_droppedは0件のまま(混ざらない)",
            result["generic_name_dropped"], {"count": 0, "names": []},
        )
    finally:
        pic.edinet_codelist.get_companies_by_industry = original


def _ec_row(name, edinet_code, ticker_raw, capital="1000", listed="上場", industry="テスト業種"):
    """edinet_codelist.find_company_by_name向けの、コードリストの1行相当のダミー行。"""
    return {
        ec.COL_FILER_NAME: name,
        ec.COL_EDINET_CODE: edinet_code,
        ec.COL_TICKER_RAW: ticker_raw,
        ec.COL_LISTED: listed,
        ec.COL_INDUSTRY: industry,
        ec.COL_CAPITAL: capital,
    }


def test_find_company_by_name():
    """edinet_codelist.find_company_by_name(会社名から上場会社を1件だけ引く)の
    正例・負例。部分一致・あいまい一致では絶対に当ててはいけない(引けないことは
    失敗ではなく正しい動作)。"""

    base_rows = [
        _ec_row("テスト銀行株式会社", "E-C001", "10000", capital="5000"),
        _ec_row("テスト非上場株式会社", "E-C002", "20000", listed="非上場", capital="3000"),
        _ec_row("テストコード無し株式会社", "E-C003", "", capital="1000"),
        _ec_row("テスト重複株式会社", "E-C004", "30000", capital="2000"),
        _ec_row("テスト重複株式会社", "E-C005", "40000", capital="2500"),
    ]

    # --- 負例1: '銀行'のような一般名詞では、どの会社にも当ててはいけない ---
    check("company/負例1: '銀行'は何も返さない", ec.find_company_by_name("銀行", base_rows, []), None)

    # --- 負例2: '日本'のような一般名詞でも同様 ---
    check("company/負例2: '日本'は何も返さない", ec.find_company_by_name("日本", base_rows, []), None)

    # --- 負例3: 'トヨタ'で'トヨタ自動車'を部分一致で拾ってはいけない ---
    toyota_rows = [_ec_row("トヨタ自動車株式会社", "E-TOYOTA", "70000", capital=100000)]
    check(
        "company/負例3: 'トヨタ'は'トヨタ自動車'を部分一致で拾わない",
        ec.find_company_by_name("トヨタ", toyota_rows, []), None,
    )

    # --- 負例4: 上場区分が'上場'でない会社は当たらない ---
    check(
        "company/負例4: 上場区分が'上場'でない会社は何も返さない",
        ec.find_company_by_name("テスト非上場株式会社", base_rows, []), None,
    )

    # --- 負例5: 証券コードが空の会社は当たらない ---
    check(
        "company/負例5: 証券コードが空の会社は何も返さない",
        ec.find_company_by_name("テストコード無し株式会社", base_rows, []), None,
    )

    # --- 負例6: 同じ名前の行が2つあれば、複数一致として何も返さない ---
    check(
        "company/負例6: 同名の行が2つあれば何も返さない(複数一致)",
        ec.find_company_by_name("テスト重複株式会社", base_rows, []), None,
    )

    # --- 正例7: 提出者名との完全一致 ---
    result = ec.find_company_by_name("テスト銀行株式会社", base_rows, [])
    check("company/正例7: matched_byは'filer_name'", result["matched_by"], "filer_name")
    check("company/正例7: entity_relationは'same'", result["entity_relation"], "same")
    check("company/正例7: company_nameは'テスト銀行株式会社'", result["company_name"], "テスト銀行株式会社")

    # --- 正例8: 法人格・全角半角の違いがあっても同じ会社に一致する ---
    result_variant = ec.find_company_by_name("テスト銀行", base_rows, [])
    check("company/正例8: '株式会社'を省いても同じedinet_codeに一致する", result_variant["edinet_code"], "E-C001")
    check("company/正例8: matched_byは'filer_name'", result_variant["matched_by"], "filer_name")

    # --- 正例9: 別名表(aliases.csv相当)にだけある呼び名での一致 ---
    alias_rows = [{
        "news_name": "テスト銀行アルファ",
        "official_name": "テストフィナンシャルグループ株式会社",
        "entity_relation": "parent",
        "edinet_code": "E-C010",
        "ticker": "8411",
    }]
    rows_with_group = base_rows + [_ec_row("テストフィナンシャルグループ株式会社", "E-C010", "84110", capital="8000")]
    result_alias = ec.find_company_by_name("テスト銀行アルファ", rows_with_group, alias_rows)
    check("company/正例9: matched_byは'alias'", result_alias["matched_by"], "alias")
    check("company/正例9: news_entityは元の呼び名'テスト銀行アルファ'", result_alias["news_entity"], "テスト銀行アルファ")
    check("company/正例9: entity_relationは別名表の'parent'", result_alias["entity_relation"], "parent")
    check(
        "company/正例9: company_nameはコードリストの正式名'テストフィナンシャルグループ株式会社'",
        result_alias["company_name"], "テストフィナンシャルグループ株式会社",
    )


def test_check_lower_relation_text():
    """作業C(8.1): 検査27(relation_textが作業Aの定型文と完全一致するか)の正例・負例。"""
    industry = "テスト業種"
    mentioned_text = ve.pick_industry_companies.RELATION_TEXT_MENTIONED.format(industry=industry)
    capital_text = ve.pick_industry_companies.RELATION_TEXT_CAPITAL.format(industry=industry)

    def example(relation_text, company_name="テスト物産株式会社", ind=industry):
        return {"relation_text": relation_text, "industry": ind, "company_name": company_name}

    # --- 通るべき例(3件以上) ---
    check(
        "検査27/正例1: mentioned_in_textの定型文と完全一致すれば合格",
        ve.check_lower_relation_text(example(mentioned_text)), None,
    )
    check(
        "検査27/正例2: capital_rankの定型文と完全一致すれば合格",
        ve.check_lower_relation_text(example(capital_text)), None,
    )
    other_industry_text = ve.pick_industry_companies.RELATION_TEXT_MENTIONED.format(industry="銀行業")
    check(
        "検査27/正例3: 業種名が変わっても、その業種名を使った定型文なら合格",
        ve.check_lower_relation_text(example(other_industry_text, ind="銀行業")), None,
    )

    # --- 落ちるべき例(5件以上) ---
    old_text = "東証33業種の「テスト業種」に属する上場企業の例です。同じ業種でも反応は分かれます。"
    check(
        "検査27/負例1: 古い文(1種類だった頃の文言)は不合格",
        ve.check_lower_relation_text(example(old_text)), "lower_relation_text_mismatch",
    )

    no_period_text = mentioned_text[:-1]
    check(
        "検査27/負例2: 文末の句点が無ければ不合格",
        ve.check_lower_relation_text(example(no_period_text)), "lower_relation_text_mismatch",
    )

    fullwidth_space_text = mentioned_text.replace("この記事の", "この記事の　")
    check(
        "検査27/負例3: 文の途中に全角スペースが入っていれば不合格",
        ve.check_lower_relation_text(example(fullwidth_space_text)), "lower_relation_text_mismatch",
    )

    appended_text = mentioned_text + "テスト物産は特に有望です。"
    check(
        "検査27/負例4: 定型文に会社ごとの説明が付け足されていれば不合格",
        ve.check_lower_relation_text(example(appended_text)), "lower_relation_text_mismatch",
    )

    check(
        "検査27/負例5: 定型文は正しいが、company_nameの文字列がrelation_textに含まれていれば不合格",
        ve.check_lower_relation_text(example(mentioned_text, company_name="業種")),
        "lower_relation_text_mismatch",
    )


def test_relation_text_capital_has_machine_wording():
    """改修27-1(4-7): 下段の資本金順の定型文に「機械が」を足したこと、検査27
    (check_lower_relation_text)がpick_industry_companies.RELATION_TEXT_CAPITALを
    そのまま参照しているため新しい文言で合格すること、「機械が」の無い古い文言は
    不合格になる(過去の号のeditions/・hypotheses/は直さない方針のため、再照合すると
    下段から消えることの確認)ことを確かめる。"""
    check(
        "RELATION_TEXT_CAPITAL/正例: 新しい定型文に「機械が」が入っている",
        "機械が選んでいます" in pic.RELATION_TEXT_CAPITAL, True,
    )
    new_example = {
        "industry": "銀行業", "company_name": "テスト銀行",
        "relation_text": pic.RELATION_TEXT_CAPITAL.format(industry="銀行業"),
    }
    check(
        "改修27-1(4-7)/検査27: 新しい定型文はcheck_lower_relation_text()を合格する",
        ve.check_lower_relation_text(new_example), None,
    )

    old_text = (
        "東証33業種の「銀行業」に属する上場企業の例です。"
        "この業種で資本金がもっとも大きい会社から順に選んでいます。"
    )
    old_example = {"industry": "銀行業", "company_name": "テスト銀行", "relation_text": old_text}
    check(
        "改修27-1(4-7)/検査27: 「機械が」の無い古い定型文は不合格になる",
        ve.check_lower_relation_text(old_example), "lower_relation_text_mismatch",
    )


def test_check_lower_industry():
    """作業C(8.2): 検査22(業種の許可リスト・impact_kind)の正例・負例。
    許可リストはこのテストの中だけの架空集合(データから作る処理は
    build_allowed_industriesとして別に実装済み)。"""
    allowed = {"テスト業種", "銀行業"}

    def example(industry, impact_kind=None):
        return {"industry": industry, "impact_kind": impact_kind}

    # --- 落ちるべき例(5件以上) ---
    check(
        "検査22/負例1: 'サービス業'は許可リストに無い",
        ve.check_lower_industry(example("サービス業"), allowed), "lower_industry_not_allowed",
    )
    check(
        "検査22/負例2: 'その他製品'は許可リストに無い",
        ve.check_lower_industry(example("その他製品"), allowed), "lower_industry_not_allowed",
    )
    check(
        "検査22/負例3: 'その他金融業'は許可リストに無い",
        ve.check_lower_industry(example("その他金融業"), allowed), "lower_industry_not_allowed",
    )
    check(
        "検査22/負例4: '外国法人・組合'は許可リストに無い",
        ve.check_lower_industry(example("外国法人・組合"), allowed), "lower_industry_not_allowed",
    )
    check(
        "検査22/負例5: '銀行'(正式名'銀行業'でない略称)は許可リストに無い",
        ve.check_lower_industry(example("銀行"), allowed), "lower_industry_not_allowed",
    )
    check(
        "検査22/負例6: impact_kindが'fact_only'になっている下段の会社は不合格",
        ve.check_lower_industry(example("テスト業種", impact_kind="fact_only"), allowed),
        "lower_industry_not_allowed",
    )

    # --- 通るべき例(2件以上) ---
    check(
        "検査22/正例1: 許可リストにある業種で、impact_kindがnullなら合格",
        ve.check_lower_industry(example("テスト業種"), allowed), None,
    )
    check(
        "検査22/正例2: 別の許可業種('銀行業')でも合格",
        ve.check_lower_industry(example("銀行業"), allowed), None,
    )


def test_check_lower_line_mark():
    """作業C(8.3): 検査28(根拠の行の確定した印)の正例・負例。"""
    line_marks = {
        "L-OK1": "source_number_match",
        "L-OK2": "reported_unverified",
        "L-NG1": "explainer",
        "L-NG2": "unverified",
    }

    def example(line_ids):
        return {"line_ids": line_ids}

    check(
        "検査28/正例1: 確定した印がsource_number_matchなら合格",
        ve.check_lower_line_mark(example(["L-OK1"]), line_marks), None,
    )
    check(
        "検査28/正例2: 確定した印がreported_unverifiedなら合格",
        ve.check_lower_line_mark(example(["L-OK2"]), line_marks), None,
    )
    check(
        "検査28/負例1: 確定した印がexplainerなら不合格",
        ve.check_lower_line_mark(example(["L-NG1"]), line_marks), "lower_line_mark_invalid",
    )
    check(
        "検査28/負例2: 確定した印がunverifiedなら不合格",
        ve.check_lower_line_mark(example(["L-NG2"]), line_marks), "lower_line_mark_invalid",
    )
    check(
        "検査28/負例3: line_idsが空なら不合格",
        ve.check_lower_line_mark(example([]), line_marks), "lower_line_mark_invalid",
    )
    check(
        "検査28/負例4: line_idsがNoneなら不合格",
        ve.check_lower_line_mark({"line_ids": None}, line_marks), "lower_line_mark_invalid",
    )
    check(
        "検査28/負例5: 紙面に存在しない行IDを指していれば不合格",
        ve.check_lower_line_mark(example(["L-NOT-EXIST"]), line_marks), "lower_line_mark_invalid",
    )


def test_check_lower_ticker():
    """作業C: 検査13(下段のticker/ticker_source)の正例・負例。"""
    check(
        "検査13(下段)/正例: tickerとticker_sourceが両方あれば合格",
        ve.check_lower_ticker({"ticker": "9001", "ticker_source": "edinet_codelist"}), None,
    )
    check(
        "検査13(下段)/負例1: tickerが空なら不合格",
        ve.check_lower_ticker({"ticker": None, "ticker_source": "edinet_codelist"}), "lower_ticker_missing",
    )
    check(
        "検査13(下段)/負例2: ticker_sourceが空なら不合格",
        ve.check_lower_ticker({"ticker": "9001", "ticker_source": ""}), "lower_ticker_missing",
    )


def test_check_lower_listed_and_ticker_match():
    """作業C: 検査21(コードリスト上「上場」か)・検査31(証券コードの一致)の正例・負例。"""
    rows = [
        _ec_row("テスト検証株式会社", "E-VERIFY-1", "90010", capital="1000"),
        _ec_row("テスト非上場株式会社", "E-VERIFY-2", "80010", listed="非上場", capital="1000"),
    ]

    def example(company_name, ticker, ticker_source="edinet_codelist"):
        return {"company_name": company_name, "ticker": ticker, "ticker_source": ticker_source}

    check(
        "検査21/正例: コードリスト上「上場」で見つかれば合格",
        ve.check_lower_listed(example("テスト検証株式会社", "9001"), rows), None,
    )
    check(
        "検査21/負例1: コードリスト上「上場」でない会社は不合格",
        ve.check_lower_listed(example("テスト非上場株式会社", "8001"), rows), "lower_not_listed",
    )
    check(
        "検査21/負例2: コードリストに存在しない会社名は不合格",
        ve.check_lower_listed(example("テスト架空株式会社", "9999"), rows), "lower_not_listed",
    )
    check(
        "検査21/負例3: ticker_sourceがedinet_codelist以外なら検査21自体は適用しない(合格)",
        ve.check_lower_listed(example("テスト架空株式会社", "9999", ticker_source="other"), rows), None,
    )

    check(
        "検査31/正例: tickerがコードリスト上の証券コードと一致すれば合格",
        ve.check_lower_ticker_match(example("テスト検証株式会社", "9001"), rows), None,
    )
    check(
        "検査31/負例: tickerがコードリスト上の証券コードと食い違えば不合格",
        ve.check_lower_ticker_match(example("テスト検証株式会社", "9999"), rows), "lower_ticker_mismatch",
    )


def test_run_slot_allocation():
    """作業D(8.4): 検査29(枠配分)の正例。
    上段5社・下段2社→下段0社、上段下段の重複排除、同業種3社→2社、
    同記事3社→2社、primary3社+reported3社(計6社)→primary3社+reported2社(計5社)。"""

    def hyp(ticker, grade, article_id):
        return {"ticker": ticker, "evidence_grade": grade, "line_ids": [f"L-{article_id}"]}

    def lower(ticker, industry, article_id):
        return {"ticker": ticker, "industry": industry, "article_id": article_id}

    def edition_for(article_ids):
        return {
            "sections": [{
                "section_id": "s",
                "articles": [
                    {"article_id": aid, "lines": [{"line_id": f"L-{aid}"}]}
                    for aid in article_ids
                ],
            }],
        }

    # --- 正例1: 上段5社・下段2社 → 合計5社になり、下段が0社になる ---
    hyps1 = [hyp(f"U{i}", "primary", f"ART-U{i}") for i in range(5)]
    examples1 = [lower("L1", "業種A", "ART-L1"), lower("L2", "業種B", "ART-L2")]
    doc1 = {"hypotheses": hyps1, "industry_examples": examples1}
    edition1 = edition_for([f"ART-U{i}" for i in range(5)] + ["ART-L1", "ART-L2"])
    ve.run_slot_allocation(doc1, edition1)
    check(
        "検査29/正例1: 上段5社・下段2社は合計5社になる",
        len(doc1["hypotheses"]) + len(doc1["industry_examples"]), 5,
    )
    check("検査29/正例1: 下段は0社になる", len(doc1["industry_examples"]), 0)
    check("検査29/正例1: 上段は5社のまま残る", len(doc1["hypotheses"]), 5)

    # --- 正例2: 上段2社・下段2社で、同じtickerが両方にいる → 下段側が消える ---
    hyps2 = [hyp("DUP", "primary", "ART-U1"), hyp("U2", "primary", "ART-U2")]
    examples2 = [lower("DUP", "業種A", "ART-L1"), lower("L2", "業種B", "ART-L2")]
    doc2 = {"hypotheses": hyps2, "industry_examples": examples2}
    edition2 = edition_for(["ART-U1", "ART-U2", "ART-L1", "ART-L2"])
    ve.run_slot_allocation(doc2, edition2)
    check(
        "検査29/正例2: 上段と下段に同じticker('DUP')がいたら下段側が消える",
        [e["ticker"] for e in doc2["industry_examples"]], ["L2"],
    )
    check("検査29/正例2: 上段はそのまま2社残る", len(doc2["hypotheses"]), 2)

    # --- 正例3: 同じ業種の会社が3社 → 2社になる ---
    examples3 = [lower(f"L{i}", "業種A", f"ART-L{i}") for i in range(3)]
    doc3 = {"hypotheses": [], "industry_examples": examples3}
    edition3 = edition_for([f"ART-L{i}" for i in range(3)])
    ve.run_slot_allocation(doc3, edition3)
    check("検査29/正例3: 同じ業種の会社が3社なら2社になる", len(doc3["industry_examples"]), 2)

    # --- 正例4: 同じ記事に3社(上段+下段の合計) → 2社になる ---
    hyps4 = [hyp("U1", "primary", "ART-SAME")]
    examples4 = [lower("L1", "業種A", "ART-SAME"), lower("L2", "業種B", "ART-SAME")]
    doc4 = {"hypotheses": hyps4, "industry_examples": examples4}
    edition4 = edition_for(["ART-SAME"])
    ve.run_slot_allocation(doc4, edition4)
    check(
        "検査29/正例4: 同じ記事に3社(上段1+下段2)いたら2社になる",
        len(doc4["hypotheses"]) + len(doc4["industry_examples"]), 2,
    )

    # --- 正例5: primary3社・reported3社(計6社) → primary3社+reported2社(計5社) ---
    hyps5 = (
        [hyp(f"P{i}", "primary", f"ART-P{i}") for i in range(3)]
        + [hyp(f"R{i}", "reported", f"ART-R{i}") for i in range(3)]
    )
    doc5 = {"hypotheses": hyps5, "industry_examples": []}
    edition5 = edition_for([f"ART-P{i}" for i in range(3)] + [f"ART-R{i}" for i in range(3)])
    ve.run_slot_allocation(doc5, edition5)
    kept_grades = [h["evidence_grade"] for h in doc5["hypotheses"]]
    check("検査29/正例5: primary3社は全部残る", kept_grades.count("primary"), 3)
    check("検査29/正例5: reportedは1社削られて2社になる", kept_grades.count("reported"), 2)
    check("検査29/正例5: 合計5社になる", len(doc5["hypotheses"]), 5)


def test_testdata_copy_integration():
    """3.3: scripts/testdata を一時フォルダにコピーし、コピーの方に対してCLI全体を
    走らせることで、本体のscripts/testdataには一切書き込まないことを確かめる。
    main()は紙面JSON・仮説JSONへ検査結果を書き戻すため、直接scripts/testdataの
    パスを渡すとリポジトリのフィクスチャが壊れてしまう(過去に2回発生)。
    修正Aにより検査24(号の日付)が常に実行時刻を基準にするため、日付固定
    (2026-09-24)のtestdataはそのままでは使えない。コピーの方を「今日のevening号」
    に書き直し、専用の一時カレンダー(_write_temp_calendar)を使って実行する(修正F)。"""
    src_testdata = REPO_ROOT / "scripts" / "testdata"
    with tempfile.TemporaryDirectory() as d:
        dst = Path(d) / "testdata"
        shutil.copytree(src_testdata, dst)

        edition_path, hyp_path, today_str = _rebuild_testdata_as_today_evening(src_testdata, dst)
        cache_dir = dst / "cache"
        calendar_dir = _write_temp_calendar(d, dt.datetime.strptime(today_str, "%Y-%m-%d").date(), 40)

        result = _run_verify(d, edition_path, hyp_path, cache_dir, calendar_dir)
        check(
            "testdata統合/コピーしたtestdataに対してCLI(main)が正常終了する(終了コード0)",
            result.returncode, 0,
        )

    _assert_testdata_untouched("testdata統合")


def test_verify_edition_industry_integration():
    """作業B(8.6): verify_edition.pyを1回実行するだけでindustry_examplesができること、
    同じファイルに2回続けて実行しても結果が変わらない(二重に増えない)ことを確かめる。
    実データ(EDINETコードリスト)には依存せず、架空の会社(テスト統合株式会社)だけを
    含む偽のコードリストCSVを、一時フォルダのcwd相対.cache/reference/に置いて使う。
    修正Aにより検査24(号の日付)が常に実行時刻を基準にするため、日付固定
    (2026-09-24)のtestdataはそのままでは使えない。コピーの方を「今日のevening号」
    に書き直し、専用の一時カレンダーを使って実行する(修正F)。"""
    src_testdata = REPO_ROOT / "scripts" / "testdata"
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        dst = work_dir / "testdata"
        shutil.copytree(src_testdata, dst)

        edition_path, hyp_path, today_str = _rebuild_testdata_as_today_evening(src_testdata, dst)
        cache_dir = dst / "cache"
        calendar_dir = _write_temp_calendar(work_dir, dt.datetime.strptime(today_str, "%Y-%m-%d").date(), 40)

        # L-12(article_id=ART-TEST-001)は claimed_mark が reported_unverified で、
        # 出典の照合を経ずに確定した印がreported_unverifiedになる行(検査28を通る
        # 根拠として使う。L-01は出典の照合条件(processing_note等)を満たさず
        # unverifiedになるため使わない)。
        hyp_doc = json.loads(hyp_path.read_text(encoding="utf-8"))
        hyp_doc["industry_picks"] = [{
            "article_id": "ART-TEST-001", "event_id": "EVT-INTEG",
            "industry": "テスト業種", "industry_line_ids": ["L-12"],
        }]
        hyp_path.write_text(json.dumps(hyp_doc, ensure_ascii=False, indent=1), encoding="utf-8")

        codelist_dir = work_dir / ".cache" / "reference"
        codelist_dir.mkdir(parents=True, exist_ok=True)
        csv_text = (
            "ダウンロード実行日,2026-09-24\n"
            "ＥＤＩＮＥＴコード,提出者名,提出者業種,上場区分,資本金,証券コード\n"
            "E-INTEG-1,テスト統合株式会社,テスト業種,上場,5000,90010\n"
        )
        (codelist_dir / "EdinetcodeDlInfo_2026-09-24.csv").write_bytes(csv_text.encode("cp932"))

        def run_once():
            return _run_verify(work_dir, edition_path, hyp_path, cache_dir, calendar_dir)

        result1 = run_once()
        check(
            "作業B/8.6: industry_picksを含めてverify_edition.pyを実行すると正常終了する(終了コード0)",
            result1.returncode, 0,
        )

        hyp_after1 = json.loads(hyp_path.read_text(encoding="utf-8"))
        examples1 = hyp_after1.get("industry_examples")
        check("作業B/8.6: 1回の実行でindustry_examplesが1社作られる", len(examples1 or []), 1)
        if examples1:
            check(
                "作業B/8.6: 選ばれた会社は偽コードリストの'テスト統合株式会社'",
                examples1[0]["company_name"], "テスト統合株式会社",
            )
            check("作業B/8.6: ticker_sourceが'edinet_codelist'", examples1[0].get("ticker_source"), "edinet_codelist")

        edition_after1 = json.loads(edition_path.read_text(encoding="utf-8"))
        check(
            "作業B/8.6: verificationにindustry_examples_totalが記録される",
            edition_after1["verification"].get("industry_examples_total"), 1,
        )

        result2 = run_once()
        check("作業B/8.6: 同じファイルに2回目を実行しても正常終了する(終了コード0)", result2.returncode, 0)
        hyp_after2 = json.loads(hyp_path.read_text(encoding="utf-8"))
        check(
            "作業B/8.6: 2回続けて実行してもindustry_examplesが二重に増えない(1社のまま)",
            len(hyp_after2.get("industry_examples") or []), 1,
        )

    _assert_testdata_untouched("作業B/8.6")


SOURCE_POLICY_PATH = REPO_ROOT / "scripts" / "source_policy.csv"
CALENDAR_DIR = REPO_ROOT / "calendar"


def test_apply_source_policy():
    """作業A(source_policy.csv)の負例。usage/publisher_typeはAIの自己申告ではなく
    scripts/source_policy.csvの値が必ず勝つことを確かめる。"""

    # --- 負例1: AIがquotableと書いたニュースのドメイン(www.nippon.com)がsnippet_onlyに上書きされる ---
    edition1 = {"sources": [
        {"source_id": "SRC-1", "url": "https://www.nippon.com/ja/some-article/", "usage": "quotable", "publisher_type": "news"},
    ]}
    result1 = ve.apply_source_policy(edition1, SOURCE_POLICY_PATH)
    check(
        "作業A/負例1: AIがquotableと書いたwww.nippon.comはsnippet_onlyに上書きされる",
        edition1["sources"][0]["usage"], "snippet_only",
    )
    check("作業A/負例1: overwritten件数が1件になる", result1["overwritten"], 1)

    # --- 負例2: 表に無いドメイン(example.co.jp)はsnippet_only/otherになり、unlisted_domainsに載る ---
    edition2 = {"sources": [{"source_id": "SRC-1", "url": "https://example.co.jp/xyz", "usage": "quotable"}]}
    result2 = ve.apply_source_policy(edition2, SOURCE_POLICY_PATH)
    check("作業A/負例2: 表に無いドメインはsnippet_onlyになる", edition2["sources"][0]["usage"], "snippet_only")
    check("作業A/負例2: 表に無いドメインはotherになる", edition2["sources"][0]["publisher_type"], "other")
    check("作業A/負例2: 表に無いドメインはunlisted_domainsに載る", result2["unlisted_domains"], ["example.co.jp"])

    # --- 負例3: boj.or.jp(www.無し)は表に無いものとして扱われる(部分一致で引かない) ---
    edition3 = {"sources": [{"source_id": "SRC-1", "url": "https://boj.or.jp/path", "usage": "quotable"}]}
    result3 = ve.apply_source_policy(edition3, SOURCE_POLICY_PATH)
    check(
        "作業A/負例3: www.の無いboj.or.jpは表に無いものとして扱われる(snippet_only)",
        edition3["sources"][0]["usage"], "snippet_only",
    )
    check("作業A/負例3: boj.or.jpはunlisted_domainsに載る", result3["unlisted_domains"], ["boj.or.jp"])

    # --- 負例4: WWW.BOJ.OR.JP(大文字)はwww.boj.or.jpとして引ける ---
    edition4 = {"sources": [{"source_id": "SRC-1", "url": "https://WWW.BOJ.OR.JP/path", "usage": None}]}
    result4 = ve.apply_source_policy(edition4, SOURCE_POLICY_PATH)
    check(
        "作業A/負例4: 大文字のWWW.BOJ.OR.JPも小文字化して表を引ける(quotable)",
        edition4["sources"][0]["usage"], "quotable",
    )
    check(
        "作業A/負例4: 大文字のWWW.BOJ.OR.JPのpublisher_typeはcentral_bank",
        edition4["sources"][0]["publisher_type"], "central_bank",
    )
    check("作業A/負例4: unlisted_domainsには載らない", result4["unlisted_domains"], [])

    # --- 負例5: usage:nullのEDINET出典がquotableになり、その行のexcerptが削除されない ---
    edition5 = {"sources": [
        {"source_id": "SRC-EDI", "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json", "usage": None, "publisher_type": None},
    ]}
    ve.apply_source_policy(edition5, SOURCE_POLICY_PATH)
    sources5 = {s["source_id"]: s for s in edition5["sources"]}
    line5 = {
        "claimed_mark": "reported_unverified", "numbers": [],
        "source_ref": "SRC-EDI", "excerpt": "何かの抜き出し",
    }
    mark5, reason5, _ = ve.verify_line(line5, sources5, "/nonexistent")
    check(
        "作業A/負例5: usage:nullのEDINET出典はquotableに埋まりexcerpt_not_allowedにならない",
        (mark5, reason5), ("reported_unverified", None),
    )
    check("作業A/負例5: excerptは削除されない", line5["excerpt"], "何かの抜き出し")

    # --- 負例6: source_policy.csvが読めない(存在しない)とき、号を保存しない(EditionInvalid) ---
    with tempfile.TemporaryDirectory() as d:
        missing_policy_path = Path(d) / "source_policy.csv"
        raised = False
        try:
            ve.apply_source_policy({"sources": []}, missing_policy_path)
        except ve.EditionInvalid:
            raised = True
        check("作業A/負例6: source_policy.csvが無いとEditionInvalidになり号を保存しない", raised, True)


def test_load_source_policy_reads_templates():
    """改修27-1(4-6): load_source_policy()がattribution_template・
    processing_note_templateも読み、値が空の行は号を保存しない(EditionInvalid)。"""
    policy = ve.load_source_policy(SOURCE_POLICY_PATH)
    edinet_entry = policy["api.edinet-fsa.go.jp"]
    check(
        "load_source_policy/正例: EDINETのprocessing_note_templateが読める",
        edinet_entry["processing_note_template"],
        "EDINET閲覧（提出）サイト（{url}）をもとに本サイト作成",
    )
    check(
        "load_source_policy/正例: EDINETのattribution_templateも読める(PDL1.0を含む)",
        "PDL1.0" in edinet_entry["attribution_template"], True,
    )
    boj_entry = policy["www.boj.or.jp"]
    check(
        "load_source_policy/正例: それ以外のドメインのprocessing_note_templateは汎用の文言",
        boj_entry["processing_note_template"],
        "{publisher}「{title}」（{url}）をもとに本サイト作成",
    )

    with tempfile.TemporaryDirectory() as d:
        bad_path = Path(d) / "source_policy.csv"
        bad_path.write_text(
            "domain,usage,publisher_type,independent_check,attribution_template,processing_note_template\n"
            "example.test,quotable,news,no,出典：{publisher},\n",
            encoding="utf-8",
        )
        raised = False
        try:
            ve.load_source_policy(bad_path)
        except ve.EditionInvalid:
            raised = True
        check(
            "load_source_policy/負例: processing_note_templateが空の行はEditionInvalidになる",
            raised, True,
        )


def test_fill_source_template():
    """改修27-1(4-6): ひな形に含まれるプレースホルダだけを見て埋める。使っている
    プレースホルダの値が1つでも空なら、'None'や空の「」を含む文を作らずNoneを返す。"""
    edinet_template = "EDINET閲覧（提出）サイト（{url}）をもとに本サイト作成"
    check(
        "fill_source_template/正例: EDINETのひな形はpublisher・titleが空でもurlだけで作れる",
        ve.fill_source_template(edinet_template, None, None, "https://disclosure2.edinet-fsa.go.jp/x"),
        "EDINET閲覧（提出）サイト（https://disclosure2.edinet-fsa.go.jp/x）をもとに本サイト作成",
    )

    generic_template = "出典：{publisher}「{title}」（{url}）"
    check(
        "fill_source_template/正例: 3つとも揃っていれば作れる",
        ve.fill_source_template(generic_template, "日本銀行", "金融政策決定会合", "https://www.boj.or.jp/x"),
        "出典：日本銀行「金融政策決定会合」（https://www.boj.or.jp/x）",
    )
    check(
        "fill_source_template/負例: publisherが空なら(汎用ひな形は使うので)Noneを返す('None'を含む文を作らない)",
        ve.fill_source_template(generic_template, None, "金融政策決定会合", "https://www.boj.or.jp/x"),
        None,
    )
    check(
        "fill_source_template/負例: publisherが空文字でもNoneを返す",
        ve.fill_source_template(generic_template, "", "金融政策決定会合", "https://www.boj.or.jp/x"),
        None,
    )
    check(
        "fill_source_template/負例: titleが空でもNoneを返す",
        ve.fill_source_template(generic_template, "日本銀行", None, "https://www.boj.or.jp/x"),
        None,
    )
    check(
        "fill_source_template/負例: urlが空でもNoneを返す",
        ve.fill_source_template(generic_template, "日本銀行", "金融政策決定会合", None),
        None,
    )


def test_apply_source_attribution():
    """改修27-1(4-6): attribution/processing_noteを、出典のtitle・url・publisherと
    ひな形から機械で作る。出典一覧と、本文の各行(source_refが指す出典の値を使う)の
    両方に書くこと、AIの値(nullを含む)を必ず上書きすること、csvに無いドメインは
    汎用ひな形を使うこと、値が足りなければnullのままにすることを確かめる。"""
    edition = {
        "sources": [
            {
                "source_id": "SRC-EDINET", "publisher": "カナリア工業", "title": "臨時報告書",
                "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100X?type=1",
                "usage": "quotable",
                "attribution": None, "processing_note": None,
            },
            {
                "source_id": "SRC-BOJ", "publisher": "日本銀行", "title": "金融政策決定会合",
                "url": "https://www.boj.or.jp/x", "attribution": "AIが書いた値", "processing_note": "AIが書いた値",
            },
            {
                "source_id": "SRC-UNLISTED", "publisher": "架空新聞社", "title": "架空の記事",
                "url": "https://example.test/article", "attribution": None, "processing_note": None,
            },
            {
                # publisherが空(AIが書けなかった)出典。汎用ひな形はpublisherを
                # 使うので生成できない。
                "source_id": "SRC-NO-PUBLISHER", "publisher": None, "title": "架空の記事2",
                "url": "https://example.test/article2", "attribution": "元の値", "processing_note": "元の値",
            },
        ],
        "sections": [{
            "section_id": "change",
            "articles": [{
                "article_id": "A-1",
                "lines": [
                    {
                        "line_id": "L-1", "claimed_mark": "source_number_match",
                        "numbers": [{"label": "件数", "value": 1}],
                        "source_ref": "SRC-EDINET", "excerpt": "何かの抜き出し",
                        "attribution": None, "processing_note": None,
                    },
                    {
                        "line_id": "L-2", "claimed_mark": "reported_unverified",
                        "numbers": [], "source_ref": "SRC-NO-PUBLISHER",
                        "attribution": "元の値", "processing_note": "元の値",
                    },
                ],
            }],
        }],
    }

    result = ve.apply_source_attribution(edition, SOURCE_POLICY_PATH)
    by_id = {s["source_id"]: s for s in edition["sources"]}
    lines_by_id = {l["line_id"]: l for l in edition["sections"][0]["articles"][0]["lines"]}

    check(
        "apply_source_attribution/正例(EDINET): attributionにPDL1.0のURLが入る",
        "https://www.digital.go.jp/resources/open_data/public_data_license_v1.0" in by_id["SRC-EDINET"]["attribution"],
        True,
    )
    check(
        "apply_source_attribution/正例(EDINET): processing_noteに「をもとに本サイト作成」が入る",
        by_id["SRC-EDINET"]["processing_note"],
        "EDINET閲覧（提出）サイト（https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100X?type=1）をもとに本サイト作成",
    )
    check(
        "apply_source_attribution/正例: AIの値(元の値)は日本銀行の出典でも上書きされる",
        by_id["SRC-BOJ"]["attribution"], "出典：日本銀行「金融政策決定会合」（https://www.boj.or.jp/x）",
    )
    check(
        "apply_source_attribution/正例: csvに無いドメインは汎用ひな形が使われる",
        (by_id["SRC-UNLISTED"]["attribution"], by_id["SRC-UNLISTED"]["processing_note"]),
        (
            "出典：架空新聞社「架空の記事」（https://example.test/article）",
            "架空新聞社「架空の記事」（https://example.test/article）をもとに本サイト作成",
        ),
    )
    check(
        "apply_source_attribution/負例: publisherが空なら'None'を含む文を作らずnullのままにする",
        (by_id["SRC-NO-PUBLISHER"]["attribution"], by_id["SRC-NO-PUBLISHER"]["processing_note"]),
        (None, None),
    )
    check(
        "apply_source_attribution/負例: 'None'という文字列が出典表記に紛れ込んでいない",
        any("None" in (by_id[sid].get("attribution") or "") for sid in by_id), False,
    )

    check(
        "apply_source_attribution/正例: 本文の行にも、その行のsource_refが指す出典の値でattributionが入る",
        lines_by_id["L-1"]["attribution"],
        by_id["SRC-EDINET"]["attribution"],
    )
    check(
        "apply_source_attribution/正例: 行のprocessing_noteも出典の値と同じになる",
        lines_by_id["L-1"]["processing_note"], by_id["SRC-EDINET"]["processing_note"],
    )
    check(
        "apply_source_attribution/負例: publisherが無い出典を参照する行もnullのままになる",
        (lines_by_id["L-2"]["attribution"], lines_by_id["L-2"]["processing_note"]),
        (None, None),
    )

    check(
        "apply_source_attribution/件数: attribution_generation_skippedは1"
        "(SRC-NO-PUBLISHERとそれを参照するL-2で2箇所nullになったが、"
        "スキップと数えるのは出典・行それぞれ1回ずつ)",
        result["attribution_generation_skipped"], 2,
    )
    check(
        "apply_source_attribution/件数: attribution_overwrittenにはnullから値にした件数(SRC-EDINET・"
        "SRC-BOJ・SRC-UNLISTED・L-1)と、生成できずnullに戻した件数(SRC-NO-PUBLISHER・L-2、"
        "AIが書いていた元の値と違う値=nullになったので変化ありと数える)の合わせて6件が入る",
        result["attribution_overwritten"], 6,
    )

    # --- 検査3が通ることの確認: AIがattribution/processing_noteにnullを置いた
    #     source_number_matchの行が、上書き後は必須項目で落ちない(missing_fieldにならない) ---
    sources_by_id = {s["source_id"]: s for s in edition["sources"]}
    mark_l1, reason_l1, _ = ve.verify_line(lines_by_id["L-1"], sources_by_id, "/nonexistent")
    check(
        "改修27-1(4-6)/検査3: attribution・processing_noteがnullだった行も、上書き後はmissing_fieldにならない"
        "(出典本文が無いのでsource_unfetchableにはなるが、missing_fieldにはならない)",
        reason_l1, "source_unfetchable",
    )


def test_compute_edinet_view_url():
    """改修27-1第6回: EDINETの出典について、読者が実際に開けるURL(view_url)を
    機械で決める規則の正例・負例。"""
    doc_source = {
        "source_id": "SRC-DOC",
        "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100Z3TZ?type=1",
    }
    check(
        "compute_edinet_view_url/正例: 個々の書類はWZEK0040.aspx?書類管理番号になる",
        ve.compute_edinet_view_url(doc_source),
        "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100Z3TZ",
    )

    list_source = {
        "source_id": "SRC-EDINET-LIST",
        "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-28&type=2",
    }
    check(
        "compute_edinet_view_url/正例: 書類一覧そのものはEDINET閲覧サイトのトップになる",
        ve.compute_edinet_view_url(list_source), "https://disclosure2.edinet-fsa.go.jp/",
    )
    list_prev_source = {"source_id": "SRC-EDINET-LIST-PREV", "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-27&type=2"}
    check(
        "compute_edinet_view_url/正例: SRC-EDINET-LIST-PREVも同じくトップになる",
        ve.compute_edinet_view_url(list_prev_source), "https://disclosure2.edinet-fsa.go.jp/",
    )

    other_edinet_source = {"source_id": "SRC-EDINET-SEARCH", "url": "https://disclosure2.edinet-fsa.go.jp/"}
    check(
        "compute_edinet_view_url/負例: 個々の書類でも一覧でもないEDINETのURLはNone(urlをそのまま使う)",
        ve.compute_edinet_view_url(other_edinet_source), None,
    )

    news_source = {"source_id": "SRC-NEWS", "url": "https://www.nikkei.com/article/xxx/"}
    check(
        "compute_edinet_view_url/負例: EDINET以外の出典はNone",
        ve.compute_edinet_view_url(news_source), None,
    )


def test_apply_edinet_view_url():
    """改修27-1第6回: apply_edinet_view_url()が出典一覧の全件にview_urlを書き込む
    (AIが書いた値も必ず上書きする)ことの正例・負例。"""
    edition = {
        "sources": [
            {
                "source_id": "SRC-DOC", "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100X?type=1",
                "view_url": "AIが書いた(誤った)値",
            },
            {"source_id": "SRC-EDINET-LIST", "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-28&type=2"},
            {"source_id": "SRC-NEWS", "url": "https://www.nikkei.com/article/xxx/", "view_url": "AIが書いた値"},
        ],
    }
    count = ve.apply_edinet_view_url(edition)
    by_id = {s["source_id"]: s for s in edition["sources"]}
    check(
        "apply_edinet_view_url/正例: AIが書いた値があっても機械の値で上書きされる",
        by_id["SRC-DOC"]["view_url"], "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100X",
    )
    check(
        "apply_edinet_view_url/正例: 一覧の出典もview_urlが埋まる",
        by_id["SRC-EDINET-LIST"]["view_url"], "https://disclosure2.edinet-fsa.go.jp/",
    )
    check(
        "apply_edinet_view_url/負例: EDINET以外の出典はview_urlがNoneに上書きされる(AIの値は残らない)",
        by_id["SRC-NEWS"]["view_url"], None,
    )
    check("apply_edinet_view_url/件数: view_urlを書いた出典は2件", count, 2)


def test_apply_source_attribution_uses_edinet_view_url():
    """改修27-1第6回をテストに必ず入れるものの確認:
      ・書類の出典で、attributionにWZEK0040.aspx?書類管理番号が入り、
        api/v2/documentsの形が出典表記に一切残らないこと
      ・出典のurlは変わっておらず、書類管理番号の取り出しが今までどおり働くこと
      ・書類一覧の出典で、出典表記にapi.edinet-fsa.go.jpのURLが残らないこと
      ・EDINET以外の出典のattributionが第4回と1文字も変わらないこと
    apply_edinet_view_url()を先に呼んでから(実際の照合スクリプトの順番どおり)
    apply_source_attribution()を呼ぶ。"""
    edition = {
        "sources": [
            {
                "source_id": "C01", "publisher": "カナリア物産株式会社", "title": "公開買付届出書",
                "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100CANARY?type=1",
                "attribution": None, "processing_note": None,
            },
            {
                "source_id": "SRC-EDINET-LIST", "publisher": "金融庁", "title": "EDINET 書類一覧",
                "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-28&type=2",
                "attribution": None, "processing_note": None,
            },
            {
                "source_id": "SRC-BOJ", "publisher": "日本銀行", "title": "金融政策決定会合",
                "url": "https://www.boj.or.jp/x", "attribution": None, "processing_note": None,
            },
        ],
        "sections": [],
    }

    # --- 第4回時点の挙動(view_urlを使わない)との比較用に、先に第4回の結果を控えておく ---
    edition_round4_only = copy.deepcopy(edition)
    ve.apply_source_attribution(edition_round4_only, SOURCE_POLICY_PATH)
    boj_attribution_round4 = {s["source_id"]: s for s in edition_round4_only["sources"]}["SRC-BOJ"]["attribution"]

    ve.apply_edinet_view_url(edition)
    ve.apply_source_attribution(edition, SOURCE_POLICY_PATH)
    by_id = {s["source_id"]: s for s in edition["sources"]}

    doc_url = by_id["C01"]["url"]
    doc_attribution = by_id["C01"]["attribution"]
    check(
        "改修27-1第6回/テストに必ず入れるもの1: 書類の出典のurlは変わっていない(書類管理番号の取り出しに使う値のまま)",
        doc_url, "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100CANARY?type=1",
    )
    check(
        "改修27-1第6回/テストに必ず入れるもの1: attributionにWZEK0040.aspx?書類管理番号が入る",
        "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100CANARY" in doc_attribution, True,
    )
    check(
        "改修27-1第6回/テストに必ず入れるもの1: attributionにapi/v2/documentsの形が一切残らない",
        "api/v2/documents" in doc_attribution, False,
    )
    check(
        "改修27-1第6回/テストに必ず入れるもの1: extract_edinet_doc_id()は今までどおりurlから書類管理番号を取り出せる",
        ve.extract_edinet_doc_id(doc_url), "S100CANARY",
    )

    list_attribution = by_id["SRC-EDINET-LIST"]["attribution"]
    check(
        "改修27-1第6回/テストに必ず入れるもの2: 書類一覧の出典表記にapi.edinet-fsa.go.jpのURLが残らない",
        "api.edinet-fsa.go.jp" in list_attribution, False,
    )
    check(
        "改修27-1第6回/テストに必ず入れるもの2: 書類一覧の出典表記にはEDINET閲覧サイトのトップが入る",
        "https://disclosure2.edinet-fsa.go.jp/" in list_attribution, True,
    )

    check(
        "改修27-1第6回/テストに必ず入れるもの3: EDINET以外の出典(日本銀行)のattributionは第4回と1文字も変わらない",
        by_id["SRC-BOJ"]["attribution"], boj_attribution_round4,
    )


def test_check_market_open():
    """作業B(検査35)の負例。market_openはAIの自己申告ではなく営業日カレンダーが
    必ず勝つことと、カレンダー自体が壊れている場合は号を保存しないことを確かめる。"""

    # --- 負例1: market_open:nullの営業日の号 → trueが埋まる ---
    edition1 = {"date": "2026-09-24", "market_open": None}
    result1 = ve.check_market_open(edition1, CALENDAR_DIR)
    check("作業B/負例1: market_open:nullの営業日はtrueが埋まる", edition1["market_open"], True)
    check("作業B/負例1: overwrittenがTrueになる(nullからの充填)", result1["overwritten"], True)
    check("作業B/負例1: market_open_reportedはnullのまま記録される", result1["reported"], None)

    # market_open=Trueが確定した号ではhypothesesがmarket_closedとして削除されない(企業欄が残る)。
    edition1["sections"] = []
    hyp_doc1 = {"hypotheses": [{"company_name": "テスト物産"}]}
    _, reasons1, _ = ve.run_hypothesis_checks(hyp_doc1, edition1, [], [], "/nonexistent", None, None)
    check("作業B/負例1: market_open=Trueならmarket_closedを理由に仮説が削除されない", "market_closed" in reasons1, False)

    # --- 負例2: AIがtrueと書いた休場日(2026-10-12・スポーツの日) → falseに上書き ---
    edition2 = {"date": "2026-10-12", "market_open": True}
    result2 = ve.check_market_open(edition2, CALENDAR_DIR)
    check("作業B/負例2: 休場日はAIがtrueと書いてもfalseに上書きされる", edition2["market_open"], False)
    check("作業B/負例2: market_open_overwrittenがtrueになる", result2["overwritten"], True)

    edition2["sections"] = []
    hyp_doc2 = {"hypotheses": [{"company_name": "テスト物産"}]}
    violations2, reasons2, _ = ve.run_hypothesis_checks(hyp_doc2, edition2, [], [], "/nonexistent", None, None)
    check("作業B/負例2: market_open=Falseになった号は仮説がmarket_closedとして全件削除される", reasons2.get("market_closed"), 1)
    check("作業B/負例2: 仮説(企業欄)が空になる", hyp_doc2["hypotheses"], [])

    # --- 負例3: AIがfalseと書いた営業日 → trueに上書き ---
    edition3 = {"date": "2026-09-24", "market_open": False}
    result3 = ve.check_market_open(edition3, CALENDAR_DIR)
    check("作業B/負例3: 営業日はAIがfalseと書いてもtrueに上書きされる", edition3["market_open"], True)
    check("作業B/負例3: market_open_overwrittenがtrueになる", result3["overwritten"], True)

    # --- 負例4: market_openキー自体が無い → 号を保存しない ---
    edition4 = {
        "edition_id": "e", "date": "2026-09-24", "slot": "morning", "generated_at": "2026-09-24T08:00:00+09:00",
        "sources": [], "sections": [],
    }
    raised4 = False
    try:
        ve.check_a_structure(edition4)
    except ve.EditionInvalid:
        raised4 = True
    check("作業B/負例4: market_openキーが無いと号を保存しない", raised4, True)

    # --- 負例5: market_open:"true"(文字列) → 号を保存しない ---
    edition5 = dict(edition4)
    edition5["market_open"] = "true"
    raised5 = False
    try:
        ve.check_a_structure(edition5)
    except ve.EditionInvalid:
        raised5 = True
    check("作業B/負例5: market_openが文字列だと号を保存しない", raised5, True)

    # --- 負例6: カレンダーの無い年(2029) → 号を保存しない ---
    edition6 = {"date": "2029-01-04", "market_open": None}
    raised6 = False
    try:
        ve.check_market_open(edition6, CALENDAR_DIR)
    except ve.EditionInvalid:
        raised6 = True
    check("作業B/負例6: カレンダーファイルの無い年は号を保存しない", raised6, True)


def test_find_number_numeric_comparison():
    """作業C(find_numberを数値として比べる)の正例・負例。
    2.0というvalueがformat_number()で文字列"2"に直されて本文中の"2.0"と
    一致しなくなっていた不具合が直っていることと、既存の判定
    (12.0の中の2、2.05に対する2.0は不一致のまま)が変わっていないことを確かめる。"""
    check("作業C/正例1: 「前年比2.0%増」とvalue 2.0は一致する", ve.find_number("前年比2.0%増", 2.0), True)
    check("作業C/正例2: 「前年比2.0%増」とvalue 2(int)も一致する", ve.find_number("前年比2.0%増", 2), True)
    check("作業C/正例3: 「金利1.0%へ」とvalue 1.0は一致する", ve.find_number("金利1.0%へ", 1.0), True)

    check("作業C/負例1: 「12.0%」とvalue 2は一致しない(別の数字の一部)", ve.find_number("12.0%", 2), False)
    check("作業C/負例2: 「2.05%」とvalue 2.0は一致しない(別の数字)", ve.find_number("2.05%", 2.0), False)
    check("作業C/負例3(これまで通り): 「2.5%」とvalue 2.5は一致する", ve.find_number("2.5%", 2.5), True)

    zenkaku_norm = ve.normalize_text("１，２３４人")
    check(
        "作業C/負例4(これまで通り): 全角「１，２３４人」を正規化後、value 1234と一致する",
        ve.find_number(zenkaku_norm, 1234), True,
    )
    check(
        "作業C/負例5: マイナスの実測値「-5.2」はvalue -5.2と一致する(既存の号で使われている形)",
        ve.find_number("-5.2", -5.2), True,
    )
    check(
        "作業C/負例6: 日付風の文字列「2026-09-25」はvalue -25(マイナス)とは一致しない",
        ve.find_number("2026-09-25", -25), False,
    )


def test_find_number_leading_zero():
    """find_numberを数値比較に変えた副作用の修正確認。日付・連番の中の「09」「08」
    のような先頭に0が付いた数字は、数値としては9・8と等しくなるが、紙面が書いた
    数字の裏付けにはならないため、照合の対象から外れることを確かめる。"""
    check(
        "先頭0/負例1: 「2026-09-25に発表」とvalue 9は一致しない(月の09が9の裏付けにならない)",
        ve.find_number("2026-09-25に発表", 9), False,
    )
    check(
        "先頭0/負例2: 「第08次」とvalue 8は一致しない",
        ve.find_number("第08次", 8), False,
    )
    check(
        "先頭0/負例3: 「09時に発表」とvalue 9は一致しない",
        ve.find_number("09時に発表", 9), False,
    )
    check(
        "先頭0/負例4(これまで通り): 「0.75%」とvalue 0.75は一致する(小数は対象外)",
        ve.find_number("0.75%", 0.75), True,
    )
    check(
        "先頭0/負例5(これまで通り): 「0%」とvalue 0は一致する(0そのものは対象外)",
        ve.find_number("0%", 0), True,
    )
    check(
        "先頭0/負例6(これまで通り): 「2026-09-25に発表」とvalue 25は一致する",
        ve.find_number("2026-09-25に発表", 25), True,
    )


# ============ 16-2b: 修正2・3・4の統合テスト用の共通部品 ============
# first_run/skip_companies/検査24は照合の本体(run_verification)の中に組み込まれて
# いるため、これらを確かめるには本体全体を走らせる必要がある。改修27-2第2回から、
# 別に起動するのではなく_run_verify()で同じプロセスの中から固定した時刻で呼ぶ
# (コマンドとしての動作は test_cli_runs_with_current_time の1本だけで確かめる)。
# scripts/testdataは一時フォルダにコピーしてから使う(本体を書き換えないため)。


def _copy_testdata_to(tmp_root):
    dst = Path(tmp_root) / "testdata"
    shutil.copytree(REPO_ROOT / "scripts" / "testdata", dst)
    return dst


# 改修27-2第2回: 同じプロセスの中から照合の本体を呼ぶときに使う、時間帯ごとの
# 固定の実行時刻(日本時間)。どれも時間帯の境目や朝号の門限(8:50)から離した時刻。
FIXED_RUN_TIME_BY_SLOT = {"morning": dt.time(7, 30), "noon": dt.time(13, 0), "evening": dt.time(18, 0)}


def fixed_run_at_for_edition(edition_path):
    """号のファイルに書かれたdateとslotから、その号を正しく照合できる固定の実行時刻を
    作る(例: 2026-09-29のevening号なら2026-09-29T18:00:00+09:00)。"""
    edition = json.loads(Path(edition_path).read_text(encoding="utf-8"))
    day = dt.datetime.strptime(edition["date"], "%Y-%m-%d").date()
    return dt.datetime.combine(day, FIXED_RUN_TIME_BY_SLOT[edition["slot"]], tzinfo=ve.JST)


def _run_verify(work_dir, edition_path, hyp_path, cache_dir, calendar_dir=None, run_at_dt=None):
    """改修27-2第2回: 照合の本体(verify_edition.run_verification)を、同じプロセスの中から
    固定した実行時刻で呼ぶ。以前はverify_edition.pyを別に起動していたが、それでは
    実行時刻を固定できず、時間帯のずれで号を止める検査24(S14)を入れると、テストを
    実行した時刻によって結果が変わってしまうため。

    run_at_dtを省略したときは、号のファイルのdate・slotからfixed_run_at_for_edition()で
    決める(その号の時間帯の中の時刻)。わざと食い違わせたいテストはrun_at_dtを渡す。
    別に起動していたときと同じく、作業フォルダ(work_dir)をカレントにして実行し
    (.cache/referenceなどの相対パスがwork_dirを指すようにするため)、終わったら戻す。
    戻り値は別に起動していたときと同じく、returncode・stdout・stderrを持つ。
    予期しない例外は、コマンドと同じく終了コード2として返す。"""
    if run_at_dt is None:
        run_at_dt = fixed_run_at_for_edition(edition_path)
    out, err = io.StringIO(), io.StringIO()
    prev_cwd = os.getcwd()
    os.chdir(str(work_dir))
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                returncode = ve.run_verification(
                    str(edition_path), str(hyp_path) if hyp_path is not None else None,
                    str(cache_dir), str(calendar_dir or CALENDAR_DIR), run_at_dt,
                )
            except Exception as e:  # コマンドの「スクリプト自体のエラー」(終了コード2)と同じ扱い
                print(f"[スクリプトエラー] {type(e).__name__}: {e}", file=sys.stderr)
                returncode = 2
    finally:
        os.chdir(prev_cwd)
    return types.SimpleNamespace(returncode=returncode, stdout=out.getvalue(), stderr=err.getvalue())


def _write_temp_calendar(work_dir, start_date, days):
    """work_dir/calendar/{年}.json を作る。business_daysはstart_dateから連続する
    days日分の日付文字列(土日も営業日として入れる)。年をまたぐ場合は年ごとに
    ファイルを分ける。本番のカレンダー(実際の休日・祝日を反映したもの)とは
    違う形になるが、テストの目的(照合の流れが通るか)には影響しない。"""
    calendar_dir = Path(work_dir) / "calendar"
    calendar_dir.mkdir(parents=True, exist_ok=True)
    by_year = {}
    current = start_date
    for _ in range(days):
        by_year.setdefault(current.year, []).append(current.strftime("%Y-%m-%d"))
        current = current + dt.timedelta(days=1)
    for year, business_days in by_year.items():
        payload = {
            "schema_version": 1, "year": year,
            "business_days_count": len(business_days), "business_days": business_days,
        }
        (calendar_dir / f"{year}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8",
        )
    return calendar_dir


def _expected_edition_date(slot, run_at_dt):
    """check_edition_date()と同じ規則で「期待される日付」を計算する(テスト用)。"""
    local_dt = run_at_dt.astimezone(ve.JST)
    if slot == "evening" and local_dt.time() < dt.time(5, 0):
        return (local_dt - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return local_dt.strftime("%Y-%m-%d")


def _rebuild_testdata_as_today_evening(src_testdata_root, dst_root):
    """scripts/testdataのmorning.json(日付固定: 2026-09-24)を、日付固定ではなく
    「今日のevening号」として書き直したコピーをdst_root配下に作る(修正F)。
    検査24が実行時刻を基準にするようになったため、日付が実行時刻と一致しないと
    号を保存できなくなった。slotをeveningにするのは、朝号・昼号のままだと
    テストの実行時刻によっては遅延(baseline_late)と判定され、企業欄が消えて
    しまうため(evening号は検査20の対象外)。baseline_price_typeをnext_openに
    するのは、baseline_dateを翌日にしたことに合わせるため。
    戻り値: (edition_path, hyp_path, today_str)。"""
    now = dt.datetime.now(ve.JST)
    today_str = _expected_edition_date("evening", now)
    tomorrow_str = (dt.datetime.strptime(today_str, "%Y-%m-%d") + dt.timedelta(days=1)).strftime("%Y-%m-%d")

    edition = json.loads((src_testdata_root / "editions" / "2026-09-24" / "morning.json").read_text(encoding="utf-8"))
    hyp = json.loads((src_testdata_root / "hypotheses" / "2026-09-24-morning.json").read_text(encoding="utf-8"))

    edition_id = f"{today_str}-evening"
    edition["edition_id"] = edition_id
    edition["date"] = today_str
    edition["slot"] = "evening"
    edition.pop("verification", None)
    edition.pop("baseline_late", None)

    hyp["edition_id"] = edition_id
    for h in hyp.get("hypotheses", []):
        horizon = h.get("horizon_business_days")
        h["baseline_date"] = tomorrow_str
        h["baseline_price_type"] = "next_open"
        if isinstance(horizon, int):
            deadline = dt.datetime.strptime(tomorrow_str, "%Y-%m-%d") + dt.timedelta(days=horizon)
            h["deadline_date"] = deadline.strftime("%Y-%m-%d")

    edition_dir = dst_root / "editions" / today_str
    edition_dir.mkdir(parents=True, exist_ok=True)
    edition_path = edition_dir / "evening.json"
    edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

    hyp_dir = dst_root / "hypotheses"
    hyp_dir.mkdir(parents=True, exist_ok=True)
    hyp_path = hyp_dir / f"{edition_id}.json"
    hyp_path.write_text(json.dumps(hyp, ensure_ascii=False, indent=1), encoding="utf-8")

    return edition_path, hyp_path, today_str


CANARY_DIR = REPO_ROOT / "scripts" / "testdata" / "canary"
# scripts/testdata/canary の中の号は、この日付を「号の日付」「直前の営業日」として
# 固定で作ってある(実データのscripts/testdataと同じ考え方)。_rebuild_canary_as_today()が
# テスト実行のたびに「今日」へ書き換える。
CANARY_BASELINE_DATE = "2026-09-24"
CANARY_PREV_DATE = "2026-09-18"


def _rebuild_canary_as_today(work_dir):
    """scripts/testdata/canary(日付固定の見本の号)を、「今日のevening号」として
    書き直したコピーをwork_dir配下に作る(_rebuild_testdata_as_today_evening()と
    同じ考え方)。日付の文字列をそのまま置き換えるだけで済むように、見本の号の
    中身(本文・出典・提出時刻)は日付を書いた文字列を含まない形にしてある。

    戻り値: (edition_path, hyp_path, cache_dir, today_str, prev_str)。"""
    now = dt.datetime.now(ve.JST)
    today_str = _expected_edition_date("evening", now)
    prev_str = (dt.datetime.strptime(today_str, "%Y-%m-%d") - dt.timedelta(days=1)).strftime("%Y-%m-%d")

    work_dir = Path(work_dir)

    cache_dir = work_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for src in (CANARY_DIR / "cache").iterdir():
        if src.suffix == ".json":
            text = src.read_text(encoding="utf-8")
            text = text.replace(CANARY_BASELINE_DATE, today_str).replace(CANARY_PREV_DATE, prev_str)
            (cache_dir / src.name).write_text(text, encoding="utf-8")
        elif src.suffix == ".txt" and "{{" in src.read_text(encoding="utf-8"):
            # 改修27-2第4回: 本文の中に前日の日付を書きたい見本の出典(C08・C12)は、
            # 置き換え用の目印({{PREV_JP_FW}}など)を含む。目印を含む本文だけを書き換え、
            # ほかの本文(ハッシュを照合するC01〜C04など)は1バイトも変えずにコピーする。
            text = src.read_text(encoding="utf-8")
            prev_date = dt.datetime.strptime(prev_str, "%Y-%m-%d")
            prev_jp = f"{prev_date.year}年{prev_date.month}月{prev_date.day}日"
            month_names = ["January", "February", "March", "April", "May", "June", "July",
                           "August", "September", "October", "November", "December"]
            prev_en = f"{month_names[prev_date.month - 1]} {prev_date.day}, {prev_date.year}"
            fullwidth = str.maketrans("0123456789", "０１２３４５６７８９")
            text = (text.replace("{{PREV_JP_FW}}", prev_jp.translate(fullwidth))
                        .replace("{{PREV_JP}}", prev_jp).replace("{{PREV_EN}}", prev_en))
            (cache_dir / src.name).write_text(text, encoding="utf-8")
        else:
            shutil.copy(src, cache_dir / src.name)

    edition_text = (CANARY_DIR / "edition.json").read_text(encoding="utf-8")
    edition_text = edition_text.replace(CANARY_BASELINE_DATE, today_str).replace(CANARY_PREV_DATE, prev_str)
    edition_dir = work_dir / "editions" / today_str
    edition_dir.mkdir(parents=True, exist_ok=True)
    edition_path = edition_dir / "evening.json"
    edition_path.write_text(edition_text, encoding="utf-8")

    hyp_text = (CANARY_DIR / "hypotheses.json").read_text(encoding="utf-8")
    hyp_text = hyp_text.replace(CANARY_BASELINE_DATE, today_str).replace(CANARY_PREV_DATE, prev_str)
    hyp_dir = work_dir / "hypotheses"
    hyp_dir.mkdir(parents=True, exist_ok=True)
    hyp_path = hyp_dir / f"{today_str}-evening.json"
    hyp_path.write_text(hyp_text, encoding="utf-8")

    return edition_path, hyp_path, cache_dir, today_str, prev_str


def _write_fake_codelist(work_dir, rows):
    """rows: (edinet_code, 会社名, 業種, 上場区分, 資本金, 証券コード)のタプルのリスト。"""
    codelist_dir = Path(work_dir) / ".cache" / "reference"
    codelist_dir.mkdir(parents=True, exist_ok=True)
    lines = ["ダウンロード実行日,2026-09-24", "ＥＤＩＮＥＴコード,提出者名,提出者業種,上場区分,資本金,証券コード"]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    csv_text = "\n".join(lines) + "\n"
    (codelist_dir / "EdinetcodeDlInfo_2026-09-24.csv").write_bytes(csv_text.encode("cp932"))


def _assert_testdata_untouched(label):
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "scripts/testdata"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    check(f"{label}: scripts/testdata本体はテスト実行後も変更されていない", status.stdout.strip(), "")


def test_build_first_run_record():
    """修正2: first_runの中身(特にgenerated_at_drift_minutesの符号)を確かめる。
    run_at_dtとgenerated_atを直接固定できるbuild_first_run_record()の単体テストとして行う
    (実時刻の経過を伴う「実行のたびに8分ずれる」ような検証は、実行環境の実時刻を
    差し替える手段が無いため、この単体テストで代替する)。"""
    run_at_dt = dt.datetime.fromisoformat("2026-09-24T07:52:10+09:00")
    run_at = run_at_dt.isoformat()

    rec_before = ve.build_first_run_record(run_at, run_at_dt, False, True, 0, "2026-09-24T07:44:10+09:00")
    check("first_run/差の計算: generated_atが8分10秒前ならdrift_minutesは8", rec_before["generated_at_drift_minutes"], 8)
    check("first_run/差の計算: generated_atが読めればgenerated_at_parsedはTrue", rec_before["generated_at_parsed"], True)

    rec_after = ve.build_first_run_record(run_at, run_at_dt, False, True, 0, "2026-09-24T07:55:10+09:00")
    check("first_run/差の計算: generated_atが3分後ならdrift_minutesは-3", rec_after["generated_at_drift_minutes"], -3)

    rec_broken = ve.build_first_run_record(run_at, run_at_dt, False, True, 0, "not-a-datetime")
    check("first_run/負例: generated_atが読めなければgenerated_at_parsedはFalse", rec_broken["generated_at_parsed"], False)
    check("first_run/負例: generated_atが読めなければdrift_minutesはNone", rec_broken["generated_at_drift_minutes"], None)

    rec_empty = ve.build_first_run_record(run_at, run_at_dt, False, True, 0, "")
    check("first_run/負例: generated_atが空文字でもgenerated_at_parsedはFalse", rec_empty["generated_at_parsed"], False)

    check("first_run/run_atがそのまま入る", rec_before["run_at"], run_at)
    check("first_run/baseline_lateがそのまま入る", rec_before["baseline_late"], False)
    check("first_run/market_open_reportedがそのまま入る", rec_before["market_open_reported"], True)
    check("first_run/source_policy_overwrittenがそのまま入る", rec_before["source_policy_overwritten"], 0)


def test_first_run_created_on_first_verification():
    """修正2の正例: first_runが無い号を初めて照合するとfirst_runが作られる。
    修正1の負例もあわせて確かめる: generated_atが壊れていても号は保存され、
    first_run内でgenerated_at_parsed=false・generated_at_drift_minutes=nullになる。"""
    with tempfile.TemporaryDirectory() as d:
        dst = _copy_testdata_to(d)
        edition_path = dst / "editions" / "2026-09-24" / "morning.json"
        hyp_path = dst / "hypotheses" / "2026-09-24-morning.json"
        cache_dir = dst / "cache"

        today = _expected_edition_date("evening", dt.datetime.now(ve.JST))
        edition = json.loads(edition_path.read_text(encoding="utf-8"))
        edition.pop("verification", None)
        edition["date"] = today
        edition["slot"] = "evening"  # 門限(検査20)の対象外にして、baseline_lateを気にせず済むようにする
        edition["generated_at"] = "壊れた日時"
        edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

        result = _run_verify(d, edition_path, hyp_path, cache_dir)
        check("first_run/正例: 初回照合は正常終了する(終了コード0)", result.returncode, 0)

        after = json.loads(edition_path.read_text(encoding="utf-8"))
        first_run = after.get("verification", {}).get("first_run")
        check("first_run/正例: 初回照合でfirst_runが作られる", first_run is not None, True)
        if first_run:
            check(
                "first_run/負例: generated_atが読めない場合generated_at_parsedはFalse",
                first_run.get("generated_at_parsed"), False,
            )
            check(
                "first_run/負例: generated_atが読めない場合generated_at_drift_minutesはNone",
                first_run.get("generated_at_drift_minutes"), None,
            )

    _assert_testdata_untouched("first_run/初回照合テスト")


def test_first_run_unchanged_on_second_run():
    """修正2の正例: 同じ号をもう一度照合しても、first_runの中身が1文字も変わらない。"""
    with tempfile.TemporaryDirectory() as d:
        dst = _copy_testdata_to(d)
        edition_path = dst / "editions" / "2026-09-24" / "morning.json"
        hyp_path = dst / "hypotheses" / "2026-09-24-morning.json"
        cache_dir = dst / "cache"

        today = _expected_edition_date("evening", dt.datetime.now(ve.JST))
        edition = json.loads(edition_path.read_text(encoding="utf-8"))
        edition.pop("verification", None)
        edition["date"] = today
        edition["slot"] = "evening"  # 門限(検査20)の対象外にして、baseline_lateを気にせず済むようにする
        edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

        r1 = _run_verify(d, edition_path, hyp_path, cache_dir)
        check("first_run/正例: 1回目の照合は正常終了する", r1.returncode, 0)
        first_run_1 = json.loads(edition_path.read_text(encoding="utf-8"))["verification"]["first_run"]

        r2 = _run_verify(d, edition_path, hyp_path, cache_dir)
        check("first_run/正例: 2回目の照合も正常終了する", r2.returncode, 0)
        first_run_2 = json.loads(edition_path.read_text(encoding="utf-8"))["verification"]["first_run"]

        check("first_run/正例: 2回続けて照合してもfirst_runの中身が1文字も変わらない", first_run_2, first_run_1)

    _assert_testdata_untouched("first_run/2回照合テスト")


def test_should_abort_rerun():
    """修正C(F.4): should_abort_rerun()の単体テスト。C.1の表の7通りをそのまま
    テストにする。実行時刻には一切依存しない(この環境には時刻を差し替える手段が
    無く、あってはならないため。紙面を作るAIが同じコマンドを実行できてしまう)。"""
    check(
        "should_abort_rerun/1: verificationが無い(初回)+今回真 → 偽(中止しない)",
        ve.should_abort_rerun(None, True), False,
    )
    check(
        "should_abort_rerun/2: 既存baseline_late=false+今回真 → 真(中止する)",
        ve.should_abort_rerun({"baseline_late": False}, True), True,
    )
    check(
        "should_abort_rerun/3: 既存baseline_late=false+今回偽 → 偽",
        ve.should_abort_rerun({"baseline_late": False}, False), False,
    )
    check(
        "should_abort_rerun/4: 既存baseline_late=true+今回真 → 偽",
        ve.should_abort_rerun({"baseline_late": True}, True), False,
    )
    check(
        "should_abort_rerun/5: 既存baseline_late=true+今回偽 → 偽",
        ve.should_abort_rerun({"baseline_late": True}, False), False,
    )
    check(
        "should_abort_rerun/6: 既存にbaseline_lateキーが無い+今回真 → 偽",
        ve.should_abort_rerun({}, True), False,
    )
    check(
        "should_abort_rerun/7: 既存のbaseline_lateがNone+今回真 → 偽",
        ve.should_abort_rerun({"baseline_late": None}, True), False,
    )


def test_number_coverage():
    """修正4: compute_number_coverage()の単体テスト。判定には使わない記録専用。"""

    # --- 正例10: 数字が無い行ではtext_number_tokensが0 ---
    edition_no_digits = {
        "sections": [{
            "section_id": "s", "articles": [{
                "article_id": "A", "lines": [
                    {"line_id": "L1", "text": "本日は特に変化がなかった。", "numbers": [], "mark": "unverified"},
                ],
            }],
        }],
    }
    coverage_no_digits = ve.compute_number_coverage(edition_no_digits)
    check("number_coverage/正例10: 数字が無い行ではtext_number_tokensが0", coverage_no_digits["text_number_tokens"], 0)
    check("number_coverage/正例10: numbers_declaredも0", coverage_no_digits["numbers_declared"], 0)
    check("number_coverage/正例10: gapも0", coverage_no_digits["gap"], 0)

    # --- 正例11: 全角数字「２０２６」が正規化後に数えられる ---
    edition_fullwidth = {
        "sections": [{
            "section_id": "s", "articles": [{
                "article_id": "A", "lines": [
                    {"line_id": "L2", "text": "２０２６年の予測です。", "numbers": [2026], "mark": "source_number_match"},
                ],
            }],
        }],
    }
    coverage_fullwidth = ve.compute_number_coverage(edition_fullwidth)
    check("number_coverage/正例11: 全角数字'２０２６'は正規化後に1個として数えられる", coverage_fullwidth["text_number_tokens"], 1)
    check("number_coverage/正例11: numbers_declaredは1(numbersが1件)", coverage_fullwidth["numbers_declared"], 1)
    check("number_coverage/正例11: 一致していればgapは0", coverage_fullwidth["gap"], 0)
    check(
        "number_coverage/正例11: by_markの'source_number_match'に内訳が入る",
        coverage_fullwidth["by_mark"]["source_number_match"],
        {"text_number_tokens": 1, "numbers_declared": 1},
    )

    # --- 反応してほしくない例: 「1,901」のようなカンマ区切りの数字は
    # normalize_text()でカンマが消えるため1個として数える(2個にならない) ---
    edition_comma = {
        "sections": [{
            "section_id": "s", "articles": [{
                "article_id": "A", "lines": [
                    {"line_id": "L3", "text": "売上高は1,901億円だった。", "numbers": [1901], "mark": "source_number_match"},
                ],
            }],
        }],
    }
    coverage_comma = ve.compute_number_coverage(edition_comma)
    check(
        "number_coverage/反応してほしくない例: カンマ区切り'1,901'は1個として数える(2個にならない)",
        coverage_comma["text_number_tokens"], 1,
    )

    # --- 反応してほしくない例: 日付「2026年9月18日」は3個として数える ---
    edition_date = {
        "sections": [{
            "section_id": "s", "articles": [{
                "article_id": "A", "lines": [
                    {"line_id": "L4", "text": "2026年9月18日に発表された。", "numbers": [], "mark": "explainer"},
                ],
            }],
        }],
    }
    coverage_date = ve.compute_number_coverage(edition_date)
    check(
        "number_coverage/反応してほしくない例: 日付'2026年9月18日'は3個として数える(判定には使わない)",
        coverage_date["text_number_tokens"], 3,
    )


def test_sources_published_at_null():
    """修正5: count_sources_published_at_null()の単体テスト。
    change枠の行が落ちた件数を数える既存のunknown_published_at_hitsとは別集計で、
    そちらの値は変えないことも確かめる。"""

    # --- 正例12: published_atがnullの出典が正しく数えられる(null/空文字/missing) ---
    edition = {
        "sections": [{
            "section_id": "change", "articles": [{
                "article_id": "A", "lines": [
                    {"line_id": "L1", "text": "本文1。", "numbers": [], "source_ref": "S1", "claimed_mark": "unverified"},
                ],
            }],
        }],
        "sources": [
            {"source_id": "S1", "published_at": None},
            {"source_id": "S2", "published_at": ""},
            {"source_id": "S3"},
            {"source_id": "S4", "published_at": "2026-09-19T10:00:00+09:00"},
        ],
    }
    result = ve.count_sources_published_at_null(edition)
    check("sources_published_at_null/正例12: null/空文字/キー無しの3件が数えられる", result["count"], 3)
    check(
        "sources_published_at_null/正例12: source_idsに該当する3件が入る(値が入っているS4は入らない)",
        sorted(result["source_ids"]), ["S1", "S2", "S3"],
    )

    # --- unknown_published_at_hits(change枠の行が落ちた件数)は既存どおり ---
    edition_for_stale = json.loads(json.dumps(edition))  # run_check_e_stale_sourcesは行を削除するため複製を使う
    now = dt.datetime.now(ve.JST)
    stale_hits, unknown_published_at_hits, _ = ve.run_check_e_stale_sources(edition_for_stale, now)
    check(
        "sources_published_at_null/正例12: unknown_published_at_hitsは変わらない"
        "(change枠でpublished_atが無い出典を参照する行1件のみ)",
        unknown_published_at_hits, 1,
    )
    check(
        "sources_published_at_null/正例12: sources_published_at_null(3件)と"
        "unknown_published_at_hits(1件)は別集計になる(S2・S3はどの行からも参照されていないため)",
        result["count"] != unknown_published_at_hits, True,
    )


def test_rerun_detected():
    """修正8: compute_rerun_detected()の単体テスト。実行時刻には一切依存しない
    (号にverificationがあるかどうかだけで決まる)。判定には使わない。"""
    check(
        "rerun_detected/正例13: verificationが無い号(初回)ではFalse",
        ve.compute_rerun_detected(None), False,
    )
    check(
        "rerun_detected/正例13: verificationが空辞書でも(キー自体はある)Trueになる",
        ve.compute_rerun_detected({}), True,
    )
    check(
        "rerun_detected/正例13: verificationに中身がある号(2回目以降)ではTrue",
        ve.compute_rerun_detected({"script_version": "2.0.0", "run_at": "2026-09-19T08:00:00+09:00"}), True,
    )


def test_skip_companies_when_market_closed():
    """修正3の正例: market_openがfalseの号は、下段(industry_examples)も作らず、
    industry_picks_discardedに件数が記録される。
    修正Aにより検査24が常に実行時刻の日付を基準にするため、dateは今日(動的)にする
    必要がある。市場が休みかどうかは実際の暦とは無関係にしたいので、この年の
    business_daysが空の専用カレンダーを一時フォルダに作って渡す(市場は必ず休み
    という設定にする)。"""
    with tempfile.TemporaryDirectory() as d:
        dst = _copy_testdata_to(d)
        edition_path = dst / "editions" / "2026-09-24" / "morning.json"
        hyp_path = dst / "hypotheses" / "2026-09-24-morning.json"
        cache_dir = dst / "cache"

        now = dt.datetime.now(ve.JST)
        today = _expected_edition_date("evening", now)
        edition = json.loads(edition_path.read_text(encoding="utf-8"))
        edition.pop("verification", None)
        edition["date"] = today
        edition["slot"] = "evening"
        edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

        hyp = json.loads(hyp_path.read_text(encoding="utf-8"))
        hyp["industry_picks"] = [
            {"article_id": "ART-TEST-001", "event_id": "EVT-A", "industry": "テスト休場業種A", "industry_line_ids": ["L-12"]},
            {"article_id": "ART-TEST-001", "event_id": "EVT-B", "industry": "テスト休場業種B", "industry_line_ids": ["L-13"]},
        ]
        hyp_path.write_text(json.dumps(hyp, ensure_ascii=False, indent=1), encoding="utf-8")

        # この年は1日も営業日が無い、という専用カレンダー(市場は必ず休みになる)。
        calendar_dir = Path(d) / "calendar"
        calendar_dir.mkdir(parents=True, exist_ok=True)
        year = today[:4]
        (calendar_dir / f"{year}.json").write_text(
            json.dumps({"schema_version": 1, "year": int(year), "business_days_count": 0, "business_days": []}),
            encoding="utf-8",
        )

        result = _run_verify(d, edition_path, hyp_path, cache_dir, calendar_dir)
        check("検査14/正例: 休場日の号は正常終了する", result.returncode, 0)

        edition_after = json.loads(edition_path.read_text(encoding="utf-8"))
        check("検査14/正例: 休場日はmarket_openがfalseになる", edition_after.get("market_open"), False)
        check(
            "検査14/正例: 休場日はindustry_picks_discardedが2になる",
            edition_after["verification"].get("industry_picks_discarded"), 2,
        )

        hyp_after = json.loads(hyp_path.read_text(encoding="utf-8"))
        check("検査14/正例: 休場日はindustry_examplesが0件になる", len(hyp_after.get("industry_examples") or []), 0)
        check("検査14/正例: 休場日はhypotheses(上段)も0件になる", len(hyp_after.get("hypotheses") or []), 0)
        check(
            "検査14/正例: industry_picksの中身自体は消さない(記録として残す)",
            len(hyp_after.get("industry_picks") or []), 2,
        )

    _assert_testdata_untouched("検査14/休場日テスト")


def test_industry_examples_not_skipped_when_market_open_and_not_late():
    """修正3の負例(5件以上): 営業日・遅延なしの号では、業種の数や記事の数を変えても
    これまでどおり下段(industry_examples)が作られ、industry_picks_discardedは
    0のまま(修正E)であることを確かめる。
    修正Aにより検査24が常に実行時刻の日付を基準にするため、dateは今日(動的)にし、
    slotはevening(門限の対象外)にする。今日が営業日として扱われるよう、専用の
    一時カレンダー(_write_temp_calendar)を使う。"""
    variants = [
        (
            "1業種1社(1記事)",
            [{"article_id": "ART-TEST-001", "event_id": "EVT-N1", "industry": "テスト非休場業種1", "industry_line_ids": ["L-12"]}],
            [("E-N1-1", "テスト非休場一号株式会社", "テスト非休場業種1", "上場", "1000", "91010")],
            None,
        ),
        (
            "2業種2社(同じ記事)",
            [
                {"article_id": "ART-TEST-001", "event_id": "EVT-N2", "industry": "テスト非休場業種2", "industry_line_ids": ["L-12"]},
                {"article_id": "ART-TEST-001", "event_id": "EVT-N3", "industry": "テスト非休場業種3", "industry_line_ids": ["L-13"]},
            ],
            [
                ("E-N2-1", "テスト非休場二号株式会社", "テスト非休場業種2", "上場", "1000", "91020"),
                ("E-N2-2", "テスト非休場三号株式会社", "テスト非休場業種3", "上場", "1000", "91030"),
            ],
            None,
        ),
        (
            "1業種1社(2本目の記事を追加)",
            [{"article_id": "ART-TEST-EXTRA", "event_id": "EVT-N4", "industry": "テスト非休場業種4", "industry_line_ids": ["L-EXTRA-1"]}],
            [("E-N3-1", "テスト非休場四号株式会社", "テスト非休場業種4", "上場", "1000", "91040")],
            {
                "article_id": "ART-TEST-EXTRA",
                "lines": [{
                    "line_id": "L-EXTRA-1", "claimed_mark": "reported_unverified", "numbers": [],
                    "text": "テスト非休場四号株式会社の業績に関する記述。",
                }],
            },
        ),
    ]

    for label, industry_picks, codelist_rows, extra_article in variants:
        with tempfile.TemporaryDirectory() as d:
            dst = _copy_testdata_to(d)
            edition_path = dst / "editions" / "2026-09-24" / "morning.json"
            hyp_path = dst / "hypotheses" / "2026-09-24-morning.json"
            cache_dir = dst / "cache"

            today = _expected_edition_date("evening", dt.datetime.now(ve.JST))
            edition = json.loads(edition_path.read_text(encoding="utf-8"))
            edition.pop("verification", None)
            edition["date"] = today
            edition["slot"] = "evening"
            if extra_article:
                edition["sections"][0]["articles"].append(extra_article)
            edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

            hyp = json.loads(hyp_path.read_text(encoding="utf-8"))
            hyp["industry_picks"] = industry_picks
            hyp_path.write_text(json.dumps(hyp, ensure_ascii=False, indent=1), encoding="utf-8")

            _write_fake_codelist(d, codelist_rows)
            calendar_dir = _write_temp_calendar(d, dt.datetime.strptime(today, "%Y-%m-%d").date(), 5)

            result = _run_verify(d, edition_path, hyp_path, cache_dir, calendar_dir)
            check(f"検査14/負例({label}): 正常終了する", result.returncode, 0)

            edition_after = json.loads(edition_path.read_text(encoding="utf-8"))
            check(
                f"検査14/負例({label}): industry_picks_discardedは0のまま(修正E、スキップされていない)",
                edition_after.get("verification", {}).get("industry_picks_discarded"), 0,
            )

            hyp_after = json.loads(hyp_path.read_text(encoding="utf-8"))
            check(
                f"検査14/負例({label}): industry_examplesが作られる(0件ではない)",
                len(hyp_after.get("industry_examples") or []) > 0, True,
            )

    _assert_testdata_untouched("検査14/負例テスト")


def test_check_edition_date():
    """検査24(号の日付の整合)の正例(落ちるべき)・負例(通るべき)。"""
    def edition(slot, date_str):
        return {"slot": slot, "date": date_str}

    def passes(slot, date_str, run_at_iso):
        try:
            ve.check_edition_date(edition(slot, date_str), dt.datetime.fromisoformat(run_at_iso))
            return True
        except ve.EditionInvalid:
            return False

    # --- 落ちるべき ---
    check(
        "検査24/正例: 0:12実行の夕方号でdateが当日(2026-09-21)だと号を保存しない",
        passes("evening", "2026-09-21", "2026-09-21T00:12:00+09:00"), False,
    )

    # --- 通るべき ---
    check(
        "検査24/正例: 0:12実行の夕方号はdateが前日(2026-09-20)なら通る",
        passes("evening", "2026-09-20", "2026-09-21T00:12:00+09:00"), True,
    )
    check(
        "検査24/通るべき1: 夕方号を17:30に実行して当日の日付なら通る",
        passes("evening", "2026-09-20", "2026-09-20T17:30:00+09:00"), True,
    )
    check(
        "検査24/通るべき2: 朝号を07:35に実行して当日の日付なら通る",
        passes("morning", "2026-09-20", "2026-09-20T07:35:00+09:00"), True,
    )
    check(
        "検査24/通るべき3: 昼号を13:10に実行して当日の日付なら通る",
        passes("noon", "2026-09-20", "2026-09-20T13:10:00+09:00"), True,
    )
    check(
        "検査24/通るべき4: 夕方号を04:59に実行して前日の日付なら通る",
        passes("evening", "2026-09-19", "2026-09-20T04:59:00+09:00"), True,
    )
    check(
        "検査24/通るべき5: 夕方号を05:00に実行して当日の日付なら通る",
        passes("evening", "2026-09-20", "2026-09-20T05:00:00+09:00"), True,
    )


def test_edition_date_check_always_runs_even_with_first_run():
    """検査24(F.3、修正Aにより仕様が反転): first_runが既にある号でも検査24は
    飛ばされない。日付が実行時刻と食い違っていれば、first_runの有無に関係なく
    保存できない(終了コード1)。実行時刻に依存せず、常に成り立つ(今日以外の
    日付を書けば必ず落ちるため)。"""
    with tempfile.TemporaryDirectory() as d:
        dst = _copy_testdata_to(d)
        edition_path = dst / "editions" / "2026-09-24" / "morning.json"
        hyp_path = dst / "hypotheses" / "2026-09-24-morning.json"
        cache_dir = dst / "cache"

        edition = json.loads(edition_path.read_text(encoding="utf-8"))
        edition["date"] = "2026-01-01"  # 実行時刻とは明らかに食い違う日付
        edition["verification"] = {"first_run": {
            "run_at": "2026-09-24T08:00:00+09:00", "baseline_late": False,
            "market_open_reported": True, "source_policy_overwritten": 0,
            "generated_at_reported": None, "generated_at_parsed": False,
            "generated_at_drift_minutes": None,
        }}
        edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

        # 改修27-2第2回: 実行時刻を固定(2026-09-24の朝号の時間帯。号のslotはmorningなので
        # 時間帯は合っていて、日付だけが食い違う)。
        result = _run_verify(d, edition_path, hyp_path, cache_dir,
                             run_at_dt=dt.datetime.fromisoformat("2026-09-24T07:30:00+09:00"))
        check(
            "検査24/F.3: first_runが既にあっても日付が食い違えば保存できない(終了コード1)",
            result.returncode, 1,
        )

    _assert_testdata_untouched("検査24/F.3テスト")


def test_edition_date_check_cannot_be_bypassed_by_forged_first_run():
    """検査24の捏造対策テスト(F.5): 紙面を作るAIがverification.first_run.baseline_late
    にfalseを書き込み、かつdateを実行時刻と食い違わせても、検査24は飛ばされず
    号は保存されない。号のファイルも一切書き換わらないことを確かめる。"""
    with tempfile.TemporaryDirectory() as d:
        dst = _copy_testdata_to(d)
        edition_path = dst / "editions" / "2026-09-24" / "morning.json"
        hyp_path = dst / "hypotheses" / "2026-09-24-morning.json"
        cache_dir = dst / "cache"

        edition = json.loads(edition_path.read_text(encoding="utf-8"))
        edition["date"] = "2026-01-01"  # 実行時刻とは明らかに食い違う日付(AIによる捏造)
        edition["verification"] = {"first_run": {
            "run_at": "2026-01-01T08:00:00+09:00", "baseline_late": False,
            "market_open_reported": True, "source_policy_overwritten": 0,
            "generated_at_reported": None, "generated_at_parsed": False,
            "generated_at_drift_minutes": None,
        }}
        before_text = json.dumps(edition, ensure_ascii=False, indent=1)
        edition_path.write_text(before_text, encoding="utf-8")

        # 改修27-2第2回: 実行時刻を固定(時間帯は合っていて、日付だけが食い違う)。
        result = _run_verify(d, edition_path, hyp_path, cache_dir,
                             run_at_dt=dt.datetime.fromisoformat("2026-09-24T07:30:00+09:00"))
        check(
            "検査24/捏造対策: first_run.baseline_late=falseを書いても保存できない(終了コード1)",
            result.returncode, 1,
        )

        after_text = edition_path.read_text(encoding="utf-8")
        check(
            "検査24/捏造対策: 号のファイルは一切書き換わっていない",
            after_text, before_text,
        )

    _assert_testdata_untouched("検査24/捏造対策テスト")


def test_evidence_downgrade_target_is_reported():
    """修正5: 検査11に不合格の仮説は、evidence_gradeがinferredではなくreportedになる。"""
    hyps = [{"evidence_grade": "primary", "evidence_source_ref": None, "company_name": "テスト物産"}]
    downgraded, unreadable = ve.run_check_hypothesis_evidence(hyps, {}, ".")
    check("検査11/修正5: 不合格の仮説はevidence_gradeがreportedになる", hyps[0]["evidence_grade"], "reported")
    check("検査11/修正5: primary_evidence_unverifiedの件数は1", downgraded, 1)
    check("検査11/修正5: evidence_source_unreadableの件数は0", unreadable, 0)


def test_check_hypothesis_baseline_late_input():
    """検査17(修正6・改修27-1の4-5で検算に変更): baseline_late_inputが真の仮説は、
    baseline_dateではなくbaseline_observed_atの日付(営業日でなければ後の最初の営業日)
    から期限日を数え直す。改修27-1により、期限日がずれていても会社は消えなくなった
    (horizon_recount_mismatchに記録するだけ)。deadline_dateがnull(=apply_observation_
    window()が計算できなかった場合)だけ、今までどおり削除される。"""
    business_days = ve.load_business_days(str(CALENDAR_DIR))
    sources = {}
    line_ids = {"L-1": "verified"}
    ng_words = []

    def base(**kw):
        h = {
            "company_name": "テスト物産", "relation_text": "業績に影響しうる",
            "baseline_price_type": "close",
            "direction": "plus", "evidence_grade": "reported",
            "ticker": "8801", "ticker_source": "edinet_codelist", "line_ids": ["L-1"],
            # added_by="manual": このテストは検査17(期限日の計算)だけを確かめる対象で、
            # edition({})にslotが無いため、検査32(baseline_price_type/dateの機械的な確認、
            # added_byがmanualでない仮説だけが対象)を意図せず発動させないため。
            "added_by": "manual",
        }
        h.update(kw)
        return h

    def result_for(hyp):
        extra_counts = {"codelist_unavailable": True, "baseline_date_check_skipped": 0}
        reason = ve.check_hypothesis(hyp, {}, line_ids, business_days, ng_words, sources, ".", None, None, extra_counts)
        return reason, extra_counts.get("horizon_recount_mismatch", 0)

    # --- 正例1: baseline_observed_atが営業日そのもの ---
    expected1 = ve.compute_deadline(business_days, "2026-09-25", 5)
    hyp1 = base(
        baseline_late_input=True, baseline_observed_at="2026-09-25T10:00:00+09:00",
        baseline_date="2026-09-24", horizon_business_days=5, deadline_date=expected1,
    )
    reason1, mismatch1 = result_for(hyp1)
    check(
        "検査17/正例1: baseline_late_inputが真ならbaseline_observed_atの日付から数えた期限日が合格する",
        reason1, None,
    )
    check("検査17/正例1: 期限日が合っていればhorizon_recount_mismatchは増えない", mismatch1, 0)

    # --- 正例2: baseline_observed_atが非営業日(土曜) → 後の最初の営業日(2026-09-28)から数える ---
    expected2 = ve.compute_deadline(business_days, "2026-09-28", 3)
    hyp2 = base(
        baseline_late_input=True, baseline_observed_at="2026-09-26T09:00:00+09:00",
        baseline_date="2026-09-24", horizon_business_days=3, deadline_date=expected2,
    )
    reason2, mismatch2 = result_for(hyp2)
    check(
        "検査17/正例2: baseline_observed_atが非営業日なら後の最初の営業日から数え直す",
        reason2, None,
    )
    check("検査17/正例2: 期限日が合っていればhorizon_recount_mismatchは増えない", mismatch2, 0)

    # --- 負例(反応してほしい): baseline_observed_atがnullで期限日を計算できなかった
    #     場合(apply_observation_window()がdeadline_dateをnullにする場合と同じ状態)
    #     → 削除。これは検算のずれではなく「期限日が無い」ための削除(今までどおり)。 ---
    hyp3 = base(
        baseline_late_input=True, baseline_observed_at=None,
        baseline_date="2026-09-24", horizon_business_days=5, deadline_date=None,
    )
    reason3, _mismatch3 = result_for(hyp3)
    check(
        "検査17/負例: baseline_observed_atがnullでdeadline_dateがnullならdeadline_date_mismatchで削除される",
        reason3, "deadline_date_mismatch",
    )

    # --- 負例(反応してほしくない例。5件以上): baseline_late_inputが偽・無い・nullは従来どおり ---
    expected_normal = ve.compute_deadline(business_days, "2026-09-24", 5)
    hyp4 = base(baseline_late_input=False, baseline_date="2026-09-24", horizon_business_days=5, deadline_date=expected_normal)
    reason4, mismatch4 = result_for(hyp4)
    check(
        "検査17/反応してほしくない例1: baseline_late_inputが偽ならbaseline_dateから従来どおり計算される",
        reason4, None,
    )
    check("検査17/反応してほしくない例1: 期限日が合っていればhorizon_recount_mismatchは増えない", mismatch4, 0)

    hyp5 = base(baseline_date="2026-09-24", horizon_business_days=5, deadline_date=expected_normal)
    check(
        "検査17/反応してほしくない例2: baseline_late_inputキーが無くても従来どおり計算される",
        result_for(hyp5)[0], None,
    )

    hyp6 = base(baseline_late_input=None, baseline_date="2026-09-24", horizon_business_days=5, deadline_date=expected_normal)
    check(
        "検査17/反応してほしくない例3: baseline_late_inputがnullでも従来どおり計算される",
        result_for(hyp6)[0], None,
    )

    # --- 改修27-1(4-5)の中心の確認: 期限日がずれていても会社は消えず、記録だけされる ---
    hyp7 = base(baseline_late_input=False, baseline_date="2026-09-24", horizon_business_days=5, deadline_date="2099-01-01")
    reason7, mismatch7 = result_for(hyp7)
    check(
        "改修27-1/検査17: 期限日がずれていても会社は消えない(検算に変わったため)",
        reason7, None,
    )
    check(
        "改修27-1/検査17: 期限日がずれていればhorizon_recount_mismatchが1増える",
        mismatch7, 1,
    )

    expected_alt_horizon = ve.compute_deadline(business_days, "2026-09-25", 3)
    hyp8 = base(
        baseline_late_input=True, baseline_observed_at="2026-09-25T09:00:00+09:00",
        baseline_date="2026-09-24", horizon_business_days=3, deadline_date=expected_alt_horizon,
    )
    reason8, mismatch8 = result_for(hyp8)
    check(
        "検査17/反応してほしくない例5: horizonを変えても営業日起算の仕組み自体は壊れていない",
        reason8, None,
    )
    check("検査17/反応してほしくない例5: 期限日が合っていればhorizon_recount_mismatchは増えない", mismatch8, 0)

    # --- 負例(反応してほしい): horizon_business_daysが整数でなければ従来どおり削除される ---
    hyp9 = base(baseline_date="2026-09-24", horizon_business_days=None, deadline_date=expected_normal)
    check(
        "検査17/負例: horizon_business_daysが無い(整数でない)ならdeadline_date_mismatchで削除される",
        result_for(hyp9)[0], "deadline_date_mismatch",
    )

    # --- 負例(反応してほしい): deadline_dateがnull(計算できなかった)なら削除される ---
    hyp10 = base(baseline_date="2026-09-24", horizon_business_days=5, deadline_date=None)
    check(
        "検査17/負例: deadline_dateがnull(計算できなかった)ならdeadline_date_mismatchで削除される",
        result_for(hyp10)[0], "deadline_date_mismatch",
    )


def test_observation_window_horizon():
    """改修27-1(4-5): impact_kindから観察の営業日数を機械で決める。"""
    check("observation_window_horizon/正例: price_statedは5営業日", ve.observation_window_horizon("price_stated"), 5)
    check("observation_window_horizon/負例: amount_statedは20営業日", ve.observation_window_horizon("amount_stated"), 20)
    check("observation_window_horizon/負例: fact_onlyは20営業日", ve.observation_window_horizon("fact_only"), 20)
    check("observation_window_horizon/負例: nullは20営業日", ve.observation_window_horizon(None), 20)


def test_compute_deadline_base_date():
    """改修27-1(4-5): 期限日の起算日を決める規則(baseline_late_inputの例外を含む)。"""
    business_days = ve.load_business_days(str(CALENDAR_DIR))

    base_normal, reason_normal = ve.compute_deadline_base_date({"baseline_date": "2026-09-24"}, business_days)
    check("compute_deadline_base_date/正例: baseline_late_inputが無ければbaseline_dateがそのまま起算日", base_normal, "2026-09-24")
    check("compute_deadline_base_date/正例: 理由はNone", reason_normal, None)

    base_observed, _r1 = ve.compute_deadline_base_date(
        {"baseline_late_input": True, "baseline_observed_at": "2026-09-25T10:00:00+09:00", "baseline_date": "2026-09-24"},
        business_days,
    )
    check(
        "compute_deadline_base_date/正例: baseline_late_inputが真なら営業日のbaseline_observed_atがそのまま起算日",
        base_observed, "2026-09-25",
    )

    base_weekend, _r2 = ve.compute_deadline_base_date(
        {"baseline_late_input": True, "baseline_observed_at": "2026-09-26T09:00:00+09:00", "baseline_date": "2026-09-24"},
        business_days,
    )
    check(
        "compute_deadline_base_date/正例: baseline_observed_atが非営業日(土曜)なら後の最初の営業日",
        base_weekend, "2026-09-28",
    )

    base_none, reason_none = ve.compute_deadline_base_date(
        {"baseline_late_input": True, "baseline_observed_at": None, "baseline_date": "2026-09-24"}, business_days,
    )
    check("compute_deadline_base_date/負例: baseline_observed_atが読めなければ起算日はNone", base_none, None)
    check("compute_deadline_base_date/負例: 理由はbaseline_observed_at_unparseable", reason_none, "baseline_observed_at_unparseable")

    base_missing, reason_missing = ve.compute_deadline_base_date({"baseline_date": None}, business_days)
    check("compute_deadline_base_date/負例: baseline_dateが無ければ起算日はNone", base_missing, None)
    check("compute_deadline_base_date/負例: 理由はbaseline_date_missing", reason_missing, "baseline_date_missing")


def test_recount_deadline_by_stepping():
    """改修27-1(4-5): 検査17の検算(compute_deadline()とは別の書き方)が、同じ入力なら
    compute_deadline()と同じ答えを返すこと、境界で正しくNoneを返すことを確かめる。"""
    business_days = ve.load_business_days(str(CALENDAR_DIR))

    for base_date, horizon in (("2026-09-24", 5), ("2026-09-24", 20), ("2026-09-25", 1)):
        expected = ve.compute_deadline(business_days, base_date, horizon)
        actual = ve.recount_deadline_by_stepping(business_days, base_date, horizon)
        check(
            f"recount_deadline_by_stepping/正例: compute_deadline()と同じ答え(base={base_date}, horizon={horizon})",
            actual, expected,
        )

    check(
        "recount_deadline_by_stepping/負例: base_dateがbusiness_daysに無ければNone(土曜)",
        ve.recount_deadline_by_stepping(business_days, "2026-09-26", 5), None,
    )
    check(
        "recount_deadline_by_stepping/負例: 営業日が足りなければNone(カレンダーの末尾を超える)",
        ve.recount_deadline_by_stepping(business_days, business_days[-1], 1), None,
    )


def test_apply_observation_window():
    """改修27-1(4-5): horizon_business_days・deadline_dateを機械で必ず上書きすること、
    AIが書いた値の一致・不一致にかかわらず上書きすること、件数の記録を確かめる。"""
    business_days = ve.load_business_days(str(CALENDAR_DIR))

    price_hyp = {
        "impact_kind": "price_stated", "baseline_date": "2026-09-24",
        "horizon_business_days": 999, "deadline_date": "AIが書いた値",
    }
    other_hyp = {"impact_kind": "fact_only", "baseline_date": "2026-09-24"}
    unresolvable_hyp = {"impact_kind": None, "baseline_date": None}

    hyps = [price_hyp, other_hyp, unresolvable_hyp]
    counts = ve.apply_observation_window(hyps, business_days)

    check("apply_observation_window/正例: price_statedは5営業日で上書きされる", price_hyp["horizon_business_days"], 5)
    check(
        "apply_observation_window/正例: price_statedのdeadline_dateはcompute_deadline()と一致",
        price_hyp["deadline_date"], ve.compute_deadline(business_days, "2026-09-24", 5),
    )
    check("apply_observation_window/正例: fact_onlyは20営業日になる", other_hyp["horizon_business_days"], 20)
    check(
        "apply_observation_window/正例: horizon_business_daysが未記入でも機械が埋める",
        other_hyp["deadline_date"], ve.compute_deadline(business_days, "2026-09-24", 20),
    )
    check(
        "apply_observation_window/負例: 起算日が無ければdeadline_dateはnullになる",
        unresolvable_hyp["deadline_date"], None,
    )
    check(
        "apply_observation_window/負例: 起算日が無くてもhorizon_business_daysは20が入る(nullのままにはしない)",
        unresolvable_hyp["horizon_business_days"], 20,
    )

    check("apply_observation_window/件数: horizon_overridden(3件とも値が変わった)", counts["horizon_overridden"], 3)
    check("apply_observation_window/件数: deadline_uncomputable(起算日が無い1件)", counts["deadline_uncomputable"], 1)

    # 既に正しい値が入っている場合は上書きされても件数は増えない(値が変わらないため)。
    already_correct = {
        "impact_kind": "price_stated", "baseline_date": "2026-09-24",
        "horizon_business_days": 5, "deadline_date": ve.compute_deadline(business_days, "2026-09-24", 5),
    }
    counts2 = ve.apply_observation_window([already_correct], business_days)
    check("apply_observation_window/負例: 既に正しい値ならhorizon_overriddenは増えない", counts2["horizon_overridden"], 0)


def test_required_hypothesis_fields_no_falsifier_or_horizon():
    """改修27-1(決定1・4-5): 上段の必須項目からfalsifier・horizon_business_daysを外した。
    紙面を書くAIがこの2つを書かなくても(キー自体が無くても)、そのために会社が
    消えることはない(REQUIRED_HYPOTHESIS_FIELDSのループでmissing_fieldにならない)。"""
    check(
        "REQUIRED_HYPOTHESIS_FIELDS/正例: falsifierが必須項目に含まれていない",
        "falsifier" in ve.REQUIRED_HYPOTHESIS_FIELDS, False,
    )
    check(
        "REQUIRED_HYPOTHESIS_FIELDS/正例: horizon_business_daysが必須項目に含まれていない",
        "horizon_business_days" in ve.REQUIRED_HYPOTHESIS_FIELDS, False,
    )

    business_days = ve.load_business_days(str(CALENDAR_DIR))
    sources = {}
    line_ids = {"L-1": "verified"}
    ng_words = []

    hyp = {
        "company_name": "テスト物産", "relation_text": "業績に影響しうる",
        "baseline_price_type": "close", "baseline_date": "2026-09-24",
        "evidence_grade": "reported", "ticker": "8801", "ticker_source": "edinet_codelist",
        "line_ids": ["L-1"], "added_by": "manual",
        # apply_observation_window()が埋めた後の状態を模して、horizon_business_days・
        # deadline_dateは機械の値を入れておく(check_hypothesis()単体では
        # apply_observation_window()を呼ばないため)。falsifier・direction・
        # evidence_excerptはキーごと書かない(第7.1版9.4の(2))。
        "horizon_business_days": 20,
        "deadline_date": ve.compute_deadline(business_days, "2026-09-24", 20),
    }
    extra_counts = {"codelist_unavailable": True, "baseline_date_check_skipped": 0}
    reason = ve.check_hypothesis(hyp, {}, line_ids, business_days, ng_words, sources, ".", None, None, extra_counts)
    check(
        "改修27-1/正例: falsifier・direction・evidence_excerptを書かなくても会社は消えない",
        reason, None,
    )


def test_inference_falsifier_still_required():
    """改修27-1(決定1)の確認: 上段の仮説のfalsifierは必須項目から外れたが、記事の
    推論欄(inferences)のfalsifierはrun_check_d_inferences()で今までどおり必須のまま。
    上段と推論欄のfalsifierを取り違えていないことを確かめる。"""
    edition = {
        "sections": [{
            "section_id": "big",
            "articles": [{
                "article_id": "A-1",
                "lines": [],
                "inferences": [
                    {
                        "text": "説明文", "falsifier": "反証条件",
                        "check_metric": "指標", "check_by": "2026-10-01",
                    },
                    {
                        "text": "説明文2", "falsifier": None,
                        "check_metric": "指標2", "check_by": "2026-10-02",
                    },
                ],
            }],
        }],
    }
    dropped = ve.run_check_d_inferences(edition)
    check("改修27-1/推論欄: falsifierが無い推論は今までどおり削除される", dropped, 1)
    remaining = edition["sections"][0]["articles"][0]["inferences"]
    check("改修27-1/推論欄: falsifierがある推論は残る", len(remaining), 1)
    check("改修27-1/推論欄: 残った推論のfalsifierは元のまま", remaining[0]["falsifier"], "反証条件")


def test_override_generated_at():
    """改修27-1(4-1): generated_atをAIの自己申告から実行時刻に上書きし、元の値を返すこと。"""
    doc1 = {"generated_at": "2026-09-24T08:00:00+09:00", "other": 1}
    reported1 = ve.override_generated_at(doc1, "2026-09-28T18:30:00+09:00")
    check("override_generated_at/正例: 戻り値はAIが書いていた元の値", reported1, "2026-09-24T08:00:00+09:00")
    check("override_generated_at/正例: docのgenerated_atは実行時刻に上書きされる", doc1["generated_at"], "2026-09-28T18:30:00+09:00")
    check("override_generated_at/正例: 他のキーは変わらない", doc1["other"], 1)

    doc2 = {"generated_at": None}
    reported2 = ve.override_generated_at(doc2, "2026-09-28T18:30:00+09:00")
    check("override_generated_at/正例: AIの値がnullでも戻り値はnull", reported2, None)
    check("override_generated_at/正例: nullでも実行時刻に上書きされる", doc2["generated_at"], "2026-09-28T18:30:00+09:00")

    doc3 = {}
    reported3 = ve.override_generated_at(doc3, "2026-09-28T18:30:00+09:00")
    check("override_generated_at/正例: generated_atキー自体が無くても戻り値はnull", reported3, None)
    check("override_generated_at/正例: キーが無くても実行時刻のキーが作られる", doc3["generated_at"], "2026-09-28T18:30:00+09:00")


def test_compute_expected_slot():
    """改修27-1(決定4): 実行時刻の時刻部分だけから期待する時間帯を決める境界値を確かめる。"""
    def slot_at(iso):
        return ve.compute_expected_slot(dt.datetime.fromisoformat(iso))

    check("compute_expected_slot/境界: 4:59はevening(前日分)", slot_at("2026-09-28T04:59:00+09:00"), "evening")
    check("compute_expected_slot/境界: 5:00はmorning", slot_at("2026-09-28T05:00:00+09:00"), "morning")
    check("compute_expected_slot/境界: 10:59はmorning", slot_at("2026-09-28T10:59:00+09:00"), "morning")
    check("compute_expected_slot/境界: 11:00はnoon", slot_at("2026-09-28T11:00:00+09:00"), "noon")
    check("compute_expected_slot/境界: 15:59はnoon", slot_at("2026-09-28T15:59:00+09:00"), "noon")
    check("compute_expected_slot/境界: 16:00はevening", slot_at("2026-09-28T16:00:00+09:00"), "evening")
    check("compute_expected_slot/境界: 23:59はevening", slot_at("2026-09-28T23:59:00+09:00"), "evening")
    check("compute_expected_slot/境界: 0:00はevening", slot_at("2026-09-28T00:00:00+09:00"), "evening")


def test_round1_end_to_end_missing_ai_fields():
    """改修27-1(第1回)をCLI全体(main())で確かめる統合テスト。紙面を書くAIへの指示
    第7.1版で、上段の会社がfalsifier・horizon_business_days・deadline_date・direction・
    evidence_excerptを一切書かなくても(キーごと省略しても)、会社が消えないことを
    確かめる。generated_atがnull(未記入)でも実行時刻で上書きされ、AIの値(null)が
    first_run.generated_at_reported/hypotheses_generated_at_reportedに残ることも確かめる。
    scripts/testdataは使わず、その場で作った最小限の架空データだけを使う
    (ticker_sourceが無いなど、この確認に無関係な理由で仮説が落ちるのを避けるため)。"""
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        now = dt.datetime.now(ve.JST)
        today_str = _expected_edition_date("evening", now)

        edition = {
            "edition_id": f"{today_str}-evening",
            "date": today_str,
            "slot": "evening",
            "generated_at": None,
            "market_open": None,
            "sources": [],
            "sections": [{
                "section_id": "change",
                "articles": [{
                    "article_id": "A-1",
                    "lines": [{
                        "line_id": "L-1", "text": "架空の会社の発表内容です",
                        "claimed_mark": "reported_unverified", "numbers": [],
                    }],
                }],
            }],
        }
        hyp_doc = {
            "edition_id": f"{today_str}-evening",
            "generated_at": None,
            "hypotheses": [{
                "hypothesis_id": "H-1",
                "company_name": "カナリア工業",
                "relation_text": "業績に影響しうる可能性がある",
                "evidence_grade": "reported",
                "ticker": "1234",
                "ticker_source": "edinet_seccode",
                "baseline_date": today_str,
                "baseline_price_type": "close",
                "added_by": "manual",
                "line_ids": ["L-1"],
                # falsifier・horizon_business_days・deadline_date・direction・
                # evidence_excerptはキーごと省略する(第7.1版9.4の(1)(2))。
            }],
        }

        edition_dir = work_dir / "editions" / today_str
        edition_dir.mkdir(parents=True)
        edition_path = edition_dir / "evening.json"
        edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

        hyp_dir = work_dir / "hypotheses"
        hyp_dir.mkdir(parents=True)
        hyp_path = hyp_dir / f"{today_str}-evening.json"
        hyp_path.write_text(json.dumps(hyp_doc, ensure_ascii=False, indent=1), encoding="utf-8")

        cache_dir = work_dir / "cache"
        cache_dir.mkdir(parents=True)
        calendar_dir = _write_temp_calendar(work_dir, dt.datetime.strptime(today_str, "%Y-%m-%d").date(), 40)

        result = _run_verify(work_dir, edition_path, hyp_path, cache_dir, calendar_dir)
        check("改修27-1/統合: 正常終了する(終了コード0)", result.returncode, 0)

        after_edition = json.loads(edition_path.read_text(encoding="utf-8"))
        after_hyp = json.loads(hyp_path.read_text(encoding="utf-8"))

        check(
            "改修27-1/統合: falsifier・horizon_business_days等を書かなくても会社は消えない",
            len(after_hyp.get("hypotheses") or []), 1,
        )
        if after_hyp.get("hypotheses"):
            kept = after_hyp["hypotheses"][0]
            check(
                "改修27-1/統合: horizon_business_daysが機械で埋まる(fact_only相当=20営業日)",
                kept.get("horizon_business_days"), 20,
            )
            check("改修27-1/統合: deadline_dateが機械で埋まる(null以外)", kept.get("deadline_date") is not None, True)

        v = after_edition.get("verification") or {}
        check("改修27-1/統合: hypothesis_violationsが0(会社が消えていない)", v.get("hypothesis_violations"), 0)

        check(
            "改修27-1/統合: 紙面のgenerated_atが実行時刻に上書きされる(nullのままではない)",
            after_edition.get("generated_at") is not None, True,
        )
        check(
            "改修27-1/統合: 仮説ファイルのgenerated_atも実行時刻に上書きされる",
            after_hyp.get("generated_at") is not None, True,
        )

        first_run = v.get("first_run") or {}
        check(
            "改修27-1/統合: first_run.generated_at_reportedにAIの元の値(null)が残る",
            first_run.get("generated_at_reported"), None,
        )
        check(
            "改修27-1/統合: first_run.hypotheses_generated_at_reportedにAIの元の値(null)が残る",
            first_run.get("hypotheses_generated_at_reported"), None,
        )

        check(
            "改修27-1/統合: slot_expectedキーが記録される",
            v.get("slot_expected") in ("morning", "noon", "evening"), True,
        )
        check("改修27-1/統合: slot_mismatchは真偽値", isinstance(v.get("slot_mismatch"), bool), True)

    _assert_testdata_untouched("改修27-1/統合テスト")


def test_apply_edinet_evidence():
    """2026年9月21日の追加指示: evidence_filer_name/evidence_doc_type/evidence_role/
    impact_kind/impact_kind_source/auto_check_targetを、AIに書かせず出典URLの書類管理番号
    からEDINET書類一覧を引いて機械で確定するapply_edinet_evidence()の正例・負例。

    このテストはtest_evidence_role_and_auto_check_target()を置き換えるもの(旧版は
    evidence_filer_nameとcompany_nameの文字列比較で判定していたが、今回その仕組み自体を
    ふさいだため、EDINET書類一覧を引く新しい仕組みで確かめ直す)。"""
    edinet_companies = [
        # 書類種別240(公開買付届出書)。フェローテックが提出した書類(doc_id: S100Z392)。
        # company_nameが提出者本人の場合と、対象会社(TOB例外)の場合の両方をこの1件で試す。
        {
            "filer_name": "株式会社フェローテック", "ticker": "6890",
            "doc_id": "S100Z392", "doc_type_code": "240",
            "doc_description": "公開買付届出書",
        },
        # 書類種別350(大量保有報告書・変更報告書)
        {
            "filer_name": "テスト投資株式会社", "ticker": "1000",
            "doc_id": "S100AMOUNT", "doc_type_code": "350",
            "doc_description": "変更報告書",
        },
        # 書類種別120(対応表に無いコード。有価証券報告書)
        {
            "filer_name": "テスト物産", "ticker": "9999",
            "doc_id": "S100UNMAPPED", "doc_type_code": "120",
            "doc_description": "有価証券報告書",
        },
    ]

    sources = {
        "SRC-TOB": {
            "source_id": "SRC-TOB",
            "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100Z392?type=1",
        },
        "SRC-AMOUNT": {
            "source_id": "SRC-AMOUNT",
            "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100AMOUNT?type=1",
        },
        "SRC-UNMAPPED": {
            "source_id": "SRC-UNMAPPED",
            "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100UNMAPPED?type=1",
        },
        "SRC-NOTFOUND": {
            "source_id": "SRC-NOTFOUND",
            "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100NONE?type=1",
        },
        "SRC-NEWS": {
            "source_id": "SRC-NEWS",
            "url": "https://www.nikkei.com/article/DGXZQOxxxxxxx/",
        },
        # EDINETのドメインだが、書類一覧APIそのもののURL(書類管理番号を含まない形)。
        # editions/2026-09-18/evening.json のSRC-004と同じ形。
        "SRC-UNPARSED": {
            "source_id": "SRC-UNPARSED",
            "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-18&type=2",
        },
    }

    def hyp(**kw):
        h = {"company_name": "テスト物産", "evidence_source_ref": None, "impact_kind": None}
        h.update(kw)
        return h

    # --- 1: 書類種別240、company_nameが提出者本人 → filer_self/price_stated/edinet_doctype ---
    h1 = hyp(company_name="株式会社フェローテック", evidence_source_ref="SRC-TOB")
    ve.apply_edinet_evidence([h1], sources, edinet_companies, None)
    check(
        "apply_edinet_evidence/#1: 書類種別240・提出者本人はfiler_self/price_stated/edinet_doctype",
        (h1["evidence_role"], h1["impact_kind"], h1["impact_kind_source"]),
        ("filer_self", "price_stated", "edinet_doctype"),
    )
    check(
        "apply_edinet_evidence/#1: evidence_filer_name/evidence_doc_typeも書類一覧の値で埋まる",
        (h1["evidence_filer_name"], h1["evidence_doc_type"]),
        ("株式会社フェローテック", "公開買付届出書"),
    )
    check(
        "改修27-1(4-10)/apply_edinet_evidence: filer_selfの書類種別240はtob_sideがbidderになる",
        h1["tob_side"], "bidder",
    )

    # --- 7・9(★今回ふさぐ穴): TOB対象会社(提出者は別会社=フェローテック) ---
    #     AIがevidence_filer_nameにcompany_nameと同じ文字列を書いていても、機械は
    #     出典の実際の提出者(フェローテック)で判定するのでmentionedになる。
    h9 = hyp(
        company_name="株式会社日本抵抗器製作所", evidence_source_ref="SRC-TOB",
        evidence_filer_name="株式会社日本抵抗器製作所",  # AIの自己申告(誤り)
        impact_kind="fact_only",  # AIの自己申告(こちらも機械の値で上書きされる)
    )
    counts9 = ve.apply_edinet_evidence([h9], sources, edinet_companies, None)
    check(
        "apply_edinet_evidence/#9(★穴の再現): AIが書いたevidence_filer_nameは使われず、"
        "evidence_roleはmentionedになる",
        h9["evidence_role"], "mentioned",
    )
    check(
        "改修27-1(4-10)/apply_edinet_evidence: subjectEdinetCodeが無い書類種別240はtob_sideが"
        "今の規則(mentioned→target)に倒れる",
        h9["tob_side"], "target",
    )
    check(
        "改修27-1(4-10)/apply_edinet_evidence: subjectEdinetCodeが無ければtob_side_subject_code_missingが1",
        counts9["tob_side_subject_code_missing"], 1,
    )
    check(
        "apply_edinet_evidence/#9: evidence_filer_nameも書類一覧の提出者名(フェローテック)で上書きされる",
        h9["evidence_filer_name"], "株式会社フェローテック",
    )
    check(
        "apply_edinet_evidence/#7(TOB例外): impact_kindは書類種別からprice_stated/edinet_doctypeに決まる",
        (h9["impact_kind"], h9["impact_kind_source"]), ("price_stated", "edinet_doctype"),
    )
    check(
        "apply_edinet_evidence/#7(TOB例外): evidence_roleがmentionedでもauto_check_targetはtrue(条件b)",
        h9["auto_check_target"], True,
    )

    # --- 2: 書類種別350 → amount_stated ---
    h2 = hyp(company_name="テスト投資株式会社", evidence_source_ref="SRC-AMOUNT")
    ve.apply_edinet_evidence([h2], sources, edinet_companies, None)
    check(
        "apply_edinet_evidence/#2: 書類種別350はamount_stated/edinet_doctype",
        (h2["impact_kind"], h2["impact_kind_source"]), ("amount_stated", "edinet_doctype"),
    )
    check(
        "改修27-1(4-10)/apply_edinet_evidence: 公開買付関係でない書類種別350はtob_sideがnull",
        h2["tob_side"], None,
    )

    # --- 3・8: 対応表に無いコード(120) → null/edinet_doctype_unmapped、auto_check_targetもfalse ---
    h3 = hyp(evidence_source_ref="SRC-UNMAPPED", impact_kind="price_stated")
    counts3 = ve.apply_edinet_evidence([h3], sources, edinet_companies, None)
    check(
        "apply_edinet_evidence/#3: 対応表に無いコード(120)はimpact_kindがnull、edinet_doctype_unmapped",
        (h3["impact_kind"], h3["impact_kind_source"]), (None, "edinet_doctype_unmapped"),
    )
    check(
        "apply_edinet_evidence/#3: impact_kind_undetermined_by_doc_typeに'120'が1件記録される",
        counts3["impact_kind_undetermined_by_doc_type"].get("120"), 1,
    )
    check(
        "apply_edinet_evidence/#8: AIがprice_statedと書いても、対応表に無ければauto_check_targetはfalse",
        h3["auto_check_target"], False,
    )

    # --- 4: 書類管理番号は取れるが、その日の一覧に無い → mentioned/null/edinet_doc_not_found ---
    h4 = hyp(evidence_source_ref="SRC-NOTFOUND", impact_kind="amount_stated")
    ve.apply_edinet_evidence([h4], sources, edinet_companies, None)
    check(
        "apply_edinet_evidence/#4: 一覧に無いdoc_idはmentioned/null/edinet_doc_not_found",
        (h4["evidence_role"], h4["impact_kind"], h4["impact_kind_source"]),
        ("mentioned", None, "edinet_doc_not_found"),
    )

    # --- 5: 出典が報道記事(EDINETでない) → mentioned/null/no_edinet_doc ---
    h5 = hyp(evidence_source_ref="SRC-NEWS", impact_kind="price_stated")
    ve.apply_edinet_evidence([h5], sources, edinet_companies, None)
    check(
        "apply_edinet_evidence/#5: 報道記事の出典はmentioned/null/no_edinet_doc",
        (h5["evidence_role"], h5["impact_kind"], h5["impact_kind_source"]),
        ("mentioned", None, "no_edinet_doc"),
    )

    # --- 6: 書類一覧が読めない(edinet_companies=None) → 全件mentioned/null/doclist_unavailable ---
    h6a = hyp(company_name="株式会社フェローテック", evidence_source_ref="SRC-TOB",
              evidence_filer_name="株式会社フェローテック", impact_kind="price_stated")
    h6b = hyp(evidence_source_ref="SRC-NEWS")
    hyps6 = [h6a, h6b]
    ve.apply_edinet_evidence(hyps6, sources, None, None)
    check(
        "apply_edinet_evidence/#6: 書類一覧が読めない場合、全件mentioned/null/doclist_unavailableになる",
        [(h["evidence_role"], h["impact_kind"], h["impact_kind_source"]) for h in hyps6],
        [("mentioned", None, "doclist_unavailable"), ("mentioned", None, "doclist_unavailable")],
    )
    check("apply_edinet_evidence/#6: 仮説は削除されない(件数がそのまま2件)", len(hyps6), 2)

    # --- edinet_url_unparsed: EDINETドメインだが書類管理番号が取れないURL ---
    h_unparsed = hyp(evidence_source_ref="SRC-UNPARSED")
    counts_unparsed = ve.apply_edinet_evidence([h_unparsed], sources, edinet_companies, None)
    check(
        "apply_edinet_evidence/URL形: EDINETドメインだが書類管理番号が取れないURLはedinet_url_unparsedが1",
        counts_unparsed["edinet_url_unparsed"], 1,
    )
    check(
        "apply_edinet_evidence/URL形: 書類管理番号が取れなかった場合もimpact_kind_sourceはno_edinet_doc",
        h_unparsed["impact_kind_source"], "no_edinet_doc",
    )
    h_news_for_count = hyp(evidence_source_ref="SRC-NEWS")
    counts_news = ve.apply_edinet_evidence([h_news_for_count], sources, edinet_companies, None)
    check(
        "apply_edinet_evidence/URL形: 報道記事(EDINETドメインでない)はedinet_url_unparsedに数えない",
        counts_news["edinet_url_unparsed"], 0,
    )

    # --- overridden件数・impact_kind_source_counts ---
    h_over = hyp(
        company_name="株式会社フェローテック", evidence_source_ref="SRC-TOB",
        evidence_filer_name="違う値", evidence_doc_type="違う値", impact_kind="amount_stated",
    )
    counts_over = ve.apply_edinet_evidence([h_over], sources, edinet_companies, None)
    check(
        "apply_edinet_evidence/overridden: AIの値と機械の値が違えば3つとも1件ずつ数えられる",
        (
            counts_over["evidence_filer_name_overridden"],
            counts_over["evidence_doc_type_overridden"],
            counts_over["impact_kind_overridden"],
        ),
        (1, 1, 1),
    )
    check(
        "apply_edinet_evidence/impact_kind_source_counts: edinet_doctypeが1件記録される",
        counts_over["impact_kind_source_counts"].get("edinet_doctype"), 1,
    )


def test_compute_tob_side():
    """改修27-1(4-10、Q8の回答): tob_sideを機械で決める規則の正例・負例。"""
    codelist_rows = [{
        "ＥＤＩＮＥＴコード": "E-TARGET", "提出者名": "カナリア物産",
        "提出者業種": "小売業", "上場区分": "上場", "資本金": "1000", "証券コード": "12340",
    }]

    tob_record = {"doc_type_code": "240", "subject_edinet_code": "E-TARGET"}

    # --- 正例1: filer_self → bidder(subjectEdinetCodeを見るまでもなく決まる) ---
    counts1 = {"tob_side_subject_code_missing": 0}
    hyp_bidder = {"company_name": "だれでもよい"}
    result1 = ve.compute_tob_side(hyp_bidder, tob_record, "filer_self", codelist_rows, counts1)
    check("compute_tob_side/正例1: filer_selfはbidder", result1, "bidder")
    check("compute_tob_side/正例1: subjectEdinetCodeを見ないのでtob_side_subject_code_missingは増えない", counts1["tob_side_subject_code_missing"], 0)

    # --- 正例2: mentionedで、companyのEDINETコードがsubjectEdinetCodeと一致 → target ---
    counts2 = {"tob_side_subject_code_missing": 0}
    hyp_target = {"company_name": "カナリア物産"}
    result2 = ve.compute_tob_side(hyp_target, tob_record, "mentioned", codelist_rows, counts2)
    check("compute_tob_side/正例2: companyのEDINETコードがsubjectEdinetCodeと一致すればtarget", result2, "target")
    check("compute_tob_side/正例2: 一致すればtob_side_subject_code_missingは増えない", counts2["tob_side_subject_code_missing"], 0)

    # --- 負例1: mentionedで、companyのEDINETコードがsubjectEdinetCodeと不一致 → null ---
    counts3 = {"tob_side_subject_code_missing": 0}
    hyp_other = {"company_name": "別の会社"}  # コードリストに無い
    result3 = ve.compute_tob_side(hyp_other, tob_record, "mentioned", codelist_rows, counts3)
    check("compute_tob_side/負例1: companyのEDINETコードが分からなければnull", result3, None)
    check("compute_tob_side/負例1: これはsubjectEdinetCode自体は取れているのでtob_side_subject_code_missingは増えない", counts3["tob_side_subject_code_missing"], 0)

    # --- 負例2: subjectEdinetCodeが空 → 今の規則(mentioned→target)に倒し、件数を記録 ---
    counts4 = {"tob_side_subject_code_missing": 0}
    record_no_subject = {"doc_type_code": "240", "subject_edinet_code": None}
    result4 = ve.compute_tob_side(hyp_target, record_no_subject, "mentioned", codelist_rows, counts4)
    check("compute_tob_side/負例2: subjectEdinetCodeが空ならmentioned→targetに倒れる", result4, "target")
    check("compute_tob_side/負例2: tob_side_subject_code_missingが1増える", counts4["tob_side_subject_code_missing"], 1)

    # --- 負例3: 書類種別が対象外(240〜280でない) → null ---
    counts5 = {"tob_side_subject_code_missing": 0}
    non_tob_record = {"doc_type_code": "350", "subject_edinet_code": "E-TARGET"}
    result5 = ve.compute_tob_side(hyp_target, non_tob_record, "mentioned", codelist_rows, counts5)
    check("compute_tob_side/負例3: 公開買付関係でない書類種別はnull", result5, None)

    # --- 負例4: recordがNone(書類が見つからない) → null ---
    counts6 = {"tob_side_subject_code_missing": 0}
    result6 = ve.compute_tob_side(hyp_target, None, "mentioned", codelist_rows, counts6)
    check("compute_tob_side/負例4: 参照した書類が見つからなければnull", result6, None)

    # --- 負例5: codelist_rowsがNone(コードリスト未取得)で、subjectEdinetCodeはある → null ---
    counts7 = {"tob_side_subject_code_missing": 0}
    result7 = ve.compute_tob_side(hyp_target, tob_record, "mentioned", None, counts7)
    check("compute_tob_side/負例5: コードリストが読めずcompanyのEDINETコードが分からなければnull", result7, None)


def test_last_business_day_before():
    """改修27-1(4-4): edinet_doclist_partialの日付表示に使う「直前の営業日」の算出。"""
    business_days = ve.load_business_days(str(CALENDAR_DIR))
    check(
        "last_business_day_before/正例: 2026-09-24より前の最後の営業日は2026-09-18"
        "(19〜23日は祝日・休日を挟むため)",
        ve.last_business_day_before(business_days, "2026-09-24"), "2026-09-18",
    )
    check(
        "last_business_day_before/負例: business_daysの先頭より前を指定すればNone",
        ve.last_business_day_before(business_days, business_days[0]), None,
    )


def test_format_edinet_submit_datetime():
    """改修27-1(4-2): EDINETのsubmitDateTime('YYYY-MM-DD hh:mm')を+09:00付きに変える。"""
    check(
        "format_edinet_submit_datetime/正例: 分単位の値がそのままJSTのISO8601になる",
        ve.format_edinet_submit_datetime("2026-09-25 17:59"),
        "2026-09-25T17:59:00+09:00",
    )
    check("format_edinet_submit_datetime/負例: nullはNone", ve.format_edinet_submit_datetime(None), None)
    check("format_edinet_submit_datetime/負例: 空文字はNone", ve.format_edinet_submit_datetime(""), None)
    check(
        "format_edinet_submit_datetime/負例: 読み取れない形式はNone",
        ve.format_edinet_submit_datetime("2026/09/25 17:59"), None,
    )


def _write_edinet_list_json(path, date_str, results):
    """改修27-1のテスト専用: edinet_fetch.py listが保存する生データと同じ形
    (metadata.parameter.date + results)のJSONファイルを作る。"""
    payload = {
        "metadata": {"status": "200", "parameter": {"date": date_str, "type": "2"}},
        "results": results,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def test_load_edinet_companies_two_days():
    """改修27-1(4-4): SRC-EDINET-LIST.json(号の日付)とSRC-EDINET-LIST-PREV.json
    (直前の営業日)の両方を読み、合わせて1つの配列にする。片方しか無ければ、
    読めた方だけで続ける。.cache/edinet/companies.jsonは読まない(経路を廃止)。"""
    with tempfile.TemporaryDirectory() as d:
        cache_dir = Path(d)
        _write_edinet_list_json(
            cache_dir / "SRC-EDINET-LIST.json", "2026-09-28",
            [{"edinetCode": "E-TODAY", "filerName": "今日の会社", "docID": "S-TODAY", "docTypeCode": "180"}],
        )
        _write_edinet_list_json(
            cache_dir / "SRC-EDINET-LIST-PREV.json", "2026-09-25",
            [{"edinetCode": "E-PREV", "filerName": "前日の会社", "docID": "S-PREV", "docTypeCode": "180"}],
        )

        companies, availability = ve.load_edinet_companies(str(cache_dir))
        check("load_edinet_companies/正例: 両方読めればavailabilityが両方True", availability, {"today": True, "prev": True})
        check(
            "load_edinet_companies/正例: 2日分が1つの配列に合わさる",
            sorted(c["doc_id"] for c in companies), ["S-PREV", "S-TODAY"],
        )

    # --- 負例: 今日の分しか無い場合 ---
    with tempfile.TemporaryDirectory() as d:
        cache_dir = Path(d)
        _write_edinet_list_json(
            cache_dir / "SRC-EDINET-LIST.json", "2026-09-28",
            [{"edinetCode": "E-TODAY", "filerName": "今日の会社", "docID": "S-TODAY", "docTypeCode": "180"}],
        )
        companies, availability = ve.load_edinet_companies(str(cache_dir))
        check("load_edinet_companies/負例: prevが無ければavailability.prevはFalse", availability["prev"], False)
        check("load_edinet_companies/負例: todayだけでも読めた方は使う(1件)", len(companies), 1)

    # --- 負例: どちらも無い場合 → companiesはNone ---
    with tempfile.TemporaryDirectory() as d:
        companies_none, availability_none = ve.load_edinet_companies(d)
        check("load_edinet_companies/負例: 両方無ければcompaniesはNone", companies_none, None)
        check("load_edinet_companies/負例: 両方無ければavailabilityは両方False", availability_none, {"today": False, "prev": False})

    # --- 負例: .cache/edinet/companies.json相当のファイルがあっても読まない(経路廃止) ---
    with tempfile.TemporaryDirectory() as d:
        cache_dir = Path(d)
        companies_path = cache_dir / "companies.json"
        companies_path.write_text(json.dumps([{"filer_name": "旧経路の会社", "ticker": "9999"}]), encoding="utf-8")
        companies_legacy, availability_legacy = ve.load_edinet_companies(str(cache_dir))
        check(
            "load_edinet_companies/負例: companies.json相当のファイルがあっても読まない(SRC-EDINET-LISTが無ければNoneのまま)",
            companies_legacy, None,
        )
        check("load_edinet_companies/負例: 上と同じ理由でavailabilityも両方False", availability_legacy, {"today": False, "prev": False})


def test_apply_edinet_source_published_at():
    """改修27-1(4-2・4-3、第3回の小さな修正1): EDINETの出典のpublished_atを機械で
    書き込む。個々の書類はsubmitDateTimeから時刻まで、書類一覧そのもの
    (source_idがSRC-EDINET-LIST/SRC-EDINET-LIST-PREVのどちらか)は取得条件の日付だけ。
    それ以外のEDINETのURL(会社の検索ページ等)は、一覧ではないのでpublished_atに
    一切触れない(小さな修正1)。"""
    with tempfile.TemporaryDirectory() as d:
        cache_dir = Path(d)
        _write_edinet_list_json(
            cache_dir / "SRC-EDINET-LIST.json", "2026-09-28",
            [{
                "edinetCode": "E-DOC", "filerName": "書類の会社", "docID": "S-DOC",
                "docTypeCode": "180", "submitDateTime": "2026-09-28 09:00",
            }],
        )
        # SRC-EDINET-LIST-PREV.jsonはわざと作らない(キャッシュファイルが無い場合の
        # 負例に使う)。

        edinet_companies, _availability = ve.load_edinet_companies(str(cache_dir))

        edition = {
            "sources": [
                {
                    "source_id": "SRC-DOC", "published_at": "AIが書いた値(嘘でもよい)",
                    "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S-DOC?type=1",
                },
                {
                    "source_id": "SRC-DOC-NOTFOUND", "published_at": "2026-01-01T00:00:00+09:00",
                    "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S-NONE?type=1",
                },
                {
                    "source_id": "SRC-EDINET-LIST", "published_at": None,
                    "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-28&type=2",
                },
                {
                    # 一覧の2つの名前のどちらか(ここではPREV)だが、対応する
                    # キャッシュファイルがまだ無い場合。
                    "source_id": "SRC-EDINET-LIST-PREV", "published_at": "元の値",
                    "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-01&type=2",
                },
                {
                    # 小さな修正1: EDINETドメインだが、書類管理番号も無く、一覧の
                    # 2つの名前のどちらでもない出典(例: 会社の検索ページ)。
                    "source_id": "SRC-EDINET-SEARCH", "published_at": "AIが書いた値のまま残るはず",
                    "url": "https://disclosure2.edinet-fsa.go.jp/",
                },
                {
                    "source_id": "SRC-NEWS", "published_at": "2026-09-27T10:00:00+09:00",
                    "url": "https://www.nikkei.com/article/xxx/",
                },
            ],
        }

        overwritten, other_url_hits = ve.apply_edinet_source_published_at(edition, str(cache_dir), edinet_companies)
        by_id = {s["source_id"]: s for s in edition["sources"]}

        check(
            "apply_edinet_source_published_at/正例(4-2): 個々の書類はsubmitDateTimeから時刻まで書き込まれる",
            by_id["SRC-DOC"]["published_at"], "2026-09-28T09:00:00+09:00",
        )
        check(
            "apply_edinet_source_published_at/負例(4-2): 一覧に見つからない書類はnullになる",
            by_id["SRC-DOC-NOTFOUND"]["published_at"], None,
        )
        check(
            "apply_edinet_source_published_at/正例(4-3): 書類一覧そのものは取得条件の日付だけになる",
            by_id["SRC-EDINET-LIST"]["published_at"], "2026-09-28",
        )
        check(
            "apply_edinet_source_published_at/正例(4-3): published_date_onlyが真になる",
            by_id["SRC-EDINET-LIST"]["published_date_only"], True,
        )
        check(
            "apply_edinet_source_published_at/負例(4-3): 対応するキャッシュファイルが無ければnullでpublished_date_onlyは立たない",
            (by_id["SRC-EDINET-LIST-PREV"]["published_at"], by_id["SRC-EDINET-LIST-PREV"]["published_date_only"]),
            (None, False),
        )
        check(
            "改修27-1第3回/小さな修正1・正例: 一覧の名前でないEDINETのURLはpublished_atに触れない",
            by_id["SRC-EDINET-SEARCH"]["published_at"], "AIが書いた値のまま残るはず",
        )
        check(
            "改修27-1第3回/小さな修正1・正例: そのURLにはpublished_date_onlyも付かない",
            "published_date_only" in by_id["SRC-EDINET-SEARCH"], False,
        )
        check(
            "apply_edinet_source_published_at/負例: EDINET以外の出典には触れない",
            by_id["SRC-NEWS"]["published_at"], "2026-09-27T10:00:00+09:00",
        )
        check(
            "apply_edinet_source_published_at/負例: EDINET以外の出典にpublished_date_onlyは付かない",
            "published_date_only" in by_id["SRC-NEWS"], False,
        )
        check(
            "apply_edinet_source_published_at/件数: 値が変わった出典は4件"
            "(SRC-DOC・SRC-DOC-NOTFOUND・SRC-EDINET-LIST・SRC-EDINET-LIST-PREV)",
            overwritten, 4,
        )
        check(
            "改修27-1第3回/小さな修正1・件数: 一覧でも個々の書類でもないEDINETのURLは1件",
            other_url_hits, 1,
        )


def _make_zip(entries):
    """entries: [(ファイル名, bytes), ...]。テスト専用のZIPバイト列を作る。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def test_select_document_files_and_extract_text_payload():
    """改修27-1(4-8、Q1の回答=案B): ZIPの中から本文として使うファイルを選ぶ規則。
    honbunを含むファイルをファイル名の昇順でつなげ、表紙(0000000_header)があれば
    最後に1つだけ付け加える。honbunが1つも無ければ、フォールバックとして今までどおり
    いちばん大きいファイルを1つ選ぶ(実装は安全側+記録)。"""
    # --- 正例: honbunが2つ(ファイル名の順が逆)+表紙が1つ+無関係なファイル ---
    zip_bytes = _make_zip([
        ("XBRL/PublicDoc/0101010_honbun_b.htm", b"HONBUN-B"),
        ("XBRL/PublicDoc/0101010_honbun_a.htm", b"HONBUN-A"),
        ("XBRL/PublicDoc/0000000_header_x.htm", b"HEADER"),
        ("XBRL/PublicDoc/manifest_PublicDoc.xml", b"<manifest/>"),
    ])
    parts, fallback = edinet_fetch.extract_text_payload(zip_bytes)
    check("extract_text_payload/正例: フォールバックではない(honbunが見つかった)", fallback, False)
    check(
        "extract_text_payload/正例: honbunをファイル名の昇順でつなげ、最後に表紙を付ける",
        [name for _payload, name in parts],
        [
            "XBRL/PublicDoc/0101010_honbun_a.htm",
            "XBRL/PublicDoc/0101010_honbun_b.htm",
            "XBRL/PublicDoc/0000000_header_x.htm",
        ],
    )
    check(
        "extract_text_payload/正例: 選んだ内容も対応するバイト列になっている",
        [payload for payload, _name in parts],
        [b"HONBUN-A", b"HONBUN-B", b"HEADER"],
    )

    # --- 正例: honbunが1つ、表紙が無い書類(臨時報告書以外の一部を想定) ---
    zip_bytes2 = _make_zip([("XBRL/PublicDoc/0101010_honbun.htm", b"HONBUN-ONLY")])
    parts2, fallback2 = edinet_fetch.extract_text_payload(zip_bytes2)
    check("extract_text_payload/正例: 表紙が無くてもhonbunだけで正常に選べる", fallback2, False)
    check(
        "extract_text_payload/正例: honbunが1つならそれだけを選ぶ",
        [name for _payload, name in parts2], ["XBRL/PublicDoc/0101010_honbun.htm"],
    )

    # --- 負例(フォールバック): honbunを含むファイルが1つも無い ---
    zip_bytes3 = _make_zip([
        ("XBRL/PublicDoc/0000000_header_x.htm", b"HEADER"),
        ("XBRL/PublicDoc/small.htm", b"S"),
        ("XBRL/PublicDoc/biggest.htm", b"BIGGEST-CONTENT-HERE"),
    ])
    parts3, fallback3 = edinet_fetch.extract_text_payload(zip_bytes3)
    check(
        "extract_text_payload/負例(実装は安全側): honbunが無ければフォールバックと記録される",
        fallback3, True,
    )
    check(
        "extract_text_payload/負例: フォールバック時はいちばん大きいファイルを1つだけ選ぶ(今までどおり)",
        [name for _payload, name in parts3], ["XBRL/PublicDoc/biggest.htm"],
    )

    # --- ZIPでない場合はそのまま1件として返す(今までどおり) ---
    parts4, fallback4 = edinet_fetch.extract_text_payload(b"not a zip")
    check("extract_text_payload/負例: ZIPでなければそのまま1件で返す", parts4, [(b"not a zip", None)])
    check("extract_text_payload/負例: ZIPでなければフォールバックの扱いにしない", fallback4, False)


def test_strip_html_tags():
    """改修27-1(4-9): EDINETのHTML本文からタグを取り除く処理。<style>・<script>は
    丸ごと除き、タグをまたいだ表記(1株当たり1,500円のような分割)をつなげて読める
    ようにし、HTMLエンティティは元の文字に戻す。"""
    html_text = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<p>【会社名】カナリア工業株式会社</p>"
        "<p>1株当たり<span>1,500</span>円で<script>var x=1;</script>買付ける</p>"
        "<p>AT&amp;T株式会社との比較</p>"
        "</body></html>"
    )
    plain = ve.strip_html_tags(html_text)
    check("strip_html_tags/正例: 会社名がタグ無しで読める", "カナリア工業株式会社" in plain, True)
    check("strip_html_tags/正例: タグをまたいだ数字がつながって読める", "1株当たり1,500円で買付ける" in plain, True)
    check("strip_html_tags/正例: styleブロックの中身は本文に残らない", "color:red" in plain, False)
    check("strip_html_tags/正例: scriptブロックの中身は本文に残らない", "var x=1" in plain, False)
    check("strip_html_tags/正例: HTMLエンティティは元の文字に戻る", "AT&T株式会社との比較" in plain, True)
    check("strip_html_tags/負例: nullはそのままNoneを返す", ve.strip_html_tags(None), None)
    check("strip_html_tags/負例: 空文字はそのまま空文字を返す", ve.strip_html_tags(""), "")


def test_read_source_body_for_checks():
    """改修27-1(4-9): 出典がEDINETのものだけタグを取り除く。EDINET以外の出典は
    そのまま返す。キャッシュの元ファイル自体は書き換えない。"""
    with tempfile.TemporaryDirectory() as d:
        edinet_path = write(d, "SRC-EDINET.txt", "<p>カナリア工業<b>株式会社</b></p>")
        news_path = write(d, "SRC-NEWS.txt", "<p>タグに見える文字列</p>そのままの記事")

        edinet_source = {"url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S100X?type=1"}
        news_source = {"url": "https://www.nikkei.com/article/xxx/"}

        body_edinet = ve.read_source_body_for_checks(edinet_path, edinet_source)
        check(
            "read_source_body_for_checks/正例: EDINETの出典はタグが取り除かれる",
            body_edinet, "カナリア工業株式会社",
        )

        body_news = ve.read_source_body_for_checks(news_path, news_source)
        check(
            "read_source_body_for_checks/負例: EDINET以外の出典はタグに見える文字列もそのまま残る",
            body_news, "<p>タグに見える文字列</p>そのままの記事",
        )

        check(
            "read_source_body_for_checks/負例: キャッシュの元ファイルは書き換えられていない",
            edinet_path.read_text(encoding="utf-8"), "<p>カナリア工業<b>株式会社</b></p>",
        )

        check(
            "read_source_body_for_checks/負例: 存在しないファイルはNone",
            ve.read_source_body_for_checks(Path(d) / "not_exist.txt", edinet_source), None,
        )


def test_collect_edinet_doc_files():
    """改修27-1(4-8): edinet_fetch.pyが書き出したSRC-xxx.files.jsonを、
    edinet_doc_filesとして写す。"""
    with tempfile.TemporaryDirectory() as d:
        cache_dir = Path(d)
        (cache_dir / "SRC-DOC.files.json").write_text(
            json.dumps({
                "doc_id": "S-DOC",
                "files": ["XBRL/PublicDoc/0101010_honbun.htm", "XBRL/PublicDoc/0000000_header.htm"],
                "fallback": False,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        # SRC-DOC-NOFILE.files.jsonはわざと作らない(記録が無い場合の負例)。

        edition = {
            "sources": [
                {
                    "source_id": "SRC-DOC",
                    "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S-DOC?type=1",
                },
                {
                    "source_id": "SRC-DOC-NOFILE",
                    "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S-NOFILE?type=1",
                },
                {
                    # EDINETの個々の書類でない出典(一覧そのもの)は対象外。
                    "source_id": "SRC-EDINET-LIST",
                    "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-28&type=2",
                },
                {
                    "source_id": "SRC-NEWS",
                    "url": "https://www.nikkei.com/article/xxx/",
                },
            ],
        }

        result = ve.collect_edinet_doc_files(edition, str(cache_dir))
        check(
            "collect_edinet_doc_files/正例: 記録があるEDINETの書類は写される",
            result.get("SRC-DOC"),
            {"files": ["XBRL/PublicDoc/0101010_honbun.htm", "XBRL/PublicDoc/0000000_header.htm"], "fallback": False},
        )
        check(
            "collect_edinet_doc_files/負例: 記録が無いEDINETの書類はキーごと入らない",
            "SRC-DOC-NOFILE" in result, False,
        )
        check(
            "collect_edinet_doc_files/負例: 書類一覧そのもの・EDINET以外の出典は対象外",
            ("SRC-EDINET-LIST" in result, "SRC-NEWS" in result), (False, False),
        )


def test_run_check_e_stale_sources_36h_boundary():
    """改修27-1(4-2、Q2の回答): 検査10の境目を「実行時刻を分単位に切り捨てて、
    36時間を超えたら古い」に直したことの確認。17:59提出の書類が、翌々日の5:59の
    実行では新しい、6:00の実行では古いという依頼文の例をそのまま確かめる。"""
    def make_edition(published_at):
        return {
            "sections": [{
                "section_id": "change",
                "articles": [{"lines": [{"line_id": "X-01", "source_ref": "SRC-X"}]}],
            }],
            "sources": [{"source_id": "SRC-X", "published_at": published_at}],
        }

    published = "2026-09-25T17:59:00+09:00"

    edition_a = make_edition(published)
    stale_a, _, _ = ve.run_check_e_stale_sources(edition_a, dt.datetime.fromisoformat("2026-09-27T05:59:00+09:00"))
    check(
        "検査10/境界(改修27-1・4-2): 17:59提出は翌々日5:59の実行では新しい(36時間ちょうど、落とさない)",
        stale_a, 0,
    )
    check(
        "検査10/境界: 5:59の実行では行が残る",
        len(edition_a["sections"][0]["articles"][0]["lines"]), 1,
    )

    edition_b = make_edition(published)
    stale_b, _, _ = ve.run_check_e_stale_sources(edition_b, dt.datetime.fromisoformat("2026-09-27T06:00:00+09:00"))
    check(
        "検査10/境界(改修27-1・4-2): 17:59提出は翌々日6:00の実行では古い(36時間1分超過)",
        stale_b, 1,
    )
    check(
        "検査10/境界: 6:00の実行では行が落とされる",
        len(edition_b["sections"][0]["articles"][0]["lines"]), 0,
    )

    # 実行時刻の秒によって境目がぶれないことの確認(分単位への切り捨て)。
    edition_c = make_edition(published)
    stale_c, _, _ = ve.run_check_e_stale_sources(edition_c, dt.datetime.fromisoformat("2026-09-27T05:59:59+09:00"))
    check(
        "検査10/境界: 実行時刻の秒が59でも分単位に切り捨てられ、5:59の判定のまま新しい",
        stale_c, 0,
    )


def test_run_check_published_at_skips_date_only():
    """改修27-1(4-3): published_date_onlyが真の出典は検査36の対象から外す。"""
    edition = {
        "sections": [{"section_id": "change", "articles": [{"lines": []}]}],
        "sources": [{
            "source_id": "SRC-EDINET-LIST", "published_at": "2026-09-28",
            "published_date_only": True,
        }],
    }
    with tempfile.TemporaryDirectory() as d:
        # キャッシュに本文が無くても(そもそも対象外なので)エラーにならず、0件のまま。
        hits, unverified_sources = ve.run_check_published_at(edition, d)
        check("検査36/負例(改修27-1・4-3): published_date_onlyの出典は対象から外れる(0件)", hits, 0)
        check("検査36/負例: 確認できなかった出典の一覧にも入らない", unverified_sources, [])


def test_round2_end_to_end_edinet_published_at_and_tob_side():
    """改修27-1(第2回)をCLI全体(main())で確かめる統合テスト。EDINET書類一覧(2日分)を
    キャッシュに置き、出典のpublished_atが機械で書き込まれること(4-2)、それより前の
    段階で書き込まれるため検査10(36時間ルール)が正しく働くこと、公開買付関係の
    書類でtob_sideがbidderになること(4-10)を、実データの取得はせず確かめる。
    scripts/testdataは使わない(round1の統合テストと同じ理由)。"""
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        now = dt.datetime.now(ve.JST)
        today_str = _expected_edition_date("evening", now)
        # 実行時刻の1時間前を提出時刻にする(36時間以内に確実に収まる余裕を持たせる)。
        submit_str = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")

        cache_dir = work_dir / "cache"
        cache_dir.mkdir(parents=True)
        _write_edinet_list_json(
            cache_dir / "SRC-EDINET-LIST.json", today_str,
            [{
                "edinetCode": "E-BID", "filerName": "カナリア工業", "docID": "S-BID",
                "docTypeCode": "240", "submitDateTime": submit_str,
            }],
        )
        _write_edinet_list_json(cache_dir / "SRC-EDINET-LIST-PREV.json", today_str, [])

        edition = {
            "edition_id": f"{today_str}-evening",
            "date": today_str,
            "slot": "evening",
            "generated_at": None,
            "market_open": None,
            "sources": [{
                "source_id": "SRC-BID", "publisher": "カナリア工業", "title": "公開買付届出書",
                "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S-BID?type=1",
                "published_at": None,
            }],
            "sections": [{
                "section_id": "change",
                "articles": [{
                    "article_id": "A-1",
                    "lines": [{
                        "line_id": "L-1", "text": "カナリア工業が公開買付を届け出た内容です",
                        "claimed_mark": "reported_unverified", "numbers": [], "source_ref": "SRC-BID",
                    }],
                }],
            }],
        }
        hyp_doc = {
            "edition_id": f"{today_str}-evening",
            "generated_at": None,
            "hypotheses": [{
                "hypothesis_id": "H-1",
                "company_name": "カナリア工業",
                "relation_text": "公開買付の対象として影響しうる",
                "evidence_grade": "reported",
                "evidence_source_ref": "SRC-BID",
                "impact_reason": "買付価格が示されているため",
                "ticker": "1234",
                "ticker_source": "edinet_codelist",
                "baseline_date": today_str,
                "baseline_price_type": "close",
                "added_by": "manual",
                "line_ids": ["L-1"],
            }],
        }

        edition_dir = work_dir / "editions" / today_str
        edition_dir.mkdir(parents=True)
        edition_path = edition_dir / "evening.json"
        edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

        hyp_dir = work_dir / "hypotheses"
        hyp_dir.mkdir(parents=True)
        hyp_path = hyp_dir / f"{today_str}-evening.json"
        hyp_path.write_text(json.dumps(hyp_doc, ensure_ascii=False, indent=1), encoding="utf-8")

        calendar_dir = _write_temp_calendar(work_dir, dt.datetime.strptime(today_str, "%Y-%m-%d").date(), 40)

        result = _run_verify(work_dir, edition_path, hyp_path, cache_dir, calendar_dir)
        check("改修27-1第2回/統合: 正常終了する(終了コード0)", result.returncode, 0)

        after_edition = json.loads(edition_path.read_text(encoding="utf-8"))
        after_hyp = json.loads(hyp_path.read_text(encoding="utf-8"))
        v = after_edition.get("verification") or {}

        source_after = after_edition["sources"][0]
        check(
            "改修27-1第2回/統合(4-2): 出典のpublished_atがsubmitDateTimeから機械で書き込まれる",
            source_after.get("published_at"), submit_str.replace(" ", "T") + ":00+09:00",
        )

        check(
            "改修27-1第2回/統合(4-2): published_atを書き込んだ後に検査10が働くため、"
            "新しい出典の行は落とされない",
            len(after_edition["sections"][0]["articles"][0]["lines"]), 1,
        )
        check("改修27-1第2回/統合: stale_source_hitsは0(出典が新しいため)", v.get("stale_source_hits"), 0)

        check(
            "改修27-1第2回/統合(4-4): 2日分とも読めたのでedinet_doclist_partialは空",
            v.get("edinet_doclist_partial"), [],
        )
        check(
            "改修27-1第2回/統合(4-2): edinet_published_at_overwrittenが1以上",
            (v.get("edinet_published_at_overwritten") or 0) >= 1, True,
        )

        check(
            "改修27-1第2回/統合: 会社は消えない(hypothesesが1件残る)",
            len(after_hyp.get("hypotheses") or []), 1,
        )
        if after_hyp.get("hypotheses"):
            kept = after_hyp["hypotheses"][0]
            check(
                "改修27-1第2回/統合(4-10): 提出者本人の公開買付届出書はtob_sideがbidderになる",
                kept.get("tob_side"), "bidder",
            )
            check(
                "改修27-1第2回/統合: impact_kindは書類種別からprice_statedになる(5営業日)",
                kept.get("horizon_business_days"), 5,
            )

    _assert_testdata_untouched("改修27-1第2回/統合テスト")


def test_round3_end_to_end_html_tags_and_doc_files():
    """改修27-1(第3回)をCLI全体(main())で確かめる統合テスト。EDINETの書類本文が
    タグ入りのHTMLでも、タグをまたいだ抜き出し文が検査1で一致すること(4-9)、
    どのファイルを本文に選んだかがedinet_doc_filesに記録されること(4-8)を確かめる。"""
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        now = dt.datetime.now(ve.JST)
        today_str = _expected_edition_date("evening", now)
        submit_str = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")

        cache_dir = work_dir / "cache"
        cache_dir.mkdir(parents=True)
        _write_edinet_list_json(
            cache_dir / "SRC-EDINET-LIST.json", today_str,
            [{
                "edinetCode": "E-DOC", "filerName": "カナリア電機株式会社", "docID": "S-DOC",
                "docTypeCode": "180", "submitDateTime": submit_str,
            }],
        )
        _write_edinet_list_json(cache_dir / "SRC-EDINET-LIST-PREV.json", today_str, [])

        # タグの中に数字が分かれて入っている、実物のEDINET書類(HTML)を模した本文。
        html_body = (
            "<html><body>"
            "<p>【会社名】カナリア電機株式会社</p>"
            "<p>今期の設備投資額は<span>1,200</span>百万円を予定している</p>"
            "</body></html>"
        )
        doc_path = cache_dir / "SRC-DOC.txt"
        doc_path.write_text(html_body, encoding="utf-8")
        content_hash = hashlib.sha256(doc_path.read_bytes()).hexdigest()

        (cache_dir / "SRC-DOC.files.json").write_text(
            json.dumps({
                "doc_id": "S-DOC",
                "files": ["XBRL/PublicDoc/0101010_honbun_x.htm", "XBRL/PublicDoc/0000000_header_x.htm"],
                "fallback": False,
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        edition = {
            "edition_id": f"{today_str}-evening",
            "date": today_str,
            "slot": "evening",
            "generated_at": None,
            "market_open": None,
            "sources": [{
                "source_id": "SRC-DOC", "publisher": "カナリア電機株式会社", "title": "有価証券報告書",
                "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S-DOC?type=1",
                "published_at": None, "usage": "quotable", "publisher_type": "company_disclosure",
                "content_sha256": content_hash,
            }],
            "sections": [{
                "section_id": "change",
                "articles": [{
                    "article_id": "A-1",
                    "lines": [{
                        "line_id": "L-1",
                        "text": "カナリア電機の設備投資額は1,200百万円を予定している",
                        "claimed_mark": "source_number_match",
                        "numbers": [{"label": "金額", "value": 1200}],
                        "source_ref": "SRC-DOC",
                        # タグ(<span>)で1,200が分かれている元のHTMLとは違い、
                        # 抜き出し文自体はAIが書く普通の(タグの無い)文。タグを
                        # 取り除いた本文の中に、この文がそのまま見つかるかどうかを試す。
                        "excerpt": "今期の設備投資額は1,200百万円を予定している",
                        "attribution": "出典：カナリア電機株式会社「有価証券報告書」（https://example.test/）",
                        "processing_note": "本サイトが同資料をもとに作成",
                    }],
                }],
            }],
        }
        hyp_doc = {"edition_id": f"{today_str}-evening", "generated_at": None, "hypotheses": []}

        edition_dir = work_dir / "editions" / today_str
        edition_dir.mkdir(parents=True)
        edition_path = edition_dir / "evening.json"
        edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

        hyp_dir = work_dir / "hypotheses"
        hyp_dir.mkdir(parents=True)
        hyp_path = hyp_dir / f"{today_str}-evening.json"
        hyp_path.write_text(json.dumps(hyp_doc, ensure_ascii=False, indent=1), encoding="utf-8")

        calendar_dir = _write_temp_calendar(work_dir, dt.datetime.strptime(today_str, "%Y-%m-%d").date(), 40)

        result = _run_verify(work_dir, edition_path, hyp_path, cache_dir, calendar_dir)
        check("改修27-1第3回/統合: 正常終了する(終了コード0)", result.returncode, 0)

        after_edition = json.loads(edition_path.read_text(encoding="utf-8"))
        v = after_edition.get("verification") or {}
        line_after = after_edition["sections"][0]["articles"][0]["lines"][0]

        check(
            "改修27-1第3回/統合(4-9): タグをまたいだ抜き出し文でも出典と数字が一致する(source_number_match)",
            line_after.get("mark"), "source_number_match",
        )
        check(
            "改修27-1第3回/統合(4-8): どのファイルを本文に選んだかがedinet_doc_filesに記録される",
            v.get("edinet_doc_files", {}).get("SRC-DOC"),
            {
                "files": ["XBRL/PublicDoc/0101010_honbun_x.htm", "XBRL/PublicDoc/0000000_header_x.htm"],
                "fallback": False,
            },
        )

    _assert_testdata_untouched("改修27-1第3回/統合テスト")


def test_round4_end_to_end_attribution():
    """改修27-1(第4回)をCLI全体(main())で確かめる統合テスト。AIがattribution・
    processing_noteにnullを置いたEDINETの出典・行が、機械が作った出典表記で
    埋まり、検査3で消えないことを確かめる。"""
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        now = dt.datetime.now(ve.JST)
        today_str = _expected_edition_date("evening", now)

        cache_dir = work_dir / "cache"
        cache_dir.mkdir(parents=True)
        doc_path = cache_dir / "SRC-DOC.txt"
        doc_path.write_text("今期の売上高は1,000百万円だった", encoding="utf-8")
        content_hash = hashlib.sha256(doc_path.read_bytes()).hexdigest()

        # 出典のpublished_atが機械で埋まるよう(検査10で「公表時刻が分からない」と
        # 落とされないよう)、EDINET書類一覧をキャッシュに置く(4-2・4-4)。
        submit_str = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        _write_edinet_list_json(
            cache_dir / "SRC-EDINET-LIST.json", today_str,
            [{
                "edinetCode": "E-DOC", "filerName": "カナリア工業", "docID": "S-DOC",
                "docTypeCode": "120", "submitDateTime": submit_str,
            }],
        )
        _write_edinet_list_json(cache_dir / "SRC-EDINET-LIST-PREV.json", today_str, [])

        edition = {
            "edition_id": f"{today_str}-evening",
            "date": today_str,
            "slot": "evening",
            "generated_at": None,
            "market_open": None,
            "sources": [{
                "source_id": "SRC-DOC", "publisher": "カナリア工業", "title": "有価証券報告書",
                "url": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/S-DOC?type=1",
                "published_at": None, "content_sha256": content_hash,
                # 第7.1版どおり、AIはattribution・processing_noteをnullのまま置く。
                "attribution": None, "processing_note": None,
            }],
            "sections": [{
                "section_id": "change",
                "articles": [{
                    "article_id": "A-1",
                    "lines": [{
                        "line_id": "L-1", "text": "カナリア工業の売上高は1,000百万円だった",
                        "claimed_mark": "source_number_match",
                        "numbers": [{"label": "金額", "value": 1000}],
                        "source_ref": "SRC-DOC", "excerpt": "今期の売上高は1,000百万円だった",
                        "attribution": None, "processing_note": None,
                    }],
                }],
            }],
        }
        hyp_doc = {"edition_id": f"{today_str}-evening", "generated_at": None, "hypotheses": []}

        edition_dir = work_dir / "editions" / today_str
        edition_dir.mkdir(parents=True)
        edition_path = edition_dir / "evening.json"
        edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")

        hyp_dir = work_dir / "hypotheses"
        hyp_dir.mkdir(parents=True)
        hyp_path = hyp_dir / f"{today_str}-evening.json"
        hyp_path.write_text(json.dumps(hyp_doc, ensure_ascii=False, indent=1), encoding="utf-8")

        calendar_dir = _write_temp_calendar(work_dir, dt.datetime.strptime(today_str, "%Y-%m-%d").date(), 40)

        result = _run_verify(work_dir, edition_path, hyp_path, cache_dir, calendar_dir)
        check("改修27-1第4回/統合: 正常終了する(終了コード0)", result.returncode, 0)

        after_edition = json.loads(edition_path.read_text(encoding="utf-8"))
        v = after_edition.get("verification") or {}
        source_after = after_edition["sources"][0]
        line_after = after_edition["sections"][0]["articles"][0]["lines"][0]

        check(
            "改修27-1第4回/統合: 出典のattributionがnullから機械の値に上書きされる",
            source_after.get("attribution") is not None, True,
        )
        check(
            "改修27-1第4回/統合: 行のattributionも出典と同じ値になる",
            line_after.get("attribution"), source_after.get("attribution"),
        )
        check(
            "改修27-1第4回/統合: attribution・processing_noteがnullだった行が、"
            "検査3(必須項目)では落ちず出典と数字が一致する(source_number_match)",
            line_after.get("mark"), "source_number_match",
        )
        check(
            "改修27-1第4回/統合: attribution_overwrittenが記録される(1以上)",
            (v.get("attribution_overwritten") or 0) >= 1, True,
        )
        check(
            "改修27-1第4回/統合: attribution_generation_skippedキーが記録される(0)",
            v.get("attribution_generation_skipped"), 0,
        )

    _assert_testdata_untouched("改修27-1第4回/統合テスト")


def test_recent_business_days():
    """改修27-1(4-11): recent_business_days()の「3営業日」の数え方(date_str自身を
    含めて直近count日分)。scripts/recent_headlines.pyと27-2の続報判定の両方から
    使う共通関数。"""
    business_days = ve.load_business_days(str(CALENDAR_DIR))
    window = ve.recent_business_days(business_days, "2026-09-24", 3)
    check(
        "recent_business_days/正例: 2026-09-24を含めて直近3営業日(19〜23日は祝日・休日)",
        window, ["2026-09-17", "2026-09-18", "2026-09-24"],
    )
    check(
        "recent_business_days/正例: date_str自身が営業日でなくても、それ以前の営業日から数える(土曜)",
        ve.recent_business_days(business_days, "2026-09-26", 2), ["2026-09-24", "2026-09-25"],
    )
    check(
        "recent_business_days/負例: business_daysが空なら空配列",
        ve.recent_business_days([], "2026-09-24", 3), [],
    )
    check(
        "recent_business_days/負例: date_strより前の営業日が1つも無ければ空配列",
        ve.recent_business_days(business_days, "2000-01-01", 3), [],
    )


def test_record_only_keys_edition_level():
    """改修27-1(4-12): date_only_number_lines・self_declared_unverified・
    banned_word_hits(紙面側)・change_verified_lines_by_sectionの正例・負例。
    どれも記録専用で、会社も行も消さないことを確かめる。"""
    edition = {
        "sections": [
            {
                "section_id": "change",
                "articles": [{
                    "article_id": "A-1",
                    "headline": "架空商事が注目の発表",
                    "lines": [
                        {
                            "line_id": "L-01", "text": "日付と件数だけの行",
                            "claimed_mark": "reported_unverified",
                            "numbers": [{"label": "日付", "value": "2026-09-24"}, {"label": "件数2", "value": 3}],
                            "mark": "reported_unverified",
                        },
                        {
                            "line_id": "L-02", "text": "金額も入っている行",
                            "claimed_mark": "reported_unverified",
                            "numbers": [{"label": "日付", "value": "2026-09-24"}, {"label": "金額", "value": 100}],
                            "mark": "source_number_match",
                        },
                        {
                            "line_id": "L-03", "text": "数字が無い行", "claimed_mark": "explainer",
                            "numbers": [], "mark": "explainer",
                        },
                        {
                            "line_id": "L-04", "text": "AIが最初からunverifiedと名乗った行",
                            "claimed_mark": "unverified", "numbers": [], "mark": "unverified",
                        },
                    ],
                    "inferences": [
                        {"text": "主要な仕入先だと考えられる", "falsifier": "x", "check_metric": "y", "check_by": "z"},
                    ],
                }],
            },
            {
                "section_id": "big",
                "articles": [{
                    "article_id": "A-2",
                    "headline": "見出しに禁止語は無い",
                    "lines": [
                        {
                            "line_id": "L-05", "text": "こちらも出典と数字が一致した行",
                            "claimed_mark": "source_number_match", "numbers": [{"label": "金額", "value": 1}],
                            "mark": "source_number_match",
                        },
                    ],
                }],
            },
        ],
    }

    # --- date_only_number_lines ---
    date_only = ve.compute_date_only_number_lines(edition)
    check(
        "date_only_number_lines/正例: labelが日付・件数だけの行(L-01)だけが数えられる",
        (date_only["count"], date_only["line_ids"]), (1, ["L-01"]),
    )

    # --- self_declared_unverified ---
    self_declared = ve.compute_self_declared_unverified(edition)
    check(
        "self_declared_unverified/正例: claimed_mark='unverified'のL-04だけが数えられる",
        (self_declared["count"], self_declared["line_ids"]), (1, ["L-04"]),
    )

    # --- banned_word_hits(紙面側) ---
    banned_hits = ve.compute_banned_word_hits_edition(edition)
    check(
        "banned_word_hits/正例: 見出しの禁止語(注目)が場所headlineで記録される",
        {"word": "注目", "location": "headline", "id": "A-1"} in banned_hits, True,
    )
    check(
        "banned_word_hits/正例: 推論欄の禁止語(主要)が場所inference_textで記録される",
        {"word": "主要", "location": "inference_text", "id": "A-1"} in banned_hits, True,
    )
    check(
        "banned_word_hits/負例: 禁止語を含まない見出し・行からは何も記録されない",
        any(h["id"] == "A-2" for h in banned_hits), False,
    )

    # --- change_verified_lines_by_section(mark='source_number_match'の行だけ数える) ---
    verified_by_section = ve.compute_change_verified_lines_by_section(edition)
    check(
        "change_verified_lines_by_section/正例: change枠はL-02の1件だけがsource_number_match",
        verified_by_section.get("change"), 1,
    )
    check(
        "change_verified_lines_by_section/正例: big枠はL-05の1件",
        verified_by_section.get("big"), 1,
    )

    # 会社・行そのものは消えていないことの確認(記録専用)。
    check(
        "改修27-1(4-12)/正例: 記録関数を呼んだだけでは行は1つも消えない",
        sum(len(a["lines"]) for s in edition["sections"] for a in s["articles"]), 5,
    )


def test_record_only_keys_hyp_level():
    """改修27-1(4-12): speculative_word_counts・banned_word_hits(仮説側)の正例・負例。"""
    hyps = [
        {
            "hypothesis_id": "H-1", "relation_text": "業績への恩恵が見込まれる展開",
            "impact_reason": "取引拡大が意識される",
        },
        {
            "hypothesis_id": "H-2", "relation_text": "有望な有力企業として代表的",
            "impact_reason": "価格が示されている",
        },
    ]

    speculative = ve.compute_speculative_word_counts(hyps)
    check(
        "speculative_word_counts/正例: relation_textの「恩恵」「見込まれる」が数えられる",
        (speculative["relation_text"]["恩恵"], speculative["relation_text"]["見込まれる"]), (1, 1),
    )
    check(
        "speculative_word_counts/正例: impact_reasonの「意識される」が数えられる",
        speculative["impact_reason"]["意識される"], 1,
    )
    check(
        "speculative_word_counts/負例: 出てこない語(なりやすい)は0のまま",
        speculative["relation_text"]["なりやすい"], 0,
    )

    banned_hyp_hits = ve.compute_banned_word_hits_hyps(hyps)
    check(
        "banned_word_hits(仮説)/正例: relation_textの禁止語(有望・有力・代表)がすべて記録される",
        sum(1 for h in banned_hyp_hits if h["id"] == "H-2" and h["location"] == "hypothesis_relation_text"),
        3,
    )
    check(
        "banned_word_hits(仮説)/負例: 禁止語の無いH-1のimpact_reasonからは記録されない",
        any(h["id"] == "H-1" and h["location"] == "hypothesis_impact_reason" for h in banned_hyp_hits),
        False,
    )

    check(
        "改修27-1(4-12)/正例: 記録関数を呼んだだけでは仮説は1つも消えない(2件のまま)",
        len(hyps), 2,
    )


def test_check_recent_headlines_status():
    """改修27-1(4-11): scripts/recent_headlines.pyの実行結果(印のファイル)を読み、
    失敗したかどうかを判定する。ファイルが無い・status不正・日付/slotの不一致は
    すべて失敗として扱う。"""
    with tempfile.TemporaryDirectory() as d:
        cache_dir = Path(d)

        check(
            "check_recent_headlines_status/負例: ファイルが無ければ失敗(True)",
            ve.check_recent_headlines_status(str(cache_dir), "2026-09-28", "evening"), True,
        )

        (cache_dir / "RECENT-HEADLINES.json").write_text(
            json.dumps({"status": "ok", "date": "2026-09-28", "slot": "evening"}), encoding="utf-8",
        )
        check(
            "check_recent_headlines_status/正例: status:okで日付・slotが一致すれば成功(False)",
            ve.check_recent_headlines_status(str(cache_dir), "2026-09-28", "evening"), False,
        )
        check(
            "check_recent_headlines_status/負例: 日付が違う号には使えない(True)",
            ve.check_recent_headlines_status(str(cache_dir), "2026-09-29", "evening"), True,
        )

        (cache_dir / "RECENT-HEADLINES.json").write_text(
            json.dumps({"status": "error", "date": "2026-09-28", "slot": "evening", "error": "x"}),
            encoding="utf-8",
        )
        check(
            "check_recent_headlines_status/負例: status:errorなら失敗(True)",
            ve.check_recent_headlines_status(str(cache_dir), "2026-09-28", "evening"), True,
        )

        (cache_dir / "RECENT-HEADLINES.json").write_text("{ 壊れたJSON", encoding="utf-8")
        check(
            "check_recent_headlines_status/負例: ファイルが壊れていれば失敗(True)",
            ve.check_recent_headlines_status(str(cache_dir), "2026-09-28", "evening"), True,
        )


def test_recent_headlines_is_before_and_select():
    """改修27-1(4-11、Q5の回答): is_before()・select_recent_index_entries()の
    正例・負例(日付が違えばその前後、同じ日なら時間帯の順、対象窓に無い日付は除く)。"""
    check("recent_headlines.is_before/正例: 日付が前なら真", rh.is_before("2026-09-24", "evening", "2026-09-28", "morning"), True)
    check("recent_headlines.is_before/負例: 日付が後なら偽", rh.is_before("2026-09-29", "morning", "2026-09-28", "evening"), False)
    check(
        "recent_headlines.is_before/正例: 同じ日でも前の時間帯(morning<evening)なら真",
        rh.is_before("2026-09-28", "morning", "2026-09-28", "evening"), True,
    )
    check(
        "recent_headlines.is_before/負例: 同じ日で同じ時間帯・後の時間帯は偽",
        (rh.is_before("2026-09-28", "evening", "2026-09-28", "evening"), rh.is_before("2026-09-28", "evening", "2026-09-28", "morning")),
        (False, False),
    )

    index_entries = [
        {"date": "2026-09-24", "slot": "evening", "edition_id": "A"},
        {"date": "2026-09-28", "slot": "morning", "edition_id": "B"},
        {"date": "2026-09-28", "slot": "evening", "edition_id": "C"},  # target自身より後なので除外
        {"date": "2026-09-01", "slot": "evening", "edition_id": "D"},  # 窓の外なので除外
    ]
    window_dates = ["2026-09-18", "2026-09-24", "2026-09-28"]
    selected = rh.select_recent_index_entries(index_entries, window_dates, "2026-09-28", "evening")
    check(
        "recent_headlines.select_recent_index_entries/正例: 窓の中・targetより前の号だけ選ばれ、日付昇順になる",
        [e["edition_id"] for e in selected], ["A", "B"],
    )


def test_recent_headlines_script_end_to_end():
    """改修27-1(4-11)をCLI全体で確かめる統合テスト。直近3営業日分の見出し・出典URLが
    出ること、失敗しても(号を止めず)印のファイルにstatus:'error'が書かれ、
    check_recent_headlines_status()がそれを失敗として読めることを確かめる。"""
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        # 全日を営業日として並べる一時カレンダーでは、直近3営業日の窓は
        # target(2026-09-28)を含めた直前3日(26・27・28)になる。過去の号の日付は
        # その中に入る2026-09-26にする。
        calendar_dir = _write_temp_calendar(work_dir, dt.date(2026, 9, 20), 15)

        edition_dir = work_dir / "editions" / "2026-09-26"
        edition_dir.mkdir(parents=True)
        past_edition = {
            "sections": [{
                "section_id": "change",
                "articles": [{
                    "article_id": "A-1", "headline": "過去の号の見出し",
                    "lines": [{"line_id": "L-1", "source_ref": "SRC-1"}],
                }],
            }],
            "sources": [{"source_id": "SRC-1", "url": "https://example.test/past-article"}],
        }
        (edition_dir / "evening.json").write_text(json.dumps(past_edition, ensure_ascii=False), encoding="utf-8")

        index_doc = {
            "generated_at": "2026-09-26T18:00:00+09:00",
            "editions": [{
                "date": "2026-09-26", "slot": "evening", "edition_id": "2026-09-26-evening",
                "edition_path": "editions/2026-09-26/evening.json",
            }],
        }
        (work_dir / "editions" / "index.json").write_text(json.dumps(index_doc, ensure_ascii=False), encoding="utf-8")

        out_path = work_dir / "cache" / "RECENT-HEADLINES.json"
        result = subprocess.run(
            [
                sys.executable, str(REPO_ROOT / "scripts" / "recent_headlines.py"),
                "--date", "2026-09-28", "--slot", "morning",
                "--calendar", str(calendar_dir), "--editions-index", str(work_dir / "editions" / "index.json"),
                "--out", str(out_path),
            ],
            capture_output=True, text=True, cwd=str(work_dir),
        )
        check("recent_headlines.py/正例: 正常終了する(終了コード0)", result.returncode, 0)
        check("recent_headlines.py/正例: 見出しが標準出力に出る", "過去の号の見出し" in result.stdout, True)
        check("recent_headlines.py/正例: 出典URLが標準出力に出る", "https://example.test/past-article" in result.stdout, True)

        record = json.loads(out_path.read_text(encoding="utf-8"))
        check("recent_headlines.py/正例: 出力ファイルのstatusはok", record.get("status"), "ok")
        check(
            "recent_headlines.py/正例: 出力ファイルに記事の見出し・出典URLが入っている",
            (record["articles"][0]["headline"], record["articles"][0]["source_urls"]),
            ("過去の号の見出し", ["https://example.test/past-article"]),
        )

        check(
            "改修27-1(4-11)/正例: 成功した実行はcheck_recent_headlines_status()で失敗と判定されない",
            ve.check_recent_headlines_status(str(work_dir / "cache"), "2026-09-28", "morning"), False,
        )

        # --- 負例: editions/index.jsonが無い場合、失敗しても印のファイルにstatus:errorが書かれる ---
        out_path2 = work_dir / "cache2" / "RECENT-HEADLINES.json"
        result2 = subprocess.run(
            [
                sys.executable, str(REPO_ROOT / "scripts" / "recent_headlines.py"),
                "--date", "2026-09-28", "--slot", "morning",
                "--calendar", str(calendar_dir), "--editions-index", str(work_dir / "does-not-exist.json"),
                "--out", str(out_path2),
            ],
            capture_output=True, text=True, cwd=str(work_dir),
        )
        check("recent_headlines.py/負例: 一覧が無ければ終了コード1(号は止めない設計、失敗を伝えるだけ)", result2.returncode, 1)
        record2 = json.loads(out_path2.read_text(encoding="utf-8"))
        check("recent_headlines.py/負例: 失敗してもstatus:errorのファイルが書かれる", record2.get("status"), "error")
        check(
            "改修27-1(4-11)/負例: 失敗した実行はcheck_recent_headlines_status()で失敗と判定される",
            ve.check_recent_headlines_status(str(work_dir / "cache2"), "2026-09-28", "morning"), True,
        )


def test_build_index_skips_editions_without_verification():
    """改修27-1(4-13): build_entry()は、verification(照合結果)の無い号を一覧に
    入れない。既にverificationがある号は今までどおり一覧に入る。"""
    with tempfile.TemporaryDirectory() as d:
        fake_root = Path(d)
        (fake_root / "editions" / "2026-09-24").mkdir(parents=True)
        (fake_root / "hypotheses").mkdir(parents=True)

        with_verification = {
            "edition_id": "2026-09-24-morning", "generated_at": "2026-09-24T08:00:00+09:00",
            "market_open": True, "verification": {"script_version": "2.0.0"},
        }
        without_verification = {
            "edition_id": "2026-09-24-noon", "generated_at": None, "market_open": None,
        }
        (fake_root / "editions" / "2026-09-24" / "morning.json").write_text(
            json.dumps(with_verification, ensure_ascii=False), encoding="utf-8",
        )
        (fake_root / "editions" / "2026-09-24" / "noon.json").write_text(
            json.dumps(without_verification, ensure_ascii=False), encoding="utf-8",
        )

        original_root = build_index.REPO_ROOT
        original_editions_dir = build_index.EDITIONS_DIR
        original_hypotheses_dir = build_index.HYPOTHESES_DIR
        build_index.REPO_ROOT = fake_root
        build_index.EDITIONS_DIR = fake_root / "editions"
        build_index.HYPOTHESES_DIR = fake_root / "hypotheses"
        try:
            entry_with = build_index.build_entry(
                "2026-09-24", "morning", build_index.EDITIONS_DIR / "2026-09-24" / "morning.json",
            )
            entry_without = build_index.build_entry(
                "2026-09-24", "noon", build_index.EDITIONS_DIR / "2026-09-24" / "noon.json",
            )
        finally:
            build_index.REPO_ROOT = original_root
            build_index.EDITIONS_DIR = original_editions_dir
            build_index.HYPOTHESES_DIR = original_hypotheses_dir

        check(
            "build_index/正例(4-13): verificationがある号は一覧に入る",
            entry_with is not None, True,
        )
        check(
            "build_index/負例(4-13): verificationが無い号は一覧に入らない(None)",
            entry_without, None,
        )


def test_check12_removed_direction_not_read():
    """改修27-2(S6): 検査12を廃止した。directionがminusでも、evidence_excerptが無くても、
    evidence_roleがfiler_selfでなくても、上段の会社は消えない(directionとevidence_excerptを
    読まない)。検査番号12は欠番のまま(他の検査の番号はずらさない)。"""
    check("検査12廃止/正例: check_minus_directionはもう存在しない", hasattr(ve, "check_minus_direction"), False)

    business_days = ve.load_business_days(str(CALENDAR_DIR))
    deadline = ve.compute_deadline(business_days, "2026-09-24", 5)
    line_ids = {"L-1": "source_number_match"}

    def base(**kw):
        h = {
            "company_name": "テスト物産", "relation_text": "業績に影響しうる",
            "baseline_price_type": "close", "baseline_date": "2026-09-24",
            "evidence_grade": "reported", "ticker": "8801", "ticker_source": "edinet_codelist",
            "line_ids": ["L-1"], "added_by": "manual",
            "horizon_business_days": 5, "deadline_date": deadline,
        }
        h.update(kw)
        return h

    def reason_for(hyp):
        extra = {"codelist_unavailable": True, "baseline_date_check_skipped": 0}
        return ve.check_hypothesis(hyp, {}, line_ids, business_days, [], {}, ".", None, None, extra)

    check(
        "検査12廃止/反応してほしくない例1: directionがminusでもevidence_excerptが無くても、会社は消えない",
        reason_for(base(direction="minus", evidence_excerpt=None)), None,
    )
    check(
        "検査12廃止/反応してほしくない例2: directionがminusでevidence_roleがmentioned・evidence_gradeがreportedでも消えない",
        reason_for(base(direction="minus", evidence_role="mentioned", evidence_grade="reported")), None,
    )
    check(
        "検査12廃止/反応してほしくない例3: 過去の号のようにdirection・evidence_excerptが残っていても、値は書き換えない",
        (lambda h: (reason_for(h), h.get("direction"), h.get("evidence_excerpt")))(
            base(direction="minus", evidence_excerpt="抜き出し")),
        (None, "minus", "抜き出し"),
    )
    check(
        "検査12廃止/参考: 廃止した理由名minus_condition_failedは検査の理由として返らない",
        reason_for(base(direction="minus", evidence_grade="inferred")) != "minus_condition_failed", True,
    )


def test_is_date_only_string():
    """改修27-2(S1): 「YYYY-MM-DD」の形で実在する日付のときだけ真。"""
    cases = [
        ("2026-09-18", True, "日付だけ"),
        ("2026-09-18T16:03:00+09:00", False, "時刻付き(タイムゾーンあり)"),
        ("2026-09-18T16:03:00", False, "時刻付き(タイムゾーンなし)"),
        ("2026-09-18 16:03", False, "時刻付き(空白区切り)"),
        (None, False, "null"),
        ("", False, "空文字"),
        (20260918, False, "数値(文字列でない)"),
        ("2026-13-45", False, "実在しない日付"),
        ("2026-02-30", False, "実在しない日付(2月30日)"),
        (" 2026-09-18", False, "前に空白"),
        ("2026-09-18 ", False, "後ろに空白"),
        ("2026/09/18", False, "スラッシュ区切り(この形は日付だけと扱わない)"),
        ("2026-9-8", False, "ゼロ埋めなし"),
        ("２０２６-０９-１８", False, "全角数字"),
    ]
    for value, expected, label in cases:
        check(f"published_date_only/S1判定: {label}({value!r})は{expected}", ve.is_date_only_string(value), expected)


def test_apply_published_date_only():
    """改修27-2(S1): 全出典のpublished_date_onlyを、AIの自己申告ではなくpublished_atの
    形から機械で書く。AIの値は一致・不一致にかかわらず上書きし、キーは必ず書く。"""
    edition = {"sources": [
        {"source_id": "A", "published_at": "2026-09-18"},
        {"source_id": "B", "published_at": "2026-09-18T16:03:00+09:00", "published_date_only": True},
        {"source_id": "C", "published_at": None},
        {"source_id": "D", "published_at": "2026-09-19", "published_date_only": False},
        {"source_id": "SRC-EDINET-LIST", "published_at": "2026-09-24", "published_date_only": True},
        {"source_id": "SRC-EDINET-LIST-PREV", "published_at": None, "published_date_only": False},
        {"source_id": "F"},
    ]}
    result = ve.apply_published_date_only(edition)
    flags = {s["source_id"]: s["published_date_only"] for s in edition["sources"]}
    check("published_date_only/正例: 日付だけの出典(A)は、AIが書いていなくても真になる", flags["A"], True)
    check("published_date_only/正例: AIが偽と書いた日付だけの出典(D)も真に上書きする", flags["D"], True)
    check("published_date_only/正例: AIが真と書いた時刻付きの出典(B)は偽に上書きする", flags["B"], False)
    check("published_date_only/正例: published_atがnullの出典(C)は偽になり、キーは必ず書かれる", flags["C"], False)
    check("published_date_only/正例: published_atのキー自体が無い出典(F)も偽で、キーが書かれる", flags["F"], False)
    check(
        "published_date_only/正例: 書類一覧の2つは、日付があれば真・一覧が読めずnullなら偽",
        (flags["SRC-EDINET-LIST"], flags["SRC-EDINET-LIST-PREV"]), (True, False),
    )
    check(
        "published_date_only/正例: 戻り値は真にした出典の件数とIDを、出典の並び順で返す",
        result, {"count": 3, "source_ids": ["A", "D", "SRC-EDINET-LIST"]},
    )
    check("published_date_only/負例: 出典が0件でも落ちず、0件を返す", ve.apply_published_date_only({"sources": []}), {"count": 0, "source_ids": []})
    check("published_date_only/負例: sourcesのキーが無い号でも落ちない", ve.apply_published_date_only({}), {"count": 0, "source_ids": []})


def test_check36_timed_only_record_skips_date_only():
    """改修27-2第4回: 記録だけの検査36(run_check_published_at)は時刻付きの出典だけが対象で、
    日付だけの出典(書類一覧の2つを含む)は対象外(件数にも入れない)。日付だけの出典は
    行を落とす run_check_published_date_only_required() が別に扱う。
    (第1回では、AIが日付だけを書いた出典は「今までどおりこの検査にかかる」としていたが、
    第4回で日付だけの出典の扱いを新しい関数に移したため期待値を変えた。)"""
    with tempfile.TemporaryDirectory() as d:
        for sid in ("C08", "SRC-EDINET-LIST", "T-1"):
            write(d, f"{sid}.txt", "この本文には日付が書かれていない。".encode("utf-8"))
        edition = {
            "sections": [{"section_id": "big", "articles": [{"lines": [
                {"line_id": "L-1", "source_ref": "C08"},
                {"line_id": "L-2", "source_ref": "SRC-EDINET-LIST"},
                {"line_id": "L-3", "source_ref": "T-1"},
            ]}]}],
            "sources": [
                {"source_id": "C08", "url": "https://example.test/a", "published_at": "2026-09-18", "published_date_only": True},
                {"source_id": "SRC-EDINET-LIST", "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-18&type=2",
                 "published_at": "2026-09-18", "published_date_only": True},
                {"source_id": "T-1", "url": "https://example.test/t", "published_at": "2026-09-18T10:00:00+09:00", "published_date_only": False},
            ],
        }
        hits, unverified_sources = ve.run_check_published_at(edition, d)
    check(
        "検査36(記録)/27-2第4回: 日付だけの出典(C08・書類一覧)は対象外で、時刻付きのT-1だけが記録される",
        (hits, unverified_sources), (1, ["T-1"]),
    )


def test_empty_title_or_url_refs():
    """改修27-2(S12): titleかurlが空の出典を参照する行は、確定した印をunverifiedにし、
    empty_title_or_url_refsの元(stats["empty_title_or_url_line_ids"])に記録する。"""
    def make(sources, lines):
        return {"sources": sources, "sections": [{"section_id": "big", "articles": [{"lines": lines}]}]}

    def src(source_id, title="題名", url="https://example.test/x"):
        return {"source_id": source_id, "title": title, "url": url, "usage": "snippet_only"}

    def line(line_id, ref, claimed="reported_unverified", **kw):
        l = {"line_id": line_id, "text": "本文", "claimed_mark": claimed, "numbers": [], "source_ref": ref}
        l.update(kw)
        return l

    with tempfile.TemporaryDirectory() as d:
        edition = make(
            [src("S-OK"), src("S-NOTITLE", title=None), src("S-BLANKTITLE", title="   "),
             src("S-FULLWIDTH", title="\u3000\u3000"), src("S-EMPTYURL", url=""), src("S-NOURL", url=None),
             src("S-BLANKURL", url="  ")],
            [
                line("L-ok", "S-OK"),
                line("L-notitle", "S-NOTITLE"),
                line("L-blanktitle", "S-BLANKTITLE"),
                line("L-fullwidth", "S-FULLWIDTH"),
                line("L-emptyurl", "S-EMPTYURL"),
                line("L-nourl", "S-NOURL"),
                line("L-blankurl", "S-BLANKURL"),
                line("L-noref", None, claimed="explainer"),
                line("L-explainer-with-empty", "S-NOTITLE", claimed="explainer"),
                line("L-number", "S-NOTITLE", claimed="source_number_match", numbers=[{"value": 1}], excerpt="1"),
            ],
        )
        stats, _ = ve.run_line_verification(edition, d)
    marks = {l["line_id"]: (l["mark"], l["mark_reason"]) for l in edition["sections"][0]["articles"][0]["lines"]}
    empty_ids = ["L-notitle", "L-blanktitle", "L-fullwidth", "L-emptyurl", "L-nourl", "L-blankurl",
                 "L-explainer-with-empty", "L-number"]
    for lid in empty_ids:
        check(f"S12/正例: {lid}は題名かURLが空の出典を参照するため、印はunverified(理由empty_title_or_url)",
              marks[lid], ("unverified", "empty_title_or_url"))
    check("S12/負例: 題名もURLも揃った出典を参照するL-okは、自己申告どおりreported_unverifiedのまま", marks["L-ok"], ("reported_unverified", None))
    check("S12/負例: 出典を参照しない行(L-noref)は対象外", marks["L-noref"], ("explainer", None))
    check("S12/記録: 記録される行IDは該当した8行だけで、並びは行の順", stats["empty_title_or_url_line_ids"], empty_ids)
    check(
        "S12/記録: 集計(合格・未確認など)が印の変更と食い違わない(未確認8・自己申告どおり1・解説1)",
        (stats["unverified"], stats["reported_unverified"], stats["explainer"], stats["passed"]), (8, 1, 1, 0),
    )
    check("S12/記録: unverified_reasonsにempty_title_or_urlが8件と数えられる", stats["unverified_reasons"], {"empty_title_or_url": 8})

    # 出典が0件・参照が無い号でも落ちず、記録は空。
    with tempfile.TemporaryDirectory() as d:
        stats2, _ = ve.run_line_verification(make([], [line("L-1", None, claimed="explainer")]), d)
    check("S12/負例: 出典が無い号でも落ちず、記録は空", stats2["empty_title_or_url_line_ids"], [])


def test_reported_relation_text_mismatch():
    """改修27-2(S13): reportedの上段の会社のrelation_textが定型文と完全一致しなければ
    記録する(記録専用。会社は消さない)。"""
    fixed = ve.REPORTED_RELATION_TEXT
    check("S13/定型文: 定数の値は依頼どおり", fixed, "検索結果の断片に社名あり（本文は未確認）")
    hyps = [
        {"hypothesis_id": "H-1", "evidence_grade": "reported", "relation_text": fixed},
        {"hypothesis_id": "H-2", "evidence_grade": "reported", "relation_text": "親会社グループから株式を取得され、連結子会社となる立場にある。"},
        {"hypothesis_id": "H-3", "evidence_grade": "primary", "relation_text": "primaryなので定型文でなくてよい"},
        {"hypothesis_id": "H-4", "evidence_grade": "reported", "relation_text": fixed + " "},
        {"hypothesis_id": "H-5", "evidence_grade": "reported", "relation_text": "検索結果の断片に社名あり(本文は未確認)"},
        {"hypothesis_id": "H-6", "evidence_grade": "reported"},
        {"hypothesis_id": "H-7", "evidence_grade": "reported", "relation_text": ""},
        {"hypothesis_id": "H-8", "evidence_grade": "inferred", "relation_text": "x"},
        {"hypothesis_id": "H-9", "evidence_grade": "reported", "relation_text": fixed},
    ]
    before = json.dumps(hyps, ensure_ascii=False)
    result = ve.compute_reported_relation_text_mismatch(hyps)
    check("S13/正例: 定型文と違うreportedを記録(H-2の別文・H-4の末尾空白・H-5の半角かっこ・H-6のキー無し・H-7の空文字)",
          result, {"count": 5, "hypothesis_ids": ["H-2", "H-4", "H-5", "H-6", "H-7"]})
    check("S13/負例: 定型文と完全一致するreported(H-1・H-9)、primary(H-3)、inferred(H-8)は記録しない",
          any(i in result["hypothesis_ids"] for i in ("H-1", "H-9", "H-3", "H-8")), False)
    check("S13/負例: 仮説の中身は書き換えない・消さない", json.dumps(hyps, ensure_ascii=False), before)
    check("S13/負例: 仮説が0件なら0件", ve.compute_reported_relation_text_mismatch([]), {"count": 0, "hypothesis_ids": []})


def test_round1_27_2_end_to_end_default_keys_without_hypotheses():
    """改修27-2(S15): --hypothesesを渡さない実行でも、第1回で足した記録キーがそろう
    (S13は0件・空で出る。S1・S12は紙面だけで決まるので値が入る)。"""
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        edition_path, _hyp_path, cache_dir, today_str, prev_str = _rebuild_canary_as_today(work_dir)
        calendar_dir = _write_temp_calendar(
            work_dir, dt.datetime.strptime(prev_str, "%Y-%m-%d").date() - dt.timedelta(days=3), 60,
        )
        result = _run_verify(work_dir, edition_path, None, cache_dir, calendar_dir)
        check("27-2 S15/正例: --hypothesesなしでも正常終了する", result.returncode, 0)
        v = json.loads(edition_path.read_text(encoding="utf-8")).get("verification") or {}
    check("27-2 S15/正例: reported_relation_text_mismatchは0件・空で出る", v.get("reported_relation_text_mismatch"), {"count": 0, "hypothesis_ids": []})
    check("27-2 S15/正例: published_date_only_sourcesは紙面だけで決まるので7件", (v.get("published_date_only_sources") or {}).get("count"), 7)
    check(
        "27-2 S15/正例(第4回): published_date_not_foundも紙面と本文のキャッシュだけで決まるので、--hypothesesなしでも出る(2件)",
        ((v.get("published_date_not_found") or {}).get("count"), (v.get("published_date_not_found") or {}).get("source_ids")),
        (2, ["C11", "C13"]),
    )
    check("27-2 S15/正例: empty_title_or_url_refsは紙面だけで決まるのでL-10の1件", v.get("empty_title_or_url_refs"), {"count": 1, "line_ids": ["L-10"]})


def test_check_edition_slot():
    """改修27-2第2回(S14): 号のslotが実行時刻から決まる時間帯と違えば号を保存しない。
    境目はcompute_expected_slot()のとおり(5:00〜10:59朝・11:00〜15:59昼・16:00〜4:59夕方)。"""
    def result(slot, date_str, run_at_iso):
        try:
            ve.check_edition_slot({"slot": slot, "date": date_str}, dt.datetime.fromisoformat(run_at_iso))
            return "ok"
        except ve.EditionInvalid as e:
            return str(e)

    cases = [
        # (号の時間帯, 実行時刻, 通るか)
        ("evening", "2026-09-28T04:59:00+09:00", True),
        ("morning", "2026-09-28T04:59:00+09:00", False),
        ("morning", "2026-09-28T05:00:00+09:00", True),
        ("evening", "2026-09-28T05:00:00+09:00", False),
        ("morning", "2026-09-28T10:59:00+09:00", True),
        ("noon", "2026-09-28T10:59:00+09:00", False),
        ("noon", "2026-09-28T11:00:00+09:00", True),
        ("morning", "2026-09-28T11:00:00+09:00", False),
        ("noon", "2026-09-28T15:59:00+09:00", True),
        ("evening", "2026-09-28T15:59:00+09:00", False),
        ("evening", "2026-09-28T16:00:00+09:00", True),
        ("noon", "2026-09-28T16:00:00+09:00", False),
        ("evening", "2026-09-28T00:00:00+09:00", True),
        ("evening", "2026-09-28T23:59:00+09:00", True),
    ]
    for slot, run_at_iso, ok in cases:
        label = "通る" if ok else "号を保存しない"
        check(
            f"検査24(S14)/境目: {run_at_iso[11:16]}に実行した{slot}号は{label}",
            result(slot, "2026-09-28", run_at_iso) == "ok", ok,
        )
    check(
        "検査24(S14)/正例: 時間帯が想定外の値(null)の号は保存しない",
        result(None, "2026-09-28", "2026-09-28T18:00:00+09:00") == "ok", False,
    )
    check(
        "検査24(S14)/正例: UTCで渡した実行時刻も日本時間に直して判定する(UTC 09:00=JST 18:00で夕方)",
        result("evening", "2026-09-28", "2026-09-28T09:00:00+00:00"), "ok",
    )
    msg = result("morning", "2026-09-28", "2026-09-28T18:05:00+09:00")
    check(
        "検査24(S14)/エラーの文: 期待した日付・時間帯、号の日付・時間帯、実行時刻がすべて書かれる",
        all(part in msg for part in (
            "期待した日付: 2026-09-28", "期待した時間帯: evening",
            "号の日付: 2026-09-28", "号の時間帯: morning", "実行時刻: 2026-09-28T18:05:00+09:00",
        )), True,
    )
    msg2 = result("morning", "2026-09-28", "2026-09-29T02:10:00+09:00")
    check(
        "検査24(S14)/エラーの文: 0:00〜4:59に実行した場合、期待した日付は前日・時間帯はevening",
        ("期待した日付: 2026-09-28" in msg2, "期待した時間帯: evening" in msg2), (True, True),
    )
    try:
        ve.check_edition_date({"slot": "evening", "date": "2026-09-21"}, dt.datetime.fromisoformat("2026-09-21T00:12:00+09:00"))
        date_msg = ""
    except ve.EditionInvalid as e:
        date_msg = str(e)
    check(
        "検査24(日付)/エラーの文: 日付のずれで止まるときも、期待した日付・時間帯、号の日付・時間帯、実行時刻が書かれる",
        all(part in date_msg for part in (
            "期待した日付: 2026-09-20", "期待した時間帯: evening",
            "号の日付: 2026-09-21", "号の時間帯: evening", "実行時刻: 2026-09-21T00:12:00+09:00",
        )), True,
    )


def test_run_verification_stops_on_slot_mismatch():
    """改修27-2第2回(S14): 照合の本体全体で、時間帯がずれた号は保存しない(終了コード1、
    号・仮説のファイルは1文字も書き換えない)。時間帯が合っていれば保存され、
    slot_mismatchは偽で記録される(キーをそろえるため残している)。"""
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        edition_path, hyp_path, cache_dir, today_str, prev_str = _rebuild_canary_as_today(work_dir)
        calendar_dir = _write_temp_calendar(
            work_dir, dt.datetime.strptime(prev_str, "%Y-%m-%d").date() - dt.timedelta(days=3), 60,
        )
        before_edition = edition_path.read_text(encoding="utf-8")
        before_hyp = hyp_path.read_text(encoding="utf-8")

        # 夕方号を、同じ日の昼(13:00)に照合する → 時間帯が違うので止まる。
        noon_run = dt.datetime.fromisoformat(f"{today_str}T13:00:00+09:00")
        r = _run_verify(work_dir, edition_path, hyp_path, cache_dir, calendar_dir, run_at_dt=noon_run)
        check("検査24(S14)/統合・正例: 夕方号を13:00に照合すると保存しない(終了コード1)", r.returncode, 1)
        check(
            "検査24(S14)/統合・正例: 止まったときの表示に、期待した時間帯(noon)と号の時間帯(evening)が出る",
            ("期待した時間帯: noon" in r.stdout, "号の時間帯: evening" in r.stdout), (True, True),
        )
        check(
            "検査24(S14)/統合・正例: 止まったとき、号と仮説のファイルは1文字も書き換わらない",
            (edition_path.read_text(encoding="utf-8") == before_edition, hyp_path.read_text(encoding="utf-8") == before_hyp),
            (True, True),
        )

        # 夕方号を同じ日の16:00(夕方の時間帯の始まり)に照合する → 保存される。
        evening_run = dt.datetime.fromisoformat(f"{today_str}T16:00:00+09:00")
        r2 = _run_verify(work_dir, edition_path, hyp_path, cache_dir, calendar_dir, run_at_dt=evening_run)
        check("検査24(S14)/統合・負例: 夕方号を16:00に照合すると保存される(終了コード0)", r2.returncode, 0)
        v = json.loads(edition_path.read_text(encoding="utf-8")).get("verification") or {}
        check(
            "検査24(S14)/統合・負例: 保存された号のslot_mismatchは偽、slot_expectedはevening、run_atは渡した時刻",
            (v.get("slot_mismatch"), v.get("slot_expected"), v.get("run_at")),
            (False, "evening", evening_run.isoformat()),
        )


def test_run_verification_requires_run_at():
    """改修27-2第2回: 照合の本体は実行時刻を必須の引数として受け取る(省略したら現在時刻、
    という既定値を付けない)。コマンド(main)は引数を増やしていない。"""
    import inspect
    params = inspect.signature(ve.run_verification).parameters
    check(
        "改修27-2第2回/正例: run_verificationのrun_at_dtには既定値が無い(省略できない)",
        ("run_at_dt" in params, params["run_at_dt"].default is inspect.Parameter.empty), (True, True),
    )
    check(
        "改修27-2第2回/正例: main()は引数を受け取らない(時刻を渡す経路が無い)",
        len(inspect.signature(ve.main).parameters), 0,
    )
    source = inspect.getsource(ve.main)
    check(
        "改修27-2第2回/正例: main()は現在時刻(dt.datetime.now(JST))を取り、環境変数を読まない",
        ("dt.datetime.now(JST)" in source, "environ" in source, "getenv" in source), (True, False, False),
    )
    help_text = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "verify_edition.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    options = sorted(set(re.findall(r"--[a-z][a-z-]*", help_text)))
    check(
        "改修27-2第2回/正例: コマンドの引数は--edition・--hypotheses・--cache・--calendar(と--help)だけ",
        options, ["--cache", "--calendar", "--edition", "--help", "--hypotheses"],
    )


def _wait_if_near_slot_boundary(margin_seconds=90):
    """実行時刻が時間帯の境目(5:00・11:00・16:00)の直前margin_seconds秒以内なら、
    境目を5秒過ぎるまで待つ(号を作ってから照合するまでの間に時間帯が変わり、
    たまたま止まってしまうのを避けるため)。待った秒数を返す。"""
    now = dt.datetime.now(ve.JST)
    for boundary in (dt.time(5, 0), dt.time(11, 0), dt.time(16, 0)):
        target = dt.datetime.combine(now.date(), boundary, tzinfo=ve.JST)
        wait = (target - now).total_seconds()
        if 0 <= wait < margin_seconds:
            time.sleep(wait + 5)
            return wait + 5
    return 0


def test_cli_runs_with_current_time():
    """改修27-2第2回: コマンドとしての動作を確かめる1本だけのテスト。verify_edition.pyを
    別に起動する(実行時刻はスクリプト自身が現在時刻を取る)。号の日付と時間帯は、
    テストを実行した時刻から決める(0:00〜4:59は前日の夕方号)。終了コード0になる
    ことだけを確かめ、件数は確かめない(時間帯によって朝号の門限など結果が変わるため)。"""
    _wait_if_near_slot_boundary()
    now = dt.datetime.now(ve.JST)
    slot = ve.compute_expected_slot(now)
    date_str = ve.expected_date_for_run(now)
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        edition_path, hyp_path, cache_dir, today_str, prev_str = _rebuild_canary_as_today(work_dir)
        # 見本の号(夕方号として作られる)を、今の時間帯の号に作り直す。
        edition = json.loads(edition_path.read_text(encoding="utf-8"))
        hyp = json.loads(hyp_path.read_text(encoding="utf-8"))
        edition_id = f"{date_str}-{slot}"
        edition.update({"date": date_str, "slot": slot, "edition_id": edition_id})
        hyp["edition_id"] = edition_id
        edition_path.unlink()
        hyp_path.unlink()
        new_edition_path = work_dir / "editions" / date_str / f"{slot}.json"
        new_edition_path.parent.mkdir(parents=True, exist_ok=True)
        new_edition_path.write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")
        new_hyp_path = work_dir / "hypotheses" / f"{edition_id}.json"
        new_hyp_path.write_text(json.dumps(hyp, ensure_ascii=False, indent=1), encoding="utf-8")
        calendar_dir = _write_temp_calendar(
            work_dir, dt.datetime.strptime(date_str, "%Y-%m-%d").date() - dt.timedelta(days=5), 60,
        )
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "verify_edition.py"),
             "--edition", str(new_edition_path), "--hypotheses", str(new_hyp_path),
             "--cache", str(cache_dir), "--calendar", str(calendar_dir)],
            capture_output=True, text=True, encoding="utf-8", cwd=str(work_dir),
        )
        check(
            f"コマンド/正例: 実行した時刻の日付・時間帯({date_str}の{slot}号)で作った号は、"
            "verify_edition.pyを別に起動しても正常終了する(終了コード0)",
            result.returncode, 0,
        )
    _assert_testdata_untouched("コマンドのテスト")


def test_stale_sources_by_source_kind():
    """改修27-2第3回(S4): 検査10の「新しい」の条件を、出典の種類で分ける(changeの行だけ)。
      時刻付き: 実行時刻から36時間を超えたら古い(今までどおり)
      日付だけ: 号の日付と同じ日か前日(暦日)なら新しい。それより前・号の日付より後は古い
      null・読めない: 落とす(unknown_published_at_hits)"""
    def make(published_at, date_only, edition_date="2026-09-25", section_id="change", source_id="SRC-X"):
        source = {"source_id": source_id, "published_at": published_at}
        if date_only is not None:
            source["published_date_only"] = date_only
        edition = {
            "date": edition_date,
            "sections": [{"section_id": section_id, "articles": [{"lines": [{"line_id": "X-01", "source_ref": source_id}]}]}],
            "sources": [source],
        }
        return edition

    def run(published_at, date_only, run_at_iso, edition_date="2026-09-25", section_id="change", source_id="SRC-X"):
        edition = make(published_at, date_only, edition_date, section_id, source_id)
        stale, unknown, by_kind = ve.run_check_e_stale_sources(edition, dt.datetime.fromisoformat(run_at_iso))
        kept = len(edition["sections"][0]["articles"][0]["lines"])
        return kept, stale, unknown, by_kind

    fresh = (1, 0, 0, {"timed": 0, "date_only": 0})
    dropped_date = (0, 1, 0, {"timed": 0, "date_only": 1})

    # --- 依頼文S4の例 ---
    check("検査10/S4例1: 9/25の朝号(7:30に照合)で、日付だけの9/24の資料は新しい", run("2026-09-24", True, "2026-09-25T07:30:00+09:00"), fresh)
    check("検査10/S4例2: 9/25の夕方号(17:30に照合)で、日付だけの9/24の資料は新しい(36時間の規則なら41.5時間前で古くなる例)",
          run("2026-09-24", True, "2026-09-25T17:30:00+09:00"), fresh)
    check("検査10/S4例3: 9/25の号で、日付だけの9/23の資料は古い", run("2026-09-23", True, "2026-09-25T07:30:00+09:00"), dropped_date)
    check("検査10/S4例4: 9/23(休日)の号で、日付だけの9/18の資料は古い(営業日ではなく暦日で数える)",
          run("2026-09-18", True, "2026-09-23T07:30:00+09:00", edition_date="2026-09-23"), dropped_date)
    check("検査10/S4例5: 月曜(9/28)の朝号で、日付だけの金曜(9/25)の資料は古い(前日ではないため)",
          run("2026-09-25", True, "2026-09-28T07:30:00+09:00", edition_date="2026-09-28"), dropped_date)
    check("検査10/S4例5の対: 月曜(9/28)の朝号で、日付だけの日曜(9/27)の資料は新しい(前日のため)",
          run("2026-09-27", True, "2026-09-28T07:30:00+09:00", edition_date="2026-09-28"), fresh)

    # --- 号の日付を基準にし、実行時刻の日付は使わない ---
    check("検査10/S4: 0:00〜4:59に照合する夕方号(号の日付は前日9/28)で、前日(9/27)の資料は新しい"
          "(実行時刻の日付=9/29を基準にすると2日前で誤って古くなる例)",
          run("2026-09-27", True, "2026-09-29T02:00:00+09:00", edition_date="2026-09-28"), fresh)
    check("検査10/S4: 同じ号で、号の日付と同じ日(9/28)の資料も新しい",
          run("2026-09-28", True, "2026-09-29T02:00:00+09:00", edition_date="2026-09-28"), fresh)
    check("検査10/S4: 同じ号で、実行時刻の日付(9/29)は号の日付より後なので、日付だけの資料としては古い扱い(未来の日付)",
          run("2026-09-29", True, "2026-09-29T02:00:00+09:00", edition_date="2026-09-28"), dropped_date)
    check("検査10/S4: 実行時刻が号の日付から大きく遅れても(9/30に照合)、日付だけの資料の判定は号の日付(9/25)で行う",
          run("2026-09-24", True, "2026-09-30T07:30:00+09:00"), fresh)

    # --- 未来の日付 ---
    check("検査10/S4/未来: 号の日付と同じ日(9/25)の日付だけの資料は新しい", run("2026-09-25", True, "2026-09-25T07:30:00+09:00"), fresh)
    check("検査10/S4/未来: 号の日付より後(9/26)の日付だけの資料は落とす(古くない側に残さない)", run("2026-09-26", True, "2026-09-25T07:30:00+09:00"), dropped_date)
    check("検査10/S4/未来: 号の日付より1年後の日付だけの資料も落とす", run("2027-09-25", True, "2026-09-25T07:30:00+09:00"), dropped_date)

    # --- 書類一覧(前の営業日の一覧)---
    check("検査10/S4/書類一覧: 月曜(9/28)の号で、前の営業日(金曜9/25)の一覧(SRC-EDINET-LIST-PREV)を出典にしたchangeの行は落ちる(意図した動き)",
          run("2026-09-25", True, "2026-09-28T07:30:00+09:00", edition_date="2026-09-28", source_id="SRC-EDINET-LIST-PREV"), dropped_date)
    check("検査10/S4/書類一覧: 同じ号の当日の一覧(SRC-EDINET-LIST、9/28)は新しい",
          run("2026-09-28", True, "2026-09-28T07:30:00+09:00", edition_date="2026-09-28", source_id="SRC-EDINET-LIST"), fresh)
    check("検査10/S4/書類一覧: 火曜(9/29)の号の前の営業日(月曜9/28)の一覧は前日なので新しい",
          run("2026-09-28", True, "2026-09-29T07:30:00+09:00", edition_date="2026-09-29", source_id="SRC-EDINET-LIST-PREV"), fresh)
    check("検査10/S4/書類一覧: 連休明け(9/24)の号で、連休前(9/18)の一覧は古い",
          run("2026-09-18", True, "2026-09-24T07:30:00+09:00", edition_date="2026-09-24", source_id="SRC-EDINET-LIST-PREV"), dropped_date)

    # --- 時刻付き(今までどおり36時間)---
    check("検査10/S4/時刻付き: 36時間以内は新しい", run("2026-09-24T08:00:00+09:00", False, "2026-09-25T17:30:00+09:00"), fresh)
    check("検査10/S4/時刻付き: 36時間超は古い(内訳は時刻付き)", run("2026-09-23T17:59:00+09:00", False, "2026-09-25T07:30:00+09:00"),
          (0, 1, 0, {"timed": 1, "date_only": 0}))
    check("検査10/S4/時刻付き: 36時間ちょうど(境目)は新しい側に残る", run("2026-09-25T17:59:00+09:00", False, "2026-09-27T05:59:00+09:00", edition_date="2026-09-27"), fresh)
    check("検査10/S4/時刻付き: 36時間1分超過は古い", run("2026-09-25T17:59:00+09:00", False, "2026-09-27T06:00:00+09:00", edition_date="2026-09-27"),
          (0, 1, 0, {"timed": 1, "date_only": 0}))
    check("検査10/S4/時刻付き: published_date_onlyのキーが無い出典は時刻付きとして扱う(36時間)", run("2026-09-24T08:00:00+09:00", None, "2026-09-25T17:30:00+09:00"), fresh)
    check("検査10/S4/時刻付き: 号の日付が読めなくても時刻付きの判定は影響を受けない", run("2026-09-24T08:00:00+09:00", False, "2026-09-25T17:30:00+09:00", edition_date=None), fresh)

    # --- null・読めない ---
    check("検査10/S4/null: 時刻付きの型でpublished_atがnullなら落とし、unknown_published_at_hitsに数える(staleには数えない)",
          run(None, False, "2026-09-25T07:30:00+09:00"), (0, 0, 1, {"timed": 0, "date_only": 0}))
    check("検査10/S4/null: 読めない文字列も同じ", run("不明", False, "2026-09-25T07:30:00+09:00"), (0, 0, 1, {"timed": 0, "date_only": 0}))
    check("検査10/S4/null: published_date_onlyが真なのにpublished_atが日付だけの形でない(矛盾)場合も、読めない扱いで落とす",
          run("2026-09-24T08:00:00+09:00", True, "2026-09-25T07:30:00+09:00"), (0, 0, 1, {"timed": 0, "date_only": 0}))
    check("検査10/S4/null: 実在しない日付(2026-13-45)は読めない扱い", run("2026-13-45", True, "2026-09-25T07:30:00+09:00"), (0, 0, 1, {"timed": 0, "date_only": 0}))

    # --- 号の日付が読めない ---
    check("検査10/S4/号の日付不明: 日付だけの出典は判定できないので落とす(通常は号ごと止まるため到達しない)",
          run("2026-09-24", True, "2026-09-25T07:30:00+09:00", edition_date=None), dropped_date)

    # --- change以外の枠は対象外 ---
    for section_id in ("big", "ripple", "deep"):
        check(f"検査10/S4/枠: {section_id}の行は、日付だけの古い資料(9/18)でも落とさない",
              run("2026-09-18", True, "2026-09-25T07:30:00+09:00", section_id=section_id), fresh)
        check(f"検査10/S4/枠: {section_id}の行は、時刻付きの古い資料でも落とさない",
              run("2026-09-01T08:00:00+09:00", False, "2026-09-25T07:30:00+09:00", section_id=section_id), fresh)

    # --- 出典が見つからない行・出典を持たない行は今までどおり残す ---
    edition = make("2026-09-01", True)
    edition["sections"][0]["articles"][0]["lines"] = [{"line_id": "X-01", "source_ref": "SRC-NONE"}, {"line_id": "X-02"}]
    ve.run_check_e_stale_sources(edition, dt.datetime.fromisoformat("2026-09-25T07:30:00+09:00"))
    check("検査10/S4/出典なし: 出典が見つからない行・出典を持たない行は残る", len(edition["sections"][0]["articles"][0]["lines"]), 2)

    # --- 内訳と合計 ---
    mixed = {
        "date": "2026-09-25",
        "sections": [{"section_id": "change", "articles": [{"lines": [
            {"line_id": "A", "source_ref": "T-OLD"}, {"line_id": "B", "source_ref": "D-OLD"},
            {"line_id": "C", "source_ref": "D-FUTURE"}, {"line_id": "D", "source_ref": "T-NEW"},
            {"line_id": "E", "source_ref": "D-NEW"}, {"line_id": "F", "source_ref": "NULL"},
            {"line_id": "G", "source_ref": "T-OLD"},
        ]}]}],
        "sources": [
            {"source_id": "T-OLD", "published_at": "2026-09-20T08:00:00+09:00", "published_date_only": False},
            {"source_id": "D-OLD", "published_at": "2026-09-20", "published_date_only": True},
            {"source_id": "D-FUTURE", "published_at": "2026-09-27", "published_date_only": True},
            {"source_id": "T-NEW", "published_at": "2026-09-25T06:00:00+09:00", "published_date_only": False},
            {"source_id": "D-NEW", "published_at": "2026-09-24", "published_date_only": True},
            {"source_id": "NULL", "published_at": None, "published_date_only": False},
        ],
    }
    stale, unknown, by_kind = ve.run_check_e_stale_sources(mixed, dt.datetime.fromisoformat("2026-09-25T07:30:00+09:00"))
    check("検査10/S4/内訳: 古い行の合計は4(時刻付き2+日付だけ2)で、内訳はtimed=2・date_only=2", (stale, by_kind), (4, {"timed": 2, "date_only": 2}))
    check("検査10/S4/内訳: 合計は内訳の和と一致し、nullの1行はunknown_published_atに別に数える", (stale == sum(by_kind.values()), unknown), (True, 1))
    check("検査10/S4/内訳: 残る行は新しい時刻付き(D)と新しい日付だけ(E)の2行",
          [l["line_id"] for l in mixed["sections"][0]["articles"][0]["lines"]], ["D", "E"])


def test_prev_edinet_list_dropped_from_change_on_monday():
    """改修27-2第3回(S4)・計画第7節: 月曜・連休明けの号では、前の営業日の書類一覧
    (SRC-EDINET-LIST-PREV)は日付だけの資料としては「前日」ではなくなるため、
    それを出典にしたchangeの行は落ちる(意図した動き)。書類一覧のキャッシュから
    published_atが書かれ(apply_edinet_source_published_at)、published_date_onlyが
    書かれ(apply_published_date_only)、検査10で落ちるまでの流れを通して確かめる。"""
    def build(date_str, prev_str):
        edition = {
            "date": date_str,
            "sections": [{"section_id": "change", "articles": [{"lines": [
                {"line_id": "L-T", "source_ref": "SRC-EDINET-LIST"},
                {"line_id": "L-P", "source_ref": "SRC-EDINET-LIST-PREV"},
            ]}]}],
            "sources": [
                {"source_id": "SRC-EDINET-LIST", "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=" + date_str + "&type=2"},
                {"source_id": "SRC-EDINET-LIST-PREV", "url": "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=" + prev_str + "&type=2"},
            ],
        }
        return edition

    def flow(date_str, prev_str):
        edition = build(date_str, prev_str)
        with tempfile.TemporaryDirectory() as d:
            for source_id, day in (("SRC-EDINET-LIST", date_str), ("SRC-EDINET-LIST-PREV", prev_str)):
                write(d, f"{source_id}.json", json.dumps({"metadata": {"parameter": {"date": day}}}))
            ve.apply_edinet_source_published_at(edition, d, [])
        ve.apply_published_date_only(edition)
        stale, unknown, by_kind = ve.run_check_e_stale_sources(edition, dt.datetime.fromisoformat(f"{date_str}T07:30:00+09:00"))
        return [l["line_id"] for l in edition["sections"][0]["articles"][0]["lines"]], stale, by_kind

    check("検査10/S4/月曜: 月曜(9/28)の号は、前の営業日(金曜9/25)の一覧を出典にした行(L-P)が落ち、当日の一覧の行(L-T)は残る",
          flow("2026-09-28", "2026-09-25"), (["L-T"], 1, {"timed": 0, "date_only": 1}))
    check("検査10/S4/連休明け: 連休明け(9/24)の号は、連休前(9/18)の一覧を出典にした行が落ちる",
          flow("2026-09-24", "2026-09-18"), (["L-T"], 1, {"timed": 0, "date_only": 1}))
    check("検査10/S4/平日: 火曜(9/29)の号は、前の営業日(月曜9/28)の一覧を出典にした行も残る(前日のため)",
          flow("2026-09-29", "2026-09-28"), (["L-T", "L-P"], 0, {"timed": 0, "date_only": 0}))


def test_date_found_in_text():
    """改修27-2第4回: 本文の日付探し(英語の書き方の追加と、別の日に一致しないための規則)。"""
    def found(text, year, month, day):
        return ve.date_found_in_text(text, ve.published_at_candidates(year, month, day))

    # --- 日本語・数字の書き方(今までどおり) ---
    for label, text in [
        ("ISO", "発表日は2026-09-18です"), ("スラッシュ(ゼロ埋め)", "2026/09/18に発表"),
        ("スラッシュ(ゼロ埋めなし)", "2026/9/18に発表"), ("漢字", "2026年9月18日に発表"),
        ("令和", "令和8年9月18日に発表"), ("年なし", "9月18日に発表"),
    ]:
        check(f"日付探し/日本語・数字({label}): 見つかる", found(text, 2026, 9, 18), True)

    # --- 全角数字はNFKCで拾える ---
    for label, text in [
        ("全角の漢字表記", "２０２６年９月１８日に発表"), ("全角のスラッシュ", "２０２６／９／１８に発表"),
        ("全角のISO", "２０２６-０９-１８に発表"), ("全角の令和", "令和８年９月１８日に発表"),
        ("全角の年なし", "９月１８日に発表"), ("全角の英語", "Ｓｅｐｔｅｍｂｅｒ　１８，　２０２６"),
    ]:
        check(f"日付探し/全角({label}): 見つかる", found(text, 2026, 9, 18), True)

    # --- 英語 ---
    for label, text in [
        ("正式名", "Published on September 18, 2026."), ("Sept.", "Published Sept. 18, 2026"),
        ("Sep.", "Published Sep. 18, 2026"), ("Sep(ピリオドなし)", "Published Sep 18, 2026"),
        ("Sept(ピリオドなし)", "Published Sept 18, 2026"),
        ("大文字", "PUBLISHED SEPTEMBER 18, 2026"), ("小文字", "published september 18, 2026"),
        ("大文字小文字が混ざる", "SePt. 18, 2026"), ("空白が特殊(改行・不可分空白)", "September\n18, 2026"),
        ("時刻が続く", "September 18, 2026, 3:00 p.m. ET"), ("時刻が続く(Sep.)", "Sep. 18, 2026, 15:00"),
    ]:
        check(f"日付探し/英語({label}): 見つかる", found(text, 2026, 9, 18), True)
    check("日付探し/英語(日が1桁): September 1, 2026 が見つかる", found("On September 1, 2026, the Board", 2026, 9, 1), True)
    check("日付探し/英語(他の月): May 5, 2026・Jun. 5, 2026・Dec. 25, 2026 が見つかる",
          (found("May 5, 2026", 2026, 5, 5), found("Jun. 5, 2026", 2026, 6, 5), found("Dec. 25, 2026", 2026, 12, 25)),
          (True, True, True))
    check("日付探し/英語(負例): 違う日・違う月・違う年は見つからない",
          (found("September 19, 2026", 2026, 9, 18), found("October 18, 2026", 2026, 9, 18), found("September 18, 2025", 2026, 9, 18)),
          (False, False, False))
    check("日付探し/英語(負例): 9月の略記Sept.は10月には使われない(10月18日の資料に Sep. 18, 2026 は一致しない)",
          found("Sep. 18, 2026", 2026, 10, 18), False)

    # --- 別の日に一致してしまう穴(ゼロ埋めしない書き方)---
    check("日付探し/別の日の穴: 2026/9/1 は 2026/9/12 に一致しない", found("2026/9/12に発表", 2026, 9, 1), False)
    check("日付探し/別の日の穴: 9月1日 は 9月12日 に一致しない", found("9月12日に発表", 2026, 9, 1), False)
    check("日付探し/別の日の穴: September 1, 2026 は September 12, 2026 に一致しない", found("September 12, 2026", 2026, 9, 1), False)
    check("日付探し/別の日の穴: 直前が数字なら一致しない(12026/9/1 の中の 2026/9/1)", found("12026/9/1", 2026, 9, 1), False)
    check("日付探し/別の日の穴: 12月1日 の中の 2月1日 に一致しない(直前が数字)", found("12月1日に発表", 2026, 2, 1), False)
    check("日付探し/別の日の穴: 直後が数字(2026-09-180)なら一致しない", found("2026-09-180", 2026, 9, 18), False)
    check("日付探し/別の日の穴: 直前が数字(12026-09-18)なら一致しない", found("12026-09-18", 2026, 9, 18), False)
    check("日付探し/別の日の穴: 年の後ろに数字が続く(September 1, 20261)は一致しない", found("September 1, 20261", 2026, 9, 1), False)
    check("日付探し/別の日の穴: 2026/9/1 そのもの(直後が文字・記号・文末)には一致する",
          (found("2026/9/1に発表", 2026, 9, 1), found("2026/9/1、続報", 2026, 9, 1), found("2026/9/1", 2026, 9, 1)), (True, True, True))
    check("日付探し/別の日の穴: 別の日が先に出ても、正しい日が別の場所にあれば見つかる",
          found("2026/9/12 と 2026/9/1 の両方", 2026, 9, 1), True)
    check("日付探し/別の日の穴: 「日」で終わる書き方は直後に数字が続いても見つかる(9月18日15時30分・2026年9月18日15:00)",
          (found("9月18日15時30分に発表", 2026, 9, 18), found("2026年9月18日15:00", 2026, 9, 18)), (True, True))
    check("日付探し/別の日の穴: 9月1日 は 9月18日 に一致しない", found("9月18日", 2026, 9, 1), False)
    check("日付探し/負例: 日付がまったく無い本文・空の本文・Noneは見つからない",
          (found("日付の無い本文", 2026, 9, 18), found("", 2026, 9, 18), found(None, 2026, 9, 18)), (False, False, False))


def test_published_date_only_required():
    """改修27-2第4回(S2・S3): 日付だけの出典は、本文に日付が見つからなければ日付不明とし、
    その出典を参照するchangeの行を落とす(検査10より前)。"""
    def line(line_id, ref):
        return {"line_id": line_id, "source_ref": ref}

    def make(sources, sections):
        return {"date": "2026-09-25", "sources": sources, "sections": sections}

    def src(source_id, published_at="2026-09-24", date_only=True):
        return {"source_id": source_id, "url": f"https://example.test/{source_id}", "published_at": published_at, "published_date_only": date_only}

    def change_only(lines):
        return [{"section_id": "change", "articles": [{"lines": lines}]}]

    def kept_ids(edition, index=0):
        return [l["line_id"] for l in edition["sections"][index]["articles"][0]["lines"]]

    with tempfile.TemporaryDirectory() as d:
        write(d, "S-JP.txt", "本文。発表日は2026年9月24日。".encode("utf-8"))
        write(d, "S-FW.txt", "本文。発表日は２０２６年９月２４日。".encode("utf-8"))
        write(d, "S-EN.txt", "Body. Published Sept. 24, 2026.".encode("utf-8"))
        write(d, "S-NONE.txt", "本文。発表日はどこにも書かれていない。".encode("utf-8"))
        write(d, "S-WRONGDAY.txt", "本文。発表日は2026/9/240。別の日は2026年9月2日。".encode("utf-8"))
        write(d, "S-BROKEN.txt", b"\x83\xff\x00\x81")
        write(d, "SRC-EDINET-LIST.txt", "日付なし".encode("utf-8"))
        write(d, "SRC-EDINET-LIST-PREV.txt", "日付なし".encode("utf-8"))
        write(d, "T-NONE.txt", "日付なし".encode("utf-8"))

        # --- 本文に日付がある(日本語・全角・英語)→ changeの行は残る ---
        for sid, label in (("S-JP", "日本語"), ("S-FW", "全角"), ("S-EN", "英語")):
            e = make([src(sid)], change_only([line("L-1", sid)]))
            r = ve.run_check_published_date_only_required(e, d)
            check(f"検査36必須/正例({label}): 本文に日付があれば、changeの行は残り、日付不明にならない",
                  (kept_ids(e), r["count"], r["dropped_line_ids"]), (["L-1"], 0, []))

        # --- 本文に日付が無い → changeの行は落ち、記録される ---
        e = make([src("S-NONE")], change_only([line("L-1", "S-NONE"), line("L-2", "S-NONE")]))
        r = ve.run_check_published_date_only_required(e, d)
        check("検査36必須/正例: 本文に日付が無ければ、その出典を参照するchangeの行は(2行とも)落ちる", kept_ids(e), [])
        check("検査36必須/正例: 記録される(出典1件・理由not_in_body・落とした行2つ)", r,
              {"count": 1, "source_ids": ["S-NONE"], "reasons": {"S-NONE": "not_in_body"}, "dropped_line_ids": ["L-1", "L-2"]})

        # --- 別の日の書き方が本文にあっても通らない(ゼロ埋めしない書き方の穴)---
        e = make([src("S-WRONGDAY", published_at="2026-09-02")], change_only([line("L-1", "S-WRONGDAY")]))
        e2 = make([src("S-WRONGDAY", published_at="2026-09-24")], change_only([line("L-1", "S-WRONGDAY")]))
        r = ve.run_check_published_date_only_required(e, d)
        r2 = ve.run_check_published_date_only_required(e2, d)
        check("検査36必須/穴: 本文の「2026/9/240」は 2026-09-24 ではない(直後が数字)ため、9/24の資料は通らない",
              (kept_ids(e2), r2["reasons"]), ([], {"S-WRONGDAY": "not_in_body"}))
        check("検査36必須/穴: 本文に「2026年9月2日」がある9/2の資料は通る", (kept_ids(e), r["count"]), (["L-1"], 0))

        # --- 本文のファイルが無い・読めない → 落ち、理由を分ける ---
        e = make([src("S-MISSING"), src("S-BROKEN")], change_only([line("L-1", "S-MISSING"), line("L-2", "S-BROKEN"), line("L-3", "S-NO-SUCH-SOURCE")]))
        r = ve.run_check_published_date_only_required(e, d)
        check("検査36必須/正例: 本文のファイルが無い出典はbody_missing、文字コードで読めない出典はbody_unreadable",
              r["reasons"], {"S-MISSING": "body_missing", "S-BROKEN": "body_unreadable"})
        check("検査36必須/正例: どちらもchangeの行は落ちる(出典が見つからない行L-3は今までどおり残る)", kept_ids(e), ["L-3"])

        # --- change以外の枠は落とさない ---
        sections = [{"section_id": sid, "articles": [{"lines": [line(f"L-{sid}", "S-NONE")]}]} for sid in ("change", "big", "ripple", "deep")]
        e = make([src("S-NONE")], sections)
        r = ve.run_check_published_date_only_required(e, d)
        check("検査36必須/枠: 日付不明でも、big・ripple・deepの行は落とさない(changeだけ落とす)",
              [kept_ids(e, i) for i in range(4)], [[], ["L-big"], ["L-ripple"], ["L-deep"]])
        check("検査36必須/枠: 記録の落とした行はchangeの1行だけ。出典は日付不明として1件数える",
              (r["count"], r["dropped_line_ids"]), (1, ["L-change"]))

        # --- 書類一覧の2つは対象外 ---
        e = make([src("SRC-EDINET-LIST"), src("SRC-EDINET-LIST-PREV")],
                 change_only([line("L-1", "SRC-EDINET-LIST"), line("L-2", "SRC-EDINET-LIST-PREV")]))
        r = ve.run_check_published_date_only_required(e, d)
        check("検査36必須/対象外: 書類一覧の2つは、本文に日付が無くても日付不明にならず、行も落とさない",
              (kept_ids(e), r["count"]), (["L-1", "L-2"], 0))
        e = make([src("SRC-EDINET-LIST"), src("SRC-EDINET-LIST-PREV")], change_only([line("L-1", "SRC-EDINET-LIST")]))
        check("検査36必須/対象外: 書類一覧の本文ファイルが無くても落ちない(body_missingにもしない)",
              ve.run_check_published_date_only_required(e, str(Path(d) / "no-such-dir"))["count"], 0)

        # --- 時刻付きは記録だけ ---
        e = make([src("T-NONE", published_at="2026-09-24T10:00:00+09:00", date_only=False)], change_only([line("L-1", "T-NONE")]))
        r = ve.run_check_published_date_only_required(e, d)
        hits, srcs = ve.run_check_published_at(e, d)
        check("検査36必須/時刻付き: 時刻付きの出典は本文に日付が無くても行を落とさない(この関数の対象外)",
              (kept_ids(e), r["count"]), (["L-1"], 0))
        check("検査36必須/時刻付き: 時刻付きは今までどおり published_at_unverified_hits に数えられる(記録だけ)",
              (hits, srcs), (1, ["T-NONE"]))

        # --- 印(published_date_only)が無い出典は、この関数の対象外(印は機械が書く)---
        e = make([{"source_id": "S-NONE", "published_at": "2026-09-24"}], change_only([line("L-1", "S-NONE")]))
        check("検査36必須/印なし: published_date_onlyが真でない出典は対象外",
              (ve.run_check_published_date_only_required(e, d)["count"], kept_ids(e)), (0, ["L-1"]))

        # --- 順番(S3): 日付不明で落ちた行は検査10に届かず、二重に数えない ---
        old = "2026-09-01"  # 号の日付(9/25)の前々日以前 → 日付が本文にあっても検査10では古い
        write(d, "S-OLD-FOUND.txt", "本文。発表日は2026-09-01。".encode("utf-8"))
        write(d, "S-OLD-NONE.txt", "本文。日付なし。".encode("utf-8"))
        e = make([src("S-OLD-FOUND", published_at=old), src("S-OLD-NONE", published_at=old)],
                 change_only([line("L-found", "S-OLD-FOUND"), line("L-none", "S-OLD-NONE")]))
        r = ve.run_check_published_date_only_required(e, d)
        check("検査36必須/順番: 本文に日付が無い古い出典(L-none)は検査36で落ちる",
              (r["dropped_line_ids"], r["source_ids"]), (["L-none"], ["S-OLD-NONE"]))
        stale, unknown, by_kind = ve.run_check_e_stale_sources(e, dt.datetime.fromisoformat("2026-09-25T07:30:00+09:00"))
        check("検査36必須/順番: 検査10に届くのは残ったL-foundだけ。古い行は1・読めない行は0(L-noneは二重に数えない)",
              (stale, unknown, by_kind, kept_ids(e)), (1, 0, {"timed": 0, "date_only": 1}, []))


def test_canary_edition():
    """改修27-1(4-15): 見本の号(scripts/testdata/canary/)を、実際にverify_edition.pyの
    CLI全体に通して確かめる。第7.1版どおり10個の値(generated_at・baseline_late・
    attribution・processing_note・impact_kind・evidence_filer_name・evidence_doc_type・
    horizon_business_days・deadline_date・EDINETのpublished_at)がnullでも、上段の
    会社4社・本文の行が消えないこと、記録専用のキーが期待どおりの値になることを
    確かめる。

    期待値は、この見本の号を実際に1回実行して出た値をそのまま使っている(手計算の
    値ではない。以前の版で「出典8」と手計算して9との食い違いに気づけなかった
    反省から、必ず実行して確かめた値を使う)。日付そのもの(deadline_dateの絶対値等)
    は実行日によって変わるため、その場でcompute_deadline()を使って求め直す。"""
    with tempfile.TemporaryDirectory() as d:
        work_dir = Path(d)
        edition_path, hyp_path, cache_dir, today_str, prev_str = _rebuild_canary_as_today(work_dir)
        calendar_dir = _write_temp_calendar(
            work_dir, dt.datetime.strptime(prev_str, "%Y-%m-%d").date() - dt.timedelta(days=3), 60,
        )

        result = _run_verify(work_dir, edition_path, hyp_path, cache_dir, calendar_dir)
        check("見本の号/正例: 正常終了する(終了コード0)", result.returncode, 0)

        after_edition = json.loads(edition_path.read_text(encoding="utf-8"))
        after_hyp = json.loads(hyp_path.read_text(encoding="utf-8"))
        v = after_edition.get("verification") or {}
        business_days = ve.load_business_days(str(calendar_dir))

        # --- 出典の数(第5回で必ず直すこと1: 9つに揃える) ---
        check("見本の号/正例: 出典は15(書類一覧2+C01〜C13。27-2第1回でC08・C09、第3回でC10、第4回でC11〜C13を足した)", len(after_edition.get("sources") or []), 15)

        # --- 会社・行が消えないこと ---
        check("見本の号/正例: 上段の会社は4社とも残る", len(after_hyp.get("hypotheses") or []), 4)
        check(
            "見本の号/正例: hypothesis_violationsは0(検査で削除された会社は無い)",
            v.get("hypothesis_violations"), 0,
        )
        check(
            "見本の号/正例: 検査時点の本文の行は16行(27-2第1回でL-09・L-10、第3回でL-11・L-12、第4回でL-13〜L-16を足した。"
            "L-12・L-13・L-15は検査36・検査10で落ちるが、行の数は落とす前に数える)",
            v.get("lines_total"), 16,
        )
        check("見本の号/正例: 出典と数字が一致した行は4件", v.get("passed"), 4)

        # --- 10個のnullが機械で埋まること ---
        check("見本の号/正例: 紙面のgenerated_atがnullから実行時刻に上書きされる", after_edition.get("generated_at") is not None, True)
        check("見本の号/正例: baseline_lateがnullから真偽値に上書きされる", after_edition.get("baseline_late"), False)
        by_source = {s["source_id"]: s for s in after_edition["sources"]}
        check(
            "見本の号/正例: EDINETの出典(C01)のattribution・processing_noteがnullから機械の値に埋まる",
            (by_source["C01"].get("attribution") is not None, by_source["C01"].get("processing_note") is not None),
            (True, True),
        )
        check(
            "見本の号/正例: EDINETの個々の書類(C01)のpublished_atがnullからsubmitDateTimeの値に埋まる",
            by_source["C01"].get("published_at") is not None, True,
        )
        by_hyp = {h["hypothesis_id"]: h for h in after_hyp["hypotheses"]}
        check(
            "見本の号/正例: H-1のimpact_kind・evidence_filer_name・evidence_doc_typeがnullから機械の値に埋まる",
            (by_hyp["H-1"].get("impact_kind"), by_hyp["H-1"].get("evidence_filer_name") is not None, by_hyp["H-1"].get("evidence_doc_type") is not None),
            ("price_stated", True, True),
        )
        expected_deadline_h1 = ve.compute_deadline(business_days, today_str, 5)
        check(
            "見本の号/正例: H-1(price_stated)のhorizon_business_days・deadline_dateがnullから5営業日後に埋まる",
            (by_hyp["H-1"].get("horizon_business_days"), by_hyp["H-1"].get("deadline_date")),
            (5, expected_deadline_h1),
        )
        expected_deadline_h2 = ve.compute_deadline(business_days, today_str, 20)
        check(
            "見本の号/正例: H-2(amount_stated)のhorizon_business_days・deadline_dateがnullから20営業日後に埋まる",
            (by_hyp["H-2"].get("horizon_business_days"), by_hyp["H-2"].get("deadline_date")),
            (20, expected_deadline_h2),
        )

        # --- falsifierの扱い: 上段には書かない、推論欄には書く ---
        check(
            "見本の号/正例: 上段の会社にfalsifierキーが無くても4社とも残る(決定1)",
            all("falsifier" not in h for h in after_hyp["hypotheses"]), True,
        )
        check(
            "見本の号/正例: falsifierを書いた推論は残り、書かなかった推論は削除される(1件)",
            v.get("inference_dropped"), 1,
        )
        kept_inferences = after_edition["sections"][0]["articles"][0]["inferences"]
        check(
            "見本の号/正例: 残った推論にはfalsifierが入っている",
            (len(kept_inferences), kept_inferences[0].get("falsifier") is not None),
            (1, True),
        )

        # --- 4-4: 前日の一覧にしか無い会社(H-3)が引き当てられる ---
        check("見本の号/正例: 前日の一覧にしかいないH-3も見つかる(edinet_doclist_partialは空)", v.get("edinet_doclist_partial"), [])
        check("見本の号/正例: H-3のevidence_filer_nameが前日の一覧から埋まる", by_hyp["H-3"].get("evidence_filer_name"), "カナリア化学株式会社")

        # --- 4-10: 提出者本人の公開買付届出書(H-1)はbidder ---
        check("見本の号/正例: H-1のtob_sideはbidder", by_hyp["H-1"].get("tob_side"), "bidder")

        # --- 4-9: タグ入りのEDINET本文でも検査1(数字・抜き出し)が通る ---
        lines_by_id = {
            l["line_id"]: l
            for s in after_edition["sections"] for a in s["articles"] for l in a["lines"]
        }
        check(
            "見本の号/正例: EDINETのタグ入り本文を参照する行(L-01・L-03・L-07・L-08)は4件ともsource_number_match",
            [lines_by_id[lid]["mark"] for lid in ("L-01", "L-03", "L-07", "L-08")],
            ["source_number_match"] * 4,
        )

        # --- 4-8: どのファイルを本文に選んだかが記録される ---
        check(
            "見本の号/正例: edinet_doc_filesにC01〜C04の4件が記録される",
            sorted((v.get("edinet_doc_files") or {}).keys()), ["C01", "C02", "C03", "C04"],
        )

        # --- 4-6: attribution_overwritten(第5回で必ず直すこと2: 出典・行の両方を
        #     合わせた第4回の数え方で計算し直した値) ---
        check(
            "見本の号/正例: attribution_overwrittenは28(出典14件+source_refを持つ行14件。題名が空のC09とL-10はひな形が作れず、nullのままなので数えない)",
            v.get("attribution_overwritten"), 28,
        )
        check(
            "見本の号/正例: attribution_generation_skippedは2(題名が空のC09と、それを参照するL-10)",
            v.get("attribution_generation_skipped"), 2,
        )

        # --- 4-12: 記録専用のキー ---
        check(
            "見本の号/正例: date_only_number_linesはL-02の1件",
            v.get("date_only_number_lines"), {"count": 1, "line_ids": ["L-02"]},
        )
        check(
            "見本の号/正例: self_declared_unverifiedはL-04の1件",
            v.get("self_declared_unverified"), {"count": 1, "line_ids": ["L-04"]},
        )
        banned_hits = v.get("banned_word_hits") or []
        check(
            "見本の号/正例: banned_word_hitsに見出し以外(行・推論欄・仮説)の3件が入る",
            sorted((h["word"], h["location"], h["id"]) for h in banned_hits),
            sorted([("注目", "line_text", "L-04"), ("主要", "line_text", "L-06"), ("有力", "hypothesis_relation_text", "H-4")]),
        )
        check(
            "見本の号/正例: speculative_word_countsにH-4の「見込まれる」「意識される」が数えられる",
            (v["speculative_word_counts"]["relation_text"]["見込まれる"], v["speculative_word_counts"]["impact_reason"]["意識される"]),
            (1, 1),
        )
        check(
            "見本の号/正例: change_verified_lines_by_sectionはchange枠2件・big枠2件",
            v.get("change_verified_lines_by_section"), {"change": 2, "big": 2},
        )

        # --- 改修27-2第1回: S1・S12・S13 ---
        check(
            "見本の号/正例(27-2 S1): published_date_onlyの出典は書類一覧2つと、日付だけを書いたC08・C10・C11・C12・C13の7件",
            v.get("published_date_only_sources"),
            {"count": 7, "source_ids": ["SRC-EDINET-LIST", "SRC-EDINET-LIST-PREV", "C08", "C10", "C11", "C12", "C13"]},
        )
        check(
            "見本の号/正例(27-2 S1): AIがpublished_date_only=falseと書いたC08(日付だけ)は機械が真に上書きする",
            by_source["C08"].get("published_date_only"), True,
        )
        check(
            "見本の号/負例(27-2 S1): 時刻付きのC05は偽になる",
            by_source["C05"].get("published_date_only"), False,
        )
        check(
            "見本の号/正例(27-2 S12): 題名が空のC09を参照するL-10だけが記録され、印はunverifiedになる",
            (v.get("empty_title_or_url_refs"), lines_by_id["L-10"]["mark"], lines_by_id["L-10"]["mark_reason"]),
            ({"count": 1, "line_ids": ["L-10"]}, "unverified", "empty_title_or_url"),
        )
        check(
            "見本の号/負例(27-2 S1・S12): 日付だけの出典C08を参照するL-09は印を変えない(reported_unverifiedのまま)",
            lines_by_id["L-09"]["mark"], "reported_unverified",
        )
        check(
            "見本の号/正例(27-2 S13): reportedの上段4社は、いずれも定型文ではないため4件とも記録され、会社は消えない",
            (v.get("reported_relation_text_mismatch"), len(after_hyp["hypotheses"])),
            ({"count": 4, "hypothesis_ids": ["H-1", "H-2", "H-3", "H-4"]}, 4),
        )

        # --- 改修27-2第3回: S4(検査10を出典の種類で分ける) ---
        change_line_ids = [l["line_id"] for l in after_edition["sections"][0]["articles"][0]["lines"]]
        check(
            "見本の号/正例(27-2 S4): changeの枠で、日付だけで前日のC08を参照するL-11は新しいので残り、"
            "日付だけで古い(号の日付の18日前)C10を参照するL-12は落ちる",
            ("L-11" in change_line_ids, "L-12" in change_line_ids), (True, False),
        )
        check(
            "見本の号/正例(27-2 S4): 落ちたのはL-12の1行(stale_source_hits=1)で、内訳は日付だけが1・時刻付きが0",
            (v.get("stale_source_hits"), v.get("stale_source_hits_by_kind")),
            (1, {"timed": 0, "date_only": 1}),
        )
        check(
            "見本の号/負例(27-2 S4): 時刻付き(C02・C03・C05・C07)のchangeの行はそのまま残る(L-01〜L-04)",
            [lid for lid in change_line_ids if lid in ("L-01", "L-02", "L-03", "L-04")], ["L-01", "L-02", "L-03", "L-04"],
        )
        check(
            "見本の号/負例(27-2 S4): 公表時刻が読めず落ちた行は0(unknown_published_at_hits)",
            v.get("unknown_published_at_hits"), 0,
        )

        # --- 改修27-2第4回: S2・S3(検査36を必須にし、検査10より先に行う) ---
        check(
            "見本の号/正例(27-2 S2): 日付だけの出典で、本文に日付が無いC11(L-13)と、本文のファイルが無いC13(L-15)は"
            "日付不明になり、changeの枠のその行が落ちる。理由はC11=not_in_body・C13=body_missing",
            v.get("published_date_not_found"),
            {"count": 2, "source_ids": ["C11", "C13"], "reasons": {"C11": "not_in_body", "C13": "body_missing"},
             "dropped_line_ids": ["L-13", "L-15"]},
        )
        check(
            "見本の号/負例(27-2 S2): 本文に日本語の全角の日付があるC08(L-11)と、英語の日付があるC12(L-14)は、changeの枠に残る",
            ("L-11" in change_line_ids, "L-14" in change_line_ids, "L-13" in change_line_ids, "L-15" in change_line_ids),
            (True, True, False, False),
        )
        big_line_ids = [l["line_id"] for l in after_edition["sections"][1]["articles"][0]["lines"]]
        check(
            "見本の号/負例(27-2 S2): 同じ日付不明のC11を参照していても、changeでない枠(big)のL-16は落とさない",
            "L-16" in big_line_ids, True,
        )
        check(
            "見本の号/正例(27-2 S3): 本文に日付があるが古いC10(L-12)は日付不明にならず、検査10で落ちる。"
            "日付不明で落ちたL-13・L-15は検査10で二重に数えず、stale_source_hitsは1・unknown_published_at_hitsは0のまま",
            ("C10" in v["published_date_not_found"]["source_ids"], v.get("stale_source_hits"),
             v.get("stale_source_hits_by_kind"), v.get("unknown_published_at_hits")),
            (False, 1, {"timed": 0, "date_only": 1}, 0),
        )
        check(
            "見本の号/負例(27-2 S2): 時刻付きの出典C01〜C07は記録だけで(published_at_unverified_hitsは7のまま)、行は落とさない",
            (v.get("published_at_unverified_hits"), sorted(v.get("published_at_unverified_sources") or [])),
            (7, ["C01", "C02", "C03", "C04", "C05", "C06", "C07"]),
        )

        # --- 4-11: RECENT-HEADLINES.jsonを置いていないので失敗として記録される(号は止まらない) ---
        check(
            "見本の号/正例: recent_headlines_failedは真(印のファイルを置いていないため)。それでも号自体は保存される",
            v.get("recent_headlines_failed"), True,
        )

    _assert_testdata_untouched("見本の号(canary)テスト")


def _hyp_base(**kw):
    h = {"company_name": "テスト検証株式会社", "ticker": "9001", "ticker_source": "edinet_codelist"}
    h.update(kw)
    return h


def test_check_hypothesis_listed_and_ticker_match():
    """タスク16-2c-1 修正4・5: 検査21(上段。コードリスト上「上場」か)・
    検査31(上段。証券コードの一致)の正例・負例。下段向けのcheck_lower_listed/
    check_lower_ticker_matchと違い、対象はticker_sourceがedinet_codelistの
    仮説だけで、コードリストが読めない場合はどちらも適用しない。"""
    rows = [
        _ec_row("テスト検証株式会社", "E-UVERIFY-1", "90010", capital="1000"),
        _ec_row("テスト検証二号株式会社", "E-UVERIFY-2", "70010", capital="1000"),
        _ec_row("テスト非上場株式会社", "E-UVERIFY-3", "80010", listed="非上場", capital="1000"),
    ]

    # --- 検査21: 正例(反応してほしい: 不合格になる) ---
    check(
        "検査21(上段)/正例: コードリスト上「上場」で見つからない会社は不合格",
        ve.check_hypothesis_listed(_hyp_base(company_name="テスト非上場株式会社", ticker="8001"), rows),
        "upper_not_listed",
    )

    # --- 検査21: 負例(反応してほしくない例。5件以上) ---
    check(
        "検査21(上段)/負例1: コードリスト上「上場」で見つかれば合格",
        ve.check_hypothesis_listed(_hyp_base(), rows), None,
    )
    check(
        "検査21(上段)/負例2: ticker_sourceがedinet_codelist以外なら検査21は適用しない",
        ve.check_hypothesis_listed(_hyp_base(company_name="テスト架空株式会社", ticker_source="edinet_seccode"), rows),
        None,
    )
    check(
        "検査21(上段)/負例3: ticker_source自体が無くても検査21は適用しない",
        ve.check_hypothesis_listed({"company_name": "テスト架空株式会社", "ticker": "9999"}, rows),
        None,
    )
    check(
        "検査21(上段)/負例4: コードリストが読めない(None)場合は検査21を適用しない",
        ve.check_hypothesis_listed(_hyp_base(company_name="テスト非上場株式会社", ticker="8001"), None),
        None,
    )
    check(
        "検査21(上段)/負例5: 別の上場会社でも社名が完全一致すれば合格",
        ve.check_hypothesis_listed(_hyp_base(company_name="テスト検証二号株式会社", ticker="7001"), rows),
        None,
    )

    # --- 検査31: 正例(反応してほしい: 不合格になる) ---
    check(
        "検査31(上段)/正例: tickerがコードリスト上の証券コードと食い違えば不合格",
        ve.check_hypothesis_ticker_match(_hyp_base(ticker="9999"), rows),
        "upper_ticker_mismatch",
    )

    # --- 検査31: 負例(反応してほしくない例。5件以上) ---
    check(
        "検査31(上段)/負例1: tickerがコードリスト上の証券コードと一致すれば合格",
        ve.check_hypothesis_ticker_match(_hyp_base(), rows), None,
    )
    check(
        "検査31(上段)/負例2: ticker_sourceがedinet_codelist以外なら検査31は適用しない",
        ve.check_hypothesis_ticker_match(_hyp_base(ticker="9999", ticker_source="edinet_seccode"), rows),
        None,
    )
    check(
        "検査31(上段)/負例3: ticker_source自体が無くても検査31は適用しない",
        ve.check_hypothesis_ticker_match({"company_name": "テスト検証株式会社", "ticker": "9999"}, rows),
        None,
    )
    check(
        "検査31(上段)/負例4: コードリストが読めない(None)場合は検査31を適用しない",
        ve.check_hypothesis_ticker_match(_hyp_base(ticker="9999"), None),
        None,
    )
    check(
        "検査31(上段)/負例5: 別の上場会社でも証券コードが一致すれば合格",
        ve.check_hypothesis_ticker_match(_hyp_base(company_name="テスト検証二号株式会社", ticker="7001"), rows),
        None,
    )


def test_check_hypothesis_impact_reason():
    """タスク16-2c-1 修正6: 検査23(impact_kindがprice_stated/amount_statedなのに
    impact_reasonが空なら不合格)の正例・負例。"""
    def hyp(impact_kind, impact_reason):
        return {"impact_kind": impact_kind, "impact_reason": impact_reason}

    # --- 正例(反応してほしい: 不合格になる。3件) ---
    check(
        "検査23/正例1: price_statedでimpact_reasonがnullなら不合格",
        ve.check_hypothesis_impact_reason(hyp("price_stated", None)), "impact_reason_missing",
    )
    check(
        "検査23/正例2: amount_statedでimpact_reasonが空文字なら不合格",
        ve.check_hypothesis_impact_reason(hyp("amount_stated", "")), "impact_reason_missing",
    )
    check(
        "検査23/正例3: price_statedでimpact_reasonが空白だけなら不合格",
        ve.check_hypothesis_impact_reason(hyp("price_stated", "   ")), "impact_reason_missing",
    )

    # --- 負例(反応してほしくない例。5件以上) ---
    check(
        "検査23/負例1: price_statedで中身のあるimpact_reasonなら合格",
        ve.check_hypothesis_impact_reason(hyp("price_stated", "前期比増収見通しのため")), None,
    )
    check(
        "検査23/負例2: amount_statedで中身のあるimpact_reasonなら合格",
        ve.check_hypothesis_impact_reason(hyp("amount_stated", "投資額が大きいため")), None,
    )
    check(
        "検査23/負例3: impact_kindがfact_onlyならimpact_reasonがnullでも対象外",
        ve.check_hypothesis_impact_reason(hyp("fact_only", None)), None,
    )
    check(
        "検査23/負例4: impact_kindがnullならimpact_reasonがnullでも対象外",
        ve.check_hypothesis_impact_reason(hyp(None, None)), None,
    )
    check(
        "検査23/負例5: 前後に空白があっても中身があれば合格",
        ve.check_hypothesis_impact_reason(hyp("price_stated", "  理由あり  ")), None,
    )


def test_check_hypothesis_relation_text_number():
    """タスク16-2c-1 修正7: 検査26(上段専用。relation_textに半角数字が含まれていたら
    不合格)の正例・負例。漢数字は対象にしない。"""
    def hyp(relation_text):
        return {"relation_text": relation_text}

    # --- 正例(反応してほしい: 不合格になる) ---
    check(
        "検査26/正例1: 半角数字を含むrelation_textは不合格",
        ve.check_hypothesis_relation_text_number(hyp("前年同期比で10%増収した。")), "relation_text_has_number",
    )
    check(
        "検査26/正例2: 全角数字もNFKC正規化後は半角として検出され不合格になる",
        ve.check_hypothesis_relation_text_number(hyp("２０２６年３月期の決算に触れている。")),
        "relation_text_has_number",
    )

    # --- 負例(反応してほしくない例。指示文で指定された5件) ---
    check(
        "検査26/負例1: 「一部の製品に使われている。」(漢数字)は反応しない",
        ve.check_hypothesis_relation_text_number(hyp("一部の製品に使われている。")), None,
    )
    check(
        "検査26/負例2: 「第一種の許可を受けている。」(漢数字)は反応しない",
        ve.check_hypothesis_relation_text_number(hyp("第一種の許可を受けている。")), None,
    )
    check(
        "検査26/負例3: 「この規制の対象となる製品を作っている。」(数字なし)は反応しない",
        ve.check_hypothesis_relation_text_number(hyp("この規制の対象となる製品を作っている。")), None,
    )
    check(
        "検査26/負例4: 「原油の調達先が中東に偏っている。」(数字なし)は反応しない",
        ve.check_hypothesis_relation_text_number(hyp("原油の調達先が中東に偏っている。")), None,
    )
    check(
        "検査26/負例5: 「半導体の製造装置を作っている。」(数字なし)は反応しない",
        ve.check_hypothesis_relation_text_number(hyp("半導体の製造装置を作っている。")), None,
    )


def test_check_hypothesis_baseline():
    """タスク16-2c-1 修正8: 検査32(added_byがmanualでない仮説のbaseline_price_type/
    baseline_dateが号のslotから機械的に決まる値と一致するか)の正例・負例。"""
    business_days = ve.load_business_days(str(CALENDAR_DIR))

    def hyp(price_type, date, added_by=None):
        h = {"baseline_price_type": price_type, "baseline_date": date}
        if added_by is not None:
            h["added_by"] = added_by
        return h

    edition_morning = {"slot": "morning", "date": "2026-09-24"}
    edition_noon = {"slot": "noon", "date": "2026-09-24"}
    edition_evening = {"slot": "evening", "date": "2026-09-24"}

    # --- 正例(反応してほしい: 不合格になる。2件) ---
    counts_p1 = {"codelist_unavailable": False, "baseline_date_check_skipped": 0}
    check(
        "検査32/正例1: 朝号でbaseline_price_typeがopenでなければ不合格",
        ve.check_hypothesis_baseline(hyp("close", "2026-09-24"), edition_morning, business_days, counts_p1),
        "baseline_type_mismatch",
    )
    counts_p2 = {"codelist_unavailable": False, "baseline_date_check_skipped": 0}
    check(
        "検査32/正例2: 朝号でbaseline_dateが号の日付と違えば不合格",
        ve.check_hypothesis_baseline(hyp("open", "2026-09-25"), edition_morning, business_days, counts_p2),
        "baseline_date_mismatch",
    )

    # --- 負例(反応してほしくない例。5件以上) ---
    counts_n1 = {"codelist_unavailable": False, "baseline_date_check_skipped": 0}
    check(
        "検査32/負例1: 朝号でopen+号の日付なら合格",
        ve.check_hypothesis_baseline(hyp("open", "2026-09-24"), edition_morning, business_days, counts_n1),
        None,
    )
    counts_n2 = {"codelist_unavailable": False, "baseline_date_check_skipped": 0}
    check(
        "検査32/負例2: 昼号でobserved+号の日付なら合格",
        ve.check_hypothesis_baseline(hyp("observed", "2026-09-24"), edition_noon, business_days, counts_n2),
        None,
    )
    counts_n3 = {"codelist_unavailable": False, "baseline_date_check_skipped": 0}
    check(
        "検査32/負例3: 夕方号でnext_open+号の日付より後の最初の営業日(2026-09-25)なら合格",
        ve.check_hypothesis_baseline(hyp("next_open", "2026-09-25"), edition_evening, business_days, counts_n3),
        None,
    )
    counts_n4 = {"codelist_unavailable": False, "baseline_date_check_skipped": 0}
    check(
        "検査32/負例4: added_byがmanualなら型・日付が食い違っていても検査32自体を適用しない",
        ve.check_hypothesis_baseline(hyp("close", "2099-01-01", added_by="manual"), edition_morning, business_days, counts_n4),
        None,
    )
    counts_n5 = {"codelist_unavailable": False, "baseline_date_check_skipped": 0}
    check(
        "検査32/負例5: 営業日一覧が空の場合、日付の検算(b)だけを飛ばし型の確認(a)は行う",
        ve.check_hypothesis_baseline(hyp("next_open", "2026-09-25"), edition_evening, [], counts_n5),
        None,
    )
    check(
        "検査32/負例5: 日付の検算を飛ばした件数がbaseline_date_check_skippedに1件記録される",
        counts_n5["baseline_date_check_skipped"], 1,
    )


def test_check_hypothesis_evidence_source_ref():
    """タスク16-2c-1 修正9: 検査34(evidence_source_refが空でないのにedition["sources"]の
    source_idのどれとも一致しなければ不合格)の正例・負例。"""
    sources_by_id = {"S1": {"source_id": "S1"}, "S2": {"source_id": "S2"}}

    def hyp(ref):
        h = {}
        if ref is not _MISSING:
            h["evidence_source_ref"] = ref
        return h

    # --- 正例(反応してほしい: 不合格になる) ---
    check(
        "検査34/正例: 存在しないsource_idを指していれば不合格",
        ve.check_hypothesis_evidence_source_ref(hyp("S-MISSING"), sources_by_id),
        "evidence_source_ref_not_found",
    )

    # --- 負例(反応してほしくない例。5件以上) ---
    check(
        "検査34/負例1: 存在するsource_id(S1)なら合格",
        ve.check_hypothesis_evidence_source_ref(hyp("S1"), sources_by_id), None,
    )
    check(
        "検査34/負例2: 存在するsource_id(S2)なら合格",
        ve.check_hypothesis_evidence_source_ref(hyp("S2"), sources_by_id), None,
    )
    check(
        "検査34/負例3: evidence_source_refがnullなら対象外(合格)",
        ve.check_hypothesis_evidence_source_ref(hyp(None), sources_by_id), None,
    )
    check(
        "検査34/負例4: evidence_source_refが空文字なら対象外(合格)",
        ve.check_hypothesis_evidence_source_ref(hyp(""), sources_by_id), None,
    )
    check(
        "検査34/負例5: evidence_source_refキー自体が無くても対象外(合格)",
        ve.check_hypothesis_evidence_source_ref(hyp(_MISSING), sources_by_id), None,
    )


def main():
    test_read_source_text()
    test_check_evidence_source_ref()
    test_verify_line_source_unreadable()
    test_check_source_ref_not_found()
    test_check_published_at()
    test_stale_sources()
    test_stop_and_watch_split()
    test_derive_ticker_and_is_valid_ticker()
    test_check_ticker_fields()
    test_check_numbers_empty()
    test_check_excerpt_not_allowed()
    test_count_invalid_source_usages()
    test_check_baseline_late()
    test_stop_words_remove_line_not_whole_edition()
    test_pick_industry_companies_matching()
    test_generic_words_dictionary()
    test_v12_generic_word_negative_examples()
    test_pick_industry_companies_relation_and_ticker()
    test_pick_industry_companies_excluded_tickers()
    test_dropped_names_records()
    test_find_company_by_name()
    test_check_lower_relation_text()
    test_relation_text_capital_has_machine_wording()
    test_check_lower_industry()
    test_check_lower_line_mark()
    test_check_lower_ticker()
    test_check_lower_listed_and_ticker_match()
    test_run_slot_allocation()
    test_apply_edinet_evidence()
    test_check_hypothesis_listed_and_ticker_match()
    test_check_hypothesis_impact_reason()
    test_check_hypothesis_relation_text_number()
    test_check_hypothesis_baseline()
    test_check_hypothesis_evidence_source_ref()
    test_testdata_copy_integration()
    test_verify_edition_industry_integration()
    test_apply_source_policy()

    # 改修27-1(第4回): 4-6(出典表記の機械生成)のテスト。
    test_load_source_policy_reads_templates()
    test_fill_source_template()
    test_apply_source_attribution()

    # 改修27-1(第6回): EDINETの出典表記が読者向けの閲覧URLを使うようにする修正。
    test_compute_edinet_view_url()
    test_apply_edinet_view_url()
    test_apply_source_attribution_uses_edinet_view_url()
    test_check_market_open()
    test_find_number_numeric_comparison()
    test_find_number_leading_zero()
    test_build_first_run_record()
    test_first_run_created_on_first_verification()
    test_first_run_unchanged_on_second_run()
    test_should_abort_rerun()
    test_number_coverage()
    test_sources_published_at_null()
    test_rerun_detected()
    test_skip_companies_when_market_closed()
    test_industry_examples_not_skipped_when_market_open_and_not_late()
    test_check_edition_date()
    test_edition_date_check_always_runs_even_with_first_run()
    test_edition_date_check_cannot_be_bypassed_by_forged_first_run()
    test_evidence_downgrade_target_is_reported()
    test_check_hypothesis_baseline_late_input()

    # 改修27-1(第1回): decision1(falsifier)・4-5(観察窓の機械化)・4-1(generated_atの
    # 上書き)・決定4(検査24の時間帯記録)のテスト。
    test_observation_window_horizon()
    test_compute_deadline_base_date()
    test_recount_deadline_by_stepping()
    test_apply_observation_window()
    test_required_hypothesis_fields_no_falsifier_or_horizon()
    test_inference_falsifier_still_required()
    test_override_generated_at()
    test_compute_expected_slot()
    test_round1_end_to_end_missing_ai_fields()

    # 改修27-1(第2回): 4-4(EDINET書類一覧2日分)・4-2(published_atの書き込みと検査10の
    # 境目)・4-3(一覧そのもののpublished_at)・4-10(tob_side)のテスト。
    test_compute_tob_side()
    test_last_business_day_before()
    test_format_edinet_submit_datetime()
    test_load_edinet_companies_two_days()
    test_apply_edinet_source_published_at()
    test_run_check_e_stale_sources_36h_boundary()
    test_run_check_published_at_skips_date_only()
    test_round2_end_to_end_edinet_published_at_and_tob_side()

    # 改修27-1(第3回): 4-8(EDINETの表紙ファイルの除外)・4-9(タグ除去)・
    # 小さな修正1(一覧でないEDINETのURL)のテスト。
    test_select_document_files_and_extract_text_payload()
    test_strip_html_tags()
    test_read_source_body_for_checks()
    test_collect_edinet_doc_files()
    test_round3_end_to_end_html_tags_and_doc_files()
    test_round4_end_to_end_attribution()

    # 改修27-1(第5回): 記録キー(4-12)・recent_headlines.py(4-11)・build_index.pyの
    # verification無し除外(4-13)のテスト。
    test_recent_business_days()
    test_record_only_keys_edition_level()
    test_record_only_keys_hyp_level()
    test_check_recent_headlines_status()
    test_recent_headlines_is_before_and_select()
    test_recent_headlines_script_end_to_end()
    test_build_index_skips_editions_without_verification()

    # 改修27-2(第1回): 検査12の廃止(S6)・published_date_onlyの機械書き込み(S1)・
    # titleかurlが空の出典(S12)・reportedの定型文(S13)のテスト。
    test_check12_removed_direction_not_read()
    test_is_date_only_string()
    test_apply_published_date_only()
    test_check36_timed_only_record_skips_date_only()
    test_empty_title_or_url_refs()
    test_reported_relation_text_mismatch()
    test_round1_27_2_end_to_end_default_keys_without_hypotheses()

    # 改修27-2(第2回): 検査24の時間帯のずれで止める(S14)・照合の本体を実行時刻を
    # 引数で受け取る関数に分けたことのテスト。コマンドを別に起動するのは最後の1本だけ。
    test_check_edition_slot()
    test_run_verification_stops_on_slot_mismatch()
    test_run_verification_requires_run_at()
    test_cli_runs_with_current_time()

    # 改修27-2(第3回): 検査10の「新しい」の条件を出典の種類で分ける(S4)のテスト。
    test_stale_sources_by_source_kind()
    test_prev_edinet_list_dropped_from_change_on_monday()

    # 改修27-2(第4回): 検査36を必須にし(S2)、検査10より先に行う(S3)。英語の日付・
    # 別の日に一致しないための規則のテスト。
    test_date_found_in_text()
    test_published_date_only_required()

    # 改修27-1(4-15): 見本の号(Canary)。
    test_canary_edition()

    total = len(results)
    passed = sum(1 for _, ok, _, _ in results if ok)
    print("=" * 60)
    print("セルフテスト結果")
    print("=" * 60)
    for name, ok, actual, expected in results:
        mark = "OK" if ok else "NG"
        print(f"[{mark}] {name}")
        if not ok:
            print(f"      期待: {expected!r} / 実際: {actual!r}")
    print("-" * 60)
    print(f"{total}件中{passed}件が期待通り(不一致{total - passed}件)")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
