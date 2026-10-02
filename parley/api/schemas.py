"""Shapes of API responses. `from_attributes=True` lets FastAPI build them
straight from database objects."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from parley.core.domain import (
    CaseState,
    CloseReason,
    Direction,
    InvoiceStatus,
    MessageStatus,
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


class SyncOut(BaseModel):
    invoices_seen: int
    invoices_created: int
    invoices_updated: int
    invoices_no_longer_open: int
    payments_created: int
    cases_opened: int
    cases_closed: int
    cases_reopened: int
