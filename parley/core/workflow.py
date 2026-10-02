"""The case state machine (spec: "Collections workflow").

Each function takes the current case and an event, and returns a `Transition`
saying what the case should become and what side effects to perform. Nothing
here touches the database; the services layer applies the result.

    Scheduled ──timer──▶ Awaiting reply ──no reply──▶ Scheduled
        │                    │  promise ──▶ Promised ──date passes──▶ Scheduled
        │                    │  dispute / paid claim ──▶ Investigating
        │                    └─ question / wrong contact ──▶ Needs human
        └─ reminder limit ──▶ Needs human
    Any state ──source shows paid, void or removed──▶ Closed
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from parley.core.domain import (
    CaseState,
    CloseReason,
    InvoiceStatus,
    PromiseStatus,
    ReplyIntent,
    TaskAction,
    TaskKind,
)
from parley.core.policy import Policy


class WorkflowError(Exception):
    """An event that cannot happen in the case's current state."""


@dataclass(frozen=True)
class CaseView:
    """The parts of a case the rules need to decide its next step."""

    state: CaseState
    reminders_sent: int
    next_action_at: datetime | None
    amount_due: int
    close_reason: CloseReason | None = None
    extra_reminders: int = 0


@dataclass(frozen=True)
class Promise:
    amount: int
    promised_date: date


@dataclass(frozen=True)
class Transition:
    """What should happen to a case. Only `state` and `next_action_at` are always set."""

    state: CaseState
    next_action_at: datetime | None
    send_reminder: bool = False
    task: TaskKind | None = None
    task_summary: str = ""
    close_reason: CloseReason | None = None
    # Applied to the case's open promise, if it has one.
    promise_outcome: PromiseStatus | None = None
    # Set when a reply is a valid promise to pay.
    new_promise: Promise | None = None
    # Set when a reply disputes the invoice; the text is the customer's reason.
    new_dispute: str | None = None
    # Extra reminders a person allowed beyond the policy limit (when resuming).
    grant_extra_reminders: int = 0


# --- Whether the customer may be contacted right now -------------------------
# The services layer works this out (quiet hours, weekly cap, holds, missing
# email) and passes one of these three values to `on_timer`.


@dataclass(frozen=True)
class ContactNow:
    pass


@dataclass(frozen=True)
class ContactLater:
    at: datetime


@dataclass(frozen=True)
class CannotContact:
    reason: str


Contact = ContactNow | ContactLater | CannotContact


# --- Events ------------------------------------------------------------------


def on_timer(current: CaseView, policy: Policy, now: datetime, contact: Contact) -> Transition:
    """The case's `next_action_at` has been reached."""
    match current.state:
        case CaseState.SCHEDULED:
            if current.reminders_sent >= reminder_limit(current, policy):
                return _needs_human(
                    TaskKind.ESCALATION,
                    f"Still unpaid after {current.reminders_sent} reminders.",
                )
            match contact:
                case CannotContact(reason=reason):
                    return _needs_human(TaskKind.ESCALATION, reason)
                case ContactLater(at=at):
                    return Transition(CaseState.SCHEDULED, next_action_at=at)
                case ContactNow():
                    return Transition(
                        CaseState.AWAITING_REPLY,
                        next_action_at=now + timedelta(days=policy.reminder_gap_days),
                        send_reminder=True,
                    )

        case CaseState.AWAITING_REPLY:
            # No reply within the gap: go back to Scheduled. The next reminder
            # uses the next tone because `reminders_sent` has gone up.
            return Transition(CaseState.SCHEDULED, next_action_at=now)

        case CaseState.PROMISED:
            # The promised date plus grace has passed and the invoice is still open.
            return Transition(
                CaseState.SCHEDULED,
                next_action_at=now,
                promise_outcome=PromiseStatus.BROKEN,
            )

    raise WorkflowError(f"a case in state {current.state} has no timer")


@dataclass(frozen=True)
class Reply:
    """A reply after it has been read. Fields other than `intent` may be missing."""

    intent: ReplyIntent
    promised_date: date | None = None
    promised_amount: int | None = None
    summary: str = ""  # one line for the person who picks up any task


