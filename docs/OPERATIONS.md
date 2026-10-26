# 運用マニュアル

コマンド例は Docker Compose 運用を前提とします（`docker compose exec classbot classbot <cmd>`）。
以下では `classbot <cmd>` と略記します。

## 1. 起動・停止

| 操作 | コマンド |
|---|---|
| 起動（ビルド込み） | `docker compose up -d --build` |
| 停止 | `docker compose stop` |
| 再起動 | `docker compose restart` |
| 停止して削除（データは残る） | `docker compose down` |
| 状態確認 | `docker compose ps`（`healthy` であること） |

- `restart: unless-stopped` により、異常終了やホスト再起動後も自動で再起動します。
- 資料・インデックス・利用履歴・会話履歴はすべて `data/`（SQLite）と `materials/` に永続化されます。
- 停止時に処理中だったジョブは、起動時に再実行されます。資料解析は最初からやり直し、質問は `QA_JOB_MAX_AGE_MIN` 分以内のものだけ再実行し、それより古いものは失敗通知します。

## 2. 資料管理

資料には 2 つの登録経路があり、保存場所と管理情報を分離しています。

| 経路 | 保存場所 | 管理情報 | 更新方法 | 削除方法 |
|---|---|---|---|---|
| サーバー配置 | `materials/`（相対パスで識別） | `source=server` | ファイルを上書き | ファイルを削除 / Slack の `資料削除` |
| Slack 登録 | `data/uploads/`（内部名で保存） | `source=slack` | `資料更新 <ID>` | Slack の `資料削除` |

同じファイル名の資料が両方にあっても別資料（別 ID）として扱われ、競合しません。

### 2.1 サーバーから追加・更新・削除

- **追加**：`materials/` にコピー（サブフォルダ可）。watchdog による即時検知に加え、起動時と `SCAN_INTERVAL_SEC` ごとに再照合するため取りこぼしません。
- **更新**：同名で上書き。SHA-256 で内容を比較し、内容が変わっていなければ再処理しません。
- **削除**：ファイルを削除すると検索対象から外れます。
- コピー途中のファイルは、最終更新から `FILE_STABLE_SEC` 秒経過するまで処理を待ちます。
- 隠しファイル・`~$`・`.part`・`.crdownload`・`.tmp`・`.swp`・未対応形式は無視します。`materials/` の外を指すシンボリックリンクも無視します。

### 2.2 Slack から登録・更新・削除（管理者のみ・授業チャンネル内）

- **登録**：`@ClaudeBot 資料登録` とファイルを同じメッセージで送信。別々でも可（メンション後 `UPLOAD_PENDING_MINUTES` 分以内に添付、または添付後に同時間内にメンション）。処理はバックグラウンドで行い、完了・失敗をスレッドに通知します。
- **更新**：`@ClaudeBot 資料更新 <ID>` と差し替えファイルを送信。サーバー配置資料は対象外（サーバー上のファイルを置き換えてください）。
- **削除**：`@ClaudeBot 資料削除` → 一覧から選択 → 確認ダイアログで確定。`@ClaudeBot 資料削除 <ID>` で直接指定も可。
- 形式・サイズが不正な場合は理由を返信します。

### 2.3 更新失敗時の挙動

新しい版の抽出・登録に失敗した場合、**最後に正常登録できた版が検索対象のまま残ります**（資料一覧に警告表示）。
新しい版のチャンクを全て書き込んだ後に 1 トランザクションで切り替えるため、途中で停止しても中途半端な状態にはなりません。

### 2.4 スキャンPDF

テキストを含まない PDF は状態 `no_text` となり、「テキストを抽出できませんでした」と通知されます。
OCR を使う場合は `.env` で `INSTALL_OCR=true`・`OCR_ENABLED=true` にして `docker compose up -d --build` し、`classbot reindex --id <ID> --force` を実行します。OCR はローカル処理で、CPU 時間がかかります。

### 2.5 ゴミ箱からの復元

Slack からの削除・資料更新で置き換えられた古いファイルは `data/trash/` にタイムスタンプ付きで移動されます。

