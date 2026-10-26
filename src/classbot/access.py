"""ボットを使える人の制限（SLACK_RESTRICT_USERS=true のとき）。

使えるのは、管理者（SLACK_ADMIN_USER_IDS）・SLACK_ALLOWED_USER_IDS・管理者が Slack の
「利用者追加」で登録した人（allowed_users テーブル）。チャンネルを公開にしても、
登録していない人の質問では Claude API を呼ばない（費用がかからない）。
"""

from __future__ import annotations

import re
import sqlite3

from classbot.config import Settings
from classbot.db import Database, now_iso

NOT_ALLOWED_MSG = ("このボットは登録された利用者だけが使えます。"
                   "利用したい場合は授業担当者（管理者）に登録を依頼してください。")

# Slack のユーザーID（U… / W…）。メンションは <@U123> / <@U123|name> の形で届く
_MENTION_ID_RE = re.compile(r"<@([UW][A-Z0-9]{2,})(?:\|[^>]*)?>")
_BARE_ID_RE = re.compile(r"(?<![A-Za-z0-9])([UW][A-Z0-9]{6,})(?![A-Za-z0-9])")


def user_ids_in(text: str, exclude: str = "") -> list[str]:
    """メッセージ中のメンション（<@U…>）と、直接書かれたユーザーID（U0123ABCD）を順に返す。"""
    ids = _MENTION_ID_RE.findall(text or "") + _BARE_ID_RE.findall(_MENTION_ID_RE.sub(" ", text or ""))
    return [i for i in dict.fromkeys(ids) if i != exclude]


class AccessList:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.s = settings

    def is_allowed(self, user_id: str | None) -> bool:
        if not user_id:
            return False
        if not self.s.slack_restrict_users or self.s.is_admin(user_id) \
                or user_id in self.s.slack_allowed_user_ids:
            return True
        return self.db.query_one("SELECT 1 FROM allowed_users WHERE user_id=?", (user_id,)) is not None

    def add(self, user_ids: list[str], added_by: str) -> list[str]:
        """登録して、新しく登録したIDを返す（登録済みは除く）。"""
        added = []
        with self.db.tx() as conn:
            for uid in user_ids:
                cur = conn.execute("INSERT OR IGNORE INTO allowed_users(user_id, added_by, created_at) "
                                   "VALUES (?, ?, ?)", (uid, added_by, now_iso()))
                if cur.rowcount == 1:
                    added.append(uid)
        return added

    def remove(self, user_ids: list[str]) -> list[str]:
        """登録を解除して、解除したIDを返す（.env の SLACK_ALLOWED_USER_IDS は Slack からは外せない）。"""
        removed = []
        with self.db.tx() as conn:
            for uid in user_ids:
                if conn.execute("DELETE FROM allowed_users WHERE user_id=?", (uid,)).rowcount:
                    removed.append(uid)
        return removed

    def registered(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT user_id, added_by, created_at FROM allowed_users ORDER BY created_at, user_id")
