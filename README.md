# classbot — 授業用 Slack AI アシスタント

大学の授業用 Slack チャンネルで、登録済みの授業資料を検索（RAG）し、根拠となる出典を示しながら
Anthropic Claude API で日本語回答するボットです。

- 学生は Claude のアカウントや API キー不要。API キーはサーバーで一元管理します。
- 授業資料は `materials/` にファイルを置くだけで自動登録（追加・更新・削除を自動検知）。
- 管理者は Slack からも資料の登録・更新・削除ができます。
- 質問と追加質問は Slack スレッド単位で管理します。
- 学生は質問に画像（PNG / JPEG / WebP / GIF）を添付でき、画像と授業資料の両方を参照して回答します。
- ユーザー別の日次上限・月額予算・同時実行数で API 費用を管理します。
- 構成は単一プロセス（Python / asyncio）＋ SQLite（FTS5 全文検索）。Docker Compose で常時稼働します。

設計の詳細は [docs/DESIGN.md](docs/DESIGN.md)、運用手順は [docs/OPERATIONS.md](docs/OPERATIONS.md)、
セキュリティは [docs/SECURITY.md](docs/SECURITY.md)、検索方式は [docs/search.md](docs/search.md) を参照してください。

## 動作の概要

```
学生: @ClaudeBot 第3回の課題について教えて
  └─ ClaudeBot（スレッド返信）:
       第3回の課題は … [S1]
       ※ 提出期限は登録された資料からは確認できませんでした。
       出典：第03回_演習問題.pdf p.2
学生（同じスレッド・メンション不要）: 提出形式は？
```

- 最初の質問にはメンションが必要です。ボットが回答したスレッドでは、誰でもメンションなしで追加質問できます。
- 指定した 1 つのチャンネル以外では応答しません。無関係な会話には割り込みません。
- 回答は「登録資料 → スレッドの文脈 → AI の一般知識」の優先順位で作られます。資料に基づく内容を先に出典ID付きで書き、資料に無い部分は「補足（AIの一般知識）」として区別して補います。資料に該当が無い質問も、その旨を1行添えたうえで AI の知識で回答します。
- 質問が指している資料は、キーワード検索に頼らず**全文**（既定 100,000 文字まで、`FOCUS_MATERIAL_MAX_CHARS`）を根拠として渡します。対象は、①そのスレッドで `資料登録` / `資料更新` した資料、②質問にファイル名が書かれた資料、③質問の「第N回」に該当する資料（ファイル名に無ければ資料の1ページ目から判定）、④①〜③が無く「今回の資料」「最新のスライド」のように尋ねた場合は最も新しく登録された資料です。参照した資料は、同じスレッドの追加質問でも引き続き参照します。例：資料登録したスレッドで `@ClaudeBot 今回渡した資料の授業内容と提出物は？`
- Ubuntu / Linux、SSH、Python、WordPress、ネットワーク、Web サーバーなどの技術的な質問やエラーには、資料に載っていなくても Claude の知識で「原因の候補 → 確認方法 → 解決手順」を案内します。情報が足りなければ必要なログやエラーメッセージを尋ね、危険な操作には影響の説明とバックアップを先に案内します。
- 課題の締切・提出方法・採点基準・出席などの授業固有の情報は、資料に書かれていればその内容を答えます。書かれていない具体的な値（締切日時・配点など）は AI が作り出さず「資料には記載がありません」と伝えたうえで、一般的な補足と授業担当者への確認を案内します。
- 質問に貼られたパスワード・API キー・SSH 秘密鍵などらしき文字列は、伏せ字にしてから Anthropic API に送信・保存します（完全な検出はできないため、学生には伏せて貼るよう案内します）。
- 出典はボットが実際に検索した資料のメタデータ（ファイル名・ページ／スライド／見出し／行）からコードで生成します。AI が存在しない資料名やページを出典に書いても表示されません。

### 画像付きの質問

```
学生: @ClaudeBot この画像について教えて   ［スクリーンショット.png を添付］
  └─ ClaudeBot: 添付画像によると … 授業資料では … [S1]
学生（同じスレッド・メンション不要）: 2行目のエラーはどう直せばいい？
```

