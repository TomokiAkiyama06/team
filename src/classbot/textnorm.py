"""日本語向けの正規化とトークン化（文字 bi-gram + 英数字単語）。

SQLite FTS5 の標準トークナイザ（unicode61）は日本語を分かち書きできず、
trigram トークナイザは「課題」「締切」のような 2 文字語を検索できない。
そこでアプリ側で正規化した文字 bi-gram を空白区切りで格納し、
unicode61 には「空白区切りの単語列」として渡す。理由の詳細は docs/search.md。
"""

from __future__ import annotations

# 正規化・トークン化の規則を変えたら上げる。起動時に DB の値と異なれば全資料を再インデックスする。
TOKENIZER_VERSION = "2"

import re
import unicodedata

_KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9}
# 「第三回」「第十二回」などの漢数字（1〜99）を算用数字に揃える
_KANJI_NUM_RE = re.compile(r"(?<=第)([〇零一二三四五六七八九十]{1,3})(?=[回章週講節])")
_LEADING_ZERO_RE = re.compile(r"(?<![0-9.])0+(?=[0-9])")
_SPLIT_RE = re.compile(r"[\s　、。，．,.!?！？「」『』（）()\[\]【】〔〕<>＜＞:：;；/／\\・…\-─—=+*#|~^\"'`]+")
_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+")
_HIRAGANA_ONLY_RE = re.compile(r"^[ぁ-ゟ]+$")


def _kanji_to_int(s: str) -> str:
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _KANJI_DIGITS.get(head, 1) if head else 1
        ones = _KANJI_DIGITS.get(tail, 0) if tail else 0
        return str(tens * 10 + ones)
    if all(c in _KANJI_DIGITS for c in s):
        return "".join(str(_KANJI_DIGITS[c]) for c in s)
    return s


def normalize(text: str) -> str:
    """NFKC・小文字化・漢数字の授業回・先頭ゼロ除去（第03回→第3回）。"""
    t = unicodedata.normalize("NFKC", text).lower()
    t = _KANJI_NUM_RE.sub(lambda m: _kanji_to_int(m.group(1)), t)
    t = _LEADING_ZERO_RE.sub("", t)
    return t


def _segments(norm: str) -> list[str]:
    return [s for s in _SPLIT_RE.split(norm) if s]


def _bigrams(seg: str) -> list[str]:
    if len(seg) == 1:
        return [seg]
    return [seg[i:i + 2] for i in range(len(seg) - 1)]


def index_tokens(text: str) -> str:
    """インデックス用トークン列（空白区切り）。"""
    out: list[str] = []
    for seg in _segments(normalize(text)):
        out.extend(_bigrams(seg))
        out.extend(w for w in _ASCII_WORD_RE.findall(seg) if w != seg)
    return " ".join(out)


def query_tokens(text: str) -> list[str]:
    """検索用トークン（重複除去・順序保持）。ひらがなのみの bi-gram
    （助詞など）は精度を下げるため、他に手掛かりがある場合は除外する。"""
    toks: list[str] = []
    for seg in _segments(normalize(text)):
        toks.extend(_bigrams(seg))
        toks.extend(w for w in _ASCII_WORD_RE.findall(seg) if w != seg)
    seen: dict[str, None] = {}
    for t in toks:
        seen.setdefault(t, None)
    uniq = list(seen)
    content = [t for t in uniq if not _HIRAGANA_ONLY_RE.match(t)]
    return content or uniq


def fts_match_expr(tokens: list[str]) -> str:
    """FTS5 MATCH 式（OR 結合、各トークンを二重引用符でエスケープ）。"""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def lecture_numbers(text: str) -> set[int]:
    """「第3回」「3回目」「第3週」などから授業回番号を抽出する。"""
    norm = normalize(text)
    nums = re.findall(r"第\s*([0-9]{1,3})\s*[回章週講節]", norm)
    nums += re.findall(r"([0-9]{1,3})\s*回目", norm)
    nums += re.findall(r"(?:lecture|week|lec|w)[\s_-]*([0-9]{1,3})", norm)
    return {int(n) for n in nums}
