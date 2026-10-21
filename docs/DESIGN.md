# 設計とモジュール間インターフェース

単一プロセス（asyncio）＋SQLite の構成。マイクロサービスは使わない。

```
Slack (Socket Mode) ──> slack_app.py ──(即時ACK・DB に job 登録)──> jobs テーブル
                                                                    │
                     ┌──────────────────────────────────────────────┤
                     ▼                                              ▼
            QA ワーカー × MAX_CONCURRENT_REQUESTS          資料ワーカー × 1
            search.py → budget.py → llm.py → Slack       indexer.py（抽出は子プロセス）
                                                                    ▲
                               scanner.py（起動時・定期・watchdog）──┘
```

## 確定済み共通モジュール（変更時は全体へ影響）

- `classbot.config.Settings` / `get_settings()` — 全設定。
- `classbot.db.Database` — `connect()`, `tx()`, `query()`, `query_one()`, `execute()`, `run()`, `init_schema()`, `backup_to()`、`now_iso()`, `utcnow()`。スキーマは `db.py` の SCHEMA が唯一の正。
- `classbot.textnorm` — `normalize()`, `index_tokens()`, `query_tokens()`, `fts_match_expr()`, `lecture_numbers()`。
- `classbot.logging_setup` — `setup_logging()`, `redact()`。

## モジュール契約

### A. 抽出（`extract.py`, `safety.py`, `chunking.py`）

```python
# safety.py
SUPPORTED_TYPES = {"pdf", "pptx", "docx", "txt", "md"}
class UnsupportedFileError(Exception): ...       # 利用者に理由を表示できる日本語メッセージ
def safe_filename(name: str) -> str              # パス区切り・制御文字・先頭ドット除去、長さ制限
def detect_file_type(filename: str) -> str | None   # 拡張子 → SUPPORTED_TYPES（.markdown→md）
def validate_file(path: Path, file_type: str, settings) -> None  # サイズ・マジックバイト・ZIP爆弾検査。NG なら UnsupportedFileError
def is_within(base: Path, target: Path) -> bool  # resolve 後に base 配下か（パストラバーサル/シンボリックリンク対策）
def sha256_file(path: Path) -> str

# chunking.py
@dataclass
class Segment:  text: str; location: str; page: int | None; heading: str | None
@dataclass
class Chunk:    seq: int; text: str; location: str; page: int | None; heading: str | None
def chunk_segments(segments: Iterable[Segment], size: int, overlap: int) -> Iterator[Chunk]
    # セグメント（ページ/スライド/見出し節）をまたがない。文境界で分割。同一資料内の完全重複は除去。

# extract.py
@dataclass
class ExtractResult: ok: bool; status: str  # "ready" | "no_text" | "failed"
                     chunks_path: Path | None; page_count: int | None; chunk_count: int; error: str | None
def iter_segments(path: Path, file_type: str, settings) -> Iterator[Segment]   # 子プロセス内で使う
def extract_to_jsonl(path: Path, file_type: str, settings, out_path: Path) -> ExtractResult
    # 子プロセス（spawn）で抽出→チャンク化し、1行1チャンクの JSONL を out_path に書く。
    # タイムアウト(extract_timeout_sec)で kill、RLIMIT_AS でメモリ制限。クラッシュしても親は落ちない。
    # テキストがほぼ無い PDF は status="no_text"（OCR 有効時は PyMuPDF+Tesseract でローカル OCR）。
```

位置表示（location）: PDF `p.12` / PPTX `スライド3` / DOCX `見出し「…」`（見出し無しは `段落12付近`）/ MD `見出し「…」` / TXT `L10-25`。

### B. 資料管理（`materials.py`, `indexer.py`, `scanner.py`）

```python
# materials.py
class MaterialStore:
    def __init__(self, db: Database, settings: Settings)
    def get(self, material_id: int) -> sqlite3.Row | None
    def list(self, offset: int, limit: int) -> tuple[list[Row], int]   # (rows, total)
    def file_path(self, row) -> Path      # server: materials_dir/rel_path, slack: uploads_dir/stored_name
    def upsert_server(self, rel_path, size, mtime, file_type) -> int
    def create_slack(self, display_name, stored_name, file_type, size, uploaded_by) -> int
    def delete(self, material_id: int) -> Row | None   # ファイルを trash へ移動 → チャンク/FTS/行を削除
    def status_counts(self) -> dict[str, int]

# indexer.py
class Indexer:
    def __init__(self, db, settings, store: MaterialStore)
    def index_material(self, material_id: int, force: bool = False) -> IndexOutcome  # 同期（スレッドで実行）
    # hash が indexed_hash と同じで ready なら skip。新 version のチャンクを書き込んでから
    # 1 トランザクションで active_version を切替え旧版を削除。失敗時は旧版を保持し last_error を記録。
    def replace_slack_file(self, material_id: int, new_tmp_path: Path, display_name: str) -> IndexOutcome
    def cleanup_orphans(self) -> None     # active でない version のチャンク削除（起動時）
@dataclass
class IndexOutcome: material_id: int; status: str; skipped: bool; message: str; chunk_count: int

# scanner.py
class Scanner:
    def __init__(self, db, settings, store, enqueue_ingest: Callable[[int], None])
    def scan(self) -> ScanReport          # materials/ と DB の照合。追加/変更(サイズ・mtime→hash)/削除を検知
    def start_watchdog(self, loop, on_change: Callable[[], None]) / stop()
```

