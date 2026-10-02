"""Names shared across the app: case states, invoice statuses, task kinds and so on.

`StrEnum` members behave like plain strings ("scheduled"), which is how they are
stored in the database and shown in the API.
"""

from enum import StrEnum


class CaseState(StrEnum):
    SCHEDULED = "scheduled"
    AWAITING_REPLY = "awaiting_reply"
    PROMISED = "promised"
    INVESTIGATING = "investigating"
    NEEDS_HUMAN = "needs_human"
    CLOSED = "closed"


# States that carry a timer (`next_action_at`) the worker acts on.
TIMED_STATES = frozenset({CaseState.SCHEDULED, CaseState.AWAITING_REPLY, CaseState.PROMISED})


class CloseReason(StrEnum):
    PAID = "paid"
    VOID = "void"
    REMOVED = "removed"  # the source system no longer returns the invoice
    HUMAN = "human"


class InvoiceStatus(StrEnum):
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    REMOVED = "removed"


class TaskKind(StrEnum):
    ESCALATION = "escalation"
    REVIEW_REPLY = "review_reply"
    APPROVE_SEND = "approve_send"
    VERIFY_PAYMENT = "verify_payment"
    REVIEW_DISPUTE = "review_dispute"


class TaskStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class ReplyIntent(StrEnum):
    PROMISE = "promise"
    DISPUTE = "dispute"
    PAID_CLAIM = "paid_claim"
    QUESTION = "question"
    WRONG_CONTACT = "wrong_contact"
    OUT_OF_OFFICE = "out_of_office"
    OTHER = "other"


class PromiseStatus(StrEnum):
    OPEN = "open"
    KEPT = "kept"
    BROKEN = "broken"


class MessageStatus(StrEnum):
    PENDING = "pending"  # written to the outbox, not yet handed to the channel
    SENT = "sent"
    FAILED = "failed"  # gave up after repeated delivery errors


class Direction(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"
