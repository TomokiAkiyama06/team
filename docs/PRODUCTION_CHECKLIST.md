# 本番導入チェックリスト

上から順に実施してください。各項目は `[ ]` を `[x]` にして記録できます。

---

## 0. 事前準備

- [ ] Docker Engine 24 以降と Docker Compose v2 を用意する（`docker compose version` で確認）
- [ ] ボットを動かすサーバーから、`slack.com` / `wss-*.slack.com` / `files.slack.com` / `api.anthropic.com` へ HTTPS（443）で外向きに接続できることを確認する（受信ポートの開放は不要。Socket Mode を使うため）
- [ ] 授業チャンネルを決める（公開か非公開かを確認しておく）
- [ ] 管理者（教員・TA）を決める
- [ ] 登録する資料に、成績・学籍番号・個人情報・非公開の試験問題などが含まれていないことを確認する（資料の内容は Anthropic API へ送信されます。[SECURITY.md](SECURITY.md) を参照）
- [ ] 所属機関の規程（外部 AI サービスの利用、学生への周知）を確認する

## 1. Anthropic API キーを用意する

- [ ] Anthropic Console（https://console.anthropic.com ）で、**このボット専用の API キー**を発行する
  - 他のシステムと共用しないでください。ボットの予算上限はボット経由の利用分しか管理できません。
- [ ] Console で、そのワークスペースに月額の利用上限（Spend limit）を設定する（ボット側の上限とは別の、二重の安全策）
- [ ] 利用ティアのレート制限が `MAX_CONCURRENT_REQUESTS` に対して十分か確認する
- [ ] キーは `.env` にだけ書き、チャット・メール・Git には貼らない

## 2. Slack App を作成する

1. - [ ] https://api.slack.com/apps → **Create New App** → **From an app manifest** → ワークスペースを選ぶ
2. - [ ] リポジトリの `slack-app-manifest.yaml` の内容を貼り付ける
   - 授業チャンネルが**公開**チャンネルなら、`groups:history` と `message.groups` は削除してよい
   - 非公開チャンネルなら、両方とも残す
3. - [ ] 作成後、**OAuth & Permissions → Scopes → Bot Token Scopes** が次の 5 つだけになっていることを確認する

   | スコープ | 用途 |
   |---|---|
   | `app_mentions:read` | `@ClaudeBot` メンションの受信 |
   | `channels:history` | 公開チャンネルでのメンションなし追加質問・管理者の添付の受信 |
   | `groups:history` | 非公開チャンネルの場合のみ（同上） |
   | `chat:write` | 回答の投稿・更新、管理者へのエフェメラル通知 |
   | `files:read` | 管理者が添付した資料のダウンロード |

4. - [ ] **Event Subscriptions → Subscribe to bot events** が `app_mention` / `message.channels` /（非公開なら）`message.groups` になっていることを確認する
   - `message.channels` / `message.groups` が無いと、**メンションなしのスレッド内追加質問と、メンションと別に送った添付ファイルを受信できません**
5. - [ ] **Socket Mode** が On、**Interactivity** が On であることを確認する（削除確認ボタン・ページ送りに必要。Request URL は不要）
6. - [ ] **Basic Information → App-Level Tokens → Generate Token and Scopes** で、スコープ `connections:write` のトークンを作る → `xapp-` で始まる値を控える（`SLACK_APP_TOKEN`）
7. - [ ] **OAuth & Permissions → Install to Workspace** → 承認 → **Bot User OAuth Token**（`xoxb-`）を控える（`SLACK_BOT_TOKEN`）
   - スコープやイベントを後から変更した場合は、**再インストール**が必要です
8. - [ ] 授業チャンネルで `/invite @ClaudeBot` を実行してボットを招待する

## 3. ID を調べる

- [ ] **チャンネル ID**：チャンネル名 → 詳細 → 最下部の `C...`（非公開でも `C...` のことがあります）→ `SLACK_CHANNEL_ID`
- [ ] **管理者のユーザー ID**：プロフィール → 「︙」→ **メンバー ID をコピー**（`U...`）→ `SLACK_ADMIN_USER_IDS`（カンマ区切り）
  - 表示名やメールアドレスではなく、必ず ID を使ってください（表示名は誰でも変更できます）
- [ ] （任意）**ワークスペース ID**（`T...`）→ `SLACK_TEAM_ID`。設定すると Slack Connect の外部組織ユーザーからの操作を無視します

## 4. サーバーに配置して設定する

```bash
git clone <このリポジトリ> classbot && cd classbot
cp .env.example .env
chmod 600 .env
```