### C. 検索・LLM・コスト（`search.py`, `pricing.py`, `budget.py`, `prompts.py`, `llm.py`, `citations.py`）

```python
# search.py
@dataclass
class SearchHit: source_id: str  # "S1".. ; chunk_id; material_id; display_name; location; text; score
@dataclass
class SearchResult: hits: list[SearchHit]; sufficiency: str  # "sufficient" | "weak" | "none"
class Searcher:
    def __init__(self, db, settings)
    def search(self, query: str, extra_context: str | None = None) -> SearchResult

# pricing.py
class PriceTable: load(path) ; price_for(model) -> ModelPrice ; conservative() -> ModelPrice
def cost_usd(price, input, output, cache_creation, cache_read) -> float

# budget.py
class BudgetManager:
    def __init__(self, db, settings, prices)
    def check_daily(self, user_id, is_admin) -> tuple[bool, int, int]       # (allowed, used, limit)
    def reserve(self, user_id, channel, thread_ts, est_input_tokens) -> Reservation | Rejection
    def settle(self, reservation, model, usage, request_id) / fail(reservation, error, billed_unknown: bool)
    def recover_stale(self)              # 起動時: 残った予約を予約額で計上（保守的）
    def summary(self) -> UsageSummary    # 日次/月次/ユーザー別

# prompts.py / citations.py / llm.py
def load_system_prompt(path) -> str
def build_messages(history, question, search_result) -> list[dict]
def parse_answer(text, hits) -> ParsedAnswer   # 出典IDを検証し、不正IDを除去。footer は DB 由来メタデータから生成
class ClaudeClient: async answer(system, messages) -> LLMResponse  # AsyncAnthropic
```

### D. Slack・ジョブ・起動（`jobs.py`, `commands.py`, `slack_format.py`, `slack_app.py`, `app.py`, `cli.py`, `healthcheck.py`）

- `jobs.JobQueue(db)`: `enqueue(kind, payload, dedupe_key=None)`, `claim(kind)`, `complete(id)`, `fail(id, err)`, `requeue_running()`。
- コマンドは `commands.parse_command(text) -> Command | None`（決定的な表引き。AIに解釈させない）。
- 冪等性: `processed_events` に `channel:ts` を INSERT OR IGNORE。app_mention と message の二重配送も同キーで排除。

### E. 画像入力（`images.py`, `slack_files.py`）

```
app_mention / message(file_share) ──> slack_app: split_files() で 画像 / 未対応画像 / その他 に分類
   │  枚数・形式・メタデータ上のサイズを受付時に検査（NG なら案内のみ・API 呼び出しなし）
   │  画像の file_id を QA ジョブの payload に入れる（app_mention に files が無い場合は
   │  message イベント側の添付を message_files に記録し、ワーカーが IMAGE_EVENT_GRACE_SEC 待って補完）
   ▼
qa_worker._prepare_images: files.info → files.slack.com から認証付き取得 → normalize_image()
   （内容で形式判定・画像爆弾検査・EXIF 除去の再エンコード・先頭フレーム・縮小）→ ImageStore.save()
   ▼
prompts.build_messages_with_images: 各ユーザーターンで 画像 → テキスト の順に配置。
   今回の画像を優先し、残り枠（IMAGE_MAX_PER_REQUEST）でスレッド履歴の画像を新しい順に再送
   ▼
予算予約（文字数見積もり + ⌈w/28⌉×⌈h/28⌉ の画像トークン）→ Claude（base64 画像ブロック）→ 精算
   ▼
会話履歴（conversation_turns）と画像を turn_images で関連付け。期限切れは ImageStore.cleanup() が削除
```

- 資料登録コマンドは画像以外（`is_material_file`）だけを対象にし、画像付き質問と混同しない。
- 画像は `data/images/` にのみ保存（DB はメタデータのみ）。画像データ・ファイル名・URL はログに出さない。
