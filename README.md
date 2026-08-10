# The Daily Duck — Version 3 Daily Update

このバージョンは **毎日更新用** です。
見た目はVersion 2を維持しつつ、毎日の更新場所を減らしています。

## 毎日触るのは基本2つだけ

1. `data/today.json`
2. `assets/ducks/YYYY-MM-DD-name.png`

そして過去のダックを残すため、
3. `data/archive.json`
にその日のデータを1件追加します。

## 今日の更新手順

### 1. 新しいダック画像を追加
例:
`assets/ducks/2026-08-11-goal-duck.png`

### 2. `data/today.json` を今日の内容に置き換える
タイトル、日付、日英本文、画像パス、出典URLを変更します。

### 3. `data/archive.json` の先頭に同じデータを追加
これで過去のダックがArchiveに残ります。

### 4. GitHubへ3ファイルをアップロードして Commit
Vercelが自動公開します。

## 重要
- `index.html`
- `styles.css`
- `script.js`

は、普段は変更不要です。

つまり公開後の日常運用では、
**今日のJSON + 画像 + Archive JSON** だけで更新できます。
