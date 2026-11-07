# Protected workflow reapproval record — 2026-11-07

**Status: HUMAN_APPROVED on 2026-11-07.**

Approved by: the user, through an explicit instruction in this session. Approved at: 2026-11-07 (JST).

The user explicitly approved the exact ten path/SHA256 pairs below and the two reviewed candidate files on 2026-11-07. This is human reapproval of the protected-test permitted versions. Technical review, local PASS, prior merge and model PASS are separate evidence, not the authority for this approval.

The fixed baseline remains `23f54a4234bcb332f8f0b97bedb255b749235dd0`. The guard accepts only the exact ten known deltas and hashes; it retains all protected paths and the separate Gate A section guard. Git CRLF checkout is normalized to LF for these SHA256 checks. Future changes, deletions, reversions and additional tracked protected deltas fail. Rename detection is explicitly disabled so deletion sources cannot be hidden. All ten versions must remain regular non-symlink files with Git index mode 100644; mode changes and symlink substitutions are rejected.

This baseline reapproval grants no permission to execute workflows, generate paid content, send mail, publish to Web/X, or merge main. The same user instruction separately authorizes applying these two files, pushing only the dedicated branch and creating a Draft PR. Runtime execution gates remain independent.

Reviewed candidate SHA256 before approval metadata update:

- tests/test_approval_receiver_contract.py: a3e0d69d4d2fd4723936b52a3f4728aa9ed03c498a31f7f5a16a7a52831d9b10
- this record draft: 5deaa4fa70500b8a6b6ee6e6239efdecd8f8da15555e6907c83d813ef158476e

Only approval comments/status were updated on application; test AST and all ten path/SHA256 pairs remain identical to the reviewed candidates.

## .github/workflows/archive-only-publish-2026-10-11.yml

Purpose: 過去日のWeb記事のみ補填

SHA256 of Git LF content: `c89946abf027884d568b4f70ca599982dbc9d9a7cb3abac67ae095c679f62f11`

History: e35d9e94d3856fe5104ed52d9a01f4a1f49a34d5 2026-10-12T13:48:32+09:00 Fix historical publication compatibility

Boundary and impact: 手動確認文字列、実行者/再実行者を固定、artifact ID/run/digestと画像SHAを固定。contents:write。過去記事と結果をmainへ書く。通常today/index/ready不変を検査。X投稿なし。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.

## .github/workflows/archive-only-publish-2026-10-26.yml

Purpose: 過去日のWeb記事のみ補填

SHA256 of Git LF content: `24e002e83c0d414223f0996efdf63eeee8de674df5128a023623dfd49a22db4f`

History: a1f559ef2f59655135fb99edf1d68523def48442 2026-10-27T05:04:32-07:00 Add files via upload

Boundary and impact: 手動確認文字列、実行者/再実行者を固定、artifact ID/run/digestと画像SHAを固定。contents:write。過去記事と結果をmainへ書く。通常today/index/ready不変を検査。X投稿なし。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.

## .github/workflows/automation-chain-audit.yml

Purpose: PR #16による朝の障害検知時刻の前倒し

SHA256 of Git LF content: `c90c94eabce594dd4e11bab00f32f1222d1e212ef654d804a31816faa691437d`

