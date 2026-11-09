"""Public retrieval contracts shared by preview and future typed knowledge tools."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator

from backend.model_schemas import TestUsage
from backend.schemas import DTO


class SearchPreview(DTO):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    document_ids: Annotated[list[UUID], Field(max_length=1000)] = []

    @field_validator("document_ids")
    @classmethod
    def unique_documents(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("Document selectors must be unique")
        return value


class RetrievalItem(DTO):
    chunk_id: UUID
    document_id: UUID
    attempt_id: UUID
    file_name: str
    text: str
    locator: dict[str, Any]
    score: float


class RetrievalCoverage(DTO):
    eligible_document_count: int = 0
    eligible_chunk_count: int = 0
    vector_count: int = 0
    keyword_count: int = 0
    candidate_count: int = 0
    returned_count: int = 0
    omitted_chunk_count: int = 0
    omitted_document_count: int = 0
    excerpt_truncated_count: int = 0
    excerpt_truncated_document_count: int = 0
    context_truncated_count: int = 0
    query_terms_truncated: bool = False
    evidence_bytes: int = 0
    budget_basis: Literal["utf8-byte-upper-bound"] = "utf8-byte-upper-bound"
    partial: bool = False
    note: str = "仅反映本次检索片段，不代表全文覆盖；排序分数不是事实可信度。"


class RetrievalResult(DTO):
    operation_id: UUID
    query: str
    searched_document_ids: list[UUID]
    items: list[RetrievalItem]
    coverage: RetrievalCoverage
    usage: list[TestUsage]
