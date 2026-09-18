"""verify_edition.py の主要な判定が今まで通り動くことを固定するためのテスト。

新しい検査ルールを追加するものではない。既存の挙動(検査9・検査11・
停止/注意のキー分割・出典本文の文字コード対応)が壊れていないかを、
その場で一時ファイルを作って確かめる。テスト中に作った一時ファイル・
一時フォルダはテストの終わりに消し、リポジトリには残さない。

使い方:
  python3 scripts/selftest_checks.py

1件でも期待と異なれば、終了コード1で終わる。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_edition as ve

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
        }
        line = {
            "claimed_mark": "source_number_match",
            "numbers": [],
            "source_ref": "SRC-BROKEN",
            "excerpt": "何か",
            "attribution": "出典：テスト",
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


def main():
    test_read_source_text()
    test_check_evidence_source_ref()
    test_verify_line_source_unreadable()
    test_stale_sources()
    test_stop_and_watch_split()

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
