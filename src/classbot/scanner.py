"""materials/ フォルダと DB の照合（追加・更新・削除の検知）。

起動時・定期実行・watchdog 通知（デバウンス）のいずれからも scan() を呼ぶ。
watchdog は取りこぼしがあり得るため、定期スキャンを正とし watchdog は早期検知に使う。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from classbot.config import Settings
from classbot.db import Database, now_iso
from classbot.materials import MaterialStore
from classbot.safety import detect_file_type, is_within, sha256_file

log = logging.getLogger(__name__)

_TEMP_PREFIXES = ("~$", ".~", "~")
_TEMP_SUFFIXES = (".part", ".crdownload", ".tmp", ".swp", ".swx", ".download", "~")


@dataclass
class ScanReport:
    added: list[int] = field(default_factory=list)
    changed: list[int] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    requeued: list[int] = field(default_factory=list)
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)
    needs_rescan: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"added": len(self.added), "changed": len(self.changed),
                "removed": len(self.removed), "requeued": len(self.requeued),
                "unchanged": self.unchanged, "skipped": len(self.skipped),
                "needs_rescan": self.needs_rescan, "errors": len(self.errors)}


def is_ignored_name(name: str) -> bool:
    """隠しファイル・一時ファイル（Office のロックファイル、ダウンロード途中など）。"""
    if name.startswith(".") or name.startswith(_TEMP_PREFIXES):
        return True
    return name.lower().endswith(_TEMP_SUFFIXES)


class Scanner:
    def __init__(self, db: Database, settings: Settings, store: MaterialStore,
                 enqueue_ingest: Callable[[int], None]):
        self.db = db
        self.settings = settings
        self.store = store
        self.enqueue_ingest = enqueue_ingest
        self._lock = threading.Lock()
        self._observer = None
        self.last_scan_at: str | None = None

    # --- スキャン ---
    def _walk(self, report: ScanReport) -> dict[str, tuple[Path, os.stat_result, str]]:
        base = self.settings.materials_dir
        found: dict[str, tuple[Path, os.stat_result, str]] = {}
        if not base.is_dir():
            return found
        base_resolved = base.resolve()
        # DATA_DIR を materials/ 配下に置く誤設定でも、Slack 登録資料・ゴミ箱・tmp を
        # サーバー配置資料として二重登録しない
        data_resolved = self.settings.data_dir.resolve()
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if not is_ignored_name(d)
                           and (Path(dirpath) / d).resolve() != data_resolved]
            for name in filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(base).as_posix()
                if is_ignored_name(name):
                    continue
                file_type = detect_file_type(name)
                if file_type is None:
                    report.skipped.append(rel)
                    continue
                if not is_within(base_resolved, path):
                    log.warning("symlink outside materials skipped", extra={"path": rel})
                    report.skipped.append(rel)
                    continue
                try:
                    st = path.stat()
                except OSError as e:
                    report.errors.append(f"{rel}: {e}")
                    continue
                if not path.is_file():
                    continue
                found[rel] = (path, st, file_type)
        return found

    def scan(self, startup: bool = False) -> ScanReport:
        """フォルダと DB を照合し、必要な資料のインデックス処理をキューに入れる。"""
        with self._lock:
            report = ScanReport()
            try:
                found = self._walk(report)
            except OSError as e:
                log.exception("materials scan failed")
                report.errors.append(str(e))
                return report
            now = time.time()
            known = {r["rel_path"]: r for r in self.store.all_server()}

            for rel, (path, st, file_type) in found.items():
                try:
                    self._reconcile_file(rel, path, st, file_type, known.get(rel), now,
                                         startup, report)
                except Exception as e:  # 1 ファイルの失敗で全体を止めない
                    log.exception("scan of file failed", extra={"path": rel})
                    report.errors.append(f"{rel}: {type(e).__name__}")

            for rel, row in known.items():
                if rel not in found:
                    self.store.remove_rows(row["id"])
                    report.removed.append(rel)
                    log.info("material removed from folder",
                             extra={"material_id": row["id"], "path": rel})

            self.last_scan_at = now_iso()
            if any(report.summary()[k] for k in ("added", "changed", "removed", "requeued")):
                log.info("materials scan", extra=report.summary())
            return report

    def _reconcile_file(self, rel: str, path: Path, st: os.stat_result, file_type: str,
                        row, now: float, startup: bool, report: ScanReport) -> None:
        # コピー途中のファイルは安定するまで待つ
        if now - st.st_mtime < self.settings.file_stable_sec:
            report.needs_rescan = True
            return
        if row is None:
            if not path.is_file():
                return  # 走査後に削除された（Slack からの削除と並行した場合など）
            mid = self.store.upsert_server(rel, st.st_size, st.st_mtime, file_type)
            self.enqueue_ingest(mid)
            report.added.append(mid)
            return

        mid = row["id"]
        if startup and row["status"] in ("pending", "indexing"):
            self.store.upsert_server(rel, st.st_size, st.st_mtime, file_type)
            self.enqueue_ingest(mid)
            report.requeued.append(mid)
            return
        if row["size_bytes"] == st.st_size and row["mtime"] == st.st_mtime:
            report.unchanged += 1
            return
        # サイズか mtime が変わった → 内容ハッシュで本当に変わったか判定
        digest = sha256_file(path)
        self.db.execute("UPDATE materials SET size_bytes = ?, mtime = ?, current_hash = ?, "
                        "updated_at = ? WHERE id = ?",
                        (st.st_size, st.st_mtime, digest, now_iso(), mid))
        if digest == row["indexed_hash"]:
            report.unchanged += 1
            return
        if row["status"] in ("failed", "no_text") and digest == row["current_hash"]:
            # 内容が同じ失敗ファイルは自動では再処理しない（再インデックスで強制可能）
            report.unchanged += 1
            return
        self.enqueue_ingest(mid)
        report.changed.append(mid)

    # --- watchdog ---
    def start_watchdog(self, loop: asyncio.AbstractEventLoop,
                       on_change: Callable[[], None], debounce_sec: float = 3.0) -> bool:
        """ファイル変更を検知したら、デバウンス後にイベントループ上で on_change を呼ぶ。"""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog not available; periodic scan only")
            return False

        state = {"handle": None}

        def fire() -> None:
            state["handle"] = None
            on_change()

        def schedule() -> None:
            if state["handle"] is not None:
                state["handle"].cancel()
            state["handle"] = loop.call_later(debounce_sec, fire)

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:  # noqa: ANN001
                if event.event_type in ("opened", "closed_no_write"):
                    return
                loop.call_soon_threadsafe(schedule)

        self.settings.materials_dir.mkdir(parents=True, exist_ok=True)
        try:
            observer = Observer()
            observer.schedule(_Handler(), str(self.settings.materials_dir), recursive=True)
            observer.daemon = True
            observer.start()
        except Exception:
            log.exception("failed to start watchdog; periodic scan only")
            return False
        self._observer = observer
        return True

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
