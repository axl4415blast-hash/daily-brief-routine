# ネットワーク到達性テスト（通信テスト）

このファイルは通信テスト専用です。紙面（コンテンツ）は作成していません。

## 実行環境情報

- 実行日時（日本時間）: 2026-09-18 09:06:28 JST（UTC 2026-09-18 00:06:28）
- 環境変数 `CLAUDE_CODE_ENVIRONMENT_NAME`: なし（未設定）

## 結果一覧

| サイト | curlの結果 | WebFetchの結果 | 備考 |
|---|---|---|---|
| 財務省 貿易統計（customs.go.jp） | 成功（HTTP 200） | 成功（貿易統計ポータルの内容を取得） | x-deny-reasonヘッダーなし |
| 財務省（mof.go.jp） | 成功（HTTP 200） | 成功（財務省公式サイトの内容を取得） | x-deny-reasonヘッダーなし |
| 日本銀行（boj.or.jp） | 成功（HTTP 200） | 成功（日本銀行公式サイトの内容を取得） | x-deny-reasonヘッダーなし |
| e-Stat（e-stat.go.jp） | 成功（HTTP 200） | 成功（政府統計の総合窓口の内容を取得） | x-deny-reasonヘッダーなし |
| e-Stat API（api.e-stat.go.jp） | HTTP 403（エラー応答は届いた） | 失敗（HTTP 403 Forbidden） | APIキー未設定のためのエラー応答と想定され、想定通りの結果。x-deny-reasonヘッダーなし |
| EDINET（disclosure2.edinet-fsa.go.jp） | 成功（HTTP 200） | 成功（ただしページ内容はごく短い「閲覧サイト」表示のみ。JavaScript描画のため主要コンテンツは別途読み込まれる可能性） | x-deny-reasonヘッダーなし |
| 東証 適時開示 TDnet（release.tdnet.info） | 成功（HTTP 200） | 失敗（HTTP 403 Forbidden） | curlは成功したがWebFetchは403。サイト側がWebFetchのアクセス元を拒否している可能性（未確認）。x-deny-reasonヘッダーなし |
| JPX 決算発表予定日（jpx.co.jp） | 成功（HTTP 200） | 失敗（HTTP 403 Forbidden） | curlは成功、WebFetchのみ403（TDnetと同様の傾向）。x-deny-reasonヘッダーなし |
| 米国FRB（federalreserve.gov） | 成功（HTTP 200） | 成功（FRB公式サイトの内容を取得） | x-deny-reasonヘッダーなし |

補足: 今回もどのサイトの応答ヘッダーにも `x-deny-reason` は見つかりませんでした（プロキシによる遮断は発生せず）。9サイト中8サイトがcurl・WebFetchの両方またはcurlのみで到達可能でした。

## 手順3（JPX Excelファイルのダウンロード検証）

JPXの決算発表予定日ページ（curlで取得成功、WebFetchは403のためcurlで取得したHTMLからリンクを抽出）内から、Excelファイルへのリンクを2件発見し、そのうち1件を使用しました。

- curlでのダウンロード: 成功（ファイルサイズ約31KB、Microsoft Excel 2007+形式と確認）
- Pythonでの先頭5行の読み取り: 成功（openpyxlを使用。中身は記録していません）

## 手順4（WebSearch確認）

「日本銀行 金融政策決定会合」で検索を実行し、結果が返ってきたことを確認しました（成功）。日銀公式サイト（boj.or.jp）を含む複数の検索結果が返りました。

## まとめ

- 今回のテストでは、9サイト中8サイトにcurlで到達できました（api.e-stat.go.jpのみHTTP 403で、これはAPIキー未設定によるものと想定され、想定通りの挙動です）。
- WebFetchでは9サイト中6サイトが成功、3サイト（api.e-stat.go.jp、release.tdnet.info、jpx.co.jp）が失敗しました。ただしtdnetとjpxはcurlでは成功しているため、この2件の失敗はこの環境のネットワーク遮断ではなく、サイト側がWebFetchのアクセス元を拒否している可能性が高いです（未確認）。
- 応答ヘッダーに `x-deny-reason` は一度も見つかりませんでした。今回の実行では、この環境のプロキシによる明確な遮断（EGRESS_BLOCKEDなど）は発生しませんでした。
- 前回の実行（2026-09-18 00:44 JST）とほぼ同様の結果でした。curlの到達性・WebFetchの成否パターン（tdnetとjpxのみWebFetch失敗）ともに再現しており、ネットワーク到達性はおおむね安定していると考えられます。
- WebSearchツールは正常に機能し、結果を取得できました。