- [ ] `.env` に次を設定する

  | 変数 | 値 |
  |---|---|
  | `SLACK_BOT_TOKEN` | `xoxb-...` |
  | `SLACK_APP_TOKEN` | `xapp-...` |
  | `SLACK_CHANNEL_ID` | `C...` |
  | `SLACK_ADMIN_USER_IDS` | `U...,U...` |
  | `ANTHROPIC_API_KEY` | 手順 1 のキー |
  | `CLAUDE_MODEL` | `claude-sonnet-5-5`（既定） |
  | `CLAUDE_EFFORT` | `medium`（既定） |
  | `CLAUDE_REFUSAL_FALLBACK` | `false`（既定。[COST.md](COST.md) 参照） |
  | `DAILY_REQUEST_LIMIT_PER_USER` | 例 `30` |
  | `MONTHLY_BUDGET_USD` | 例 `20` |
  | `MAX_CONCURRENT_REQUESTS` | 例 `3` |
  | `TIMEZONE` | `Asia/Tokyo`（日次・月次の区切り） |

- [ ] `config/pricing.toml` の単価を、公式料金ページ（https://platform.claude.com/docs/en/about-claude/pricing ）と照合する
- [ ] `config/system_prompt.md` に授業固有の方針（科目名、問い合わせ先、回答してほしくない話題など）を追記する
- [ ] データ用ディレクトリの所有者を、コンテナのユーザー（UID 10001）に合わせる

```bash
mkdir -p materials data
sudo chown -R 10001:10001 materials data
```

- [ ] `git status` に `.env` が表示されないことを確認する（`.gitignore` 済み）

## 5. 起動する

```bash
docker compose up -d --build
docker compose ps                 # STATUS が healthy になるまで 1〜2 分待つ
docker compose logs -f classbot
```

- [ ] ログに `classbot started` が出る
- [ ] ログに `missing required settings`（終了コード 2）や `pricing configuration error`（料金表にモデルが無い）が出ていない
- [ ] `docker compose ps` が `healthy` になる
- [ ] ログに API キーやトークンの文字列が出ていない（`docker compose logs classbot | grep -E "xox|xapp|sk-ant"` の結果が空）

## 6. 最初の動作確認（実際の Slack・API を使う。少額の API 費用が発生します）

**資料の登録**

- [ ] サーバーで、テスト用の資料（例 `第01回_テスト.md` に「第1回の課題はテスト提出です」と書いたもの）を `materials/` にコピーする
- [ ] 1 分以内に、管理者が `@ClaudeBot 資料一覧` を実行し、`✅ 検索可能` と表示される
- [ ] PDF・PPTX・DOCX を 1 つずつ置き、それぞれ `検索可能` になる（スキャン PDF は `テキストなし` になるのが正常）

**質問と回答**

- [ ] 学生役のアカウントで `@ClaudeBot 第1回の課題は？` → スレッドに「資料を検索しています…」が出て、回答に置き換わる
- [ ] 回答の末尾に `出典：第01回_テスト.md ...` が表示される
- [ ] 同じスレッドで、**メンションなし**で「締切は？」と送ると回答が返る（返らない場合は手順 2-4 のイベントと再インストールを確認）
- [ ] 資料に無いことを聞くと「登録された資料からは確認できませんでした」と答える
- [ ] スレッド外の通常の投稿にはボットが反応しない
- [ ] 他のチャンネルでメンションしても反応しない

**管理者機能**

- [ ] 学生役で `@ClaudeBot 資料一覧` → 「管理者のみ」と表示される
- [ ] 管理者で `@ClaudeBot 資料登録` と同時に PDF を添付 → 「受け付けました」→ 完了通知が届く
- [ ] 管理者で `@ClaudeBot 資料登録`（添付なし）→ 案内が出た後にファイルを添付 → 登録される
- [ ] `@ClaudeBot 資料削除` → 削除ボタン → 確認ダイアログ → 削除される。ファイルが `data/trash/` に移動している
- [ ] `@ClaudeBot 利用状況` に、今のテストの件数と推定費用が表示される
- [ ] `@ClaudeBot ステータス` で、Slack 接続が「接続中」と表示される

**費用の照合**

- [ ] 翌日、Anthropic Console の Usage と `利用状況` の推定費用を比べ、大きくずれていないことを確認する（[COST.md](COST.md) の「差が出る理由」を参照）

## 6.1 画像付き質問の動作確認（実際の Slack・API を使う）

テスト用に、個人情報が写っていない画像（授業スライドのスクリーンショット、エラー画面、図など）を用意してください。

**基本**

- [ ] 学生役で PNG を 1 枚添付して `@ClaudeBot この画像について教えて` → 画像の内容に触れた回答が返る
- [ ] 画像 3 枚を同じ投稿に添付して質問 → 3 枚すべてに触れた回答が返る
- [ ] スマートフォンで縦向きに撮影した JPEG を添付 → 向きが正しく解釈される（回転した文字として扱われない）
- [ ] WebP を添付 → 回答が返る
- [ ] アニメーション GIF を添付 → 回答末尾に「アニメーション画像は最初のフレームだけを参照しました」と表示される
- [ ] メンションに画像だけを添付（本文なし）→ 画像の説明が返る

**拒否されること**

- [ ] HEIC（iPhone の既定形式）を添付 → 「対応していない画像形式」と表示され、`利用状況` の件数が増えない
- [ ] 画像 5 枚以上を添付 → 「画像は1回の質問につき4枚まで」と表示される
- [ ] 10MB を超える画像を添付 → 「ファイルサイズが上限を超えています」と表示される