- 復元：ファイルを `materials/` にコピー（サーバー配置資料として再登録されます）。
- 容量が気になる場合は古いものから手動で削除してください（自動削除はしません）。

### 2.6 再インデックス

| 目的 | 方法 |
|---|---|
| 照合のみ（変更分だけ処理） | `classbot scan` |
| 特定資料を再処理 | `classbot reindex --id <ID> --force` / Slack `再インデックス <ID>` |
| 全資料を再処理 | `classbot reindex --force` / Slack `再インデックス` |

CLI はジョブを DB に登録し、稼働中のボットのワーカーが処理します。ボット停止中に処理したい場合は `--inline` を付けます。

検索の正規化ルール（`textnorm.TOKENIZER_VERSION`）が変わったバージョンに更新した場合は、起動時に自動で全資料の再インデックスが登録されます。抽出ロジックの変更時は手動で `classbot reindex --force` を実行してください。

### 2.7 学生の質問画像

- 保存先：`data/images/`（正規化・メタデータ除去済み）。メタデータは DB の `images` テーブル。
- 保存期間：`IMAGE_RETENTION_HOURS`（既定 24 時間）。`IMAGE_CLEANUP_INTERVAL_SEC` ごと（既定 10 分）と起動時に期限切れを削除します。DB に記録の無い孤立ファイル（1 時間以上前のもの）も削除します。
- すぐに全画像を消したい場合：`docker compose stop` → `rm -f data/images/*` → `docker compose start`。DB の行は残りますが、ファイルが無い画像は「保存期間を過ぎた」扱いになり、次回の削除処理で行も整理されます（SQL での手作業は不要）。
- 機能を止める：`.env` で `IMAGE_INPUT_ENABLED=false` → `docker compose up -d`。画像付きの投稿には「画像付きの質問は受け付けていません」と返します。
- 利用量：`利用状況` / `classbot usage` に画像付き質問の件数・枚数・推定画像トークンと、現在保存中の画像枚数が表示されます。

| 学生に表示される案内 | 原因 | 対応 |
|---|---|---|
| 対応していない画像形式が含まれています | HEIC・SVG・BMP・TIFF など | PNG / JPEG に変換して再投稿（iPhone は「互換性優先」設定で JPEG になる） |
| 画像は1回の質問につき N 枚までです | `IMAGE_MAX_PER_MESSAGE` 超過 | 枚数を減らす、または設定値を見直す |
| 画像のファイルサイズが上限を超えています | `IMAGE_MAX_FILE_MB` 超過 | 縮小して再投稿 |
| 添付画像を読み取れなかったため回答できませんでした | 壊れた画像・形式の偽装・画素数過大・ダウンロード失敗 | 画像を保存し直して再投稿。続く場合は `files:read` スコープとログ（`image processing failed`）を確認 |
| 保存期間を過ぎたため参照できません | 追加質問の時点で画像が削除済み | 画像を再度添付して質問 |

## 3. バックアップと復元

### バックアップ

- 自動：`BACKUP_INTERVAL_HOURS` ごとに `data/backups/` に作成し、`BACKUP_KEEP` 世代を保持します。
- 手動（稼働中でも安全）：`classbot backup`
- ホストの `sqlite3` を使う場合：`sqlite3 data/classbot.db ".backup data/backups/manual.db"`
- コンテナ停止中のファイルコピー：`docker compose stop && cp data/classbot.db* /backup/ && docker compose start`
  （稼働中に `classbot.db` 単体を `cp` すると WAL の内容が欠けるため避けてください）
- `materials/` と `data/uploads/` も合わせてバックアップしてください（DB はファイル本体を含みません）。学生の質問画像（`data/images/`）は一時データのため、外部バックアップから除外することを推奨します。

### 復元

```bash
docker compose stop
mv data/classbot.db data/classbot.db.broken
rm -f data/classbot.db-wal data/classbot.db-shm
cp data/backups/<バックアップ>.db data/classbot.db
sudo chown 10001:10001 data/classbot.db
docker compose start
docker compose exec classbot classbot scan
```

