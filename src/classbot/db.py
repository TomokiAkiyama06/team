"""SQLite 永続化層。

- WAL モード + busy_timeout でボット本体と CLI の同時アクセスに耐える。
- 接続はスレッドごとに分ける（sqlite3 接続はスレッド間共有しない）。
- async コードからは `await db.run(fn, ...)` でワーカースレッド上で実行する。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 資料。source='server' は materials/ 配下（rel_path で一意）、
-- source='slack' は data/uploads/ 配下（stored_name）。
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK (source IN ('server', 'slack')),
    rel_path TEXT,                 -- server: materials/ からの相対パス
    stored_name TEXT,              -- slack: uploads/ 内の保存ファイル名
    display_name TEXT NOT NULL,    -- 出典表示用の元ファイル名
    file_type TEXT NOT NULL,       -- pdf | pptx | docx | txt | md
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime REAL,                    -- server: 最終確認時の mtime
    current_hash TEXT,             -- 現在のファイルの SHA-256
    indexed_hash TEXT,             -- 検索対象になっている版の SHA-256
    active_version INTEGER,        -- 検索対象の chunks.version（NULL=未登録）
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'indexing', 'ready', 'failed', 'no_text')),
    last_error TEXT,
    page_count INTEGER,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    uploaded_by TEXT,              -- slack: 登録した管理者のユーザーID
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    indexed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_materials_server_path
    ON materials(rel_path) WHERE source = 'server';
CREATE UNIQUE INDEX IF NOT EXISTS ux_materials_slack_name
    ON materials(stored_name) WHERE source = 'slack';

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    page INTEGER,                  -- PDF ページ / PPTX スライド番号（1始まり）
    location TEXT NOT NULL,        -- 出典表示用の位置（"p.12" / "スライド3" / "見出し「…」" / "L10-25"）
    heading TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_material ON chunks(material_id, version);

-- 検索用 FTS5。rowid = chunks.id。
-- title_tokens / body_tokens は textnorm.index_tokens() が生成した空白区切りトークン列。
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title_tokens, body_tokens,
    tokenize = "unicode61 remove_diacritics 0"
);

CREATE TABLE IF NOT EXISTS version_seq (
    id INTEGER PRIMARY KEY AUTOINCREMENT
);

-- 永続ジョブキュー（kind: qa | ingest | delete | slack_upload | reindex_all）
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,         -- JSON
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(kind, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_dedupe
    ON jobs(dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running');

-- Slack イベントの冪等性（key = "channel:ts"）
CREATE TABLE IF NOT EXISTS processed_events (
    event_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- ボットが参加しているスレッドと、ボット宛ての会話履歴のみを保存する
CREATE TABLE IF NOT EXISTS threads (
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, thread_ts)
);
CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    user_id TEXT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_turns_thread ON conversation_turns(channel, thread_ts, id);

-- 利用量（1 リクエスト 1 行）。status: pending | ok | error | rejected_budget | rejected_daily
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    user_id TEXT NOT NULL,
    channel TEXT,
    thread_ts TEXT,
    model TEXT,
    status TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    cost_is_estimate_only INTEGER NOT NULL DEFAULT 0,  -- 1=実測不能で予約額を計上
    request_id TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_usage_created ON usage_events(created_at);
CREATE INDEX IF NOT EXISTS ix_usage_user ON usage_events(user_id, created_at);

-- 同時実行時の予算超過を防ぐ予約
CREATE TABLE IF NOT EXISTS budget_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_event_id INTEGER NOT NULL,
    amount_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);

-- Slack 経由の資料登録待ち（メンションとファイル添付が別イベントの場合）
CREATE TABLE IF NOT EXISTS pending_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    reply_thread_ts TEXT NOT NULL,
    target_material_id INTEGER,    -- NULL=新規登録 / 値あり=資料更新
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 質問ジョブの進捗（再起動時に二重回答・二重課金を防ぐ。job_key = "channel:ts"）
CREATE TABLE IF NOT EXISTS qa_progress (
    job_key TEXT PRIMARY KEY,
    placeholder_ts TEXT,           -- 「検索しています…」メッセージの ts（再実行時は再利用）
    answer_text TEXT,              -- 確定した返信本文（API 呼び出し・課金済み）
    delivered INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
-- 学生の質問画像（正規化・メタデータ除去済みのファイルを data/images/ に保存し、期限で削除）
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    user_id TEXT NOT NULL,
    slack_file_id TEXT NOT NULL,
    path TEXT NOT NULL,            -- images_dir からの相対パス（<uuid>.<ext>）
    media_type TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    tokens INTEGER NOT NULL,       -- 推定ビジュアルトークン数
    animated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_images_msg_file ON images(channel, message_ts, slack_file_id);
CREATE INDEX IF NOT EXISTS ix_images_expires ON images(expires_at);
-- 会話履歴の質問と画像の対応（スレッド内の追加質問で画像を再送するため）
CREATE TABLE IF NOT EXISTS turn_images (
    turn_id INTEGER NOT NULL,
    image_id INTEGER NOT NULL,
    PRIMARY KEY (turn_id, image_id)
);
-- ボット宛ての投稿に添付されたファイルID（app_mention に files が無い場合の補完用）
CREATE TABLE IF NOT EXISTS message_files (
    channel TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    file_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, message_ts, file_id)
);
-- 画像付き質問の記録（利用状況の表示用。費用・トークンは usage_events に含まれる）
CREATE TABLE IF NOT EXISTS image_requests (
    usage_event_id INTEGER PRIMARY KEY,
    image_count INTEGER NOT NULL,
    est_image_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
-- スレッドと資料の対応。そのスレッドで登録・更新した資料や、質問で指定された資料を記録し、
-- 同じスレッドの質問（「この資料」「今回の資料」など）ではその資料の全文を回答の根拠に使う
CREATE TABLE IF NOT EXISTS thread_materials (
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    material_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, thread_ts, material_id)
);
CREATE INDEX IF NOT EXISTS ix_thread_materials_material ON thread_materials(material_id);
CREATE TABLE IF NOT EXISTS recent_admin_files (
    file_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


class DatabaseCorruptError(Exception):
    """DB ファイルが壊れている（PRAGMA quick_check 失敗など）。"""


class Database:
    def __init__(self, path: Path | str, busy_timeout_ms: int = 30000):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()

    # --- 接続 ---
    def connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            if str(self.path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000,
                                   isolation_level=None, check_same_thread=True)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))

    # --- トランザクション ---
    def quick_check(self) -> None:
        """起動時の整合性確認。壊れていれば DatabaseCorruptError（復旧手順は docs/OPERATIONS.md）。"""
        try:
            rows = self.connect().execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as e:
            self.close()
            raise DatabaseCorruptError(f"{self.path}: {e}") from e
        problems = [r[0] for r in rows if r[0] != "ok"]
        if problems:
            raise DatabaseCorruptError(f"{self.path}: " + "; ".join(problems[:5]))

    @contextmanager
    def tx(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """書き込みトランザクション。BEGIN IMMEDIATE で書き込みロックを先に取る。
        待機は busy_timeout に任せ、それでも取れない場合だけ短く 2 回だけ再試行する
        （busy_timeout × 再試行回数だけスレッドを塞がないように）。"""
        conn = self.connect()
        for attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e) or attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        try:
            yield conn
        except BaseException:
            # ディスク満杯などで SQLite が自動ロールバック済みの場合に元の例外を隠さない
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            try:
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        """単発の書き込み（autocommit）。"""
        return self.connect().execute(sql, params)

    # --- async ブリッジ ---
    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, *args, **kwargs)

    # --- バックアップ ---
    def backup_to(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            self.connect().backup(target)
        finally:
            target.close()
