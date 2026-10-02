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


class TaskAction(StrEnum):
    """What a person can do with a task."""

    APPROVE = "approve"  # approve_send: send the draft as it is
    EDIT = "edit"  # approve_send: send an edited version
    REJECT = "reject"  # approve_send: do not send this reminder
    RESUME = "resume"  # other tasks: go back to chasing
    CLOSE = "close"  # other tasks: stop chasing this invoice


class DisputeStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class PromiseStatus(StrEnum):
    OPEN = "open"
    KEPT = "kept"
    BROKEN = "broken"


class MessageStatus(StrEnum):
    # Outbound: drafting -> (awaiting_approval ->) pending -> sent
    DRAFTING = "drafting"  # queued; the drafter has not written it yet
    AWAITING_APPROVAL = "awaiting_approval"  # a person must approve, edit or reject it
    PENDING = "pending"  # written to the outbox, not yet handed to the channel
    SENT = "sent"
    FAILED = "failed"  # gave up after repeated delivery errors
    REJECTED = "rejected"  # a reviewer chose not to send it
    # Inbound: received -> read
    RECEIVED = "received"  # stored, not yet read by the reply reader
    READ = "read"


# Outbound messages that have not gone out yet but will (or may).
UNSENT_STATUSES = frozenset(
    {MessageStatus.DRAFTING, MessageStatus.AWAITING_APPROVAL, MessageStatus.PENDING}
)


class Direction(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"
