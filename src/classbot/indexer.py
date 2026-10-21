"""資料のインデックス登録・更新。

版（version）方式で原子的に置き換える:
1. 子プロセスで抽出・チャンク化し JSONL に書き出す（extract_to_jsonl）。
2. 新しい version 番号でチャンクと FTS 行をバッチ挿入する（検索対象外のまま）。
3. 1 トランザクションで active_version を切り替え、旧版を削除する。
途中で失敗・停止しても旧版は残り、検索は最後に成功した版で継続する。
未切替の版は起動時の cleanup_orphans() で削除する。
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from classbot.config import Settings
from classbot.db import Database, now_iso
from classbot.extract import extract_to_jsonl
from classbot.materials import MaterialStore, delete_chunks
from classbot.safety import (UnsupportedFileError, is_within, safe_filename, sha256_file,
                             validate_file)
from classbot.textnorm import index_tokens

log = logging.getLogger(__name__)

_BATCH = 200
_MAX_PASSES = 3


@dataclass
class IndexOutcome:
    material_id: int
    status: str          # ready | no_text | failed | skipped | missing
    skipped: bool
    message: str
    chunk_count: int = 0


class Indexer:
    def __init__(self, db: Database, settings: Settings, store: MaterialStore):
        self.db = db
        self.settings = settings
        self.store = store

    # --- 公開 API ---
    def index_material(self, material_id: int, force: bool = False) -> IndexOutcome:
        """資料を（再）インデックスする。ハッシュが変わらず登録済みなら何もしない。

        ファイルはまず tmp へスナップショットし、そのハッシュと抽出対象を一致させる
        （処理中の上書きで indexed_hash と内容がずれないように）。処理後に元ファイルが
        変わっていれば最大 _MAX_PASSES 回までやり直す（ジョブ重複排除で変更が失われない）。
        """
        outcome = IndexOutcome(material_id, "missing", True, "資料が見つかりません")
        for _ in range(_MAX_PASSES):
            outcome, digest = self._index_once(material_id, force)
            if digest is None or outcome.status == "missing":
                return outcome
            row = self.store.get(material_id)
            if row is None:
                return outcome
            try:
                live = sha256_file(self.store.file_path(row))
            except OSError:
                return outcome  # 削除された（次のスキャンで整合）
            if live == digest:
                return outcome
            log.info("file changed during indexing; re-indexing",
                     extra={"material_id": material_id})
            force = True
        return outcome

    def _index_once(self, material_id: int, force: bool) -> tuple[IndexOutcome, str | None]:
        row = self.store.get(material_id)
        if row is None:
            return IndexOutcome(material_id, "missing", True, "資料が見つかりません"), None
        path = self.store.file_path(row)
        base = (self.settings.materials_dir if row["source"] == "server"
                else self.settings.uploads_dir)
        # スキャン後にシンボリックリンクへ差し替えられた場合などに備え、処理直前にも検査する
        if not is_within(base, path):
            return self._record_failure(row, "資料フォルダ外を指すファイルは登録できません"), None
        if not path.is_file():
            return IndexOutcome(material_id, "missing", True, "ファイルが見つかりません"), None
        try:
            if path.stat().st_size > self.settings.max_file_size_bytes:
                return self._record_failure(
                    row, f"ファイルサイズが上限（{self.settings.max_file_size_mb}MB）を超えています"), None
            self.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
            snap = self.settings.tmp_dir / f"snap-{material_id}-{uuid.uuid4().hex}.{row['file_type']}"
            shutil.copyfile(path, snap)
        except OSError as e:
            return self._record_failure(row, f"ファイルを読み込めませんでした: {e}"), None
        try:
            digest = sha256_file(snap)
            if (not force and row["status"] == "ready" and row["indexed_hash"] == digest
                    and row["active_version"] is not None):
                self.db.execute("UPDATE materials SET current_hash = ?, last_error = NULL "
                                "WHERE id = ?", (digest, material_id))
                return IndexOutcome(material_id, "ready", True, "変更なし", row["chunk_count"]), digest
            return self._index_file(row, snap, digest), digest
        finally:
            snap.unlink(missing_ok=True)

    def replace_slack_file(self, material_id: int, new_tmp_path: Path,
                           display_name: str) -> IndexOutcome:
        """Slack 登録資料を新しいファイルで更新する。新ファイルを別名で保存して
        インデックスし、成功したときだけ保存名を切り替えて旧ファイルを trash へ移す。"""
        row = self.store.get(material_id)
        if row is None or row["source"] != "slack":
            new_tmp_path.unlink(missing_ok=True)
            return IndexOutcome(material_id, "failed", False, "Slack登録資料ではありません")
        new_name = f"{uuid.uuid4().hex}__{safe_filename(display_name)}"
        new_path = self.settings.uploads_dir / new_name
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        new_tmp_path.replace(new_path)
        old_path = self.store.file_path(row)
        try:
            digest = sha256_file(new_path)
            outcome = self._index_file(row, new_path, digest,
                                       swap={"stored_name": new_name,
                                             "display_name": display_name,
                                             "size_bytes": new_path.stat().st_size})
        except Exception:
            new_path.unlink(missing_ok=True)
            raise
        if outcome.status in ("ready",):
            try:
                self.store.move_to_trash(old_path)
            except OSError:
                log.exception("failed to trash old upload", extra={"material_id": material_id})
        else:
            new_path.unlink(missing_ok=True)
        return outcome

    def cleanup_orphans(self) -> int:
        """検索対象でない版のチャンク（中断した処理の残骸）を削除する。"""
        with self.db.tx() as conn:
            conn.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT c.id FROM chunks c "
                "LEFT JOIN materials m ON m.id = c.material_id "
                "WHERE m.id IS NULL OR m.active_version IS NULL OR c.version != m.active_version)")
            cur = conn.execute(
                "DELETE FROM chunks WHERE id IN (SELECT c.id FROM chunks c "
                "LEFT JOIN materials m ON m.id = c.material_id "
                "WHERE m.id IS NULL OR m.active_version IS NULL OR c.version != m.active_version)")
            n = cur.rowcount
        if n:
            log.info("removed orphan chunks", extra={"count": n})
        return n

    # --- 内部 ---
    def _index_file(self, row, path: Path, digest: str,
                    swap: dict | None = None) -> IndexOutcome:
        material_id = row["id"]
        file_type = row["file_type"]
        self.db.execute("UPDATE materials SET status = 'indexing', current_hash = ?, "
                        "updated_at = ? WHERE id = ?", (digest, now_iso(), material_id))
        try:
            validate_file(path, file_type, self.settings)
        except UnsupportedFileError as e:
            return self._record_failure(row, str(e))

        self.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.settings.tmp_dir / f"chunks-{material_id}-{uuid.uuid4().hex}.jsonl"
        try:
            result = extract_to_jsonl(path, file_type, self.settings, out_path)
            if result.status != "no_text" and (result.status == "failed" or not result.ok):
                return self._record_failure(row, result.error or "テキスト抽出に失敗しました")
            if result.status == "no_text" or result.chunk_count == 0:
                msg = result.error or "テキストを抽出できませんでした"
                return self._record_failure(row, msg, status="no_text")
            version = self._new_version()
            title = swap["display_name"] if swap else row["display_name"]
            count = self._insert_chunks(material_id, version, title,
                                        result.chunks_path or out_path)
            if count == 0:
                self._drop_version(material_id, version)
                return self._record_failure(row, "テキストを抽出できませんでした",
                                            status="no_text")
            if not self._activate(material_id, version, digest, count, result.page_count, swap):
                if self.store.get(material_id) is None:
                    return IndexOutcome(material_id, "missing", True, "処理中に資料が削除されました")
                return IndexOutcome(material_id, "ready", True, "より新しい版が既に登録されています",
                                    self.store.get(material_id)["chunk_count"])
            log.info("material indexed", extra={"material_id": material_id, "chunks": count})
            return IndexOutcome(material_id, "ready", False, "登録しました", count)
        except Exception as e:  # 1 資料の失敗で全体を止めない
            log.exception("indexing failed", extra={"material_id": material_id})
            return self._record_failure(row, f"インデックス作成に失敗しました: {type(e).__name__}")
        finally:
            out_path.unlink(missing_ok=True)

    def _new_version(self) -> int:
        return self.db.execute("INSERT INTO version_seq DEFAULT VALUES").lastrowid

    def _insert_chunks(self, material_id: int, version: int, title: str,
                       jsonl_path: Path) -> int:
        """JSONL を逐次読み込み、バッチ単位でコミットする（メモリ使用量を一定に保つ）。"""
        count = 0
        batch: list[dict] = []

        def flush() -> None:
            with self.db.tx() as conn:
                for c in batch:
                    cur = conn.execute(
                        "INSERT INTO chunks (material_id, version, seq, page, location, "
                        "heading, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (material_id, version, c.get("seq", 0), c.get("page"),
                         c.get("location") or "", c.get("heading"), c["text"]))
                    heading = c.get("heading") or ""
                    conn.execute(
                        "INSERT INTO chunks_fts (rowid, title_tokens, body_tokens) "
                        "VALUES (?, ?, ?)",
                        (cur.lastrowid, index_tokens(f"{title} {heading}"),
                         index_tokens(c["text"])))
            batch.clear()

        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                c = json.loads(line)
                if not (c.get("text") or "").strip():
                    continue
                batch.append(c)
                count += 1
                if len(batch) >= _BATCH:
                    flush()
        if batch:
            flush()
        return count

    def _drop_version(self, material_id: int, version: int) -> None:
        with self.db.tx() as conn:
            delete_chunks(conn, material_id, version=version)

    def _activate(self, material_id: int, version: int, digest: str, count: int,
                  page_count: int | None, swap: dict | None) -> bool:
        """新しい版を検索対象に切り替える。並行処理（CLI --inline 等）で既により新しい版が
        有効になっていれば自分の版を捨てる。処理中の他の版のチャンクは消さない
        （未切替の版は cleanup_orphans が掃除する）。"""
        now = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute("SELECT active_version FROM materials WHERE id = ?",
                               (material_id,)).fetchone()
            if cur is None:
                # 処理中に削除された
                delete_chunks(conn, material_id)
                return False
            prev = cur["active_version"]
            if prev is not None and prev > version:
                delete_chunks(conn, material_id, version=version)
                return False
            conn.execute(
                "UPDATE materials SET active_version = ?, indexed_hash = ?, current_hash = ?, "
                "status = 'ready', last_error = NULL, chunk_count = ?, page_count = ?, "
                "indexed_at = ?, updated_at = ? WHERE id = ?",
                (version, digest, digest, count, page_count, now, now, material_id))
            if swap:
                conn.execute(
                    "UPDATE materials SET stored_name = ?, display_name = ?, size_bytes = ? "
                    "WHERE id = ?",
                    (swap["stored_name"], swap["display_name"], swap["size_bytes"], material_id))
            if prev is not None:
                delete_chunks(conn, material_id, version=prev)
        return True

    def _record_failure(self, row, message: str, status: str = "failed") -> IndexOutcome:
        """失敗を記録する。以前の正常版があれば status='ready' のまま旧版で検索を継続する。"""
        material_id = row["id"]
        fresh = self.store.get(material_id)
        has_active = fresh is not None and fresh["active_version"] is not None
        new_status = "ready" if has_active else status
        self.db.execute("UPDATE materials SET status = ?, last_error = ?, updated_at = ? "
                        "WHERE id = ?", (new_status, message, now_iso(), material_id))
        log.warning("indexing did not complete",
                    extra={"material_id": material_id, "status": status, "error": message})
        return IndexOutcome(material_id, status, False,
                            message + ("（以前の版で検索を継続します）" if has_active else ""),
                            fresh["chunk_count"] if has_active else 0)
