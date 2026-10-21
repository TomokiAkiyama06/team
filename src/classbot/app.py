"""ボット本体の起動と常駐タスク。

起動手順:
1. 設定検証・DB 初期化・料金表読み込み（不足があれば起動しない）
2. 前回の中断からの復旧（実行中ジョブの再登録・未切替版の掃除・残った予算予約の計上）
3. materials/ と DB の照合（起動時スキャン）→ watchdog と定期スキャン
4. Slack Socket Mode 接続、ワーカー起動、ハートビート・バックアップ
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.http_retry.builtin_async_handlers import (AsyncConnectionErrorRetryHandler,
                                                         AsyncRateLimitErrorRetryHandler)
from slack_sdk.web.async_client import AsyncWebClient

from classbot.budget import BudgetManager
from classbot.config import Settings, get_settings
from classbot.db import Database, DatabaseCorruptError, now_iso
from classbot.images import ImageStore
from classbot.indexer import Indexer
from classbot.jobs import KIND_INGEST, KIND_REINDEX_ALL, JobQueue
from classbot.llm import ClaudeClient
from classbot.logging_setup import setup_logging
from classbot.material_worker import MaterialWorker
from classbot.materials import MaterialStore
from classbot.pricing import PriceTable, PricingError
from classbot.qa_worker import QAWorker
from classbot.scanner import Scanner
from classbot.search import Searcher
from classbot.textnorm import TOKENIZER_VERSION
from classbot.slack_app import SlackHandlers, register

log = logging.getLogger(__name__)


def make_enqueue_ingest(queue: JobQueue):
    def enqueue_ingest(material_id: int) -> None:
        queue.enqueue(KIND_INGEST, {"material_id": material_id}, dedupe_key=f"ingest:{material_id}")
    return enqueue_ingest


def backup_now(settings: Settings, db: Database) -> str:
    dest = settings.backup_dir / f"classbot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    db.backup_to(dest)
    backups = sorted(settings.backup_dir.glob("classbot-*.db"))
    for old in backups[:-settings.backup_keep]:
        old.unlink(missing_ok=True)
    return str(dest)


class Bot:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = Database(settings.db_path)
        self.queue = JobQueue(self.db)
        self.store = MaterialStore(self.db, settings)
        self.indexer = Indexer(self.db, settings, self.store)
        self.prices = PriceTable.load(settings.pricing_file)
        self.llm = ClaudeClient(settings)
        # 未登録モデルなら PricingError。フォールバックが動く設定なら予約額を保守的にする
        self.budget = BudgetManager(self.db, settings, self.prices,
                                    fallback_possible=self.llm.use_fallback)
        self.searcher = Searcher(self.db, settings)
        self.stop = asyncio.Event()
        self.exit_code = 0
        self.qa_wake = asyncio.Event()
        self.mat_wake = asyncio.Event()
        self.runtime: dict = {"started_at": time.time(), "slack_connected": False,
                              "slack_connected_changed": time.time(), "wake": self._wake_all}
        self.scanner = Scanner(self.db, settings, self.store, make_enqueue_ingest(self.queue))
        self.client = AsyncWebClient(
            token=settings.slack_bot_token.get_secret_value(),
            retry_handlers=[AsyncRateLimitErrorRetryHandler(max_retry_count=3),
                            AsyncConnectionErrorRetryHandler(max_retry_count=3)])

    def _wake_all(self) -> None:
        self.qa_wake.set()
        self.mat_wake.set()

    # ------------------------------------------------------------------ 復旧
    def recover(self) -> None:
        self.db.init_schema()
        n_jobs = self.queue.requeue_running()
        n_orphans = self.indexer.cleanup_orphans()
        n_res = self.budget.recover_stale()
        log.info("recovered", extra={"requeued_jobs": n_jobs, "orphan_chunks": n_orphans,
                                     "stale_reservations": n_res})
        self.check_tokenizer_version()

    def check_tokenizer_version(self) -> bool:
        """検索トークンの規則が変わっていたら全資料の再インデックスを登録する。"""
        row = self.db.query_one("SELECT value FROM meta WHERE key='tokenizer_version'")
        if row is not None and row["value"] == TOKENIZER_VERSION:
            return False
        has_materials = self.db.query_one("SELECT 1 FROM materials LIMIT 1") is not None
        if has_materials:
            self.queue.enqueue(KIND_REINDEX_ALL, {"force": True}, dedupe_key="reindex_all")
            log.warning("tokenizer changed; full reindex scheduled",
                        extra={"from": row["value"] if row else None, "to": TOKENIZER_VERSION})
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('tokenizer_version', ?)",
                        (TOKENIZER_VERSION,))
        return has_materials

    def request_scan(self) -> None:
        if not self.stop.is_set():
            asyncio.ensure_future(self._scan_async())

    async def _scan_async(self, startup: bool = False) -> None:
        try:
            report = await self.db.run(self.scanner.scan, startup)
        except Exception:
            log.exception("scan failed")
            return
        self.runtime["last_scan_at"] = now_iso()
        self.runtime["last_scan_summary"] = json.dumps(report.summary(), ensure_ascii=False)
        if report.added or report.changed or report.removed or report.requeued:
            log.info("scan", extra=report.summary())
            self.mat_wake.set()
        if report.needs_rescan:
            asyncio.get_running_loop().call_later(self.s.file_stable_sec + 1, self.request_scan)

    # ------------------------------------------------------------------ 常駐タスク
    async def periodic_scan(self) -> None:
        while not self.stop.is_set():
            await self._sleep(self.s.scan_interval_sec)
            await self._scan_async()

    async def heartbeat(self, handler: AsyncSocketModeHandler) -> None:
        while not self.stop.is_set():
            try:
                connected = await handler.client.is_connected()
            except Exception:
                connected = False
            if connected != self.runtime["slack_connected"]:
                self.runtime["slack_connected"] = connected
                self.runtime["slack_connected_changed"] = time.time()
                log.log(logging.INFO if connected else logging.WARNING, "slack connection",
                        extra={"connected": connected})
            down = time.time() - self.runtime["slack_connected_changed"]
            if not connected and down > self.s.socket_disconnect_exit_sec:
                # SDK の自動再接続で復旧しない場合はプロセスを終了し、再起動ポリシーに任せる
                log.error("slack disconnected too long; exiting", extra={"seconds": int(down)})
                os._exit(1)
            hb = {"ts": time.time(), "pid": os.getpid(), "slack_connected": connected,
                  "slack_connected_changed": self.runtime["slack_connected_changed"]}
            tmp = self.s.heartbeat_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(hb), encoding="utf-8")
            tmp.replace(self.s.heartbeat_file)
            await self._sleep(self.s.heartbeat_interval_sec)

    async def housekeeping(self) -> None:
        while not self.stop.is_set():
            try:
                dest = await self.db.run(backup_now, self.s, self.db)
                log.info("backup created", extra={"path": dest})
                await self.db.run(self.queue.purge_old)
                await self.db.run(self.db.execute, "DELETE FROM pending_uploads WHERE expires_at < ?", (now_iso(),))
            except Exception:
                log.exception("housekeeping failed")
            await self._sleep(self.s.backup_interval_hours * 3600)

    async def image_cleanup(self) -> None:
        """保存期間を過ぎた質問画像を定期的に削除する（起動直後にも 1 回実行）。"""
        store = ImageStore(self.db, self.s)
        while not self.stop.is_set():
            try:
                n = await self.db.run(store.cleanup)
                if n:
                    log.info("expired images deleted", extra={"count": n})
            except Exception:
                log.exception("image cleanup failed")
            await self._sleep(self.s.image_cleanup_interval_sec)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    # ------------------------------------------------------------------ 起動
    def _on_task_done(self, task: asyncio.Task) -> None:
        """常駐タスクが予期せず終了したら、黙って機能停止せずプロセスを終了して再起動に任せる。"""
        if task.cancelled() or self.stop.is_set():
            return
        exc = task.exception()
        log.critical("background task stopped unexpectedly; exiting for restart",
                     extra={"task": task.get_name(), "error": repr(exc)})
        self.exit_code = 1
        self.stop.set()

    async def run(self) -> int:
        self.s.ensure_dirs()
        await self.db.run(self.recover)
        await self._scan_async(startup=True)
        loop = asyncio.get_running_loop()
        if self.s.use_watchdog:
            self.runtime["watchdog"] = self.scanner.start_watchdog(loop, self.request_scan)

        auth = await self.client.auth_test()
        self.runtime["bot_user_id"] = auth["user_id"]
        app = AsyncApp(client=self.client)
        handlers = SlackHandlers(self.s, self.db, self.queue, self.store, self.budget, self.runtime)
        register(app, handlers)
        handler = AsyncSocketModeHandler(app, self.s.slack_app_token.get_secret_value())

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)

        qa = QAWorker(self.s, self.db, self.queue, self.searcher, self.budget, self.llm, self.client)
        mat = MaterialWorker(self.s, self.db, self.queue, self.store, self.indexer, self.client)
        tasks = [asyncio.create_task(qa.run(self.qa_wake, self.stop), name=f"qa-{i}")
                 for i in range(self.s.max_concurrent_requests)]
        tasks += [asyncio.create_task(mat.run(self.mat_wake, self.stop), name="materials"),
                  asyncio.create_task(self.periodic_scan(), name="scan"),
                  asyncio.create_task(self.heartbeat(handler), name="heartbeat"),
                  asyncio.create_task(self.housekeeping(), name="housekeeping"),
                  asyncio.create_task(self.image_cleanup(), name="image-cleanup")]
        for t in tasks:
            t.add_done_callback(self._on_task_done)
        await handler.connect_async()
        log.info("classbot started", extra={"model": self.s.claude_model, "channel": self.s.slack_channel_id,
                                            "workers": self.s.max_concurrent_requests})
        await self.stop.wait()

        log.info("shutting down")
        self.scanner.stop()
        await handler.close_async()
        # 実行中のジョブは中断され、次回起動時に再実行される
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.llm.close()
        return self.exit_code


def run_bot() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    missing = settings.validate_for_bot()
    if missing:
        log.error("missing required settings", extra={"missing": missing})
        sys.exit(2)
    if not settings.slack_admin_user_ids:
        log.warning("SLACK_ADMIN_USER_IDS is empty; admin commands are disabled")
    if not check_database(settings):
        sys.exit(3)
    try:
        bot = Bot(settings)
    except PricingError as e:
        log.error("pricing configuration error", extra={"error": str(e)})
        sys.exit(2)
    sys.exit(asyncio.run(bot.run()))


def check_database(settings: Settings) -> bool:
    """起動前の DB 整合性確認。壊れていれば復旧手順を示して起動しない。"""
    if not settings.db_path.exists():
        return True
    db = Database(settings.db_path)
    try:
        db.quick_check()
        return True
    except DatabaseCorruptError as e:
        log.critical("database is corrupted; refusing to start. Restore from data/backups/ "
                     "(see docs/OPERATIONS.md: backup/restore)", extra={"error": str(e)})
        return False
    finally:
        db.close()
