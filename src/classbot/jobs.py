"""SQLite 上の永続ジョブキュー。

プロセスが再起動しても未処理ジョブは失われず、実行中だったジョブは
起動時に pending へ戻して再実行する（資料解析の途中停止にも対応）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from classbot.db import Database, now_iso, utcnow

KIND_QA = "qa"
KIND_INGEST = "ingest"
KIND_DELETE = "delete"
KIND_SLACK_UPLOAD = "slack_upload"
KIND_REINDEX_ALL = "reindex_all"

MATERIAL_KINDS = (KIND_INGEST, KIND_DELETE, KIND_SLACK_UPLOAD, KIND_REINDEX_ALL)
MAX_ATTEMPTS = 3


@dataclass
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(row["id"], row["kind"], json.loads(row["payload"]), row["attempts"],
                   row["created_at"])

    def age(self) -> timedelta:
        return utcnow() - datetime.fromisoformat(self.created_at)


class JobQueue:
    def __init__(self, db: Database):
        self.db = db

    def enqueue(self, kind: str, payload: dict[str, Any], dedupe_key: str | None = None) -> int | None:
        """ジョブを登録する。同じ dedupe_key の未完了ジョブがあれば登録せず None を返す。"""
        ts = now_iso()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO jobs(kind, payload, dedupe_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False), dedupe_key, ts, ts))
        return cur.lastrowid if cur.rowcount else None

    def claim(self, kinds: tuple[str, ...] | str) -> Job | None:
        """最も古い pending ジョブを running にして返す。"""
        if isinstance(kinds, str):
            kinds = (kinds,)
        marks = ",".join("?" * len(kinds))
        with self.db.tx() as conn:
            row = conn.execute(
                f"SELECT * FROM jobs WHERE status='pending' AND kind IN ({marks}) ORDER BY id LIMIT 1",
                kinds).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (now_iso(), row["id"]))
        job = Job.from_row(row)
        job.attempts += 1
        return job

    def complete(self, job_id: int) -> None:
        self.db.execute("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (now_iso(), job_id))

    def fail(self, job_id: int, error: str, retry: bool = False) -> None:
        """失敗を記録する。retry=True かつ試行回数が上限未満なら pending に戻す。"""
        row = self.db.query_one("SELECT attempts FROM jobs WHERE id=?", (job_id,))
        status = "pending" if retry and row and row["attempts"] < MAX_ATTEMPTS else "failed"
        self.db.execute("UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE id=?",
                        (status, error[:2000], now_iso(), job_id))

    def requeue_running(self) -> int:
        """起動時: 前回プロセスで running のまま残ったジョブを pending に戻す。
        何度も処理中にプロセスが落ちたジョブ（クラッシュの原因の可能性が高い）は failed にして
        再起動ループを防ぐ。"""
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', last_error='interrupted too many times', updated_at=? "
                "WHERE status='running' AND attempts >= ?", (now_iso(), MAX_ATTEMPTS))
            cur = conn.execute("UPDATE jobs SET status='pending', updated_at=? WHERE status='running'",
                               (now_iso(),))
        return cur.rowcount

    def counts(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT kind || ':' || status AS k, COUNT(*) AS n FROM jobs "
            "WHERE status IN ('pending', 'running') GROUP BY kind, status")
        return {r["k"]: r["n"] for r in rows}

    def pending_qa_for_user(self, user_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind=? AND status IN ('pending','running') "
            "AND json_extract(payload, '$.user') = ?", (KIND_QA, user_id))
        return row["n"] if row else 0

    def purge_old(self, days: int = 30) -> None:
        cutoff = (utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM jobs WHERE status IN ('done','failed') AND updated_at < ?",
                        (cutoff,))
        # Slack の再送は数分以内なので 30 日保持で十分（プロセス再起動をまたいでも重複排除できる）
        self.db.execute("DELETE FROM processed_events WHERE created_at < ?", (cutoff,))
        self.db.execute("DELETE FROM qa_progress WHERE updated_at < ?", (cutoff,))
