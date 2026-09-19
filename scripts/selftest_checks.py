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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edinet_fetch
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
    """検査9(36時間ルール)。公表時刻が古い行/わからない行を、どちらも件数を分けて落とすことを確かめる。"""
    generated_at = "2026-09-24T08:14:32+09:00"

    def make_edition(published_at):
        return {
            "generated_at": generated_at,
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
    stale, unknown, skipped = ve.run_check_e_stale_sources(fresh)
    check("検査9/正例: 36時間以内なら落とさない(stale=0)", stale, 0)
    check("検査9/正例: 36時間以内なら行が残る", len(fresh["sections"][0]["articles"][0]["lines"]), 1)

    old = make_edition("2026-09-17T08:50:00+09:00")
    stale, unknown, skipped = ve.run_check_e_stale_sources(old)
    check("検査9/負例: 36時間より古い場合はstale_source_hitsが増える", stale, 1)
    check("検査9/負例: 36時間より古い場合はunknown_published_at_hitsは増えない", unknown, 0)
    check("検査9/負例: 36時間より古い行は落とされる", len(old["sections"][0]["articles"][0]["lines"]), 0)

    unknown_pub = make_edition(None)
    stale, unknown, skipped = ve.run_check_e_stale_sources(unknown_pub)
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
    """検査20(号の遅延判定)の正例・負例。"""
    def edition(slot, generated_at):
        return {"slot": slot, "generated_at": generated_at}

    # --- 正例(反応してほしい: Trueになる) ---
    check(
        "検査20/正例: 朝号(morning)で8:51はbaseline_late",
        ve.run_check_baseline_late(edition("morning", "2026-09-24T08:51:00+09:00")),
        True,
    )
    check(
        "検査20/正例: 昼号(noon)で14:51はbaseline_late",
        ve.run_check_baseline_late(edition("noon", "2026-09-24T14:51:00+09:00")),
        True,
    )
    check(
        "検査20/正例: +09:00以外の表記でも日本時間に換算して8:51相当ならbaseline_late",
        ve.run_check_baseline_late(edition("morning", "2026-09-23T23:51:00+00:00")),
        True,
    )

    # --- 負例(反応してほしくない: Falseのまま) ---
    check(
        "検査20/負例: 朝号(morning)で8:49はbaseline_lateにならない",
        ve.run_check_baseline_late(edition("morning", "2026-09-24T08:49:00+09:00")),
        False,
    )
    check(
        "検査20/負例: 昼号(noon)で14:49はbaseline_lateにならない",
        ve.run_check_baseline_late(edition("noon", "2026-09-24T14:49:00+09:00")),
        False,
    )
    check(
        "検査20/負例: 夕方号(evening)は17:30でもbaseline_lateにならない(常に判定しない)",
        ve.run_check_baseline_late(edition("evening", "2026-09-24T17:30:00+09:00")),
        False,
    )
    check(
        "検査20/負例: +09:00以外の表記で日本時間に換算すると8:30相当ならbaseline_lateにならない",
        ve.run_check_baseline_late(edition("morning", "2026-09-23T23:30:00+00:00")),
        False,
    )
    check(
        "検査20/負例: generated_atが読み取れない場合はbaseline_lateにならない",
        ve.run_check_baseline_late(edition("morning", None)),
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

    # --- 正例4: 正式名がそのまま出ていた場合はnews_entity=null, entity_relation=self ---
    article_official = {"text_blob": "テストフィナンシャルグループは本日、決算を発表した。"}
    selected = pic.select_companies_for_pick(candidates, article_official, aliases, set(), 1)
    check("会社選定/正例4: 正式名の一致ではnews_entityがNone", selected[0]["news_entity"], None)
    check("会社選定/正例4: 正式名の一致ではentity_relationが'self'", selected[0]["entity_relation"], "self")

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


def test_testdata_copy_integration():
    """3.3: scripts/testdata を一時フォルダにコピーし、コピーの方に対してCLI全体を
    走らせることで、本体のscripts/testdataには一切書き込まないことを確かめる。
    main()は紙面JSON・仮説JSONへ検査結果を書き戻すため、直接scripts/testdataの
    パスを渡すとリポジトリのフィクスチャが壊れてしまう(過去に2回発生)。"""
    src_testdata = REPO_ROOT / "scripts" / "testdata"
    with tempfile.TemporaryDirectory() as d:
        dst = Path(d) / "testdata"
        shutil.copytree(src_testdata, dst)

        edition_path = dst / "editions" / "2026-09-24" / "morning.json"
        hyp_path = dst / "hypotheses" / "2026-09-24-morning.json"
        cache_dir = dst / "cache"
        calendar_dir = REPO_ROOT / "calendar"

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "verify_edition.py"),
             "--edition", str(edition_path), "--hypotheses", str(hyp_path),
             "--cache", str(cache_dir), "--calendar", str(calendar_dir)],
            capture_output=True, text=True,
        )
        check(
            "testdata統合/コピーしたtestdataに対してCLI(main)が正常終了する(終了コード0)",
            result.returncode, 0,
        )

    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "scripts/testdata"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    check(
        "testdata統合/scripts/testdata本体はテスト実行後もgit的に変更されていない",
        status.stdout.strip(), "",
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
    test_testdata_copy_integration()

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
