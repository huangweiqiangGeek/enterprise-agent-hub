"""Deterministic fusion and query-related, contiguous excerpts."""

from typing import Any
from uuid import UUID

from backend.rag.documents import lexemes


def fuse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores: dict[UUID, float] = {}
    chunks: dict[UUID, dict[str, Any]] = {}
    for branch in ("vector", "keyword"):
        ranked = sorted(
            (r for r in rows if r["branch"] == branch),
            key=lambda r: (-r["rank_score"], str(r["chunk_id"])),
        )
        for rank, row in enumerate(ranked, 1):
            key = row["chunk_id"]
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            chunks[key] = row
    return [chunks[key] for key in sorted(scores, key=lambda k: (-scores[k], str(k)))[:12]]


def excerpt(text: str, query: str, budget: int = 160) -> str:
    if len(text.encode()) <= budget:
        return text
    lower = text.lower()
    # Prefer the complete phrase, then longest matching normalized term. Keep original bytes.
    terms = [query.lower(), *sorted(lexemes(query).split(), key=len, reverse=True)]
    position = next((lower.find(term) for term in terms if term and term in lower), 0)
    start = max(0, position - 8)
    return text[start:].encode()[:budget].decode("utf-8", errors="ignore")
