"""Database tables (spec: "Canonical data model").

Each class is one table. `Mapped[int]` declares a column's Python type, and
`mapped_column(...)` adds database details such as indexes and defaults.

Rules that hold for every table:
- Every row has `tenant_id`, and every query filters on it.
- Rows copied from a source system are unique on (tenant_id, source, external_id),
  so syncing twice updates rows instead of duplicating them.
- Money is a whole number of minor units (BigInteger) plus a currency code.
- Times are timezone-aware (`timestamptz`), stored in UTC.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from parley.core.domain import (
    AgentRunStatus,
    CallAudit,
    CallStatus,
    CaseState,
    CloseReason,
    Direction,
    DisputeStatus,
    FindingResult,
    InvoiceStatus,
    MessageStatus,
    PromiseStatus,
    ReplyIntent,
    TaskKind,
    TaskStatus,
    WebhookStatus,
)
from parley.core.policy import Policy
from parley.core.workflow import CaseView

# Predictable constraint names, so migrations can refer to them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum(enum_class: type[StrEnum]) -> Enum:
    """Store an enum as its string value, with a CHECK constraint listing allowed values."""
    return Enum(
        enum_class,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [m.value for m in members],
        name=enum_class.__name__.lower(),
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("tenants.id"), index=True)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    default_language: Mapped[str] = mapped_column(String(16), default="en")
    # Only the settings this tenant changes; see `policy` below.
    policy_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Which accounting adapter to use and its settings, e.g. {"kind": "csv", ...}.
    adapter_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # SHA-256 of the API key. The key itself is shown once and never stored.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Static payment link or UPI id included in reminders, if the tenant set one.
    payment_link: Mapped[str | None] = mapped_column(String(500))
    # Where outbound events are posted; None means the tenant takes no webhooks.
    webhook_url: Mapped[str | None] = mapped_column(String(500))
    # Signs outbound events and verifies inbound ones (HMAC-SHA256).
    webhook_secret: Mapped[str] = mapped_column(String(64))
    # A person's phone number that calls are handed to when a customer asks for
    # one. None means the customer is told someone will call back.
    voice_transfer_number: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created_at()

    @property
    def policy(self) -> Policy:
        return Policy.model_validate(self.policy_overrides)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "source", "external_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(32))
    # The fields below belong to the core; sync never overwrites them.
    language: Mapped[str | None] = mapped_column(String(16))
    brief: Mapped[str] = mapped_column(Text, default="")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created_at()


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "external_id"),
        CheckConstraint("amount_due >= 0", name="amount_due_not_negative"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(200))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    number: Mapped[str] = mapped_column(String(100))
    amount_due: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[InvoiceStatus] = mapped_column(_enum(InvoiceStatus))
    display_details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped[Customer] = relationship()


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("tenant_id", "source", "external_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(200))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    paid_on: Mapped[date] = mapped_column(Date)
    reference: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = _created_at()


class Case(Base):
    """One collection case per overdue invoice."""

    __tablename__ = "cases"
    __table_args__ = (
        # The worker's main query: due cases of a tenant.
        Index("ix_cases_due", "tenant_id", "state", "next_action_at"),
        CheckConstraint("reminders_sent >= 0", name="reminders_sent_not_negative"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    # unique=True: an invoice never gets a second case.
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), unique=True)
    # Copied from the invoice so the worker can group cases by customer cheaply.
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    state: Mapped[CaseState] = mapped_column(_enum(CaseState))
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0)
    # Reminders a person allowed beyond the policy limit when resuming the case.
    extra_reminders: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_reason: Mapped[CloseReason | None] = mapped_column(_enum(CloseReason))

    invoice: Mapped[Invoice] = relationship()
    customer: Mapped[Customer] = relationship()

    def view(self) -> CaseView:
        """The plain snapshot the workflow rules work with."""
        return CaseView(
            state=self.state,
            reminders_sent=self.reminders_sent,
            next_action_at=self.next_action_at,
            amount_due=self.invoice.amount_due,
            close_reason=self.closed_reason,
            extra_reminders=self.extra_reminders,
        )


class Message(Base):
    """One message to or from a customer. Outbound messages double as the outbox."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_outbox", "tenant_id", "status", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    direction: Mapped[Direction] = mapped_column(_enum(Direction))
    to_address: Mapped[str] = mapped_column(String(320))
    # Inbound only: who sent it.
    from_address: Mapped[str | None] = mapped_column(String(320))
    # Outbound only: random token in the Reply-To address, so a reply finds this
    # message (and its cases) without trusting the subject line.
    reply_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[MessageStatus] = mapped_column(_enum(MessageStatus))
    # Sent to the channel unchanged on every retry, so the provider can drop duplicates.
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    links: Mapped[list["MessageCase"]] = relationship(back_populates="message")


