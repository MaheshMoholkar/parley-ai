"""The notes written to the source system about collection activity on an
invoice (spec: "Write-back").

One short line per event, written for the business's own staff (the notes are
internal: never shown to the customer). Amounts and dates come from the event
data, which came from the books; a customer's own words appear only in a
dispute, cut to a sentence or two.
"""

from datetime import date
from typing import Any

from parley.core.domain import CloseReason, EventType, TaskKind
from parley.core.money import format_money

PREFIX = "Collections: "
MAX_REASON_CHARS = 200

_TASKS = {
    TaskKind.ESCALATION: "handed to a person to follow up.",
    TaskKind.REVIEW_REPLY: "the customer's reply needs a person.",
    TaskKind.VERIFY_PAYMENT: "the customer says they paid; a person is checking the payment.",
    TaskKind.REVIEW_DISPUTE: "the dispute is with a person for review.",
    TaskKind.REVIEW_CALL: "a call is being reviewed by a person.",
    # approve_send is internal to the review screen: no note.
}

_CLOSED = {
    CloseReason.PAID: "closed, the invoice is paid.",
    CloseReason.VOID: "closed, the invoice was cancelled.",
    CloseReason.REMOVED: "closed, the invoice is no longer in the books.",
    CloseReason.HUMAN: "stopped by a person.",
}


def note_text(event_type: EventType, data: dict[str, Any], currency: str) -> str | None:
    """The note for this event, or None if the event is not worth a note.
    `currency` is the invoice's, for formatting a promised amount."""
    match event_type:
        case EventType.CASE_OPENED:
            text = "the invoice is overdue; reminders will follow the usual schedule."
        case EventType.MESSAGE_SENT:
            text = _sent(data)
        case EventType.REPLY_RECEIVED:
            text = "the customer replied by email."
        case EventType.PROMISE_CREATED:
            amount = data.get("amount")
            how_much = format_money(amount, currency) if amount else "the full amount"
            text = f"the customer promised to pay {how_much} by {_day(data['promised_date'])}."
        case EventType.PROMISE_BROKEN:
            text = f"the promise to pay by {_day(data['promised_date'])} was not kept."
        case EventType.DISPUTE_OPENED:
            reason = " ".join(str(data.get("reason", "")).split())[:MAX_REASON_CHARS]
            text = f"the customer disputes the invoice: {reason}"
        case EventType.TASK_CREATED:
            found = _TASKS.get(TaskKind(data["kind"]))
            if found is None:
                return None
            text = found
        case EventType.CASE_CLOSED:
            closed = data.get("reason")
            text = _CLOSED[CloseReason(closed)] if closed else "closed."
        case _:
            return None
    return PREFIX + text


def _sent(data: dict[str, Any]) -> str:
    if data.get("channel") == "voice":
        return "reminder call placed."
    if data.get("reminder"):
        return "reminder sent by email."
    return "payment link sent by email, as the customer asked."


def _day(value: date | str) -> str:
    day = value if isinstance(value, date) else date.fromisoformat(str(value))
    return f"{day:%d %b %Y}"
