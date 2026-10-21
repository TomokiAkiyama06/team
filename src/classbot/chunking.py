"""抽出テキストのチャンク分割。

セグメント（ページ・スライド・見出し節）をまたがないことで、
各チャンクの出典位置（ページ番号など）を正確に保つ。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass
class Segment:
    text: str
    location: str
    page: int | None = None
    heading: str | None = None


@dataclass
class Chunk:
    seq: int
    text: str
    location: str
    page: int | None = None
    heading: str | None = None


# 文末（。！？!? の後、空白が続く英文のピリオドの後）・改行で区切る。
# 区切り文字と直後の改行・空白は前の文に残す（"3.14" のような小数点では区切らない）
_SENT_RE = re.compile(r".*?(?:[。．！？!?]+\n*|\.(?=\s)\s*|\n+|$)", re.S)
_WS_RE = re.compile(r"[ \t　]+")


def _clean(text: str) -> str:
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    return "\n".join(out).strip()


def _sentences(text: str) -> list[str]:
    return [s for s in _SENT_RE.findall(text) if s.strip()]


def _split_long(sentence: str, size: int) -> list[str]:
    return [sentence[i:i + size] for i in range(0, len(sentence), size)]


def _split_text(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    for s in _sentences(text):
        pieces.extend(_split_long(s, size) if len(s) > size else [s])
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) > size:
            chunks.append(cur.strip())
            # 直前チャンクの末尾を重複させて文脈の切れ目を緩和する
            tail = cur[-overlap:] if overlap > 0 else ""
            cur = tail + p if len(tail) + len(p) <= size else p
        else:
            cur += p
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _fingerprint(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()


def chunk_segments(segments: Iterable[Segment], size: int, overlap: int) -> Iterator[Chunk]:
    """セグメントを size 文字程度のチャンクに分割する。同一資料内の完全重複
    （各スライド共通のフッター等）は 2 回目以降を除去する。"""
    overlap = max(0, min(overlap, size // 2))
    seen: set[str] = set()
    seq = 0
    for seg in segments:
        text = _clean(seg.text)
        if not text:
            continue
        for piece in _split_text(text, size, overlap):
            if len(piece.strip()) < 2:
                continue
            fp = _fingerprint(piece)
            if fp in seen:
                continue
            seen.add(fp)
            yield Chunk(seq=seq, text=piece, location=seg.location, page=seg.page,
                        heading=seg.heading)
            seq += 1