起動時の照合で `materials/` との差分は自動で修復されます。

### DB 破損時（終了コード 3）

起動時に `PRAGMA quick_check` で DB の整合性を確認します。破損を検出すると、ログに `database is corrupted; refusing to start`（CRITICAL）を出して終了コード 3 で停止します。

- `restart: unless-stopped` のため、**復元するまでコンテナは起動と停止を繰り返します**。`docker compose stop` で止めてから、上の「復元」の手順でバックアップから戻してください。
- 最新のバックアップより後の利用記録・会話履歴は失われます。資料は `materials/` と `data/uploads/` から再照合され、Slack 登録資料は DB の記録が無いと一覧に戻りません。その場合は、`data/uploads/` 内のファイル（`<uuid>__元の名前`）を Slack から再登録してください。

## 4. ログとヘルスチェック

- ログは標準出力に JSON 形式（1 行 1 イベント）で出力されます：`docker compose logs -f classbot`
- `jq` で絞り込み：`docker compose logs --no-log-prefix classbot | jq 'select(.level=="ERROR")'`
- ログは json-file ドライバで 10MB × 5 世代にローテーションされます。
- トークン・API キーはログ出力前にマスクされます。質問本文はログに記録しません。
- ヘルスチェック：ボットは `HEARTBEAT_INTERVAL_SEC` ごとに `data/heartbeat.json` を更新し、`classbot healthcheck` がその鮮度を確認します。`docker compose ps` で `unhealthy` の場合はログを確認して `docker compose restart` してください。
- Slack の状態確認：`@ClaudeBot ステータス`

## 5. 障害対応

| 状況 | ボットの挙動 | 管理者の対応 |
|---|---|---|
| Claude API タイムアウト | SDK が指数バックオフで自動リトライ。最終的に失敗したらスレッドで通知（予約額は解放） | 多発する場合は `CLAUDE_TIMEOUT_SEC` を延長、`CLAUDE_EFFORT` を下げる |
| レート制限（429） | 自動リトライ後、「混雑しています」と通知 | `MAX_CONCURRENT_REQUESTS` を下げる。Console で利用ティアを確認 |
| API 認証エラー | 「管理者に連絡してください」と通知、ERROR ログ | `ANTHROPIC_API_KEY` を確認して再起動 |
| 月額予算超過 | API に送信せず、上限到達を通知 | `MONTHLY_BUDGET_USD` を見直して再起動（翌月 1 日に自動リセット） |
| Slack 接続切断 | Socket Mode クライアントが自動再接続。`SOCKET_DISCONNECT_EXIT_SEC` 秒以上復旧しなければプロセス終了 → 再起動ポリシーで復旧 | トークン失効・ネットワークを確認 |
| DB ロック | WAL + busy_timeout + リトライで待機 | 外部ツールで長時間トランザクションを張らない |
| ファイル抽出エラー | その資料のみ `failed`。他の資料処理は継続。子プロセスのタイムアウト・メモリ上限で本体は停止しない | 資料一覧でエラー内容を確認し、ファイルを修正して再配置 |
| インデックス更新失敗 | 旧版を検索対象として維持 | 原因を解消後 `classbot reindex --id <ID> --force` |
| プロセス再起動 | 実行中ジョブを再登録（3回中断したジョブは失敗扱い）、回答作成済みで未送信の質問は API を再度呼ばずに送信、残った予算予約を保守的に計上、未完了の版を掃除、`materials/` を再照合 | 不要 |
| 常駐タスクの異常終了 | プロセスを終了コード 1 で停止し、再起動ポリシーで復旧 | 繰り返す場合はログを確認 |
| Slack への投稿失敗 | 回答は DB に保存し、投稿だけを再試行（API は再度呼ばない） | Slack の障害情報を確認 |
| DB 破損 | 起動時に検出して終了コード 3 で停止 | 「3. バックアップと復元」の手順で復元 |
| ディスク逼迫 | 書き込み失敗としてジョブ失敗 | `data/trash/`・古いバックアップを整理 |

## 6. システムプロンプトの編集

