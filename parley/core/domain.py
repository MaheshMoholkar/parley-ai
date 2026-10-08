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
    REVIEW_CALL = "review_call"  # a call's transcript failed its audit


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


class FindingResult(StrEnum):
    """The investigator's conclusion about a paid claim or a dispute."""

    PAYMENT_FOUND = "payment_found"
    PARTIAL_PAYMENT = "partial_payment"
    PAYMENT_NOT_FOUND = "payment_not_found"  # not recorded in the books yet
    DISPUTE_NEEDS_HUMAN = "dispute_needs_human"
    UNCLEAR = "unclear"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    DONE = "done"
    CANCELLED = "cancelled"  # the case moved on before the run started


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
    # No longer right to send by the time it would go: the case closed, the
    # customer was paused or disputed, or the amounts changed. Not sent.
    WITHDRAWN = "withdrawn"
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


class WebhookStatus(StrEnum):
    PENDING = "pending"  # waiting for its first or next attempt
    DELIVERED = "delivered"
    FAILED = "failed"  # gave up after the last retry


class EventType(StrEnum):
    """Events sent to the tenant's webhook URL (spec: "Outbound events")."""

    CASE_OPENED = "case.opened"
    MESSAGE_SENT = "message.sent"
    REPLY_RECEIVED = "reply.received"
    PROMISE_CREATED = "promise.created"
    PROMISE_BROKEN = "promise.broken"
    DISPUTE_OPENED = "dispute.opened"
    TASK_CREATED = "task.created"
    CASE_CLOSED = "case.closed"


class SourceEventType(StrEnum):
    """Events the source system may push to `POST /v1/events`."""

    INVOICE_CREATED = "invoice.created"
    INVOICE_UPDATED = "invoice.updated"
    INVOICE_VOIDED = "invoice.voided"
    PAYMENT_RECORDED = "payment.recorded"


class CallStatus(StrEnum):
    """A phone call's progress. A call is one outbound message on the voice channel."""

    PLACED = "placed"  # the telephony provider is dialling
    IN_PROGRESS = "in_progress"  # someone answered and the agent is talking
    ANSWERED = "answered"  # ended after a conversation
    NOT_REACHED = "not_reached"  # no answer, busy, failed, or an answering machine


# What the post-call audit can conclude.
class CallAudit(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class NoteStatus(StrEnum):
    """A note for the source system (write-back), in its outbox."""

    PENDING = "pending"
    WRITTEN = "written"
    FAILED = "failed"  # refused, unsupported, or out of retries