def on_reply(
    current: CaseView, reply: Reply, policy: Policy, now: datetime, tz: ZoneInfo
) -> Transition:
    """The customer replied and the reply has been read."""
    if current.state not in (CaseState.SCHEDULED, CaseState.AWAITING_REPLY, CaseState.PROMISED):
        # A human or the investigator already owns the case: keep it where it
        # is and let a person look at the reply.
        return Transition(
            current.state,
            current.next_action_at,
            task=TaskKind.REVIEW_REPLY,
            task_summary=f"Customer replied while the case is {current.state}.",
        )

    match reply.intent:
        case ReplyIntent.PROMISE:
            return _on_promise(current, reply, policy, now, tz)
        case ReplyIntent.DISPUTE:
            # Outreach to this customer stops until a person reviews the dispute.
            return Transition(
                CaseState.INVESTIGATING,
                next_action_at=None,
                task=TaskKind.REVIEW_DISPUTE,
                task_summary="Customer disputes this invoice.",
                new_dispute=reply.summary or "No reason given.",
            )
        case ReplyIntent.PAID_CLAIM:
            # Until the investigator arrives (M3), a person checks the books and the bank.
            return Transition(
                CaseState.INVESTIGATING,
                next_action_at=None,
                task=TaskKind.VERIFY_PAYMENT,
                task_summary="Customer says this invoice is already paid. Check the bank: "
                "the payment may not be recorded in the books yet.",
            )
        case ReplyIntent.OUT_OF_OFFICE:
            return Transition(
                CaseState.SCHEDULED,
                next_action_at=now + timedelta(days=policy.reminder_gap_days),
            )
        case ReplyIntent.QUESTION | ReplyIntent.WRONG_CONTACT | ReplyIntent.OTHER:
            return _needs_human(
                TaskKind.REVIEW_REPLY,
                f"Customer reply needs a person ({reply.intent}): {reply.summary}",
            )

    raise WorkflowError(f"unknown reply intent {reply.intent}")


def _on_promise(
    current: CaseView, reply: Reply, policy: Policy, now: datetime, tz: ZoneInfo
) -> Transition:
    today = now.astimezone(tz).date()
    promised_date = reply.promised_date
    amount = reply.promised_amount if reply.promised_amount is not None else current.amount_due

    if promised_date is None or promised_date <= today:
        return _needs_human(TaskKind.REVIEW_REPLY, "Promise has no future date.")
    if promised_date > today + timedelta(days=policy.max_promise_window_days):
        return _needs_human(
            TaskKind.REVIEW_REPLY,
            f"Promised date {promised_date} is more than "
            f"{policy.max_promise_window_days} days away.",
        )
    if not 0 < amount <= current.amount_due:
        return _needs_human(TaskKind.REVIEW_REPLY, "Promised amount is not within the amount due.")

    check_on = promised_date + timedelta(days=policy.promise_grace_days + 1)
    return Transition(
        CaseState.PROMISED,
        next_action_at=local_midnight(check_on, tz),
        new_promise=Promise(amount=amount, promised_date=promised_date),
    )


def on_source_update(
    current: CaseView, status: InvoiceStatus, amount_due: int, now: datetime
) -> Transition | None:
    """The source system reported the invoice's latest status and amount.

    Returns None when the case does not change. A change of amount alone needs
    no transition: later messages simply read the new amount.
    """
    still_owed = status == InvoiceStatus.OPEN and amount_due > 0

    if still_owed:
        reopen = current.state == CaseState.CLOSED and current.close_reason != CloseReason.HUMAN
        if reopen:
            return Transition(CaseState.SCHEDULED, next_action_at=now)
        return None

    if current.state == CaseState.CLOSED:
        return None

    if status == InvoiceStatus.OPEN or status == InvoiceStatus.PAID:
        return Transition(
            CaseState.CLOSED,
            next_action_at=None,
            close_reason=CloseReason.PAID,
            promise_outcome=PromiseStatus.KEPT,
        )
    reason = CloseReason.VOID if status == InvoiceStatus.VOID else CloseReason.REMOVED
    return Transition(CaseState.CLOSED, next_action_at=None, close_reason=reason)


def on_delivery_failed(current: CaseView, reason: str) -> Transition | None:
    """A reminder for this case could not be delivered after repeated attempts."""
    if current.state == CaseState.CLOSED:
        return None
    return _needs_human(TaskKind.ESCALATION, reason)


def on_task_resolved(
    current: CaseView, kind: TaskKind, action: TaskAction, policy: Policy, now: datetime
) -> Transition | None:
    """A person resolved a task on this case (not an approve_send task, which acts
    on the message instead). Returns None if the case has already moved on."""
    if current.state not in (CaseState.NEEDS_HUMAN, CaseState.INVESTIGATING):
        return None
    match action:
        case TaskAction.CLOSE:
            return Transition(CaseState.CLOSED, next_action_at=None, close_reason=CloseReason.HUMAN)
        case TaskAction.RESUME:
            # A case resumed at its reminder limit gets one more (final-tone)
            # reminder before it comes back to a person; otherwise it would
            # escalate again at once.
            extra = 1 if current.reminders_sent >= reminder_limit(current, policy) else 0
            return Transition(CaseState.SCHEDULED, next_action_at=now, grant_extra_reminders=extra)
    raise WorkflowError(f"{action} does not apply to a {kind} task")


# --- Helpers -------------------------------------------------------------------


def reminder_limit(current: CaseView, policy: Policy) -> int:
    return policy.max_reminders + current.extra_reminders


def first_reminder_at(due_date: date, policy: Policy, tz: ZoneInfo) -> datetime:
    """When a new case's first reminder becomes due: the start of due date plus delay."""
    return local_midnight(due_date + timedelta(days=policy.first_reminder_delay_days), tz)


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=tz)


def _needs_human(kind: TaskKind, summary: str) -> Transition:
    return Transition(CaseState.NEEDS_HUMAN, next_action_at=None, task=kind, task_summary=summary)
