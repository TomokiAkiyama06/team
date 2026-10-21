"""授業資料の全文検索（SQLite FTS5 + 文字 bi-gram）。

1. 質問を textnorm.query_tokens() で bi-gram 化し、FTS5 の OR 検索で候補を取得
2. bm25（ファイル名・見出し列を重く）に、質問トークンの被覆率と授業回の一致で補正
3. 資料ごとの件数上限と、ほぼ同一内容のチャンク（Jaccard 類似度）の除去
4. 被覆率から「十分 / 弱い / なし」を判定し、モデルに伝える
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from classbot.config import Settings
from classbot.db import Database
from classbot.textnorm import fts_match_expr, index_tokens, lecture_numbers, query_tokens

log = logging.getLogger(__name__)

# bm25 の列重み（title_tokens, body_tokens）
TITLE_WEIGHT = 4.0
BODY_WEIGHT = 1.0
CANDIDATE_LIMIT = 80
NEAR_DUP_JACCARD = 0.8
SUFFICIENT_COVERAGE = 0.5
MIN_COVERAGE = 0.2


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
class SearchResult:
    hits: list[SearchHit]
    sufficiency: str        # "sufficient" | "weak" | "none"

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

    def search(self, query: str, extra_context: str | None = None) -> SearchResult:
        """query で検索する。extra_context（直前の質問など）は検索語の補助にのみ使い、
        被覆率の計算には含めない。"""
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
            ORDER BY rank
            LIMIT ?
            """,
            (fts_match_expr(tokens), CANDIDATE_LIMIT),
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
