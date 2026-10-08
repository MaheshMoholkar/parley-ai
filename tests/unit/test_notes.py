"""The wording of write-back notes (core/notes.py)."""

from datetime import date

from parley.core.domain import CloseReason, EventType, TaskKind
from parley.core.notes import note_text


def test_each_event_has_a_short_internal_note() -> None:
    def note(event: EventType, **data: object) -> str | None:
        return note_text(event, dict(data), "INR")

    assert note(EventType.MESSAGE_SENT, channel="email", reminder=True) == (
        "Collections: reminder sent by email."
    )
    assert note(EventType.MESSAGE_SENT, channel="voice", reminder=True) == (
        "Collections: reminder call placed."
    )
    assert "payment link" in (note(EventType.MESSAGE_SENT, channel="email", reminder=False) or "")
    assert note(EventType.PROMISE_CREATED, amount=50000, promised_date=date(2026, 1, 9)) == (
        "Collections: the customer promised to pay INR 500.00 by 09 Jan 2026."
    )
    assert "the full amount" in (
        note(EventType.PROMISE_CREATED, amount=None, promised_date="2026-01-09") or ""
    )
    assert note(EventType.PROMISE_BROKEN, promised_date=date(2026, 1, 9)) == (
        "Collections: the promise to pay by 09 Jan 2026 was not kept."
    )
    assert note(EventType.CASE_CLOSED, reason=CloseReason.PAID) == (
        "Collections: closed, the invoice is paid."
    )
    assert note(EventType.TASK_CREATED, kind=TaskKind.VERIFY_PAYMENT) is not None


def test_internal_steps_get_no_note_and_reasons_are_cut() -> None:
    assert note_text(EventType.TASK_CREATED, {"kind": TaskKind.APPROVE_SEND}, "INR") is None
    long_reason = "Damaged goods.\n" + "x" * 500
    text = note_text(EventType.DISPUTE_OPENED, {"reason": long_reason}, "INR") or ""
    assert "\n" not in text and len(text) < 260