History: 2d70b0db924c13683f798f31cc82350c28b172df 2026-11-02T22:31:53-08:00 Stabilize daily candidate delivery with dated guards and queue monitoring (#16)

Boundary and impact: 差分は08:20→07:15 JSTのcronと説明コメントのみ。他時刻、contents/actions:read、メール通知条件と監査処理は不変。Web/X公開や認可変更なし。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.

## .github/workflows/backfill-2026-10-11-design-options.yml

Purpose: 過去日のデザイン候補と選択メールを補填

SHA256 of Git LF content: `4fad32ab6e2d6412c96eed4ed12eed5ab6c64d486a2443f0358ef812d5fa1d5c`

History: aab093d881c58dcceaaf6850729456fbbda0bcc4 2026-10-12T11:58:56+09:00 Add isolated 2026-10-11 design backfill; 775eebe2ba176bb1bab8d4a22efac77c65105aed 2026-10-12T12:09:11+09:00 Fix backfill runner temp initialization

Boundary and impact: main/実行者/再実行者/確認文字列とProductionを要求。artifact出所と日付を固定、既存token検証・重複送信ガードを呼ぶ。既存プロバイダーで画像生成とメール送信があり得る。contents:read。Web/X公開なし。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.

## .github/workflows/backfill-2026-10-11-final-selection.yml

Purpose: 過去日の承認済み最終選択を隔離ready artifactへ復元

SHA256 of Git LF content: `4d8fbb7f66912a83f472784f3009fca21fb61452d436a0cc2e9199211a61d76a`

History: 0aedcdf2b9549a38ebb380ad47973757c818c1e2 2026-10-12T12:34:34+09:00 Add isolated final selection backfill for 2026-10-11

Boundary and impact: main/実行者/再実行者/確認文字列とProductionを要求。固定artifactとGmail返信検証、重複artifactガード、schema/hash検証。contents:read。メール送信・画像再生成・Web/X公開なし。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.

## .github/workflows/backfill-2026-10-26-design-options.yml

Purpose: 過去日のデザイン候補と選択メールを補填

SHA256 of Git LF content: `b5960fa39175dd12825246f50dee068fb366a5ebb37103b96f1f59fb8dd3263c`

History: 4e30083c82fa0114805f96bc4d74dff53d728e8e 2026-10-27T03:35:14-07:00 Add files via upload

Boundary and impact: main/実行者/再実行者/確認文字列とProductionを要求。artifact出所と日付を固定、既存token検証・重複送信ガードを呼ぶ。既存プロバイダーで画像生成とメール送信があり得る。contents:read。Web/X公開なし。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.

## .github/workflows/backfill-2026-10-26-final-selection.yml

Purpose: 過去日の承認済み最終選択を隔離ready artifactへ復元

SHA256 of Git LF content: `63d83d9513d4d2d705d7dff23ae6b2df7387a8323d1f06d37b020d14e36abafc`

History: d19e6ee1fa538e425468e88367a9ff44e4441b53 2026-10-27T04:33:07-07:00 Add files via upload

Boundary and impact: main/実行者/再実行者/確認文字列とProductionを要求。固定artifactとGmail返信検証、重複artifactガード、schema/hash検証。contents:read。メール送信・画像再生成・Web/X公開なし。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.

## .github/workflows/daily-duck.yml

Purpose: PR #16による定期候補配信の安定化

SHA256 of Git LF content: `9fe970f9fe3dc3e166aa75cd38458b53b57289cd43f127905f9dabb50d30649c`

History: 2d70b0db924c13683f798f31cc82350c28b172df 2026-11-02T22:31:53-08:00 Stabilize daily candidate delivery with dated guards and queue monitoring (#16)

Boundary and impact: 06:57 JST、日付固定、配信済みガードを有料処理より前に実行。actions:read、同時実行制限、10分timeout、配信/attempt artifactを追加。既存の生成・メール処理を条件付きにする。承認/公開コードを変更しない。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.

## .github/workflows/x-backfill-publish-2026-10-11.yml

Purpose: 過去日のX投稿を補填

SHA256 of Git LF content: `aded2fc39e5cb5eb9b9689951653d82e9475d151a13f2995209b50f4fbfcfc89`

History: 8118a170594c0344c3eb557d84456379d851cdbe 2026-10-12T14:58:32+09:00 Add isolated X backfill for 2026-10-11

Boundary and impact: main/所有者/再実行者/確認文字列を要求。固定Web公開commit・artifact digest、重複検索、POST_IN_FLIGHT永続化後に1回投稿。contents:writeとX secretsを使用。外部X書込とmain結果保存がある。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.

## .github/workflows/x-backfill-publish-2026-10-26.yml

Purpose: 過去日のX投稿を補填

SHA256 of Git LF content: `f63916fc8b157db4dad8eb54bd68951cfe6351f4544309b815cf953f8cf7ec9f`

History: 3318742532d626ab2cdace4d4e02606a8b37145d 2026-10-27T05:14:27-07:00 Add October 26 X backfill workflow

Boundary and impact: main/所有者/再実行者/確認文字列を要求。固定Web公開commit・artifact digest、重複検索、POST_IN_FLIGHT永続化後に1回投稿。contents:writeとX secretsを使用。外部X書込とmain結果保存がある。

Technical assessment: explained; no unexplained delta found. Formal protection-baseline reapproval: explicitly approved by the user on 2026-11-07.
