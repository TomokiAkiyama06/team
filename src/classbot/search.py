"""授業資料の検索（質問が指す資料の全文 ＋ SQLite FTS5 + 文字 bi-gram の全文検索）。

retrieve() が回答に使う資料を集める。
1. 質問が指している資料（このスレッドで登録・指定された資料、ファイル名や授業回で指定された資料）を
   特定し、その全文（長すぎる場合は質問に近い部分を上限まで）を渡す
2. 全資料を対象にキーワード検索（search()）し、1 で渡していない箇所を追加する

search() の手順:
1. 質問を textnorm.query_tokens() で bi-gram 化し、FTS5 の OR 検索で候補を取得
2. bm25（ファイル名・見出し列を重く）に、質問トークンの被覆率と授業回の一致で補正
3. 資料ごとの件数上限と、ほぼ同一内容のチャンク（Jaccard 類似度）の除去
4. 被覆率から「十分 / 弱い / なし」を判定し、モデルに伝える
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from classbot.config import Settings
from classbot.db import Database
from classbot.textnorm import fts_match_expr, index_tokens, lecture_numbers, normalize, query_tokens

log = logging.getLogger(__name__)

# bm25 の列重み（title_tokens, body_tokens）
TITLE_WEIGHT = 4.0
BODY_WEIGHT = 1.0
CANDIDATE_LIMIT = 80
NEAR_DUP_JACCARD = 0.8
SUFFICIENT_COVERAGE = 0.5
MIN_COVERAGE = 0.15
# ファイル名で資料を指定したとみなす拡張子なしの名前の最短文字数（短い名前の誤一致を防ぐ）
MIN_NAMED_STEM = 4
# 先頭ページから授業回を読み取る範囲（文字数）
LECTURE_HEAD_CHARS = 300

# 質問が資料を指している理由（モデルへの説明に使う）
FOCUS_NAMED = "named"      # 質問にファイル名が書かれている
FOCUS_LECTURE = "lecture"  # 質問の授業回（第N回）に該当する
FOCUS_THREAD = "thread"    # このスレッドで登録された／以前の質問で指定された
FOCUS_LATEST = "latest"    # 「今回の資料」「最新の授業」など（他に手掛かりが無いとき、最も新しく登録された資料）

# 「今回渡した資料」「今回の授業内容」「最新のスライド」「今週の課題」など、最新の資料を指す言い方。
# 「今回、apt でエラーが…」のような質問は対象にしないよう、資料・授業などの語が近くに続く場合に限る
_LATEST_REF_RE = re.compile(r"(?:今回|最新|今週)[^。、,.!?！？\n]{0,6}?"
                            r"(?:資料|授業|講義|スライド|課題|提出物|演習|プリント|pdf)")


@dataclass
class SearchHit:
    source_id: str          # "S1", "S2", ...（この検索結果内でのみ有効な出典ID）
    chunk_id: int
    material_id: int
    display_name: str
    location: str
    text: str
    score: float
    coverage: float


@dataclass
class FocusMaterial:
    """質問が指している資料。全文（上限を超える場合は質問に近い部分）を出典付きで渡す。"""
    material_id: int
    display_name: str
    reason: str                 # FOCUS_NAMED | FOCUS_LECTURE | FOCUS_THREAD
    total_chunks: int = 0
    source_ids: list[str] = field(default_factory=list)   # 渡した部分の出典ID（S1, S2, ...）

    @property
    def complete(self) -> bool:
        return len(self.source_ids) >= self.total_chunks


@dataclass
class SearchResult:
    hits: list[SearchHit]
    sufficiency: str        # "sufficient" | "weak" | "none"
    focus: list[FocusMaterial] = field(default_factory=list)

    def by_id(self) -> dict[str, SearchHit]:
        return {h.source_id: h for h in self.hits}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Searcher:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------------ 回答に使う資料の収集
    def retrieve(self, query: str, extra_context: str | None = None, *,
                 channel: str | None = None, thread_ts: str | None = None) -> SearchResult:
        """質問が指している資料の全文と、全資料からの検索結果を合わせて返す。
        出典IDは指している資料の部分から順に S1, S2, ... と振り直す。"""
        focus = self.resolve_focus(query, channel, thread_ts)
        focus_hits = self._focus_hits(focus, query)
        searched = self.search(query, extra_context, exclude_chunk_ids={h.chunk_id for h in focus_hits})
        hits = [replace(h, source_id=f"S{i}") for i, h in enumerate(focus_hits + searched.hits, 1)]
        by_material: dict[int, list[str]] = {}
        for h in hits[:len(focus_hits)]:
            by_material.setdefault(h.material_id, []).append(h.source_id)
        focus = [replace(f, source_ids=by_material[f.material_id]) for f in focus if f.material_id in by_material]
        # 指している資料を渡した場合は、その資料自体が根拠になるため「十分」とする
        sufficiency = "sufficient" if focus_hits else searched.sufficiency
        if focus:
            log.info("focus materials", extra={"focus": [(f.material_id, f.reason, len(f.source_ids),
                                                          f.total_chunks) for f in focus],
                                               "chars": sum(len(h.text) for h in focus_hits)})
        return SearchResult(hits, sufficiency, focus)

    def resolve_focus(self, query: str, channel: str | None = None,
                      thread_ts: str | None = None) -> list[FocusMaterial]:
        """質問が指している資料を、優先度の高い順（ファイル名 → 授業回 → スレッド）に返す。
        いずれも無く「今回の資料」のように尋ねている場合は、最も新しく登録された資料を返す。"""
        rows = self.db.query(
            """
            SELECT m.id, m.display_name, m.created_at,
                   (SELECT substr(COALESCE(c.heading, '') || ' ' || c.text, 1, ?)
                    FROM chunks c WHERE c.material_id = m.id AND c.version = m.active_version
                    ORDER BY c.seq LIMIT 1) AS head
            FROM materials m
            WHERE m.active_version IS NOT NULL
            ORDER BY m.id
            """, (LECTURE_HEAD_CHARS,))
        names = {r["id"]: r["display_name"] for r in rows}
        picked: dict[int, str] = {}

        nq = normalize(query)
        for r in rows:
            stem = normalize(Path(r["display_name"]).stem).strip()
            if (len(stem) >= MIN_NAMED_STEM and stem in nq) or normalize(r["display_name"]) in nq:
                picked.setdefault(r["id"], FOCUS_NAMED)

        q_lectures = lecture_numbers(query)
        if q_lectures:
            for r in rows:
                m_lectures = lecture_numbers(r["display_name"])
                if not m_lectures and r["head"]:
                    # ファイル名に授業回が無い資料（例: proxb_41.pdf）は先頭ページの「第4回」を使う。
                    # シラバスのように複数の回が並ぶ資料は特定の回の資料とはみなさない
                    head = lecture_numbers(r["head"])
                    m_lectures = head if len(head) == 1 else set()
                if m_lectures & q_lectures:
                    picked.setdefault(r["id"], FOCUS_LECTURE)

        if channel and thread_ts:
            linked = self.db.query(
                "SELECT material_id FROM thread_materials WHERE channel=? AND thread_ts=? "
                "ORDER BY created_at DESC, rowid DESC", (channel, thread_ts))
            for r in linked:
                if r["material_id"] in names:
                    picked.setdefault(r["material_id"], FOCUS_THREAD)

        if not picked and rows and _LATEST_REF_RE.search(normalize(query)):
            latest = max(rows, key=lambda r: (r["created_at"], r["id"]))
            picked[latest["id"]] = FOCUS_LATEST

        return [FocusMaterial(mid, names[mid], reason) for mid, reason in picked.items()]

    def _focus_hits(self, focus: list[FocusMaterial], query: str) -> list[SearchHit]:
        """指している資料のチャンクを、上限（focus_material_max_chars）まで資料の順序どおりに返す。
        上限を超える資料は、先頭チャンク（表紙・概要）と質問に近いチャンクを優先して選ぶ。"""
        remaining = self.settings.focus_material_max_chars
        tokens = set(query_tokens(query))
        out: list[SearchHit] = []
        for f in focus:
            rows = self.db.query(
                """
                SELECT c.id, c.seq, c.location, c.text
                FROM chunks c JOIN materials m ON m.id = c.material_id AND c.version = m.active_version
                WHERE c.material_id = ?
                ORDER BY c.seq
                """, (f.material_id,))
            f.total_chunks = len(rows)
            if not rows or remaining <= 0:
                continue
            if sum(len(r["text"]) for r in rows) <= remaining:
                chosen = list(rows)
            else:
                def closeness(r) -> float:
                    if not tokens:
                        return 0.0
                    return len(tokens & set(index_tokens(r["text"]).split())) / len(tokens)

                ranked = [rows[0]] + sorted(rows[1:], key=lambda r: (-closeness(r), r["seq"]))
                chosen, used = [], 0
                for r in ranked:
                    if used + len(r["text"]) <= remaining:
                        chosen.append(r)
                        used += len(r["text"])
                chosen.sort(key=lambda r: r["seq"])
            remaining -= sum(len(r["text"]) for r in chosen)
            out.extend(SearchHit(source_id="", chunk_id=r["id"], material_id=f.material_id,
                                 display_name=f.display_name, location=r["location"], text=r["text"],
                                 score=0.0, coverage=1.0) for r in chosen)
        return out

    # ------------------------------------------------------------------ キーワード検索
    def search(self, query: str, extra_context: str | None = None,
               exclude_chunk_ids: set[int] | None = None) -> SearchResult:
        """query で検索する。extra_context（直前の質問など）は検索語の補助にのみ使い、
        被覆率の計算には含めない。exclude_chunk_ids のチャンク（全文を渡した資料など）は除く。"""
        main_tokens = query_tokens(query)
        tokens = list(main_tokens)
        if extra_context:
            tokens += [t for t in query_tokens(extra_context) if t not in tokens]
        if not tokens:
            return SearchResult([], "none")
        cover_tokens = set(main_tokens or tokens)
        q_lectures = lecture_numbers(query) or (lecture_numbers(extra_context) if extra_context else set())

        rows = self.db.query(
            f"""
            SELECT c.id AS chunk_id, c.material_id, c.location, c.heading, c.text,
                   m.display_name,
                   bm25(chunks_fts, {TITLE_WEIGHT}, {BODY_WEIGHT}) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN materials m ON m.id = c.material_id AND c.version = m.active_version
            WHERE chunks_fts MATCH ?
              AND c.id NOT IN (SELECT value FROM json_each(?))
            ORDER BY rank
            LIMIT ?
            """,
            (fts_match_expr(tokens), json.dumps(sorted(exclude_chunk_ids or ())), CANDIDATE_LIMIT),
        )

        scored: list[tuple[float, float, set[str], object]] = []
        for r in rows:
            chunk_toks = set(index_tokens(f"{r['display_name']} {r['heading'] or ''} {r['text']}").split())
            coverage = len(cover_tokens & chunk_toks) / len(cover_tokens)
            score = -float(r["rank"]) * (0.5 + coverage)
            if q_lectures:
                m_lectures = lecture_numbers(r["display_name"]) | lecture_numbers(r["heading"] or "")
                if m_lectures & q_lectures:
                    score *= 1.8
                elif m_lectures:
                    score *= 0.6
            scored.append((score, coverage, set(index_tokens(r["text"]).split()), r))
        scored.sort(key=lambda x: x[0], reverse=True)

        picked: list[SearchHit] = []
        picked_toks: list[set[str]] = []
        per_material: dict[int, int] = {}
        total_chars = 0
        for score, coverage, toks, r in scored:
            if len(picked) >= self.settings.search_top_k:
                break
            if coverage < MIN_COVERAGE:
                continue
            mid = r["material_id"]
            if per_material.get(mid, 0) >= self.settings.search_max_per_material:
                continue
            if any(_jaccard(toks, p) >= NEAR_DUP_JACCARD for p in picked_toks):
                continue
            text = r["text"]
            if total_chars + len(text) > self.settings.search_context_max_chars:
                if picked:
                    break
                text = text[: self.settings.search_context_max_chars]
            total_chars += len(text)
            per_material[mid] = per_material.get(mid, 0) + 1
            picked_toks.append(toks)
            picked.append(SearchHit(
                source_id=f"S{len(picked) + 1}", chunk_id=r["chunk_id"], material_id=mid,
                display_name=r["display_name"], location=r["location"], text=text,
                score=score, coverage=coverage,
            ))

        if not picked:
            sufficiency = "none"
        elif max(h.coverage for h in picked) >= SUFFICIENT_COVERAGE:
            sufficiency = "sufficient"
        else:
            sufficiency = "weak"
        log.info("search", extra={"hits": len(picked), "candidates": len(rows),
                                  "sufficiency": sufficiency})
        return SearchResult(picked, sufficiency)