**スレッドと資料**

- [ ] 画像付き質問のスレッドで、メンションなしで「右上の値は何？」→ 先の画像を参照した回答が返る
- [ ] 同じスレッドで、新しい画像を添付してメンションなしで質問 → 新しい画像を参照した回答が返る
- [ ] 授業資料に関係する画像（例：配布スライドの一部）で質問 → 回答に「添付画像によると」と資料の出典（`出典：…`）の両方が含まれる

**管理者の操作と混同しないこと**

- [ ] 管理者で `@ClaudeBot 資料登録` に画像だけを添付 → 「画像は授業資料として登録できません」と表示され、資料一覧に増えない
- [ ] 管理者で `@ClaudeBot この図の意味は？` に画像を添付 → 画像付き質問として回答される（資料登録されない）

**保存期間とログ**

- [ ] `.env` で `IMAGE_RETENTION_HOURS=0.01`（約 36 秒）・`IMAGE_CLEANUP_INTERVAL_SEC=60` にして `docker compose up -d` → 画像付き質問の 2 分後に `data/images/` が空になる → 同じスレッドで追加質問すると「保存期間を過ぎたため参照できません」と添えられる（確認後に設定を戻す）
- [ ] `docker compose logs classbot | grep -E "iVBOR|/9j/|UklGR|base64"` の結果が空（画像データがログに出ていない）
- [ ] ログにテスト画像のファイル名が出ていない（`docker compose logs classbot | grep <ファイル名>` が空）
- [ ] `@ClaudeBot 利用状況` に画像付き質問の件数・枚数が表示され、翌日 Anthropic Console の Usage と大きくずれていない

## 6.2 回答方針の確認（実際の API を使う）

授業資料に載っていない技術的な質問で、回答をあきらめずに具体的に案内することを確認します。

- [ ] `@ClaudeBot sudo apt update で「E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 2345 (unattended-upgr)」が出ます` → 原因（自動更新が実行中）・確認方法（`ps` など）・手順が示される。**ロックファイルの削除を安易に勧めない**。出典は表示されない。冒頭に「資料にありません」と長く断らない
- [ ] `@ClaudeBot python3 app.py で ModuleNotFoundError: No module named 'requests'` → `pip install` と仮想環境の確認が案内される
- [ ] `@ClaudeBot WordPress で「Error establishing a database connection」` → `wp-config.php` の確認・DB サービスの状態確認が案内され、パスワードを伏せて共有するよう添えられる
- [ ] `@ClaudeBot Nginx が起動しません` のように情報が少ない質問 → `systemctl status nginx` / `journalctl -u nginx` / `nginx -t` の結果など、必要な情報を尋ねる
- [ ] `@ClaudeBot 容量が足りないので / 以下を全部消していい？` などの危険な操作 → 影響の説明・確認・バックアップが先に案内され、危険なコマンドをそのまま勧めない
- [ ] `@ClaudeBot sshd_config で PasswordAuthentication no にしたい` → 別セッションでの接続確認など、締め出しを防ぐ注意が添えられる
- [ ] エラー画面のスクリーンショットを添付して `@ClaudeBot このエラーは？` → 画像内のエラーメッセージを読み取って解説する
- [ ] `DB_PASSWORD=...` を含むログを貼る → 返信に「伏せ字にしてから AI に送信しました」と添えられ、回答に元のパスワードが現れない
- [ ] 資料に載っている内容（例: 授業の SSH 手順）に関する技術的な質問 → 資料の手順が優先され、出典が表示される
- [ ] `@ClaudeBot 第7回の課題の締切は？`（資料に記載なし）→ 推測せず「登録された資料からは確認できませんでした」と答え、担当者への確認を勧める
- [ ] 新しいバージョンの仕様を尋ねる → バージョンにより異なる可能性と確認方法（`--version`、公式ドキュメント）が添えられる

## 7. 障害時の動作確認（任意だが推奨）

- [ ] `docker compose restart` → 再起動後も資料一覧・利用状況が保持されている
- [ ] `docker compose exec classbot classbot backup` → `data/backups/` にファイルができる
- [ ] 管理者の `.env` から一時的に `MONTHLY_BUDGET_USD=0.01` にして再起動 → 質問すると予算上限の案内が出る（確認後に元へ戻す）

## 8. 運用開始

- [ ] 学生に周知する：使い方（`@ClaudeBot ヘルプ`）、1 日の利用上限、AI の回答は誤りがあり得ること、締切や採点は必ず公式の案内を確認すること、質問内容・添付画像・資料が Anthropic API に送信されること、個人情報（顔・学籍番号・氏名など）が写った画像を送らないこと
- [ ] バックアップの保存先（`data/` と `materials/`）をサーバー外にも定期コピーする
- [ ] 学期ごとに、料金表・モデル ID・依存ライブラリ（`uv lock --upgrade` → `uv run pytest`）を見直す
- [ ] 問い合わせ・障害時の連絡先（管理者）を決めておく

障害対応やバックアップからの復元は [OPERATIONS.md](OPERATIONS.md) を参照してください。
