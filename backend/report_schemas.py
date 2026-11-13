"""Read-only M6 contracts. Date ranges use local calendar boundaries."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from backend.run_schemas import RunStatus
from backend.schemas import DTO


class RunFilters(DTO):
    scope: Literal["mine", "organization"] = "mine"
    status: RunStatus | None = None
    agent_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    timezone: str = Field(default="UTC", max_length=100)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        try:
            ZoneInfo(self.timezone)
            if bool(self.start_date) != bool(self.end_date):
                raise ValueError("Both dates are required")
            if self.start_date and self.end_date:
                if self.start_date > self.end_date or self.end_date == date.max:
                    raise ValueError("Invalid date range")
                self.bounds()
        except (ZoneInfoNotFoundError, ValueError, OverflowError) as exc:
            raise ValueError("Use a valid IANA timezone and inclusive date range") from exc
        return self

    def bounds(self) -> tuple[datetime | None, datetime | None]:
        if not self.start_date or not self.end_date:
            return None, None
        zone = ZoneInfo(self.timezone)
        return (
            datetime.combine(self.start_date, time.min, zone).astimezone(UTC),
            datetime.combine(self.end_date + timedelta(days=1), time.min, zone).astimezone(UTC),
        )


class RunListFilters(RunFilters):
    cursor: str | None = Field(default=None, max_length=2000)
    limit: int = Field(default=20, ge=1, le=100)


class UsageTotals(DTO):
    attempt_count: int = 0
    actual_usage_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_tokens: int = 0
    estimated_usage_count: int = 0
    unavailable_usage_count: int = 0
    unknown_outcome_count: int = 0
    pending_count: int = 0
    failed_count: int = 0
    billed_units: dict[str, int] = Field(default_factory=dict)


class DurationStats(DTO):
    count: int
    avg: float | None
    p50: float | None
    p95: float | None


class UsageSummary(DTO):
    run_count: int
    status_counts: dict[str, int]
    terminal_count: int
    success_rate: float | None
    chat: UsageTotals
    by_kind: dict[str, UsageTotals]
    queue_duration: DurationStats
    execution_duration: DurationStats
    range_utc: dict[str, datetime | None]
    timezone: str
    scope: str
    observed_at: datetime


class RunAgentOption(DTO):
    id: UUID
    name: str


class RunSummary(DTO):
    id: UUID
    conversation_id: UUID
    agent_id: UUID
    agent_name: str
    version_number: int
    task_type: str
    initiator_id: UUID
    initiator_name: str
    status: RunStatus
    created_at: datetime
    queue_duration_ms: int | None
    execution_duration_ms: int | None
    chat: UsageTotals


class RunReport(DTO):
    agent_name: str
    version_number: int
    initiator_name: str
    is_owner: bool
    queue_duration_ms: int | None
    execution_duration_ms: int | None
    by_kind: dict[str, UsageTotals]
    retry_ids: list[UUID]
