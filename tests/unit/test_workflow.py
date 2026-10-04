"""Every row of the spec's transition table, tested against the pure state machine."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from parley.core.domain import (
    CaseState,
    CloseReason,
    FindingResult,
    InvoiceStatus,
    PromiseStatus,
    ReplyIntent,
    TaskAction,
    TaskKind,
)
from parley.core.policy import Policy
from parley.core.workflow import (
    CannotContact,
    CaseView,
    ContactLater,
    ContactNow,
    Promise,
    Reply,
    WorkflowError,
    first_reminder_at,
    on_delivery_failed,
    on_investigation_done,
    on_reply,
    on_source_update,
    on_task_resolved,
    on_timer,
)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=IST)  # a Monday
POLICY = Policy()


def case(
    state: CaseState = CaseState.SCHEDULED,
    reminders_sent: int = 0,
    amount_due: int = 100_000,
    close_reason: CloseReason | None = None,
) -> CaseView:
    return CaseView(state, reminders_sent, NOW, amount_due, close_reason)


# --- Timer -------------------------------------------------------------------------


def test_scheduled_sends_a_reminder_when_contact_is_allowed() -> None:
    t = on_timer(case(), POLICY, NOW, ContactNow())
    assert t.state == CaseState.AWAITING_REPLY
    assert t.send_reminder
    assert t.next_action_at == NOW + timedelta(days=POLICY.reminder_gap_days)


def test_scheduled_waits_when_contact_is_not_allowed_yet() -> None:
    later = NOW + timedelta(hours=5)
    t = on_timer(case(), POLICY, NOW, ContactLater(later))
    assert t.state == CaseState.SCHEDULED
    assert t.next_action_at == later
    assert not t.send_reminder


def test_scheduled_escalates_at_the_reminder_limit() -> None:
    t = on_timer(case(reminders_sent=POLICY.max_reminders), POLICY, NOW, ContactNow())
    assert t.state == CaseState.NEEDS_HUMAN
    assert t.task == TaskKind.ESCALATION
    assert not t.send_reminder


def test_scheduled_escalates_when_the_customer_cannot_be_contacted() -> None:
    t = on_timer(case(), POLICY, NOW, CannotContact("no email"))
    assert t.state == CaseState.NEEDS_HUMAN
    assert t.task == TaskKind.ESCALATION
    assert t.task_summary == "no email"


def test_no_reply_goes_back_to_scheduled() -> None:
    t = on_timer(case(CaseState.AWAITING_REPLY, reminders_sent=1), POLICY, NOW, ContactNow())
    assert t.state == CaseState.SCHEDULED
    assert t.next_action_at == NOW


def test_promise_date_passing_marks_the_promise_broken() -> None:
    t = on_timer(case(CaseState.PROMISED), POLICY, NOW, ContactNow())
    assert t.state == CaseState.SCHEDULED
    assert t.promise_outcome == PromiseStatus.BROKEN


@pytest.mark.parametrize(
    "state", [CaseState.INVESTIGATING, CaseState.NEEDS_HUMAN, CaseState.CLOSED]
)
def test_states_without_a_timer_reject_timer_events(state: CaseState) -> None:
    with pytest.raises(WorkflowError):
        on_timer(case(state), POLICY, NOW, ContactNow())


# --- Replies -----------------------------------------------------------------------


def test_valid_promise_moves_to_promised_and_sleeps_until_date_plus_grace() -> None:
    reply = Reply(ReplyIntent.PROMISE, promised_date=date(2026, 1, 10), promised_amount=50_000)
    t = on_reply(case(CaseState.AWAITING_REPLY), reply, POLICY, NOW, IST)
    assert t.state == CaseState.PROMISED
    assert t.new_promise == Promise(amount=50_000, promised_date=date(2026, 1, 10))
    # Promised the 10th, one day of grace: broken if still unpaid when the 12th starts.
    assert t.next_action_at == datetime(2026, 1, 12, tzinfo=IST)


def test_promise_without_amount_means_the_full_amount() -> None:
    reply = Reply(ReplyIntent.PROMISE, promised_date=date(2026, 1, 10))
    t = on_reply(case(CaseState.AWAITING_REPLY), reply, POLICY, NOW, IST)
    assert t.new_promise is not None
    assert t.new_promise.amount == 100_000


@pytest.mark.parametrize(
    "reply",
    [
        Reply(ReplyIntent.PROMISE, promised_date=date(2026, 1, 4)),  # in the past
        Reply(ReplyIntent.PROMISE, promised_date=date(2026, 1, 5)),  # today
        Reply(ReplyIntent.PROMISE, promised_date=None),
        Reply(ReplyIntent.PROMISE, promised_date=date(2026, 3, 1)),  # beyond 30 days
        Reply(ReplyIntent.PROMISE, promised_date=date(2026, 1, 10), promised_amount=200_000),
        Reply(ReplyIntent.PROMISE, promised_date=date(2026, 1, 10), promised_amount=0),
        Reply(ReplyIntent.PROMISE, promised_date=date(2026, 1, 10), promised_amount=100),  # 0.1%
    ],
)
def test_invalid_promise_goes_to_a_human(reply: Reply) -> None:
    t = on_reply(case(CaseState.AWAITING_REPLY), reply, POLICY, NOW, IST)
    assert t.state == CaseState.NEEDS_HUMAN
    assert t.task == TaskKind.REVIEW_REPLY
    assert t.new_promise is None


def test_dispute_starts_an_investigation_and_records_the_dispute() -> None:
    reply = Reply(ReplyIntent.DISPUTE, summary="Says half the goods arrived damaged.")
    t = on_reply(case(CaseState.AWAITING_REPLY), reply, POLICY, NOW, IST)
    assert (t.state, t.next_action_at, t.task) == (CaseState.INVESTIGATING, None, None)
    assert t.investigate == ReplyIntent.DISPUTE
    assert t.new_dispute == "Says half the goods arrived damaged."


def test_paid_claim_starts_an_investigation() -> None:
    t = on_reply(case(CaseState.AWAITING_REPLY), Reply(ReplyIntent.PAID_CLAIM), POLICY, NOW, IST)
    assert (t.state, t.next_action_at, t.task) == (CaseState.INVESTIGATING, None, None)
    assert t.investigate == ReplyIntent.PAID_CLAIM


@pytest.mark.parametrize(
    ("claim", "kind"),
    [
        (ReplyIntent.PAID_CLAIM, TaskKind.VERIFY_PAYMENT),
        (ReplyIntent.DISPUTE, TaskKind.REVIEW_DISPUTE),
    ],
)
def test_finished_investigation_goes_to_a_person(claim: ReplyIntent, kind: TaskKind) -> None:
    t = on_investigation_done(
        case(CaseState.INVESTIGATING), claim, FindingResult.PAYMENT_NOT_FOUND, "Searched Dec-Jan."
    )
    assert t is not None
    assert (t.state, t.task) == (CaseState.NEEDS_HUMAN, kind)
    assert "Check the bank" in t.task_summary
    assert t.task_summary.endswith("Investigator (payment_not_found): Searched Dec-Jan.")


def test_finished_investigation_on_a_case_that_moved_on_changes_nothing() -> None:
    closed = case(CaseState.CLOSED, close_reason=CloseReason.PAID)
    assert on_investigation_done(closed, ReplyIntent.PAID_CLAIM, FindingResult.UNCLEAR, "") is None


def test_out_of_office_tries_again_after_the_gap() -> None:
    t = on_reply(case(CaseState.AWAITING_REPLY), Reply(ReplyIntent.OUT_OF_OFFICE), POLICY, NOW, IST)
    assert t.state == CaseState.SCHEDULED
    assert t.next_action_at == NOW + timedelta(days=POLICY.reminder_gap_days)


@pytest.mark.parametrize(
    "intent", [ReplyIntent.QUESTION, ReplyIntent.WRONG_CONTACT, ReplyIntent.OTHER]
)
def test_other_replies_go_to_a_human(intent: ReplyIntent) -> None:
    t = on_reply(case(CaseState.AWAITING_REPLY), Reply(intent), POLICY, NOW, IST)
    assert t.state == CaseState.NEEDS_HUMAN
    assert t.task == TaskKind.REVIEW_REPLY


def test_reply_while_a_human_owns_the_case_keeps_the_state() -> None:
    t = on_reply(case(CaseState.NEEDS_HUMAN), Reply(ReplyIntent.PROMISE), POLICY, NOW, IST)
    assert t.state == CaseState.NEEDS_HUMAN
    assert t.task == TaskKind.REVIEW_REPLY


# --- Source updates ------------------------------------------------------------------


@pytest.mark.parametrize("state", [s for s in CaseState if s != CaseState.CLOSED])
def test_paid_invoice_closes_the_case_from_any_state(state: CaseState) -> None:
    t = on_source_update(case(state), InvoiceStatus.PAID, 0, NOW)
    assert t is not None
    assert t.state == CaseState.CLOSED
    assert t.close_reason == CloseReason.PAID
    assert t.promise_outcome == PromiseStatus.KEPT


def test_open_invoice_with_nothing_due_counts_as_paid() -> None:
    t = on_source_update(case(), InvoiceStatus.OPEN, 0, NOW)
    assert t is not None
    assert t.close_reason == CloseReason.PAID


@pytest.mark.parametrize(
    ("status", "reason"),
    [(InvoiceStatus.VOID, CloseReason.VOID), (InvoiceStatus.REMOVED, CloseReason.REMOVED)],
)
def test_void_or_removed_invoice_closes_the_case(
    status: InvoiceStatus, reason: CloseReason
) -> None:
    t = on_source_update(case(CaseState.AWAITING_REPLY), status, 100_000, NOW)
    assert t is not None
    assert t.state == CaseState.CLOSED
    assert t.close_reason == reason
    assert t.promise_outcome is None


def test_partial_payment_changes_nothing() -> None:
    assert on_source_update(case(CaseState.AWAITING_REPLY), InvoiceStatus.OPEN, 40_000, NOW) is None


def test_already_closed_case_stays_closed() -> None:
    closed = case(CaseState.CLOSED, close_reason=CloseReason.PAID)
    assert on_source_update(closed, InvoiceStatus.PAID, 0, NOW) is None


def test_invoice_open_again_reopens_the_case() -> None:
    closed = case(CaseState.CLOSED, close_reason=CloseReason.PAID)
    t = on_source_update(closed, InvoiceStatus.OPEN, 100_000, NOW)
    assert t is not None
    assert t.state == CaseState.SCHEDULED
    assert t.next_action_at == NOW


def test_case_closed_by_a_human_is_not_reopened() -> None:
    closed = case(CaseState.CLOSED, close_reason=CloseReason.HUMAN)
    assert on_source_update(closed, InvoiceStatus.OPEN, 100_000, NOW) is None


# --- Other events and helpers --------------------------------------------------------


def test_failed_delivery_hands_the_case_to_a_human() -> None:
    t = on_delivery_failed(case(CaseState.AWAITING_REPLY), "bounced")
    assert t is not None
    assert t.state == CaseState.NEEDS_HUMAN
    assert t.task == TaskKind.ESCALATION
    assert on_delivery_failed(case(CaseState.CLOSED), "bounced") is None


def test_first_reminder_is_due_at_the_start_of_due_date_plus_delay() -> None:
    assert first_reminder_at(date(2026, 1, 1), POLICY, IST) == datetime(2026, 1, 4, tzinfo=IST)


# --- Task resolution -------------------------------------------------------------------


def test_closing_a_task_closes_the_case() -> None:
    t = on_task_resolved(
        case(CaseState.NEEDS_HUMAN), TaskKind.REVIEW_REPLY, TaskAction.CLOSE, POLICY, NOW
    )
    assert t is not None
    assert (t.state, t.close_reason) == (CaseState.CLOSED, CloseReason.HUMAN)


def test_resuming_goes_back_to_scheduled_now() -> None:
    current = case(CaseState.INVESTIGATING, reminders_sent=2)
    t = on_task_resolved(current, TaskKind.REVIEW_DISPUTE, TaskAction.RESUME, POLICY, NOW)
    assert t is not None
    assert (t.state, t.next_action_at, t.grant_extra_reminders) == (CaseState.SCHEDULED, NOW, 0)


def test_resuming_at_the_reminder_limit_allows_one_more_reminder() -> None:
    at_limit = case(CaseState.NEEDS_HUMAN, reminders_sent=POLICY.max_reminders)
    t = on_task_resolved(at_limit, TaskKind.ESCALATION, TaskAction.RESUME, POLICY, NOW)
    assert t is not None and t.grant_extra_reminders == 1

    resumed = CaseView(CaseState.SCHEDULED, POLICY.max_reminders, NOW, 100_000, extra_reminders=1)
    assert on_timer(resumed, POLICY, NOW, ContactNow()).send_reminder


def test_resolving_a_task_on_a_case_that_moved_on_changes_nothing() -> None:
    closed = case(CaseState.CLOSED, close_reason=CloseReason.PAID)
    assert on_task_resolved(closed, TaskKind.ESCALATION, TaskAction.RESUME, POLICY, NOW) is None


def test_approval_actions_do_not_apply_to_case_tasks() -> None:
    with pytest.raises(WorkflowError):
        on_task_resolved(
            case(CaseState.NEEDS_HUMAN), TaskKind.ESCALATION, TaskAction.APPROVE, POLICY, NOW
        )


def test_resuming_a_reply_review_does_not_end_an_investigation() -> None:
    current = case(CaseState.INVESTIGATING, reminders_sent=2)
    assert on_task_resolved(current, TaskKind.REVIEW_REPLY, TaskAction.RESUME, POLICY, NOW) is None
    closed = on_task_resolved(current, TaskKind.REVIEW_REPLY, TaskAction.CLOSE, POLICY, NOW)
    assert closed is not None and closed.state == CaseState.CLOSED
