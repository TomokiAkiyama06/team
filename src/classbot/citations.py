"""回答中の出典IDの検証と出典表示の生成。

モデルが出力した出典ID（本文中の [S1] と最終行の [[SOURCES: ...]]）を、実際の検索結果と照合する。
存在しないIDは本文から取り除き、出典表示は DB 由来のメタデータ（ファイル名・位置）だけから作る。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from classbot.search import SearchHit

log = logging.getLogger(__name__)

# [[SOURCES: S1, S3]]（行中にあっても可）
_SOURCES_TAG_RE = re.compile(r"[\[［]{2}\s*SOURCES\s*[:：]\s*(.*?)\s*[\]］]{2}", re.IGNORECASE)
# 括弧を省いた・片側だけの行（"SOURCES: S1" など）も出典行として扱う
_SOURCES_LINE_RE = re.compile(
    r"^[ \t]*[\[［]{0,2}[ \t]*SOURCES[ \t]*[:：][ \t]*([^\n]*?)[ \t]*[\]］]{0,2}[ \t]*$",
    re.IGNORECASE | re.MULTILINE)
# [S1] / [S1, S3] / [S1][S2] / 【S1】 / ［S1］（全角）
_INLINE_RE = re.compile(r"[\[【［]\s*([SＳ][0-9０-９]+(?:\s*[,、，]\s*[SＳ][0-9０-９]+)*)\s*[\]】］]")
_ID_RE = re.compile(r"[SＳ][0-9０-９]+")


def _ids(text: str) -> list[str]:
    """出典IDを抽出し、全角を半角に揃える（Ｓ１ → S1）。"""
    return [unicodedata.normalize("NFKC", m) for m in _ID_RE.findall(text)]


@dataclass
class ParsedAnswer:
    body: str                                   # 出典行を除いた本文（不正IDは除去済み）
    used_hits: list[SearchHit] = field(default_factory=list)
    invalid_ids: list[str] = field(default_factory=list)
    footer: str = ""                            # 「出典：…」行（無ければ空）

    def render(self) -> str:
        return f"{self.body}\n\n{self.footer}" if self.footer else self.body


def _format_footer(hits: list[SearchHit]) -> str:
    """1資料につき1行で表示する。同じ位置の出典はまとめ、位置は「、」で並べる
    （例: 出典：第4回.pdf p.1 [S1]、p.3 [S3][S4]）。"""
    groups: dict[int, dict[str, list[SearchHit]]] = {}
    for h in hits:
        groups.setdefault(h.material_id, {}).setdefault(h.location, []).append(h)
    lines = []
    for by_loc in groups.values():
        name = next(iter(by_loc.values()))[0].display_name
        parts = []
        for loc, group in by_loc.items():
            ids = "".join(f"[{g.source_id}]" for g in group)
            parts.append(f"{loc} {ids}" if loc else ids)
        lines.append(f"出典：{name} {'、'.join(parts)}")
    return "\n".join(lines)


def parse_answer(text: str, hits: list[SearchHit]) -> ParsedAnswer:
    valid = {h.source_id: h for h in hits}
    order: list[str] = []
    invalid: list[str] = []

    def note(sid: str) -> None:
        if sid in valid:
            if sid not in order:
                order.append(sid)
        elif sid not in invalid:
            invalid.append(sid)

    declared: list[str] = []
    body = text
    for pattern in (_SOURCES_TAG_RE, _SOURCES_LINE_RE):
        for m in pattern.finditer(body):
            declared.extend(_ids(m.group(1)))
        body = pattern.sub("", body)

    def replace_inline(m: re.Match) -> str:
        ids = _ids(m.group(1))
        for sid in ids:
            note(sid)
        ok = [sid for sid in ids if sid in valid]
        return "".join(f"[{sid}]" for sid in ok)

    body = _INLINE_RE.sub(replace_inline, body)
    for sid in declared:
        note(sid)

    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    used = [valid[sid] for sid in order]
    if invalid:
        log.warning("invalid_source_ids", extra={"invalid": invalid, "valid": list(valid)})
    return ParsedAnswer(body=body, used_hits=used, invalid_ids=invalid,
                        footer=_format_footer(used))
