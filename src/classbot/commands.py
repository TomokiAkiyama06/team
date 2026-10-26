"""管理コマンドの決定的な解析。

メンション本文の先頭語を固定の表で照合するだけで、AI には解釈させない。
表にない文はすべて「質問」として扱う。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

HELP = "help"
LIST = "list"
UPLOAD = "upload"
UPDATE = "update"
DELETE = "delete"
REINDEX = "reindex"
USAGE = "usage"
STATUS = "status"

_ALIASES: dict[str, str] = {
    "ヘルプ": HELP, "help": HELP, "使い方": HELP,
    "資料一覧": LIST,
    "資料登録": UPLOAD,
    "資料更新": UPDATE,
    "資料削除": DELETE,
    "再インデックス": REINDEX, "reindex": REINDEX,
    "利用状況": USAGE,
    "ステータス": STATUS, "status": STATUS,
}

# ヘルプ以外は管理者専用
ADMIN_ONLY = {LIST, UPLOAD, UPDATE, DELETE, REINDEX, USAGE, STATUS}

_MENTION_RE = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")


@dataclass(frozen=True)
class Command:
    name: str
    arg: str  # 先頭語以降の文字列（空文字あり）

    def int_arg(self) -> int | None:
        m = re.fullmatch(r"#?(\d{1,9})", self.arg.strip())
        return int(m.group(1)) if m else None


def strip_mentions(text: str) -> str:
    return _MENTION_RE.sub(" ", text or "").strip()


def parse_command(text: str) -> Command | None:
    """メンションを除いた本文がコマンドなら Command を返す。"""
    body = unicodedata.normalize("NFKC", strip_mentions(text)).strip()
    if not body:
        return None
    head, _, rest = body.partition(" ")
    name = _ALIASES.get(head.lower())
    if name is None:
        return None
    return Command(name=name, arg=rest.strip())


HELP_TEXT = """*授業AIアシスタントの使い方*
• 質問: `@{bot} 第3回の課題について教えて` のようにメンションして質問してください。回答はスレッドに返信します。
• 追加質問: ボットが回答したスレッドでは、メンションなしで続けて質問できます。
• 画像: 質問と同じ投稿に画像（PNG / JPEG / WebP / GIF、1回{images}枚まで）を添付すると、画像と授業資料の両方を参照して回答します。同じスレッドで画像について追加質問もできます。個人情報（氏名・学籍番号・顔など）が写った画像は送らないでください。
• 回答は登録された授業資料を最優先し、出典（ファイル名・ページ等）を表示します。資料にない内容は「補足（AIの一般知識）」として区別して答えます。
• `第4回の資料をまとめて` のように授業回やファイル名で資料を指定すると、その資料全体を読んで回答します。資料を登録したスレッドで質問した場合も、その資料全体を参照します。
• 課題の締切・提出方法・採点条件は、資料に書かれている内容を答えます。資料に無い日時や配点は推測しません。必ず資料や教員の案内も確認してください。
• 1人あたり1日 {daily} 回まで利用できます。

*管理者コマンド*
• `資料一覧 [ページ]` / `資料登録`（ファイルを添付） / `資料更新 <ID>`（ファイルを添付）
• `資料削除 [ID]` / `再インデックス [ID]` / `利用状況` / `ステータス`"""