class MessageCase(Base):
    """Links a message to each case it is about (one reminder can cover several invoices)."""

    __tablename__ = "message_cases"
    __table_args__ = (
        # A case's reminder N can be recorded only once, even if a step is retried.
        UniqueConstraint("case_id", "reminder_number"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    # None for messages that are not reminders (e.g. inbound replies).
    reminder_number: Mapped[int | None] = mapped_column(Integer)
    # The invoice's amount due when the message text was finalised, so an audit
    # can check what was sent against what was owed at the time.
    amount_due: Mapped[int | None] = mapped_column(BigInteger)

    message: Mapped[Message] = relationship(back_populates="links")


class Promise(Base):
    __tablename__ = "promises"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    promised_date: Mapped[date] = mapped_column(Date)
    status: Mapped[PromiseStatus] = mapped_column(_enum(PromiseStatus))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = _created_at()


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[DisputeStatus] = mapped_column(_enum(DisputeStatus))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    """One investigation: the claim, every step of the agent loop, and the finding.

    Queued when a reply is read as a paid claim or a dispute; filled in by the
    investigation step. The stored steps let a run be replayed and scored."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_queue", "tenant_id", "status", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    # The customer's reply that made the claim.
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    claim: Mapped[ReplyIntent] = mapped_column(_enum(ReplyIntent))
    status: Mapped[AgentRunStatus] = mapped_column(_enum(AgentRunStatus))
    # How the loop ended: final, step_limit, time_limit, model_error, no_model.
    outcome: Mapped[str | None] = mapped_column(String(32))
    result: Mapped[FindingResult | None] = mapped_column(_enum(FindingResult))
    finding: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_micro_usd: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Task(Base):
    """Work for a human: an escalation, a reply to review, a draft to approve, ..."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    # For approve_send tasks: the message waiting for approval.
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    # For tasks created from an investigation: the run with the evidence.
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    kind: Mapped[TaskKind] = mapped_column(_enum(TaskKind))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(_enum(TaskStatus), default=TaskStatus.OPEN)
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelCall(Base):
    """One model call, kept for cost reporting, debugging and evals."""

    __tablename__ = "model_calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"), index=True)
    prompt_version: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(100))
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Estimated cost in millionths of a US dollar (whole numbers, like money).
    cost_micro_usd: Mapped[int] = mapped_column(BigInteger, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UnmatchedInbound(Base):
    """Inbound mail that could not be matched to a case. Not tenant-scoped,
    because without a match the tenant is unknown; an operator reviews these."""

    __tablename__ = "unmatched_inbound"

    id: Mapped[uuid.UUID] = _uuid_pk()
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    from_address: Mapped[str] = mapped_column(String(320))
    to_addresses: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(200))


class InboundEvent(Base):
    """An event the source system pushed (`POST /v1/events`). Kept so a repeated
    delivery of the same event id is recognised, and so the worker knows a sync
    has been asked for since the last one."""

    __tablename__ = "inbound_events"
    __table_args__ = (UniqueConstraint("tenant_id", "event_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    event_id: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OutboundEvent(Base):
    """Outbox for webhooks (spec: "Outbound events"). Written in the same
    transaction as the change it reports, then posted by the worker until the
    receiver accepts it, so delivery is at least once."""

    __tablename__ = "outbound_events"
    __table_args__ = (Index("ix_outbound_events_due", "tenant_id", "status", "next_attempt_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Increases with every event, so events are posted in the order they happened.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    type: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[WebhookStatus] = mapped_column(_enum(WebhookStatus))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Call(Base):
    """A phone call. The call is one outbound message on the "voice" channel (the
    contact log entry); this row adds what only a call has: the provider's call
    id, the transcript with the tools the agent used, and the audit result."""

    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), unique=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    # Random; links the provider's callbacks and audio stream to this call.
    token: Mapped[str] = mapped_column(String(64), unique=True)
    provider_call_id: Mapped[str | None] = mapped_column(String(100))
    to_number: Mapped[str] = mapped_column(String(32))
    # A test call from the browser page, not a reminder.
    test: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[CallStatus] = mapped_column(_enum(CallStatus))
    # "human", "machine_start", ... as the provider detected it.
    answered_by: Mapped[str | None] = mapped_column(String(32))
    # Set once the agent's confirm_identity tool succeeded; amounts are only
    # given out after that.
    identity_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # The conversation in order: {"role", "text", "tool", "ok", "at"} per turn.
    turns: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    audit: Mapped[CallAudit | None] = mapped_column(_enum(CallAudit))
    audit_problems: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the call's audio stream connected; a call accepts only one.
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
