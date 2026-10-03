"""Pure voice rules: phone numbers, the call audit, and the workflow events."""

from datetime import UTC, date, datetime

import pytest

from parley.core.domain import CaseState, TaskKind
from parley.core.messages import InvoiceLine
from parley.core.phone import to_e164
from parley.core.policy import Policy
from parley.core.voice import Turn, audit_call
from parley.core.workflow import (
    CALL_RETRY_DELAY,
    CaseView,
    on_call_audit_failed,
    on_call_not_reached,
)

NOW = datetime(2026, 1, 5, 10, tzinfo=UTC)
LINES = [InvoiceLine("A-1", 100000, "INR", date(2026, 1, 1))]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("98123 45678", "+919812345678"),
        ("098123-45678", "+919812345678"),
        ("+91 98123 45678", "+919812345678"),
        ("919812345678", "+919812345678"),
        ("0044 20 7946 0958", "+442079460958"),
        ("12345", None),
        ("022 2345 6789", None),  # a landline without a clear format: not guessed
        ("", None),
        (None, None),
    ],
)
def test_phone_numbers_are_normalised_or_refused(raw: str | None, expected: str | None) -> None:
    assert to_e164(raw) == expected


def test_policy_decides_which_reminders_are_calls() -> None:
    assert not Policy().prefers_call(5)
    policy = Policy(call_from_reminder=2)
    assert (policy.prefers_call(1), policy.prefers_call(2), policy.prefers_call(3)) == (
        False,
        True,
        True,
    )


GOOD_CALL = [
    Turn("agent", "Namaste, I am an AI assistant calling for Acme Traders. Is this Asha?"),
    Turn("customer", "Haan, main Asha hoon."),
    Turn("tool", "", tool="confirm_identity", ok=True),
    Turn("tool", "", tool="get_invoice", ok=True),
    Turn("agent", "Invoice A-1 for INR 1,000.00 was due on 1 January."),
    Turn("customer", "I will pay 500 rupees on Friday."),
    Turn("agent", "So 500 rupees on Friday. Thank you."),
]


def test_a_call_that_follows_the_rules_passes() -> None:
    assert audit_call(GOOD_CALL, LINES, promised_amounts=[50000]) == []


def test_the_audit_catches_each_broken_rule() -> None:
    no_disclosure = [Turn("agent", "Hello, is this Asha?"), *GOOD_CALL[1:]]
    assert "did not say it is an AI assistant" in audit_call(no_disclosure, LINES, [50000])[0]

    amount_too_early = [GOOD_CALL[0], Turn("agent", "You owe INR 1,000.00."), *GOOD_CALL[1:]]
    assert any(
        "before the identity check" in p for p in audit_call(amount_too_early, LINES, [50000])
    )

    wrong_amount = [*GOOD_CALL, Turn("agent", "Actually it is 2,000 rupees.")]
    assert any("not owed" in p for p in audit_call(wrong_amount, LINES, [50000]))

    threat = [*GOOD_CALL, Turn("agent", "Otherwise we will take legal action.")]
    assert "banned phrase: 'legal action'" in audit_call(threat, LINES, [50000])

    # A refused identity check does not unlock amounts.
    refused = [GOOD_CALL[0], Turn("tool", "", tool="confirm_identity", ok=False), GOOD_CALL[4]]
    assert any("before the identity check" in p for p in audit_call(refused, LINES))


def test_a_silent_call_has_nothing_to_audit() -> None:
    assert audit_call([Turn("customer", "Hello?")], LINES) == []


def view(state: CaseState) -> CaseView:
    return CaseView(state=state, reminders_sent=2, next_action_at=NOW, amount_due=100000)


def test_an_unanswered_call_reschedules_the_case() -> None:
    transition = on_call_not_reached(view(CaseState.AWAITING_REPLY), NOW)
    assert transition is not None
    assert (transition.state, transition.next_action_at) == (
        CaseState.SCHEDULED,
        NOW + CALL_RETRY_DELAY,
    )
    # A case that already moved on (e.g. paid meanwhile) is left alone.
    assert on_call_not_reached(view(CaseState.CLOSED), NOW) is None


def test_a_failed_audit_asks_for_a_review_without_moving_the_case() -> None:
    transition = on_call_audit_failed(view(CaseState.PROMISED), "banned phrase")
    assert transition is not None
    assert (transition.state, transition.task) == (CaseState.PROMISED, TaskKind.REVIEW_CALL)
    assert on_call_audit_failed(view(CaseState.CLOSED), "x") is None