`config/system_prompt.md` を編集します（コンテナには読み取り専用でマウント）。次の質問から反映されます。

- 回答方針・口調・授業固有の注意事項などを記述できます。
- 出典 ID の出力形式とプロンプトインジェクション対策の指示はコード側で常に付加されるため、このファイルから削除しても無効にはなりません。
- 頻繁な変更はプロンプトキャッシュを無効化するため、費用がわずかに増えます。

### 既定の回答方針

既定の `config/system_prompt.md` は次の方針です。授業に合わせて調整してください。

- 関連する資料があれば最優先（出典ID付き）。資料にない技術的な質問・エラーにも一般知識で回答し、「資料にない」ことを理由に回答をあきらめない。資料にない旨は必要なときだけ短く補足する。
- エラー相談は「考えられる原因 → 確認方法 → 解決手順 → 解決しないときに送ってほしい情報」の順。情報不足なら必要なログ・バージョン等を質問する。スクリーンショットのエラーも読み取る。
- 削除・上書き・権限変更・SSH/ファイアウォール変更などは影響を説明し、確認とバックアップを先に案内する。
- ログ共有時は API キー・パスワード・秘密鍵を伏せるよう案内する。バージョン依存の情報は知識が古い可能性を添え、確認方法を示す。
- 締切・提出方法・採点・出席などは資料に根拠がない限り断定しない。

コード側でも次を常に行います（このファイルを編集しても無効になりません）。

- 質問が授業固有の情報（締切・提出・採点・出席などの語を含む）に関わる場合、「資料に根拠がなければ断定しない」注意をその質問に添える。
- 質問文中の秘密情報らしき文字列（`DB_PASSWORD=...`、`sk-ant-...`、秘密鍵ブロック、`mysql -p...`、URL 中のパスワードなど）を `****` に置き換えてから検索・API 送信・履歴保存を行い、学生に伏せ字にした旨を返信に添える。
- 情報の新しさを判断できるよう、質問に今日の日付を添える。

## 7. 料金テーブルとコスト設定

- `config/pricing.toml` にモデル別の単価（USD / 100 万トークン：入力・出力・キャッシュ書き込み・キャッシュ読み込み）を記載します。**公式の料金ページで最新の単価を確認して更新してください。**
- 既定モデルは `claude-sonnet-5-5` です。費用の内訳、推定費用と実請求の違い、フォールバックの追加費用は [COST.md](COST.md) を参照してください。
- `CLAUDE_MODEL` が料金テーブルにないと起動しません（予算管理ができないため）。フォールバック等で表に無いモデルが応答した場合は、表の最大単価で保守的に計上します。
- 上限値（`DAILY_REQUEST_LIMIT_PER_USER` / `MONTHLY_BUDGET_USD` / `MAX_CONCURRENT_REQUESTS` ほか）は `.env` を編集して `docker compose up -d` で反映します。
- 月の利用状況は `@ClaudeBot 利用状況` または `classbot usage` で確認できます。表示は推定値であり、実際の請求は Anthropic Console で確認してください。

## 8. アップグレード

```bash
classbot backup                         # 事前バックアップ（docker compose exec 経由）
git pull
docker compose up -d --build
docker compose logs -f classbot         # 起動ログを確認
```

- DB スキーマは起動時に自動で作成・更新されます。
- 依存ライブラリの更新は `uv lock --upgrade` → テスト（`uv run pytest`）→ 再ビルド。
- 抽出ロジックに変更がある場合は `classbot reindex --force` を実行します（検索の正規化ルールの変更は起動時に自動で再インデックスされます）。
- モデルを変更する場合は `config/pricing.toml` に単価があることを確認してから `CLAUDE_MODEL` を変更します。
- `.env` に書かれた値はコードの既定値より優先されます。`.env.example` の既定値が変わった項目（例: 検索量を増やした版の
  `SEARCH_TOP_K=10` / `SEARCH_MAX_PER_MATERIAL=5` / `SEARCH_CONTEXT_MAX_CHARS=24000` / `FOCUS_MATERIAL_MAX_CHARS=60000`）は、
  `.env` を見比べて必要なら更新してください。
