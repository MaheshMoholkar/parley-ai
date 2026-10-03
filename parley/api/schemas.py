"""Shapes of API responses. `from_attributes=True` lets FastAPI build them
straight from database objects."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from parley.core.domain import (
    CaseState,
    CloseReason,
    Direction,
    InvoiceStatus,
    MessageStatus,
    SourceEventType,
    TaskAction,
    TaskKind,
    TaskStatus,
)


class _FromORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InvoiceOut(_FromORM):
    id: uuid.UUID
    number: str
    amount_due: int
    currency: str
    due_date: date
    status: InvoiceStatus


class CustomerOut(_FromORM):
    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None
    paused: bool


class CaseOut(_FromORM):
    id: uuid.UUID
    state: CaseState
    paused: bool
    next_action_at: datetime | None
    reminders_sent: int
    opened_at: datetime
    closed_at: datetime | None
    closed_reason: CloseReason | None
    invoice: InvoiceOut
    customer: CustomerOut


class MessageOut(_FromORM):
    id: uuid.UUID
    channel: str
    direction: Direction
    from_address: str | None
    to_address: str
    subject: str
    body: str
    status: MessageStatus
    created_at: datetime
    sent_at: datetime | None


class TaskOut(_FromORM):
    id: uuid.UUID
    case_id: uuid.UUID
    message_id: uuid.UUID | None
    kind: TaskKind
    summary: str
    status: TaskStatus
    resolution: str | None
    created_at: datetime


class ResolveTaskIn(BaseModel):
    action: TaskAction
    subject: str | None = None  # for "edit"
    body: str | None = None  # for "edit"
    note: str = ""


class CaseDetailOut(BaseModel):
    case: CaseOut
    messages: list[MessageOut]
    tasks: list[TaskOut]


class CasePage(BaseModel):
    items: list[CaseOut]
    total: int
    limit: int
    offset: int


class TaskPage(BaseModel):
    items: list[TaskOut]
    total: int
    limit: int
    offset: int


class TierUsageOut(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_micro_usd: int
    avg_latency_ms: float


class MetricsOut(BaseModel):
    cases_by_state: dict[str, int]
    promises_made: int
    promises_kept: int
    promises_broken: int
    promise_kept_rate: float | None
    cases_collected: int
    avg_days_to_collect: float | None
    model_cost_micro_usd: int
    cases_using_model: int
    cost_per_case_micro_usd: float | None
    cache_read_share: float | None
    usage_by_tier: dict[str, TierUsageOut]


class InboundOut(BaseModel):
    outcome: Literal["stored", "duplicate", "unmatched"]
    message_id: uuid.UUID | None = None
    reason: str = ""


class EventIn(BaseModel):
    """An event from the source system. `data` is kept for the record; the
    worker re-reads the source rather than trusting it."""

    id: str = Field(min_length=1, max_length=200)
    type: SourceEventType
    data: dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    outcome: Literal["accepted", "duplicate"]


class TestCallOut(BaseModel):
    call_id: uuid.UUID
    # Open a WebSocket to this path to talk (see the /voice page).
    websocket_path: str


class SyncOut(BaseModel):
    invoices_seen: int
    invoices_created: int
    invoices_updated: int
    invoices_no_longer_open: int
    payments_created: int
    cases_opened: int
    cases_closed: int
    cases_reopened: int
