"""資料ジョブ（インデックス・削除・Slack からの登録/更新・全体再インデックス）の実行。

抽出は indexer が子プロセスで行うが、DB 書き込みを含むためここではスレッド上で
1 件ずつ直列に処理する（同じ資料への同時更新を避ける）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from classbot.config import Settings
from classbot.db import Database, now_iso
from classbot.indexer import Indexer, IndexOutcome
from classbot.jobs import (KIND_DELETE, KIND_INGEST, KIND_REINDEX_ALL, KIND_SLACK_UPLOAD,
                           MATERIAL_KINDS, Job, JobQueue)
from classbot.materials import MaterialStore
from classbot.slack_files import DownloadError, download_slack_file
from classbot.safety import (SUPPORTED_TYPES, UnsupportedFileError, detect_file_type,
                             safe_filename, sanitize_for_slack, validate_file)

log = logging.getLogger(__name__)

_STATUS_MSG = {"ready": "✅ 登録しました", "no_text": "⚠️ テキストを抽出できませんでした",
               "failed": "❌ 登録できませんでした", "missing": "❌ ファイルが見つかりません"}


class MaterialWorker:
    def __init__(self, settings: Settings, db: Database, queue: JobQueue, store: MaterialStore,
                 indexer: Indexer, client):
        self.s = settings
        self.db = db
        self.queue = queue
        self.store = store
        self.indexer = indexer
        self.client = client  # AsyncWebClient
        self.retry_delay = 5.0

    # ------------------------------------------------------------------ ループ
    async def run(self, wake: asyncio.Event, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                job = await self.db.run(self.queue.claim, MATERIAL_KINDS)
            except Exception:
                # DB ロックなど一時的な障害でワーカーが停止しないよう、待って再試行する
                log.exception("material claim failed")
                await asyncio.sleep(self.retry_delay)
                continue
            if job is None:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            try:
                await self.handle(job)
                await self.db.run(self.queue.complete, job.id)
            except Exception as e:  # 1件の失敗で他の資料処理を止めない
                log.exception("material job failed", extra={"job_id": job.id, "kind": job.kind})
                try:
                    await self.db.run(self.queue.fail, job.id, f"{type(e).__name__}: {e}", True)
                except Exception:
                    # 記録できなくても running のまま残り、次回起動時に再実行される
                    log.exception("could not record material job failure", extra={"job_id": job.id})
                    await asyncio.sleep(self.retry_delay)

    async def handle(self, job: Job) -> None:
        p = job.payload
        if job.kind in (KIND_INGEST, KIND_DELETE, KIND_REINDEX_ALL) and "user" in p \
                and not self.s.is_admin(p["user"]):
            # Slack 由来のジョブは実行時にも管理者を再検証する（CLI 由来は user を持たない）
            log.warning("non-admin material job rejected", extra={"kind": job.kind, "user": p.get("user")})
            await self._notify(p.get("notify"), "この操作は管理者のみ実行できます。")
            return
        if job.kind == KIND_DELETE and "user" not in p:
            log.warning("delete job without user rejected", extra={"job_id": job.id})
            return
        if job.kind == KIND_INGEST:
            outcome = await self.db.run(self.indexer.index_material, p["material_id"], bool(p.get("force")))
            log.info("indexed", extra={"material_id": p["material_id"], "status": outcome.status,
                                       "skipped": outcome.skipped, "chunks": outcome.chunk_count})
            if p.get("notify"):
                await self._notify(p["notify"], self._outcome_text(p["material_id"], outcome))
            elif outcome.status in ("failed", "no_text") and not outcome.skipped:
                await self._notify_admins(self._outcome_text(p["material_id"], outcome))
        elif job.kind == KIND_DELETE:
            row = await self.db.run(self.store.delete, p["material_id"])
            text = (f"🗑 #{p['material_id']} {row['display_name']} を削除しました（ファイルは data/trash に退避）。"
                    if row else f"ID {p['material_id']} の資料は既に削除されています。")
            log.info("material deleted", extra={"material_id": p["material_id"], "user": p.get("user")})
            await self._notify(p["notify"], text)
        elif job.kind == KIND_REINDEX_ALL:
            rows = await self.db.run(lambda: self.db.query("SELECT id FROM materials ORDER BY id"))
            results = {"ready": 0, "no_text": 0, "failed": 0, "missing": 0}
            for r in rows:
                outcome = await self.db.run(self.indexer.index_material, r["id"], bool(p.get("force", True)))
                key = "skipped" if outcome.skipped else outcome.status
                results[key] = results.get(key, 0) + 1
            await self._notify(p.get("notify"), "🔄 再インデックス完了: " + " / ".join(
                f"{k} {v}件" for k, v in results.items() if v) if rows else "🔄 登録資料はありません。")
        elif job.kind == KIND_SLACK_UPLOAD:
            await self._handle_upload(p)

    # ------------------------------------------------------------------ Slack 登録
    async def _handle_upload(self, p: dict) -> None:
        notify = {"channel": p["channel"], "thread_ts": p["thread_ts"]}
        if not self.s.is_admin(p["user"]):  # 受付時に検証済みだが実行時にも再検証する
            await self._notify(notify, "この操作は管理者のみ実行できます。")
            return
        lines = []
        for fid in p["file_ids"]:
            # 同じ Slack ファイルの二重登録を防ぐ（イベントの二重配送・再送）。資料ワーカーは
            # 1 本なので「確認→処理→記録」で足りる。処理途中で停止した場合は記録されず再実行される
            key = f"slackfile:{fid}:{p.get('target_material_id') or 'new'}"
            if self.db.query_one("SELECT 1 FROM processed_events WHERE event_key=?", (key,)):
                log.info("slack file already processed", extra={"file_id": fid})
                continue
            try:
                lines.append(await self._register_one(fid, p["user"], p.get("target_material_id"), notify))
            except (UnsupportedFileError, DownloadError) as e:
                lines.append(f"❌ {e}")
            except Exception:
                # 一時的な障害の可能性があるため処理済みにしない（同じファイルで再試行可能）
                log.exception("slack upload failed", extra={"file_id": fid})
                lines.append("❌ ファイルの処理中に予期しないエラーが発生しました。")
                continue
            self.db.execute("INSERT OR IGNORE INTO processed_events(event_key, created_at) VALUES (?, ?)",
                            (key, now_iso()))
        if lines:
            await self._notify(notify, "\n".join(lines))

    async def _register_one(self, file_id: str, user: str, target_id: int | None,
                            thread: dict | None = None) -> str:
        """thread（channel / thread_ts）を渡すと、登録・更新した資料をそのスレッドに対応付ける。
        以後そのスレッドの質問では、この資料の全文を回答の根拠に使う。"""
        info = (await self.client.files_info(file=file_id))["file"]
        name = safe_filename(info.get("name") or info.get("title") or "file")
        file_type = detect_file_type(name)
        if file_type not in SUPPORTED_TYPES:
            raise UnsupportedFileError(f"{name}: 未対応の形式です（PDF / PPTX / DOCX / TXT / Markdown のみ）。")
        if info.get("mode") in ("external", "snippet", "post") or info.get("is_external"):
            raise UnsupportedFileError(f"{name}: 外部ファイルやスニペットは登録できません。ファイルを直接添付してください。")
        if int(info.get("size") or 0) > self.s.max_file_size_bytes:
            raise UnsupportedFileError(f"{name}: ファイルサイズが上限（{self.s.max_file_size_mb}MB）を超えています。")

        tmp = self.s.tmp_dir / f"upload-{uuid.uuid4().hex}"
        try:
            await self._download(info.get("url_private_download") or info.get("url_private") or "", tmp)
            await self.db.run(validate_file, tmp, file_type, self.s)
            if target_id is not None:
                row = await self.db.run(self.store.get, target_id)
                if row is None or row["source"] != "slack":
                    raise UnsupportedFileError(f"ID {target_id} は Slack 登録資料ではないため更新できません。")
                if row["file_type"] != file_type:
                    raise UnsupportedFileError(f"{name}: 元の資料と形式が異なります（{row['file_type']} → {file_type}）。")
                outcome = await self.db.run(self.indexer.replace_slack_file, target_id, tmp, name)
                await self._link_thread(thread, target_id)
                prefix = "更新" if outcome.status == "ready" else "更新失敗（旧版で検索を継続）"
                return f"{_STATUS_MSG.get(outcome.status, outcome.status)} #{target_id} {name}: {prefix}" + \
                    (f" — {outcome.message}" if outcome.status != "ready" else f"（{outcome.chunk_count}チャンク）")
            stored = f"{uuid.uuid4().hex}__{name}"
            dest = self.s.uploads_dir / stored
            tmp.replace(dest)
            mid = await self.db.run(self.store.create_slack, name, stored, file_type,
                                    dest.stat().st_size, user)
            outcome = await self.db.run(self.indexer.index_material, mid, True)
            if outcome.status != "ready":
                # 検索できない資料は登録しない（一覧が失敗データで埋まらないように）
                await self.db.run(self.store.remove_rows, mid)
                dest.unlink(missing_ok=True)
                return f"{_STATUS_MSG.get(outcome.status, outcome.status)} {name}: {outcome.message}"
            await self._link_thread(thread, mid)
            return f"✅ #{mid} {name} を登録しました（{outcome.chunk_count}チャンク）。"
        finally:
            tmp.unlink(missing_ok=True)

    async def _link_thread(self, thread: dict | None, material_id: int) -> None:
        if not thread or not thread.get("thread_ts"):
            return
        try:
            await self.db.run(self.store.link_thread, thread["channel"], thread["thread_ts"], material_id)
        except Exception:
            # 対応付けに失敗しても登録自体は有効（キーワード検索では見つかる）
            log.exception("thread link failed", extra={"material_id": material_id})

    async def _download(self, url: str, dest: Path) -> None:
        """Slack のファイルを上限付きでストリーム保存する（slack_files に委譲）。"""
        await download_slack_file(url, dest, self.s.slack_bot_token.get_secret_value(),
                                  self.s.max_file_size_bytes, timeout_sec=300)

    # ------------------------------------------------------------------ 通知
    def _outcome_text(self, material_id: int, o: IndexOutcome) -> str:
        row = self.store.get(material_id)
        name = row["display_name"] if row else f"ID {material_id}"
        if o.skipped and o.status == "ready":
            return f"#{material_id} {name}: 変更がないため再処理しませんでした。"
        if o.status == "ready":
            return f"✅ #{material_id} {name}: インデックスを更新しました（{o.chunk_count}チャンク）。"
        if row and row["status"] == "ready":
            return f"⚠️ #{material_id} {name}: 更新に失敗しました（旧版で検索を継続）— {o.message}"
        return f"{_STATUS_MSG.get(o.status, o.status)} #{material_id} {name} — {o.message}"

    async def _notify(self, notify: dict | None, text: str) -> None:
        if not notify:
            return
        try:
            # ファイル名・エラー文は外部由来のため、<!channel> 等の埋め込みを無効化して投稿する
            await self.client.chat_postMessage(channel=notify["channel"], thread_ts=notify.get("thread_ts"),
                                               text=sanitize_for_slack(text))
        except Exception:
            log.exception("slack notify failed")

    async def _notify_admins(self, text: str) -> None:
        """サーバー配置資料の失敗は、学生に見えないよう管理者へのエフェメラルで通知する。"""
        for admin in self.s.slack_admin_user_ids:
            try:
                await self.client.chat_postEphemeral(channel=self.s.slack_channel_id, user=admin,
                                                     text="[資料処理] " + sanitize_for_slack(text))
            except Exception:
                log.warning("ephemeral notify failed", extra={"user": admin})