- メンションと**同じ投稿**に画像を添付してください。ボットが回答したスレッドでは、メンションなしで画像付きの追加質問もできます。画像だけを送った場合は「添付した画像について説明してください」として扱います。
- 対応形式は PNG / JPEG / WebP / GIF です。GIF・WebP のアニメーションは最初のフレームだけを参照します。HEIC・SVG などは受け付けません。
- 1 投稿 4 枚まで、1 枚 10MB まで（`IMAGE_MAX_PER_MESSAGE` / `IMAGE_MAX_FILE_MB`）。送信前に長辺 1568px・約 120 万画素以下へ縮小し、メタデータ（撮影位置などの EXIF）を除去します。
- 同じスレッドの追加質問では、保存期間（既定 24 時間、`IMAGE_RETENTION_HOURS`）内の画像を再び参照します。期限を過ぎた画像は自動削除され、参照できない旨が回答に添えられます。
- 管理者の `資料登録` / `資料更新` は画像を資料として登録しません。管理者が通常の質問に画像を添付した場合は、学生と同じく画像付きの質問として扱います。
- 氏名・学籍番号・顔などの個人情報が写った画像は送らないよう学生に周知してください。

## クイックスタート

本番導入時は [docs/PRODUCTION_CHECKLIST.md](docs/PRODUCTION_CHECKLIST.md) の手順に沿って進めてください。

### 1. Slack アプリを作成する

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest** を選び、ワークスペースを選択。
2. 本リポジトリの [`slack-app-manifest.yaml`](slack-app-manifest.yaml) の内容を貼り付けて作成。
   - 授業チャンネルが公開チャンネルなら `groups:history` と `message.groups` は削除して構いません。
3. **Basic Information → App-Level Tokens → Generate Token and Scopes** で、スコープ `connections:write` のトークンを作成 → `SLACK_APP_TOKEN`（`xapp-...`）。
4. **OAuth & Permissions → Install to Workspace** → **Bot User OAuth Token** → `SLACK_BOT_TOKEN`（`xoxb-...`）。
5. 授業チャンネルでボットを招待：`/invite @ClaudeBot`

### 2. ID を調べる

- **チャンネルID**：チャンネル名をクリック → 詳細画面の最下部（`C` で始まる ID）→ `SLACK_CHANNEL_ID`
- **ユーザーID**：管理者のプロフィール → 「︙」→ **メンバーIDをコピー**（`U` で始まる ID）→ `SLACK_ADMIN_USER_IDS`（カンマ区切り）

### 3. 設定する

```bash
cp .env.example .env
chmod 600 .env
# .env を編集: SLACK_BOT_TOKEN / SLACK_APP_TOKEN / SLACK_CHANNEL_ID /
#              SLACK_ADMIN_USER_IDS / ANTHROPIC_API_KEY / CLAUDE_MODEL
```

- `CLAUDE_MODEL` は環境変数で指定します（ソースコードには固定していません。`.env.example` の既定は `claude-sonnet-5-5`）。`config/pricing.toml` に料金が登録されたモデルでなければ起動しません。モデルIDは Anthropic 公式ドキュメントで最新のものを確認してください。
- 必要に応じて `config/system_prompt.md`（回答方針）と `config/pricing.toml`（料金テーブル）を編集します。

### 4. 起動する

```bash
mkdir -p materials data
sudo chown -R 10001:10001 materials data   # コンテナは非root（UID 10001）で動作
docker compose up -d --build
docker compose logs -f
```

スキャンPDF用の OCR（ローカル Tesseract）を使う場合は `.env` で `INSTALL_OCR=true` と `OCR_ENABLED=true` を設定してビルドします。

### 5. 資料を追加する

```bash
cp 第01回_授業概要.pdf 第02回_講義資料.pptx シラバス.docx materials/
```

数秒〜数分で自動登録されます。Slack で `@ClaudeBot 資料一覧`（管理者）を実行すると状態を確認できます。

### ローカル実行（Docker なし）

```bash
uv sync
uv run classbot run
uv run pytest
```

## Slack コマンド

コマンドは AI に解釈させず、決定的なコードで処理します。管理操作は Slack のユーザーIDをサーバー側で検証します。

