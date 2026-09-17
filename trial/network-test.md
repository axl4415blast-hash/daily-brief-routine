# ネットワーク到達性テスト（通信テスト）

このファイルは通信テスト専用です。紙面（コンテンツ）は作成していません。

## 実行環境情報

- 実行日時（日本時間）: 2026-09-18 00:37:23 JST（UTC 2026-09-17 15:37:23）
- 環境変数 `CLAUDE_CODE_ENVIRONMENT_NAME`: なし（未設定）

## 結果一覧

| サイト | curlの結果 | WebFetchの結果 | 備考 |
|---|---|---|---|
| 財務省 貿易統計（customs.go.jp） | 到達不可（プロキシが CONNECT を 403 で拒否） | 失敗（EGRESS_BLOCKED） | 応答ヘッダーに `x-deny-reason` は含まれず。両ツールとも同一原因 |
| 財務省（mof.go.jp） | 到達不可（プロキシが CONNECT を 403 で拒否） | 失敗（EGRESS_BLOCKED） | 同上 |
| 日本銀行（boj.or.jp） | 到達不可（プロキシが CONNECT を 403 で拒否） | 失敗（EGRESS_BLOCKED） | 同上 |
| e-Stat（e-stat.go.jp） | 到達不可（プロキシが CONNECT を 403 で拒否） | 失敗（EGRESS_BLOCKED） | 同上 |
| e-Stat API（api.e-stat.go.jp） | 到達不可（プロキシが CONNECT を 403 で拒否） | 失敗（EGRESS_BLOCKED） | APIキー未設定でのエラー応答を想定していたが、それ以前にプロキシ段階で遮断された |
| EDINET（disclosure2.edinet-fsa.go.jp） | 到達不可（プロキシが CONNECT を 403 で拒否） | 失敗（EGRESS_BLOCKED） | 同上 |
| 東証 適時開示 TDnet（release.tdnet.info） | 到達不可（プロキシが CONNECT を 403 で拒否） | 失敗（EGRESS_BLOCKED） | 同上 |
| JPX 決算発表予定日（jpx.co.jp） | 到達不可（プロキシが CONNECT を 403 で拒否） | 失敗（EGRESS_BLOCKED） | ページ自体が取得できなかったため、Excelリンクの探索・ダウンロード検証（手順3）は未実施 |
| 米国FRB（federalreserve.gov） | 到達不可（プロキシが CONNECT を 403 で拒否） | 失敗（EGRESS_BLOCKED） | 同上 |

## 手順3（JPX Excelファイルのダウンロード検証）

JPXの決算発表予定日ページ自体が取得できなかったため、本手順は実施していません（未検証）。

## 手順4（WebSearch確認）

「日本銀行 金融政策決定会合」で検索を実行し、結果が返ってきたことを確認しました（成功）。日銀サイト（boj.or.jp）を含む複数の検索結果が返りました。ただし、これは検索エンジン経由の結果であり、上記の直接アクセス（curl / WebFetch）とは別の経路です。

## まとめ

- 今回テストした9サイトはすべて、curl・WebFetchの両方でこの実行環境から直接アクセスできませんでした。原因は接続先サーバー側の応答ではなく、この環境のネットワークプロキシ（egress proxy）が、これらのドメインへの接続（CONNECT）自体を組織のポリシーにより拒否しているためです（プロキシの状態確認エンドポイントでも `policy denial` として記録されています）。
- 一方、WebSearchツール（検索）は正常に機能し、結果を取得できました。
