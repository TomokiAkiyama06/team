"""Slack イベントの受付。

ここでは重い処理をしない。チャンネル・権限・重複を検査し、ジョブを DB に
登録してすぐ戻る（Bolt がイベントを ACK する）。実処理は workers.py が行う。
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from slack_bolt.async_app import AsyncApp

from classbot import commands
from classbot.budget import BudgetManager
from classbot.config import Settings
from classbot.db import Database, now_iso, utcnow
from classbot.jobs import (KIND_DELETE, KIND_QA, KIND_REINDEX_ALL, KIND_SLACK_UPLOAD,
                           KIND_INGEST, JobQueue)
from classbot.images import ImageStore, classify_slack_file
from classbot.materials import MaterialStore
from classbot.slack_format import delete_blocks, delete_button, list_blocks, material_line
from classbot.status import format_status, format_usage

log = logging.getLogger(__name__)

ADMIN_ONLY_MSG = "この操作は管理者のみ実行できます。"
DEFAULT_IMAGE_QUESTION = "添付した画像について説明してください。"
IMAGE_FORMATS = "PNG / JPEG / WebP / GIF（GIF アニメーションは最初のフレームのみ）"


def split_files(files: list[dict] | None) -> tuple[list[dict], list[dict], list[dict]]:
    """添付を (対応画像, 未対応の画像, 画像以外) に分ける。"""
    images, unsupported, other = [], [], []
    for f in files or []:
        if not f.get("id"):
            continue
        kind = classify_slack_file(f)
        (images if kind == "image" else unsupported if kind == "unsupported_image" else other).append(f)
    return images, unsupported, other


def is_material_file(f: dict) -> bool:
    """資料登録の対象になり得る添付か。画像は資料として扱わない（形式の詳細検証は登録処理で行う）。"""
    return bool(f.get("id")) and classify_slack_file(f) == "other"


class SlackHandlers:
    """イベントごとの判定ロジック。Slack クライアントは引数で受け取りテスト可能にする。"""

    def __init__(self, settings: Settings, db: Database, queue: JobQueue, store: MaterialStore,
                 budget: BudgetManager, runtime: dict[str, Any]):
        self.s = settings
        self.db = db
        self.queue = queue
        self.store = store
        self.budget = budget
        self.runtime = runtime  # bot_user_id, started_at, slack_connected など
        self.bot_user_id: str = runtime.get("bot_user_id", "")

    # ------------------------------------------------------------------ 共通
    # DB アクセスはすべて `await self.db.run(...)` でワーカースレッド上で行い、
    # ロック待ち（busy_timeout）でイベントループを止めない。
    def _seen(self, channel: str, ts: str) -> bool:
        return self.db.query_one("SELECT 1 FROM processed_events WHERE event_key=?",
                                 (f"{channel}:{ts}",)) is not None

    def _claim(self, channel: str, ts: str) -> bool:
        """同じメッセージ（channel:ts）を一度だけ処理する。再送・二重配送対策。"""
        cur = self.db.execute("INSERT OR IGNORE INTO processed_events(event_key, created_at) VALUES (?, ?)",
                              (f"{channel}:{ts}", now_iso()))
        return cur.rowcount == 1

    def _accept_tx(self, channel: str, ts: str, thread_ts: str, payload: dict) -> bool:
        """重複排除の記録・スレッド登録・ジョブ登録を 1 トランザクションで行う
        （途中でプロセスが落ちても「処理済みなのに質問が失われる」状態を作らない）。"""
        with self.db.tx() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO processed_events(event_key, created_at) VALUES (?, ?)",
                               (f"{channel}:{ts}", now_iso()))
            if cur.rowcount != 1:
                return False
            conn.execute("INSERT OR IGNORE INTO threads(channel, thread_ts, created_at) VALUES (?, ?, ?)",
                         (channel, thread_ts, now_iso()))
            # 同じスレッドの接続を使うため、このトランザクション内で登録される
            self.queue.enqueue(KIND_QA, payload, dedupe_key=f"qa:{channel}:{ts}")
        return True

    def _is_bot_thread(self, channel: str, thread_ts: str) -> bool:
        return self.db.query_one("SELECT 1 FROM threads WHERE channel=? AND thread_ts=?",
                                 (channel, thread_ts)) is not None

    def _mark_thread(self, channel: str, thread_ts: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO threads(channel, thread_ts, created_at) VALUES (?, ?, ?)",
                        (channel, thread_ts, now_iso()))

    def _team_ok(self, team: str | None) -> bool:
        """SLACK_TEAM_ID 設定時は、他ワークスペース（Slack Connect 等）のユーザーを無視する。"""
        return not self.s.slack_team_id or team == self.s.slack_team_id

    def _mentions_bot(self, text: str) -> bool:
        return bool(self.bot_user_id) and f"<@{self.bot_user_id}" in (text or "")

    async def _reply(self, client, channel: str, thread_ts: str, text: str, **kw) -> None:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, **kw)

    # ------------------------------------------------------------------ イベント
    async def on_app_mention(self, event: dict, client) -> None:
        channel, user, ts = event.get("channel"), event.get("user"), event.get("ts")
        if channel != self.s.slack_channel_id or not user or event.get("bot_id"):
            return  # 指定チャンネル以外には応答しない
        if not self._team_ok(event.get("user_team") or event.get("team")):
            return
        if await self.db.run(self._seen, channel, ts):
            return
        thread_ts = event.get("thread_ts") or ts
        text = event.get("text", "")
        cmd = commands.parse_command(text)
        if cmd is not None:
            if not await self.db.run(self._claim, channel, ts):
                return
            await self.handle_command(cmd, event, client, thread_ts)
            return
        await self._accept_question(client, channel, user, ts, thread_ts, text, event.get("files"),
                                    via_mention=True)

    async def on_message(self, event: dict, client) -> None:
        subtype = event.get("subtype")
        if subtype not in (None, "file_share", "thread_broadcast") or event.get("bot_id"):
            return
        channel, user, ts = event.get("channel"), event.get("user"), event.get("ts")
        if channel != self.s.slack_channel_id or not user:
            return
        if not self._team_ok(event.get("user_team") or event.get("team")):
            return
        text = event.get("text", "")
        files = event.get("files") or []
        if subtype == "file_share" and self._mentions_bot(text):
            # 質問画像の ID を記録する（app_mention に files が含まれない場合の補完。中身は保存しない）
            await self.db.run(self._record_message_files, channel, ts, user, files)
        if subtype == "file_share" and self.s.is_admin(user) and any(is_material_file(f) for f in files):
            # メンション付きでも資料ファイルは記録する（app_mention に files が含まれない場合や、
            # message イベントが app_mention より先に届いた場合にも登録できるように）。
            # 画像は資料登録の対象にしない（学生・管理者の画像付き質問と混同しない）
            if await self._on_admin_files(client, event):
                return  # 登録待ちの資料として使われた
            # 登録待ちでなければ、ボットのスレッド内では追加質問として扱う
        if self._mentions_bot(text):
            return  # app_mention 側で処理する

        thread_ts = event.get("thread_ts")
        if not thread_ts or thread_ts == ts or not await self.db.run(self._is_bot_thread, channel, thread_ts):
            return  # 無関係な通常会話には割り込まない（添付ファイルも取得・記録しない）
        if not text.strip() and not files:
            return
        if await self.db.run(self._seen, channel, ts):
            return
        await self._accept_question(client, channel, user, ts, thread_ts, text, files)

    def _record_message_files(self, channel: str, ts: str, user: str, files: list[dict]) -> None:
        images, _, _ = split_files(files)
        for f in images:
            self.db.execute("INSERT OR IGNORE INTO message_files(channel, message_ts, file_id, user_id, created_at) "
                            "VALUES (?, ?, ?, ?, ?)", (channel, ts, f["id"], user, now_iso()))

    async def _accept_question(self, client, channel: str, user: str, ts: str, thread_ts: str,
                               text: str, files: list[dict] | None = None, *,
                               via_mention: bool = False) -> None:
        body = commands.strip_mentions(text)
        images, unsupported, other = split_files(files)

        async def reject(msg: str) -> None:
            if await self.db.run(self._claim, channel, ts):  # 再送で同じ案内を繰り返さない
                await self._reply(client, channel, thread_ts, msg)

        if images or unsupported:
            if not self.s.image_input_enabled:
                await reject("現在、画像付きの質問は受け付けていません。文章で質問してください。")
                return
            if unsupported:
                await reject(f"対応していない画像形式が含まれています。対応形式: {IMAGE_FORMATS}")
                return
            if len(images) > self.s.image_max_per_message:
                await reject(f"画像は1回の質問につき{self.s.image_max_per_message}枚までです。")
                return
            if any(int(f.get("size") or 0) > self.s.image_max_file_mb * 1024 * 1024 for f in images):
                await reject(f"画像のファイルサイズが上限（{self.s.image_max_file_mb}MB）を超えています。")
                return
            if not body:
                body = DEFAULT_IMAGE_QUESTION
        if not body:
            await reject("質問内容を書いてメンションしてください。`ヘルプ` で使い方を表示します。")
            return
        if len(body) > self.s.question_max_chars:
            await reject(f"質問が長すぎます（{self.s.question_max_chars}文字以内にしてください）。")
            return
        allowed, used, limit = await self.db.run(self.budget.check_daily, user, self.s.is_admin(user))
        if not allowed:
            await reject(f"本日の利用上限（{limit}回）に達しました。明日またご利用ください。")
            return
        if await self.db.run(self.queue.pending_qa_for_user, user) >= self.s.max_pending_per_user:
            await reject("前の質問を処理中です。回答が届いてから次の質問をしてください。")
            return
        payload = {"channel": channel, "user": user, "ts": ts, "thread_ts": thread_ts, "text": body,
                   "image_file_ids": [f["id"] for f in images], "ignored_files": len(other),
                   # app_mention に files が無い場合、添付は別の message イベントで届くことがある
                   "await_files": via_mention and files is None}
        if await self.db.run(self._accept_tx, channel, ts, thread_ts, payload):
            self.runtime["wake"]()

    # ------------------------------------------------------------------ ファイル登録
    async def _on_admin_files(self, client, event: dict) -> bool:
        """管理者の添付を記録し、登録待ちがあれば登録ジョブを作る（作ったら True）。"""
        channel, user, ts = event["channel"], event["user"], event["ts"]
        file_ids = [f["id"] for f in event.get("files", []) if f.get("id") and is_material_file(f)]
        if not file_ids:
            return False
        result = await self.db.run(self._admin_files_tx, event, file_ids)
        if result == "dup":
            return True
        if result is None:
            return False
        await self._enqueue_upload(client, channel, user, result["reply_thread_ts"], file_ids,
                                   result["target_material_id"])
        return True

    def _admin_files_tx(self, event: dict, file_ids: list[str]):
        """添付の記録と登録待ちの消費。戻り値: "dup"（再送）/ None（登録待ちなし）/ 消費した登録待ち行。"""
        channel, user, ts = event["channel"], event["user"], event["ts"]
        with self.db.tx() as conn:
            # 同じメッセージの app_mention とは別キーで重複排除する
            if conn.execute("INSERT OR IGNORE INTO processed_events(event_key, created_at) VALUES (?, ?)",
                            (f"{channel}:files:{ts}", now_iso())).rowcount != 1:
                return "dup"
            for fid in file_ids:
                conn.execute("INSERT OR IGNORE INTO recent_admin_files(file_id, user_id, channel, message_ts, "
                             "created_at) VALUES (?, ?, ?, ?, ?)", (fid, user, channel, ts, now_iso()))
            pending = conn.execute(
                "SELECT * FROM pending_uploads WHERE user_id=? AND channel=? AND expires_at>? "
                "ORDER BY id DESC LIMIT 1", (user, channel, now_iso())).fetchone()
            if pending is None:
                return None
            if self._mentions_bot(event.get("text", "")) and \
                    pending["reply_thread_ts"] != (event.get("thread_ts") or ts):
                # メンション付きの添付は app_mention 側で登録する。ここで満たしてよいのは、
                # 同じメッセージの app_mention が files なしで先に作った登録待ちだけ
                return None
            conn.execute("DELETE FROM pending_uploads WHERE id=?", (pending["id"],))
            marks = ",".join("?" * len(file_ids))
            conn.execute(f"UPDATE recent_admin_files SET consumed=1 WHERE file_id IN ({marks})", file_ids)
            return pending

    async def _enqueue_upload(self, client, channel: str, user: str, thread_ts: str,
                              file_ids: list[str], target_id: int | None) -> None:
        if target_id is not None and len(file_ids) != 1:
            await self._reply(client, channel, thread_ts, "資料更新では添付ファイルを1つだけ指定してください。")
            return
        await self.db.run(self.queue.enqueue, KIND_SLACK_UPLOAD,
                          {"channel": channel, "user": user, "thread_ts": thread_ts,
                           "file_ids": file_ids, "target_material_id": target_id})
        self.runtime["wake"]()
        await self._reply(client, channel, thread_ts,
                          f"📥 {len(file_ids)}件のファイルを受け付けました。登録処理が終わったらこのスレッドでお知らせします。")

    async def _start_upload(self, cmd: commands.Command, event: dict, client, thread_ts: str,
                            target_id: int | None) -> None:
        channel, user = event["channel"], event["user"]
        images, unsupported, other = split_files(event.get("files"))
        if (images or unsupported) and not other:
            # 資料登録コマンドに画像だけが添付された場合（画像は資料として登録しない）
            await self._reply(client, channel, thread_ts,
                              "画像は授業資料として登録できません。対応形式: PDF / PPTX / DOCX / TXT / Markdown。"
                              "画像について質問する場合は、コマンドではなく質問文と一緒に添付してください。")
            return
        file_ids = [f["id"] for f in other]
        file_ids = await self.db.run(self._collect_upload_files, channel, user, event.get("ts", ""),
                                     thread_ts, target_id, file_ids)
        if not file_ids:
            await self._reply(client, channel, thread_ts,
                              f"{self.s.upload_pending_minutes}分以内にこのチャンネルへファイルを添付してください。"
                              "対応形式: PDF / PPTX / DOCX / TXT / Markdown（最大"
                              f"{self.s.max_file_size_mb}MB）")
            return
        await self._enqueue_upload(client, channel, user, thread_ts, file_ids, target_id)

    def _collect_upload_files(self, channel: str, user: str, ts: str, thread_ts: str,
                              target_id: int | None, file_ids: list[str]) -> list[str]:
        """登録対象のファイル ID を決める。無ければ登録待ちを作って空リストを返す。"""
        with self.db.tx() as conn:
            if file_ids:
                conn.execute("DELETE FROM pending_uploads WHERE user_id=? AND channel=?", (user, channel))
                # メンションと同じメッセージの添付。後から届く file_share や次回の「資料登録」で
                # 二重登録しないよう消費済みとして記録する
                for fid in file_ids:
                    conn.execute(
                        "INSERT INTO recent_admin_files(file_id, user_id, channel, message_ts, consumed, created_at) "
                        "VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT(file_id) DO UPDATE SET consumed=1",
                        (fid, user, channel, ts, now_iso()))
                return file_ids
            # 直前（待機時間内）に同じ管理者が添付したファイルを探す
            cutoff = (utcnow() - timedelta(minutes=self.s.upload_pending_minutes)).isoformat(timespec="seconds")
            rows = conn.execute(
                "SELECT file_id FROM recent_admin_files WHERE user_id=? AND channel=? AND consumed=0 "
                "AND created_at>=? ORDER BY created_at", (user, channel, cutoff)).fetchall()
            found = [r["file_id"] for r in rows]
            if found:
                marks = ",".join("?" * len(found))
                conn.execute(f"UPDATE recent_admin_files SET consumed=1 WHERE file_id IN ({marks})", found)
                return found
            expires = (utcnow() + timedelta(minutes=self.s.upload_pending_minutes)).isoformat(timespec="seconds")
            conn.execute("DELETE FROM pending_uploads WHERE user_id=? AND channel=?", (user, channel))
            conn.execute("INSERT INTO pending_uploads(user_id, channel, reply_thread_ts, target_material_id, "
                         "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (user, channel, thread_ts, target_id, expires, now_iso()))
            return []

    # ------------------------------------------------------------------ コマンド
    async def handle_command(self, cmd: commands.Command, event: dict, client, thread_ts: str) -> None:
        channel, user = event["channel"], event["user"]
        if cmd.name in commands.ADMIN_ONLY and not self.s.is_admin(user):
            await self._reply(client, channel, thread_ts, ADMIN_ONLY_MSG)
            return
        log.info("command", extra={"command": cmd.name, "user": user})

        if cmd.name == commands.HELP:
            await self._reply(client, channel, thread_ts, commands.HELP_TEXT.format(
                bot=f"<@{self.bot_user_id}>" if self.bot_user_id else "ボット",
                daily=self.s.daily_request_limit_per_user, images=self.s.image_max_per_message))

        elif cmd.name == commands.LIST:
            page = cmd.int_arg() or 1
            await self._reply(client, channel, thread_ts, "登録資料一覧", blocks=await self.db.run(self.list_page, page))

        elif cmd.name == commands.UPLOAD:
            await self._start_upload(cmd, event, client, thread_ts, None)

        elif cmd.name == commands.UPDATE:
            mid = cmd.int_arg()
            row = await self.db.run(self.store.get, mid) if mid else None
            if row is None:
                await self._reply(client, channel, thread_ts, "更新する資料のIDを指定してください（例: `資料更新 12`）。IDは `資料一覧` で確認できます。")
            elif row["source"] == "server":
                await self._reply(client, channel, thread_ts,
                                  "この資料はサーバーの materials/ フォルダに配置された資料です。"
                                  "サーバー上のファイルを置き換えると自動で更新されます。")
            else:
                await self._start_upload(cmd, event, client, thread_ts, mid)

        elif cmd.name == commands.DELETE:
            mid = cmd.int_arg()
            if mid:
                row = await self.db.run(self.store.get, mid)
                if row is None:
                    await self._reply(client, channel, thread_ts, f"ID {mid} の資料は見つかりません。")
                    return
                await self._reply(client, channel, thread_ts, "資料の削除", blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": material_line(row, self.s.timezone)},
                     "accessory": delete_button(row)}])
            else:
                await self._reply(client, channel, thread_ts, "資料の削除", blocks=await self.db.run(self.delete_page, 1))

        elif cmd.name == commands.REINDEX:
            mid = cmd.int_arg()
            if mid:
                if await self.db.run(self.store.get, mid) is None:
                    await self._reply(client, channel, thread_ts, f"ID {mid} の資料は見つかりません。")
                    return
                await self.db.run(self.queue.enqueue, KIND_INGEST,
                                  {"material_id": mid, "force": True, "user": user,
                                   "notify": {"channel": channel, "thread_ts": thread_ts}}, f"ingest:{mid}")
            else:
                await self.db.run(self.queue.enqueue, KIND_REINDEX_ALL,
                                  {"user": user, "notify": {"channel": channel, "thread_ts": thread_ts}},
                                  "reindex_all")
            self.runtime["wake"]()
            await self._reply(client, channel, thread_ts, "🔄 再インデックスを開始しました。完了したらお知らせします。")

        elif cmd.name == commands.USAGE:
            summary = await self.db.run(self.budget.summary)
            images = await self.db.run(ImageStore(self.db, self.s).stats, self.budget.month_start_iso())
            await self._reply(client, channel, thread_ts, format_usage(summary, self.s, images=images))

        elif cmd.name == commands.STATUS:
            text = await self.db.run(format_status, self.s, self.db, self.store, self.queue, self.runtime)
            await self._reply(client, channel, thread_ts, text)

    def list_page(self, page: int) -> list[dict]:
        size = self.s.list_page_size
        rows, total = self.store.list((max(page, 1) - 1) * size, size)
        return list_blocks(rows, total, max(page, 1), size, self.s.timezone)

    def delete_page(self, page: int) -> list[dict]:
        size = min(self.s.list_page_size, 20)  # 1メッセージ 50 ブロック制限に収める
        rows, total = self.store.list((max(page, 1) - 1) * size, size)
        return delete_blocks(rows, total, max(page, 1), size, self.s.timezone)

    # ------------------------------------------------------------------ ボタン操作
    async def on_action(self, body: dict, client) -> None:
        user = body.get("user", {}).get("id")
        channel = body.get("channel", {}).get("id")
        container = body.get("container", {})
        msg_ts = container.get("message_ts")
        thread_ts = body.get("message", {}).get("thread_ts") or msg_ts
        if channel != self.s.slack_channel_id:
            return
        if not self._team_ok(body.get("user", {}).get("team_id") or body.get("team", {}).get("id")):
            return
        if not self.s.is_admin(user):
            await client.chat_postEphemeral(channel=channel, user=user, text=ADMIN_ONLY_MSG)
            return
        action = (body.get("actions") or [{}])[0]
        action_id, value = action.get("action_id", ""), action.get("value", "")
        if action_id.startswith("list_page_"):
            await client.chat_update(channel=channel, ts=msg_ts, text="登録資料一覧",
                                     blocks=await self.db.run(self.list_page, int(value)))
        elif action_id.startswith("delete_page_"):
            await client.chat_update(channel=channel, ts=msg_ts, text="資料の削除",
                                     blocks=await self.db.run(self.delete_page, int(value)))
        elif action_id == "delete_material" and re.fullmatch(r"\d{1,9}", value):
            mid = int(value)
            row = await self.db.run(self.store.get, mid)
            if row is None:
                await self._reply(client, channel, thread_ts, f"ID {mid} の資料は既に削除されています。")
                return
            await self.db.run(self.queue.enqueue, KIND_DELETE,
                              {"material_id": mid, "user": user,
                               "notify": {"channel": channel, "thread_ts": thread_ts}}, f"delete:{mid}")
            self.runtime["wake"]()
            log.info("delete requested", extra={"material_id": mid, "user": user})


def register(app: AsyncApp, h: SlackHandlers) -> None:
    @app.event("app_mention")
    async def _mention(event, client):
        await h.on_app_mention(event, client)

    @app.event("message")
    async def _message(event, client):
        await h.on_message(event, client)

    @app.action(re.compile(r"^(delete_material|delete_page_(prev|next)|list_page_(prev|next))$"))
    async def _action(ack, body, client):
        await ack()
        await h.on_action(body, client)
