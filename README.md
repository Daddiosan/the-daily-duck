# The Daily Duck — Version 3.3

変更点:
- 今日を 2026-08-10 QUACKSTRONAUT に復元
- SHARKQUACK (2026-08-11) はデータとして保存するが、まだ未公開
- Archiveカードをクリックすると、その日の記事をトップ表示
- 過去記事URL:
  `https://www.thedailyduck.ai/?date=YYYY-MM-DD`
- 未公開日のURLは表示しない

## 現在の公開状態
- 2026-08-10 QUACKSTRONAUT: published
- 2026-08-11 SHARKQUACK: prepared / unpublished

## 8/11になったら
`data/content.js` で:
- `today` を SHARKQUACK に変更
- SHARKQUACK の `"published": false` を `true` に変更

通常は画像ファイルはすでに置いてあるので、
その日は `data/content.js` だけ更新すれば公開できます。
