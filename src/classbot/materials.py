"""資料レコードの管理と保存領域の扱い。

- source='server': materials/ 配下に管理者が直接配置したファイル（rel_path で一意）。
- source='slack' : Slack から登録したファイル。data/uploads/ 配下に一意な保存名で置き、
  materials/ とは保存領域・管理情報を分離して競合を防ぐ。
- 削除時はファイルを data/trash/ へ移動してから DB の行・チャンク・FTS を消す（復元可能）。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from classbot.config import Settings
from classbot.db import Database, now_iso
from classbot.safety import strip_invisible

log = logging.getLogger(__name__)


def delete_chunks(conn: sqlite3.Connection, material_id: int, version: int | None = None,
                  exclude_version: int | None = None) -> int:
    """チャンクと対応する FTS 行を削除する（トランザクション内で呼ぶ）。"""
    where = "material_id = ?"
    params: list = [material_id]
    if version is not None:
        where += " AND version = ?"
        params.append(version)
    if exclude_version is not None:
        where += " AND version != ?"
        params.append(exclude_version)
    conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE {where})",
                 params)
    cur = conn.execute(f"DELETE FROM chunks WHERE {where}", params)
    return cur.rowcount


def link_thread(conn: sqlite3.Connection, channel: str, thread_ts: str, material_id: int) -> None:
    """資料をスレッドに対応付ける（既にあれば何もしない）。"""
    conn.execute("INSERT OR IGNORE INTO thread_materials(channel, thread_ts, material_id, created_at) "
                 "VALUES (?, ?, ?, ?)", (channel, thread_ts, material_id, now_iso()))


class MaterialStore:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    # --- 参照 ---
    def get(self, material_id: int) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM materials WHERE id = ?", (material_id,))

    def get_server(self, rel_path: str) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM materials WHERE source = 'server' AND rel_path = ?", (rel_path,))

    def all_server(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM materials WHERE source = 'server'")

    def list(self, offset: int = 0, limit: int = 15) -> tuple[list[sqlite3.Row], int]:
        """表示用の一覧（表示名→ID 順）と総件数。"""
        total = self.db.query_one("SELECT COUNT(*) AS n FROM materials")["n"]
        rows = self.db.query(
            "SELECT * FROM materials ORDER BY display_name COLLATE NOCASE, id LIMIT ? OFFSET ?",
            (limit, offset))
        return rows, total

    def status_counts(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM materials GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def file_path(self, row: sqlite3.Row) -> Path:
        if row["source"] == "server":
            return self.settings.materials_dir / row["rel_path"]
        return self.settings.uploads_dir / row["stored_name"]

    # --- 作成・更新 ---
    def upsert_server(self, rel_path: str, size: int, mtime: float, file_type: str) -> int:
        """サーバー配置資料の行を作成（既存なら size/mtime を更新）して ID を返す。"""
        now = now_iso()
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT id FROM materials WHERE source = 'server' AND rel_path = ?",
                (rel_path,)).fetchone()
            if row:
                conn.execute("UPDATE materials SET size_bytes = ?, mtime = ?, updated_at = ? "
                             "WHERE id = ?", (size, mtime, now, row["id"]))
                return row["id"]
            cur = conn.execute(
                "INSERT INTO materials (source, rel_path, display_name, file_type, size_bytes, "
                "mtime, status, created_at, updated_at) "
                "VALUES ('server', ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (rel_path, strip_invisible(Path(rel_path).name) or Path(rel_path).name, file_type,
                 size, mtime, now, now))
            return cur.lastrowid

    def create_slack(self, display_name: str, stored_name: str, file_type: str, size: int,
                     uploaded_by: str) -> int:
        now = now_iso()
        cur = self.db.execute(
            "INSERT INTO materials (source, stored_name, display_name, file_type, size_bytes, "
            "status, uploaded_by, created_at, updated_at) "
            "VALUES ('slack', ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (stored_name, display_name, file_type, size, uploaded_by, now, now))
        return cur.lastrowid

    def set_status(self, material_id: int, status: str, error: str | None = None) -> None:
        self.db.execute("UPDATE materials SET status = ?, last_error = ?, updated_at = ? "
                        "WHERE id = ?", (status, error, now_iso(), material_id))

    def link_thread(self, channel: str, thread_ts: str, material_id: int) -> None:
        """資料をスレッドに対応付ける（そのスレッドの質問で全文を参照する）。"""
        link_thread(self.db.connect(), channel, thread_ts, material_id)

    # --- 削除 ---
    def move_to_trash(self, path: Path) -> Path | None:
        """ファイルを trash へ移動する（存在しなければ None）。同名衝突は時刻で回避。"""
        if not path.exists():
            return None
        self.settings.trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest = self.settings.trash_dir / f"{stamp}__{path.name}"
        n = 1
        while dest.exists() or dest.is_symlink():  # 上書きで既存のゴミ箱ファイルを失わない
            dest = self.settings.trash_dir / f"{stamp}-{n}__{path.name}"
            n += 1
        shutil.move(str(path), dest)
        return dest

    def remove_rows(self, material_id: int) -> None:
        """行・チャンク・FTS を削除する（ファイルには触れない）。"""
        with self.db.tx() as conn:
            delete_chunks(conn, material_id)
            conn.execute("DELETE FROM thread_materials WHERE material_id = ?", (material_id,))
            conn.execute("DELETE FROM materials WHERE id = ?", (material_id,))

    def delete(self, material_id: int) -> sqlite3.Row | None:
        """資料を削除する。ファイルは trash へ移動（サーバー配置資料も再スキャンで
        復活しないよう materials/ から退避する）。削除した行を返す。"""
        row = self.get(material_id)
        if row is None:
            return None
        path = self.file_path(row)
        base = (self.settings.materials_dir if row["source"] == "server"
                else self.settings.uploads_dir)
        from classbot.safety import is_within  # 遅延 import（抽出系の依存を避ける）

        if path.exists() or path.is_symlink():
            if is_within(base, path) or path.is_symlink():
                try:
                    trashed = self.move_to_trash(path)
                    log.info("material file moved to trash",
                             extra={"material_id": material_id, "trash": str(trashed)})
                except OSError:
                    log.exception("failed to move material file to trash",
                                  extra={"material_id": material_id})
                    raise
            else:
                log.warning("material path outside base; file left untouched",
                            extra={"material_id": material_id})
        self.remove_rows(material_id)
        return row
