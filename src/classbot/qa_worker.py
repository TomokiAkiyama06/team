"""質問ジョブの実行: 検索 → 予算予約 → Claude 呼び出し → 出典検証 → Slack 返信。"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from classbot.budget import BudgetManager, Rejection
from classbot.images import ImageError, ImageStore, StoredImage, fetch_slack_image, normalize_image
from classbot.citations import parse_answer
from classbot.config import Settings
from classbot.db import Database, now_iso
from classbot.jobs import KIND_QA, Job, JobQueue
from classbot.llm import ClaudeClient, LLMError
from classbot.safety import mask_secrets, sanitize_for_slack
from classbot.prompts import (ImageInput, build_messages_with_images, build_system_prompt,
                              estimate_input_chars)
from classbot.search import Searcher
from classbot.slack_format import md_to_mrkdwn, split_message

log = logging.getLogger(__name__)

PLACEHOLDER = "🔎 資料を検索して回答を作成しています…"
INTERNAL_ERROR = "申し訳ありません。内部エラーにより回答できませんでした。時間をおいて再度お試しください。"
STALE_MESSAGE = "処理が大幅に遅れたため、この質問への回答を中止しました。お手数ですがもう一度質問してください。"


class DeliveryError(Exception):
    """Slack への送信に失敗した（回答は qa_progress に保存済みで再送可能）。"""


class QAWorker:
    def __init__(self, settings: Settings, db: Database, queue: JobQueue, searcher: Searcher,
                 budget: BudgetManager, llm: ClaudeClient, client):
        self.s = settings
        self.db = db
        self.queue = queue
        self.searcher = searcher
        self.budget = budget
        self.llm = llm
        self.client = client
        # 同じスレッドの質問は順番に処理する（履歴の整合性のため）
        self._thread_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.retry_delay = 5.0
        self.images = ImageStore(db, settings)

    async def run(self, wake: asyncio.Event, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                job = await self.db.run(self.queue.claim, KIND_QA)
            except Exception:
                # DB ロック・ディスク障害などでワーカーが黙って停止しないよう、待って再試行する
                log.exception("qa claim failed")
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
            except DeliveryError as e:
                # 回答は qa_progress に保存済み。再試行時は API を呼ばずに再送する
                log.warning("qa delivery failed; will retry", extra={"job_id": job.id})
                await self._safe(self.db.run(self.queue.fail, job.id, f"delivery: {e}", True))
                await asyncio.sleep(self.retry_delay)
            except Exception as e:
                log.exception("qa job failed", extra={"job_id": job.id})
                await self._safe(self.db.run(self.queue.fail, job.id, f"{type(e).__name__}: {e}"))
                await self._post(job.payload, INTERNAL_ERROR)

    async def handle(self, job: Job) -> None:
        p = job.payload
        async with self._thread_locks[f"{p['channel']}:{p['thread_ts']}"]:
            await self._answer(p, job.age().total_seconds())

    async def _answer(self, p: dict, age_sec: float = 0.0) -> None:
        channel, thread_ts, user = p["channel"], p["thread_ts"], p["user"]
        # ログ等に含まれる秘密情報を伏せてから検索・API 送信・履歴保存に使う
        question, n_masked = mask_secrets(p["text"])
        key = f"{channel}:{p.get('ts') or thread_ts}"
        prog = await self.db.run(self._get_progress, key)
        if prog is not None and prog["delivered"]:
            log.info("qa already delivered; skip", extra={"job_key": key})
            return
        if prog is not None and prog["answer_text"] is not None:
            # 前回プロセスで回答（課金）済み・未送信 → API を呼ばずに送る
            await self._deliver(key, channel, prog["placeholder_ts"], thread_ts, prog["answer_text"])
            return
        msg_ts = prog["placeholder_ts"] if prog is not None else None

        if age_sec > self.s.qa_job_max_age_min * 60:
            # 再起動などで大きく遅れた質問は、古い文脈で回答せず再質問を促す
            await self._finish(key, channel, msg_ts, thread_ts, STALE_MESSAGE)
            return

        if msg_ts is None:
            try:
                placeholder = await self.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                                                 text=PLACEHOLDER)
            except Exception as e:
                raise DeliveryError(str(e)) from e
            msg_ts = placeholder["ts"]
            await self.db.run(self._set_placeholder, key, msg_ts)

        # 添付画像の取得・検証・正規化（失敗した画像は理由を回答末尾に添える）
        stored, image_notes = await self._prepare_images(p, age_sec)
        if image_notes and not stored:
            # 画像についての質問なのに 1 枚も読めなかった場合は、API を呼ばずに理由を返す（課金なし）
            await self._finish(key, channel, msg_ts, thread_ts,
                               "添付画像を読み取れなかったため回答できませんでした。\n" + "\n".join(image_notes))
            return
        if p.get("ignored_files"):
            image_notes.append("画像以外の添付ファイルは読み取っていません（資料の登録は管理者のみ可能です）。")
        if any(i.animated for i in stored):
            image_notes.append("アニメーション画像は最初のフレームだけを参照しました。")

        rows = await self.db.run(
            self.db.query,
            "SELECT id, role, text FROM conversation_turns WHERE channel=? AND thread_ts=? ORDER BY id",
            (channel, thread_ts))
        turn_images = await self.db.run(self.images.for_turns, [r["id"] for r in rows if r["role"] == "user"])
        history = []
        for r in rows:
            imgs = [None if i is None else await self._image_input(i) for i in turn_images.get(r["id"], [])]
            history.append({"role": r["role"], "text": r["text"], "images": imgs})
        prev_q = next((h["text"] for h in reversed(history) if h["role"] == "user"), None)
        result = await self.db.run(self.searcher.search, question, prev_q)
        system = build_system_prompt(self.s.system_prompt_file)
        current = [await self._image_input(i) for i in stored]
        messages, image_tokens = build_messages_with_images(
            history, question, result, max_turns=self.s.history_max_turns,
            max_chars=self.s.history_max_chars, images=current, max_images=self.s.image_max_per_request,
            today=datetime.now(ZoneInfo(self.s.timezone)).date().isoformat(), masked=n_masked > 0)
        n_images = sum(1 for m in messages if isinstance(m["content"], list)
                       for b in m["content"] if b.get("type") == "image")
        est_tokens = int(estimate_input_chars(system, messages) * self.s.token_estimate_per_char) + image_tokens

        r = await self.db.run(self.budget.reserve, user, channel, thread_ts, est_tokens, self.s.is_admin(user))
        if isinstance(r, Rejection):
            log.info("request rejected", extra={"user": user, "reason": r.reason})
            await self._finish(key, channel, msg_ts, thread_ts, r.message)
            return
        if n_images:
            await self._safe(self.db.run(self._record_image_request, r.usage_event_id, n_images, image_tokens))

        try:
            resp = await self.llm.answer(system, messages)
        except LLMError as e:
            await self.db.run(self.budget.fail, r, e.detail or e.kind, e.billed_unknown)
            log.warning("llm error", extra={"user": user, "kind": e.kind})
            await self._finish(key, channel, msg_ts, thread_ts, e.user_message)
            return
        except BaseException:
            await self.db.run(self.budget.fail, r, "unexpected error", True)
            raise

        try:
            cost = await self.db.run(self.budget.settle, r, resp.model, resp.usage, resp.request_id,
                                     resp.iterations or None)
        except Exception:
            # 計上に失敗しても回答は届ける。予約は残り、次回起動時に予約額で保守的に計上される
            log.exception("settle failed; reservation kept", extra={"usage_event_id": r.usage_event_id})
            cost = r.amount_usd
        if resp.refused:
            body, stored_text = resp.text, resp.text
        else:
            parsed = parse_answer(resp.text, result.hits)
            if parsed.invalid_ids:
                log.warning("invalid source ids removed", extra={"ids": parsed.invalid_ids})
            body, stored_text = parsed.render(), parsed.body
        if resp.notice:
            body += "\n\n" + resp.notice
        if n_masked:
            image_notes.append("質問に含まれていたパスワード・キーなどらしき文字列は伏せ字にしてから AI に送信しました。"
                               "Slack 上の元の投稿は編集・削除し、漏れた可能性がある認証情報は変更してください。")
        if image_notes:
            body += "\n\n" + "\n".join(f"※ {n}" for n in image_notes)
        log.info("answered", extra={"user": user, "model": resp.model, "cost_usd": round(cost, 5),
                                    "hits": len(result.hits), "sufficiency": result.sufficiency,
                                    "images": n_images, "image_tokens_est": image_tokens,
                                    "request_id": resp.request_id})

        # モデル出力中の <!channel> や <@U…>・偽装リンクを無効化してから mrkdwn に変換する
        text = md_to_mrkdwn(sanitize_for_slack(body))
        await self.db.run(self._save_answer, key, channel, thread_ts, user, question, stored_text, text,
                          [i.id for i in stored])
        await self._deliver(key, channel, msg_ts, thread_ts, text)
        if stored and self.s.image_retention_hours <= 0:
            # 保存期間 0: 回答を届けたらすぐ削除（スレッドの追加質問では参照できなくなる）
            await self._safe(self.db.run(self.images.delete, [i.id for i in stored]))

    # ------------------------------------------------------------------ 画像
    async def _prepare_images(self, p: dict, age_sec: float) -> tuple[list[StoredImage], list[str]]:
        """質問の添付画像を取得・検証・正規化して保存する。戻り値: (使える画像, 利用者向けの注記)。
        画像データ・ファイル名・URL はログに出さない（件数・寸法・サイズのみ）。"""
        channel, ts = p["channel"], p.get("ts") or p["thread_ts"]
        if not self.s.image_input_enabled:
            return [], []
        if p.get("await_files") and age_sec < self.s.image_event_grace_sec:
            # app_mention に files が無く、添付は別の message イベントで届く場合に備えて少し待つ
            await asyncio.sleep(self.s.image_event_grace_sec - age_sec)
        ids = list(dict.fromkeys(list(p.get("image_file_ids") or []) + await self.db.run(
            lambda: [r["file_id"] for r in self.db.query(
                "SELECT file_id FROM message_files WHERE channel=? AND message_ts=? AND user_id=? ORDER BY rowid",
                (channel, ts, p["user"]))])))
        if not ids:
            return [], []
        notes: list[str] = []
        if len(ids) > self.s.image_max_per_message:
            notes.append(f"画像は{self.s.image_max_per_message}枚まで参照します（{len(ids)}枚中）。")
            ids = ids[:self.s.image_max_per_message]
        done = {i.id: i for i in await self.db.run(self.images.for_message, channel, ts)}
        done_by_file = await self.db.run(lambda: {
            r["slack_file_id"]: r["id"] for r in self.db.query(
                "SELECT id, slack_file_id FROM images WHERE channel=? AND message_ts=?", (channel, ts))})
        stored: list[StoredImage] = []
        for n, fid in enumerate(ids, 1):
            if fid in done_by_file and done_by_file[fid] in done:
                stored.append(done[done_by_file[fid]])  # 再実行時は保存済みの画像を使う
                continue
            tmp = None
            try:
                tmp = await fetch_slack_image(self.client, fid, self.s, self.s.tmp_dir)
                img = await asyncio.to_thread(normalize_image, tmp, self.s)
                stored.append(await self.db.run(
                    lambda img=img, fid=fid: self.images.save(
                        channel=channel, thread_ts=p["thread_ts"], message_ts=ts, user_id=p["user"],
                        slack_file_id=fid, img=img)))
            except ImageError as e:
                notes.append(f"{n}枚目の画像: {e}")
            except Exception:
                log.exception("image processing failed", extra={"file_id": fid})
                notes.append(f"{n}枚目の画像を処理できませんでした。")
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
        log.info("images prepared", extra={"requested": len(ids), "ok": len(stored),
                                           "dims": [f"{i.width}x{i.height}" for i in stored],
                                           "bytes": sum(i.size_bytes for i in stored)})
        return stored, notes

    async def _image_input(self, img: StoredImage) -> ImageInput:
        data = await asyncio.to_thread(self.images.load_base64, img)
        return ImageInput(media_type=img.media_type, data_b64=data, tokens=img.tokens)

    def _record_image_request(self, usage_event_id: int, count: int, tokens: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO image_requests(usage_event_id, image_count, est_image_tokens, "
                        "created_at) VALUES (?, ?, ?, ?)", (usage_event_id, count, tokens, now_iso()))

    # ------------------------------------------------------------------ 進捗（冪等性）
    def _get_progress(self, key: str):
        return self.db.query_one("SELECT * FROM qa_progress WHERE job_key=?", (key,))

    def _set_placeholder(self, key: str, msg_ts: str) -> None:
        self.db.execute(
            "INSERT INTO qa_progress(job_key, placeholder_ts, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(job_key) DO UPDATE SET placeholder_ts=excluded.placeholder_ts, "
            "updated_at=excluded.updated_at", (key, msg_ts, now_iso()))

    def _set_answer(self, conn, key: str, text: str) -> None:
        conn.execute(
            "INSERT INTO qa_progress(job_key, answer_text, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(job_key) DO UPDATE SET answer_text=excluded.answer_text, "
            "updated_at=excluded.updated_at", (key, text, now_iso()))

    def _save_answer(self, key: str, channel: str, thread_ts: str, user: str, question: str,
                     stored: str, text: str, image_ids: list[int] | None = None) -> None:
        """会話履歴と送信予定の本文を 1 トランザクションで保存する。"""
        with self.db.tx() as conn:
            ts = now_iso()
            cur = conn.execute("INSERT INTO conversation_turns(channel, thread_ts, role, user_id, text, created_at) "
                               "VALUES (?, ?, 'user', ?, ?, ?)", (channel, thread_ts, user, question, ts))
            if image_ids:
                self.images.link_turn(conn, cur.lastrowid, image_ids)
            conn.execute("INSERT INTO conversation_turns(channel, thread_ts, role, user_id, text, created_at) "
                         "VALUES (?, ?, 'assistant', NULL, ?, ?)", (channel, thread_ts, stored, ts))
            self._set_answer(conn, key, text)

    def _save_message(self, key: str, text: str) -> None:
        with self.db.tx() as conn:
            self._set_answer(conn, key, text)

    async def _finish(self, key: str, channel: str, msg_ts: str | None, thread_ts: str, text: str) -> None:
        await self.db.run(self._save_message, key, text)
        await self._deliver(key, channel, msg_ts, thread_ts, text)

    async def _deliver(self, key: str, channel: str, msg_ts: str | None, thread_ts: str, text: str) -> None:
        try:
            await self._update(channel, msg_ts, thread_ts, text)
        except Exception as e:
            raise DeliveryError(str(e)) from e
        await self.db.run(self.db.execute,
                          "UPDATE qa_progress SET delivered=1, updated_at=? WHERE job_key=?", (now_iso(), key))

    async def _update(self, channel: str, msg_ts: str | None, thread_ts: str, text: str) -> None:
        """プレースホルダーを回答で置き換える。更新できない（削除済み等）場合は新規投稿する。"""
        parts = split_message(text)
        updated = False
        if msg_ts:
            try:
                await self.client.chat_update(channel=channel, ts=msg_ts, text=parts[0])
                updated = True
            except Exception:
                log.warning("chat_update failed; posting instead", exc_info=True)
        if not updated:
            await self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=parts[0])
        for part in parts[1:]:
            await self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=part)

    @staticmethod
    async def _safe(aw) -> None:
        try:
            await aw
        except Exception:
            log.exception("bookkeeping failed")

    async def _post(self, p: dict, text: str) -> None:
        try:
            await self.client.chat_postMessage(channel=p["channel"], thread_ts=p["thread_ts"], text=text)
        except Exception:
            log.exception("slack post failed")
