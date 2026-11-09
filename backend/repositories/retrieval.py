"""Exact retrieval over a materialized authorized, homogeneous vector set."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# MATERIALIZED is a correctness boundary: WHERE alone does not constrain evaluation
# order of vector distances when unrelated tenants use different dimensions.
AUTHORIZED = """
WITH authorized AS MATERIALIZED (
 SELECT c.id AS chunk_id,c.document_id,c.attempt_id,c.text,c.locator,
        c.embedding,c.lexemes,d.original_name AS file_name
 FROM app.document_chunks c
 JOIN app.documents d ON d.id=c.document_id AND d.organization_id=c.organization_id
   AND d.knowledge_base_id=c.knowledge_base_id AND d.current_attempt_id=c.attempt_id
 JOIN app.document_attempts a ON a.id=c.attempt_id AND a.organization_id=c.organization_id
   AND a.document_id=c.document_id AND a.embedding_profile_id=c.embedding_profile_id
 JOIN app.knowledge_bases k ON k.id=c.knowledge_base_id AND k.organization_id=c.organization_id
 WHERE c.organization_id=:org AND c.knowledge_base_id=:kb
   AND c.document_id=ANY(CAST(:docs AS uuid[]))
   AND d.deleted_at IS NULL AND k.deleted_at IS NULL AND d.status='ready' AND a.status='ready'
   AND c.embedding_profile_id=:profile AND c.embedding_dimensions=:dimension
   AND c.embedding IS NOT NULL AND c.text IS NOT NULL
)
"""
HYBRID = (
    AUTHORIZED
    + """
, vector_hits AS (
 SELECT *,1-(embedding <=> CAST(:vector AS public.vector)) AS rank_score
 FROM authorized ORDER BY embedding <=> CAST(:vector AS public.vector),chunk_id LIMIT 12
), keyword_hits AS (
 SELECT *,ts_rank(lexemes,to_tsquery('simple',:terms)) AS rank_score
 FROM authorized WHERE lexemes @@ to_tsquery('simple',:terms)
 ORDER BY rank_score DESC,chunk_id LIMIT 12
)
SELECT chunk_id,document_id,attempt_id,text,locator,file_name,rank_score,'vector' AS branch
FROM vector_hits
UNION ALL
SELECT chunk_id,document_id,attempt_id,text,locator,file_name,rank_score,'keyword' AS branch
FROM keyword_hits
"""
)


class RetrievalRepository:
    def __init__(
        self, db: AsyncSession, org: UUID, kb: UUID, docs: list[UUID], profile: UUID, dimension: int
    ) -> None:
        self.db = db
        self.params: dict[str, Any] = {
            "org": org,
            "kb": kb,
            "docs": docs,
            "profile": profile,
            "dimension": dimension,
        }

    async def counts(self) -> dict[UUID, int]:
        rows = await self.db.execute(
            text(AUTHORIZED + "SELECT document_id,count(*) FROM authorized GROUP BY document_id"),
            self.params,
        )
        return {row[0]: row[1] for row in rows}

    async def search(self, vector: list[float], terms: list[str]) -> list[dict[str, Any]]:
        # lexemes() emits only CJK/ASCII alphanumerics, never tsquery operators.
        rows = await self.db.execute(
            text(HYBRID), {**self.params, "vector": json.dumps(vector), "terms": " | ".join(terms)}
        )
        return [dict(row) for row in rows.mappings()]
