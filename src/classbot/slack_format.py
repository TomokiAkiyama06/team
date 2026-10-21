"""Slack 向けの整形（Markdown → mrkdwn 変換と Block Kit 生成）。"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from classbot.safety import sanitize_for_slack

SLACK_TEXT_LIMIT = 3900  # section ブロックの上限 3000 を考慮しつつ本文は text で送る

_STATUS_LABEL = {
    "pending": "⏳ 待機中",
    "indexing": "🔄 処理中",
    "ready": "✅ 検索可能",
    "failed": "❌ 失敗",
    "no_text": "⚠️ テキストなし",
}
_SOURCE_LABEL = {"server": "サーバー", "slack": "Slack"}


def md_to_mrkdwn(text: str) -> str:
    """Claude が出力しがちな Markdown を Slack mrkdwn に寄せる（コードブロックは保持）。"""
    parts = re.split(r"(```.*?```)", text, flags=re.S)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(part)
            continue
        p = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", part, flags=re.M)
        p = re.sub(r"\*\*(.+?)\*\*", r"*\1*", p)
        p = re.sub(r"__(.+?)__", r"*\1*", p)
        p = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", p)
        p = re.sub(r"^(\s*)[-*]\s+", r"\1• ", p, flags=re.M)
        out.append(p)
    return "".join(out)


def split_message(text: str, limit: int = SLACK_TEXT_LIMIT) -> list[str]:
    """長文を段落境界で分割する。"""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if len(buf) + len(para) + 1 > limit:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def human_size(n: int | None) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}B"


def fmt_time(iso: str | None, tz: str) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def material_line(row: sqlite3.Row, tz: str) -> str:
    status = _STATUS_LABEL.get(row["status"], row["status"])
    if row["status"] == "ready" and row["last_error"]:
        status += "（最新版の更新に失敗・旧版で検索中）"
    return (f"*#{row['id']}* {sanitize_for_slack(row['display_name'])}  [{_SOURCE_LABEL.get(row['source'], row['source'])}]\n"
            f"    {status} ｜ {human_size(row['size_bytes'])} ｜ 登録 {fmt_time(row['created_at'], tz)}"
            f" ｜ 更新 {fmt_time(row['indexed_at'] or row['updated_at'], tz)}")


def pager_buttons(action_id: str, page: int, total_pages: int) -> dict | None:
    elems = []
    if page > 1:
        elems.append({"type": "button", "text": {"type": "plain_text", "text": "◀ 前へ"},
                      "action_id": f"{action_id}_prev", "value": str(page - 1)})
    if page < total_pages:
        elems.append({"type": "button", "text": {"type": "plain_text", "text": "次へ ▶"},
                      "action_id": f"{action_id}_next", "value": str(page + 1)})
    return {"type": "actions", "elements": elems} if elems else None


def list_blocks(rows: list[sqlite3.Row], total: int, page: int, page_size: int, tz: str) -> list[dict]:
    total_pages = max(1, -(-total // page_size))
    head = f"*登録資料一覧*（{total}件・{page}/{total_pages}ページ）"
    body = "\n".join(material_line(r, tz) for r in rows) or "登録されている資料はありません。"
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    for part in split_message(body, 2900):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": part}})
    pager = pager_buttons("list_page", page, total_pages)
    if pager:
        blocks.append(pager)
    return blocks


def delete_blocks(rows: list[sqlite3.Row], total: int, page: int, page_size: int, tz: str) -> list[dict]:
    """削除対象の選択 UI。各ボタンは Slack 標準の確認ダイアログを伴う。値は内部IDのみ。"""
    total_pages = max(1, -(-total // page_size))
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn",
                           "text": f"*削除する資料を選んでください*（{page}/{total_pages}ページ）"}}]
    for r in rows:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": material_line(r, tz)[:2900]},
            "accessory": delete_button(r),
        })
    if not rows:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "登録されている資料はありません。"}})
    pager = pager_buttons("delete_page", page, total_pages)
    if pager:
        blocks.append(pager)
    return blocks


def delete_button(row: sqlite3.Row) -> dict:
    where = "サーバー上のファイルはゴミ箱フォルダ（data/trash）へ移動されます。" \
        if row["source"] == "server" else "保存済みファイルはゴミ箱フォルダ（data/trash）へ移動されます。"
    return {
        "type": "button", "style": "danger",
        "text": {"type": "plain_text", "text": "削除"},
        "action_id": "delete_material", "value": str(row["id"]),
        "confirm": {
            "title": {"type": "plain_text", "text": "資料の削除"},
            "text": {"type": "mrkdwn",
                     "text": f"#{row['id']} *{sanitize_for_slack(row['display_name'][:200])}* を削除しますか？\n{where}"},
            "confirm": {"type": "plain_text", "text": "削除する"},
            "deny": {"type": "plain_text", "text": "キャンセル"},
            "style": "danger",
        },
    }
