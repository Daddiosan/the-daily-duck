# The Daily Duck — Daily Publishing Workflow (Version 4.0)

## 毎日の基本フロー

1. 今日のニュースを1本決める
2. 今日のダック名を決める
3. ダック画像を1枚作る
4. `daily-template/new-duck.json` を複製して内容を埋める
5. 画像を `assets/ducks/` に追加する
6. 今日のデータを `data/content.js` の archive 配列の先頭へ追加する
7. 公開する日に `published: true` にする
8. `/ducks/YYYY-MM-DD/index.html` の静的個別ページを作る
9. `sitemap.xml` にその個別URLを追加する
10. GitHubへアップロードしてCommit
11. VercelのProductionが更新されたことを確認
12. トップページとArchiveから個別ページを確認

## ユーザー向け最短運用

ChatGPTに次のように依頼:
「今日のダックを作って。ニュースは○○。」
または、公開準備済みなら:
「今日のダックを公開して。」

ChatGPT側で更新ZIPを作成し、GitHubへアップロードできる形にする。

## ファイル命名ルール

画像:
`assets/ducks/YYYY-MM-DD-slug.png`

個別ページ:
`ducks/YYYY-MM-DD/index.html`

例:
`assets/ducks/2026-08-11-sharkquack.png`
`ducks/2026-08-11/index.html`

## 公開チェック

- トップページの日付とダック名
- 今日の画像
- 英語 / 日本語
- 出典リンク
- Archiveカード
- 個別ページURL
- OGP / X Card
- sitemap.xml
- Google Analyticsタグ
- favicon / ブランドヘッダー

## 大事なルール

未来の日付のダックは `published: false` のままにする。
公開日になったら `published: true` にして、トップページ・Archive・個別ページ・sitemapを同時更新する。
