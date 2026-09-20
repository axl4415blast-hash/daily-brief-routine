"""verify_edition.py の主要な判定が今まで通り動くことを固定するためのテスト。

既存の挙動(検査9・検査11・停止/注意のキー分割・出典本文の文字コード対応)に加え、
今回追加した検査12・13・15・16・20についても、正例(反応してほしい例)と
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
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edinet_fetch
import edinet_codelist as ec
import verify_edition as ve
import pick_industry_companies as pic

REPO_ROOT = Path(__file__).resolve().parent.parent

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
    stale, unknown = ve.run_check_e_stale_sources(fresh, run_at_dt)
    check("検査9/正例: 36時間以内なら落とさない(stale=0)", stale, 0)
    check("検査9/正例: 36時間以内なら行が残る", len(fresh["sections"][0]["articles"][0]["lines"]), 1)

    old = make_edition("2026-09-17T08:50:00+09:00")
    stale, unknown = ve.run_check_e_stale_sources(old, run_at_dt)
    check("検査9/負例: 36時間より古い場合はstale_source_hitsが増える", stale, 1)
    check("検査9/負例: 36時間より古い場合はunknown_published_at_hitsは増えない", unknown, 0)
    check("検査9/負例: 36時間より古い行は落とされる", len(old["sections"][0]["articles"][0]["lines"]), 0)

    unknown_pub = make_edition(None)
    stale, unknown = ve.run_check_e_stale_sources(unknown_pub, run_at_dt)
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


def test_check_minus_direction():
    """検査12(directionがminusの仮説の3条件)の正例・負例。
    出典はEDINET風の文書(提出者自身の開示)を想定し、会社名は架空名(テスト物産)。"""
    with tempfile.TemporaryDirectory() as d:
        ok_body = "テスト物産株式会社は有価証券報告書を提出した。売上高は前期比で減少した。"
        write(d, "SRC-OK.txt", ok_body.encode("utf-8"))
        write(d, "SRC-OTHER.txt", "テスト電機株式会社の開示資料。".encode("utf-8"))

        sources = {
            "SRC-OK": {"source_id": "SRC-OK", "usage": "quotable"},
            "SRC-OTHER": {"source_id": "SRC-OTHER", "usage": "quotable"},
        }

        def base_minus(**overrides):
            hyp = {
                "company_name": "テスト物産",
                "direction": "minus",
                "evidence_grade": "primary",
                "evidence_filer_name": "テスト物産",
                "evidence_excerpt": "売上高は前期比で減少した",
                "evidence_source_ref": "SRC-OK",
            }
            hyp.update(overrides)
            return hyp

        # --- 正例(反応してほしい: Falseが返り、仮説が削除される) ---
        check(
            "検査12/正例: evidence_gradeがprimaryでなければ不合格",
            ve.check_minus_direction(base_minus(evidence_grade="inferred"), sources, d),
            False,
        )
        check(
            "検査12/正例: evidence_filer_nameがcompany_nameと一致しなければ不合格",
            ve.check_minus_direction(base_minus(evidence_filer_name="テスト電機"), sources, d),
            False,
        )
        check(
            "検査12/正例: evidence_excerptがnullなら不合格",
            ve.check_minus_direction(base_minus(evidence_excerpt=None), sources, d),
            False,
        )
        check(
            "検査12/正例: evidence_excerptが出典本文に存在しなければ不合格",
            ve.check_minus_direction(base_minus(evidence_excerpt="存在しない文言です"), sources, d),
            False,
        )
        check(
            "検査12/正例: evidence_source_refが別会社の出典を指していれば不合格",
            ve.check_minus_direction(base_minus(evidence_source_ref="SRC-OTHER"), sources, d),
            False,
        )

        # --- 負例(反応してほしくない: Trueが返り、仮説は残る) ---
        check(
            "検査12/負例: directionがplusならevidence_excerptがnullでも合格",
            ve.check_minus_direction(
                {"company_name": "テスト物産", "direction": "plus", "evidence_grade": "inferred",
                 "evidence_excerpt": None},
                sources, d,
            ),
            True,
        )
        check(
            "検査12/負例: 3条件をすべて満たすminusの仮説は合格",
            ve.check_minus_direction(base_minus(), sources, d),
            True,
        )
        check(
            "検査12/負例: evidence_filer_nameの前後に半角空白が入っているだけなら合格",
            ve.check_minus_direction(base_minus(evidence_filer_name=" テスト物産 "), sources, d),
            True,
        )
        check(
            "検査12/負例: evidence_filer_nameの前後に全角空白が入っているだけなら合格",
            ve.check_minus_direction(base_minus(evidence_filer_name="　テスト物産　"), sources, d),
            True,
        )
        check(
            "検査12/負例: directionがplusでevidence_gradeがinferredでも合格(minus専用の検査のため)",
            ve.check_minus_direction(
                {"company_name": "テスト物産", "direction": "plus", "evidence_grade": "inferred",
                 "evidence_excerpt": None, "evidence_filer_name": None, "evidence_source_ref": None},
                sources, d,
            ),
            True,
        )


def test_check_ticker_fields():
    """検査13(ticker/ticker_sourceの確認)の正例・負例。証券コードは実在しない9999/9998。"""
    edinet_companies = [
        {"filer_name": "テスト物産", "ticker": "9999"},
        {"filer_name": "テスト電機", "ticker": "9998"},
    ]

    def base(**overrides):
        hyp = {
            "company_name": "テスト物産",
            "ticker": "9999",
            "ticker_source": "edinet",
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
        "検査13/正例: EDINET一覧のtickerと食い違えば不合格",
        ve.check_ticker_fields(base(ticker="9998"), edinet_companies),
        "ticker_mismatch",
    )
    check(
        "検査13/正例: EDINET一覧に会社名が見つからなければ不合格",
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

    # --- 英字入りの証券コード(2024年1月以降の新規上場を想定) ---
    edinet_companies_with_letter = edinet_companies + [
        {"filer_name": "テスト新興", "ticker": "130A"},
    ]

    def base_letter(**overrides):
        hyp = {
            "company_name": "テスト新興",
            "ticker": "130A",
            "ticker_source": "edinet",
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

        result = _run_verify_cli(d, edition_path, hyp_path, cache_dir, calendar_dir)
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
            return _run_verify_cli(work_dir, edition_path, hyp_path, cache_dir, calendar_dir)

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
    _, reasons1 = ve.run_hypothesis_checks(hyp_doc1, edition1, [], [], "/nonexistent", None)
    check("作業B/負例1: market_open=Trueならmarket_closedを理由に仮説が削除されない", "market_closed" in reasons1, False)

    # --- 負例2: AIがtrueと書いた休場日(2026-10-12・スポーツの日) → falseに上書き ---
    edition2 = {"date": "2026-10-12", "market_open": True}
    result2 = ve.check_market_open(edition2, CALENDAR_DIR)
    check("作業B/負例2: 休場日はAIがtrueと書いてもfalseに上書きされる", edition2["market_open"], False)
    check("作業B/負例2: market_open_overwrittenがtrueになる", result2["overwritten"], True)

    edition2["sections"] = []
    hyp_doc2 = {"hypotheses": [{"company_name": "テスト物産"}]}
    violations2, reasons2 = ve.run_hypothesis_checks(hyp_doc2, edition2, [], [], "/nonexistent", None)
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
# first_run/skip_companies/検査24はmain()の中に組み込まれているため、これらを
# 確かめるにはCLI全体(subprocess)を走らせる必要がある。scripts/testdataは
# 一時フォルダにコピーしてから使う(本体を書き換えないため)。


def _copy_testdata_to(tmp_root):
    dst = Path(tmp_root) / "testdata"
    shutil.copytree(REPO_ROOT / "scripts" / "testdata", dst)
    return dst


def _run_verify_cli(work_dir, edition_path, hyp_path, cache_dir, calendar_dir=None):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "verify_edition.py"),
         "--edition", str(edition_path), "--hypotheses", str(hyp_path),
         "--cache", str(cache_dir), "--calendar", str(calendar_dir or CALENDAR_DIR)],
        capture_output=True, text=True, cwd=str(work_dir),
    )


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

        result = _run_verify_cli(d, edition_path, hyp_path, cache_dir)
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

        r1 = _run_verify_cli(d, edition_path, hyp_path, cache_dir)
        check("first_run/正例: 1回目の照合は正常終了する", r1.returncode, 0)
        first_run_1 = json.loads(edition_path.read_text(encoding="utf-8"))["verification"]["first_run"]

        r2 = _run_verify_cli(d, edition_path, hyp_path, cache_dir)
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

        result = _run_verify_cli(d, edition_path, hyp_path, cache_dir, calendar_dir)
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

            result = _run_verify_cli(d, edition_path, hyp_path, cache_dir, calendar_dir)
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

        result = _run_verify_cli(d, edition_path, hyp_path, cache_dir)
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

        result = _run_verify_cli(d, edition_path, hyp_path, cache_dir)
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
    """検査17(修正6): baseline_late_inputが真の仮説は、baseline_dateではなく
    baseline_observed_atの日付(営業日でなければ後の最初の営業日)から期限日を数え直す。"""
    business_days = ve.load_business_days(str(CALENDAR_DIR))
    sources = {}
    line_ids = {"L-1": "verified"}
    ng_words = []

    def base(**kw):
        h = {
            "company_name": "テスト物産", "relation_text": "業績に影響しうる",
            "falsifier": "翌月大幅に悪化した場合", "baseline_price_type": "close",
            "direction": "plus", "evidence_grade": "reported",
            "ticker": "8801", "ticker_source": "edinet_codelist", "line_ids": ["L-1"],
        }
        h.update(kw)
        return h

    def result_for(hyp):
        return ve.check_hypothesis(hyp, {}, line_ids, business_days, ng_words, sources, ".", None)

    # --- 正例1: baseline_observed_atが営業日そのもの ---
    expected1 = ve.compute_deadline(business_days, "2026-09-25", 5)
    hyp1 = base(
        baseline_late_input=True, baseline_observed_at="2026-09-25T10:00:00+09:00",
        baseline_date="2026-09-24", horizon_business_days=5, deadline_date=expected1,
    )
    check(
        "検査17/正例1: baseline_late_inputが真ならbaseline_observed_atの日付から数えた期限日が合格する",
        result_for(hyp1), None,
    )

    # --- 正例2: baseline_observed_atが非営業日(土曜) → 後の最初の営業日(2026-09-28)から数える ---
    expected2 = ve.compute_deadline(business_days, "2026-09-28", 3)
    hyp2 = base(
        baseline_late_input=True, baseline_observed_at="2026-09-26T09:00:00+09:00",
        baseline_date="2026-09-24", horizon_business_days=3, deadline_date=expected2,
    )
    check(
        "検査17/正例2: baseline_observed_atが非営業日なら後の最初の営業日から数え直す",
        result_for(hyp2), None,
    )

    # --- 負例(反応してほしい): baseline_observed_atがnull → 削除 ---
    hyp3 = base(
        baseline_late_input=True, baseline_observed_at=None,
        baseline_date="2026-09-24", horizon_business_days=5, deadline_date=expected1,
    )
    check(
        "検査17/負例: baseline_observed_atがnullならdeadline_date_mismatchで削除される",
        result_for(hyp3), "deadline_date_mismatch",
    )

    # --- 負例(反応してほしくない例。5件以上): baseline_late_inputが偽・無い・nullは従来どおり ---
    expected_normal = ve.compute_deadline(business_days, "2026-09-24", 5)
    hyp4 = base(baseline_late_input=False, baseline_date="2026-09-24", horizon_business_days=5, deadline_date=expected_normal)
    check(
        "検査17/反応してほしくない例1: baseline_late_inputが偽ならbaseline_dateから従来どおり計算される",
        result_for(hyp4), None,
    )

    hyp5 = base(baseline_date="2026-09-24", horizon_business_days=5, deadline_date=expected_normal)
    check(
        "検査17/反応してほしくない例2: baseline_late_inputキーが無くても従来どおり計算される",
        result_for(hyp5), None,
    )

    hyp6 = base(baseline_late_input=None, baseline_date="2026-09-24", horizon_business_days=5, deadline_date=expected_normal)
    check(
        "検査17/反応してほしくない例3: baseline_late_inputがnullでも従来どおり計算される",
        result_for(hyp6), None,
    )

    hyp7 = base(baseline_late_input=False, baseline_date="2026-09-24", horizon_business_days=5, deadline_date="2099-01-01")
    check(
        "検査17/反応してほしくない例4: baseline_late_inputが偽で期限日が違えばdeadline_date_mismatchになる(従来どおり)",
        result_for(hyp7), "deadline_date_mismatch",
    )

    expected_alt_horizon = ve.compute_deadline(business_days, "2026-09-25", 3)
    hyp8 = base(
        baseline_late_input=True, baseline_observed_at="2026-09-25T09:00:00+09:00",
        baseline_date="2026-09-24", horizon_business_days=3, deadline_date=expected_alt_horizon,
    )
    check(
        "検査17/反応してほしくない例5: horizonを変えても営業日起算の仕組み自体は壊れていない",
        result_for(hyp8), None,
    )


def main():
    test_read_source_text()
    test_check_evidence_source_ref()
    test_verify_line_source_unreadable()
    test_stale_sources()
    test_stop_and_watch_split()
    test_check_minus_direction()
    test_derive_ticker_and_is_valid_ticker()
    test_check_ticker_fields()
    test_check_numbers_empty()
    test_check_excerpt_not_allowed()
    test_count_invalid_source_usages()
    test_check_baseline_late()
    test_stop_words_remove_line_not_whole_edition()
    test_pick_industry_companies_matching()
    test_pick_industry_companies_relation_and_ticker()
    test_pick_industry_companies_excluded_tickers()
    test_find_company_by_name()
    test_check_lower_relation_text()
    test_check_lower_industry()
    test_check_lower_line_mark()
    test_check_lower_ticker()
    test_check_lower_listed_and_ticker_match()
    test_run_slot_allocation()
    test_testdata_copy_integration()
    test_verify_edition_industry_integration()
    test_apply_source_policy()
    test_check_market_open()
    test_find_number_numeric_comparison()
    test_find_number_leading_zero()
    test_build_first_run_record()
    test_first_run_created_on_first_verification()
    test_first_run_unchanged_on_second_run()
    test_should_abort_rerun()
    test_skip_companies_when_market_closed()
    test_industry_examples_not_skipped_when_market_open_and_not_late()
    test_check_edition_date()
    test_edition_date_check_always_runs_even_with_first_run()
    test_edition_date_check_cannot_be_bypassed_by_forged_first_run()
    test_evidence_downgrade_target_is_reported()
    test_check_hypothesis_baseline_late_input()

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