| コマンド | 対象 | 内容 |
|---|---|---|
| `@ClaudeBot ヘルプ` | 全員 | 使い方の表示 |
| `@ClaudeBot （質問文）` | 全員 | 資料を検索して回答（スレッド返信） |
| `@ClaudeBot 資料一覧 [ページ]` | 管理者 | ID・ファイル名・登録日時・更新日時・状態・サイズを表示（ページ分割） |
| `@ClaudeBot 資料登録` | 管理者 | 添付ファイルを登録。メンションと添付が別メッセージでも可（前後 `UPLOAD_PENDING_MINUTES` 分以内） |
| `@ClaudeBot 資料更新 <ID>` | 管理者 | Slack 登録資料を添付ファイルで差し替え（サーバー配置資料はファイルを置き換えて更新） |
| `@ClaudeBot 資料削除 [ID]` | 管理者 | 削除対象を選択 → 確認ダイアログ → 削除（ID で管理し同名資料の誤削除を防止） |
| `@ClaudeBot 再インデックス [ID]` | 管理者 | 全資料または指定資料を再処理 |
| `@ClaudeBot 利用状況` | 管理者 | 日次・月次のリクエスト数・トークン数・推定費用・ユーザー別利用状況 |
| `@ClaudeBot ステータス` | 管理者 | 稼働時間・キュー・資料の状態・モデル・最終スキャンなど |

対応形式：PDF / PPTX / DOCX / TXT / Markdown（既定の上限 30MB）。

## CLI

コンテナ内では `docker compose exec classbot classbot <コマンド>`、ローカルでは `uv run classbot <コマンド>`。

| コマンド | 内容 |
|---|---|
| `classbot run` | ボットを起動（Docker の既定コマンド） |
| `classbot scan` | `materials/` と DB を照合し、変更をインデックスジョブとして登録 |
| `classbot reindex [--id N] [--force]` | 再インデックス（`--force` で内容未変更でも再処理） |
| `classbot materials` | 登録資料の一覧 |
| `classbot usage` | 利用状況・推定費用の表示 |
| `classbot backup` | SQLite のオンラインバックアップを `data/backups/` に作成 |
| `classbot healthcheck` | ヘルスチェック（ハートビートの鮮度を確認。異常時は終了コード 1） |

## Anthropic API へ送信されるデータ

質問ごとに次の内容が Anthropic API に送信されます。

- システムプロンプト（`config/system_prompt.md`）
- 学生の質問文と、同じスレッド内のボット宛て質問・ボット回答（直近のみ）
- **検索でヒットした授業資料の該当部分（チャンク）とファイル名・位置情報**
- **学生が質問に添付した画像**（縮小・メタデータ除去後。同じスレッドの追加質問では保存期間内の画像を再送）

資料全体を一括送信することはありませんが、検索に該当した部分は送信されます。
**機密資料や個人情報（成績、学籍番号、氏名一覧など）を含む資料は登録しないでください。**
資料の OCR はローカルの Tesseract で行い、資料の画像を API に送信することはありません（API に送られる画像は、学生が質問に添付した画像だけです）。詳細は [docs/SECURITY.md](docs/SECURITY.md)。

## コスト管理についての注意

- 表示される費用は、API 応答のトークン数と `config/pricing.toml` の単価から計算した**推定値**です。実際の請求額は Anthropic Console の請求情報で確認してください（料金改定・端数・税・その他の課金項目により差が出ます）。
- 月額予算は、リクエスト受付時に「今月の使用額＋実行中の予約額＋今回の最大見積もり」が上限以内かを判定し、超える場合は API に送信しません。実行後に実際のトークン数で精算します。
- 費用の目安・推定と実請求の違い・フォールバックの追加費用は [docs/COST.md](docs/COST.md) を参照してください。
- **上限はこのボット経由の利用量に対するものです。** 同じ API キーを別のシステムで使用した場合の費用は管理できません。授業専用の API キー（可能なら Console のワークスペース単位の利用上限も併用）を推奨します。

## ディレクトリ構成

```
├── src/classbot/        アプリケーション本体
├── tests/               pytest
├── config/              system_prompt.md / pricing.toml（管理者が編集）
├── materials/           サーバー配置の授業資料（ホストから編集可）
├── data/                SQLite DB・Slack 登録資料（uploads/）・ゴミ箱（trash/）・バックアップ
├── docs/                設計・運用・セキュリティ・検索方式
├── Dockerfile / compose.yaml / .env.example / slack-app-manifest.yaml
```
