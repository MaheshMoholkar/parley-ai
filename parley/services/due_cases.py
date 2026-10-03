"""The worker step that acts on due cases: sends reminders, escalates, reschedules.

Work is done one customer at a time. The customer row is locked with
`FOR UPDATE SKIP LOCKED`, so when several workers run, each takes a different
customer and no customer gets two messages from a race. All of a customer's due
reminders go out as one message, written to the outbox in the same transaction
as the case changes.
"""

import logging
import secrets
import uuid
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.core.domain import (
    TIMED_STATES,
    UNSENT_STATUSES,
    CaseState,
    Direction,
    MessageStatus,
    TaskKind,
    TaskStatus,
)
from parley.core.messages import InvoiceLine, reminder_message
from parley.core.policy import CONTACT_WINDOW, contact_allowed_at
from parley.core.workflow import CannotContact, Contact, ContactLater, ContactNow, on_timer
from parley.db.models import Case, Customer, Message, MessageCase, Task, Tenant
from parley.services.calls import VOICE, callable_number
from parley.services.cases import apply_transition
from parley.services.runtime import Runtime

log = logging.getLogger(__name__)

# Messages that count toward the weekly contact cap: sent, or on their way.
COUNTS_AS_CONTACT = UNSENT_STATUSES | {MessageStatus.SENT}

# While a customer is on hold (a dispute or payment claim is being looked at),
# their due cases are checked again after this long.
HOLD_RECHECK = timedelta(days=1)

# Defensive limit: processing always moves a case's timer forward, so a customer
# should never come back more than a couple of times in one run.
MAX_ROUNDS_PER_CUSTOMER = 10


def run_due_cases(rt: Runtime, tenant_id: uuid.UUID) -> int:
    """Process every customer of the tenant that has a due case. Returns how many
    customer rounds were processed."""
    now = rt.clock.now()
    rounds: Counter[uuid.UUID] = Counter()
    skip: set[uuid.UUID] = set()

    while True:
        customer_id: uuid.UUID | None = None
        try:
            with rt.session_factory.begin() as session:
                customer = _lock_next_due_customer(session, tenant_id, now, skip)
                if customer is None:
                    break
                customer_id = customer.id
                tenant = session.get_one(Tenant, tenant_id)
                call_number = callable_number(rt, customer.phone)
                _process_customer(session, tenant, customer, now, call_number)
        except Exception:
            # One customer's failure must not stop the others. The transaction
            # was rolled back, so nothing half-done was saved; the next run retries.
            if customer_id is None:
                raise
            log.exception("failed to process due cases for customer %s", customer_id)
            skip.add(customer_id)
            continue

        rounds[customer_id] += 1
        if rounds[customer_id] >= MAX_ROUNDS_PER_CUSTOMER:
            log.error(
                "customer %s still has due cases after %d rounds",
                customer_id,
                MAX_ROUNDS_PER_CUSTOMER,
            )
            skip.add(customer_id)

    return sum(rounds.values())


def _lock_next_due_customer(
    session: Session, tenant_id: uuid.UUID, now: datetime, skip: set[uuid.UUID]
) -> Customer | None:
    has_due_case = (
        select(Case.id)
        .where(
            Case.customer_id == Customer.id,
            Case.paused.is_(False),
            Case.state.in_(TIMED_STATES),
            Case.next_action_at <= now,
        )
        .exists()
    )
    query = (
        select(Customer)
        .where(Customer.tenant_id == tenant_id, Customer.paused.is_(False), has_due_case)
        .order_by(Customer.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if skip:
        query = query.where(Customer.id.not_in(skip))
    return session.scalar(query)


def _process_customer(
    session: Session, tenant: Tenant, customer: Customer, now: datetime, call_number: str | None
) -> None:
    """`call_number` is the customer's number if they may be phoned, else None."""
    policy = tenant.policy
    due_cases = session.scalars(
        select(Case)
        .where(
            Case.customer_id == customer.id,
            Case.paused.is_(False),
            Case.state.in_(TIMED_STATES),
            Case.next_action_at <= now,
        )
        .order_by(Case.opened_at, Case.id)
        .with_for_update()
    ).all()

    contact = _contact_decision(session, tenant, customer, now, call_number)
    to_remind: list[Case] = []
    pending = list(due_cases)
    # A case can become due again in the same pass: Awaiting reply times out to
    # Scheduled "now". Re-check those before sending, so it joins this message
    # instead of going out alone. Two rounds always suffice; three is a guard.
    for _ in range(3):
        due_again = []
        for case in pending:
            transition = on_timer(case.view(), policy, now, contact)
            apply_transition(session, case, transition, now)
            if transition.send_reminder:
                to_remind.append(case)
            elif case.state in TIMED_STATES and case.next_action_at and case.next_action_at <= now:
                due_again.append(case)
        pending = due_again

    if to_remind:
        _queue_reminder(session, tenant, customer, to_remind, now, call_number)


def _contact_decision(
    session: Session, tenant: Tenant, customer: Customer, now: datetime, call_number: str | None
) -> Contact:
    """May this customer be sent a message right now?"""
    if not customer.email and call_number is None:
        return CannotContact("Customer has no email address or phone number we may call.")
    if _is_on_hold(session, customer) or _has_unsent_message(session, customer):
        return ContactLater(now + HOLD_RECHECK)

    recent = session.scalars(
        select(Message.created_at).where(
            Message.customer_id == customer.id,
            Message.direction == Direction.OUTBOUND,
            Message.status.in_(COUNTS_AS_CONTACT),
            Message.created_at > now - CONTACT_WINDOW,
        )
    ).all()
    allowed_at = contact_allowed_at(now, tenant.zone, tenant.policy, recent)
    return ContactNow() if allowed_at <= now else ContactLater(allowed_at)


def _has_unsent_message(session: Session, customer: Customer) -> bool:
    """One reminder at a time: wait while the last one is still being drafted,
    awaits approval, or waits for delivery."""
    unsent = select(Message.id).where(
        Message.customer_id == customer.id,
        Message.direction == Direction.OUTBOUND,
        Message.status.in_(UNSENT_STATUSES),
    )
    return bool(session.scalar(select(unsent.exists())))


def _is_on_hold(session: Session, customer: Customer) -> bool:
    """Spec: while any case of a customer is Investigating, or waits on a human
    dispute review, no reminder goes to that customer."""
    investigating = select(Case.id).where(
        Case.customer_id == customer.id, Case.state == CaseState.INVESTIGATING
    )
    dispute_review = (
        select(Task.id)
        .join(Case, Case.id == Task.case_id)
        .where(
            Case.customer_id == customer.id,
            Task.kind == TaskKind.REVIEW_DISPUTE,
            Task.status == TaskStatus.OPEN,
        )
    )
    return bool(session.scalar(select(investigating.exists() | dispute_review.exists())))


def _queue_reminder(
    session: Session,
    tenant: Tenant,
    customer: Customer,
    cases: list[Case],
    now: datetime,
    call_number: str | None,
) -> None:
    """Queue one outbound message covering all `cases`, in status `drafting`.

    The fixed template is written now as a safe fallback. The drafting step then
    replaces it with a model draft (if a model is configured) and decides whether
    a person must approve it; delivery sends it after that. Keeping model calls
    out of this transaction means the customer lock is held only briefly.

    From the tenant's `call_from_reminder` on, the reminder is a phone call when
    the customer may be called (or always, if there is no email address). The
    text then serves as the call's brief on the review screen."""
    for case in cases:
        case.reminders_sent += 1
    reminder_number = max(case.reminders_sent for case in cases)
    call = call_number is not None and (
        tenant.policy.prefers_call(reminder_number) or not customer.email
    )

    lines = [
        InvoiceLine(
            number=case.invoice.number,
            amount_due=case.invoice.amount_due,
            currency=case.invoice.currency,
            due_date=case.invoice.due_date,
            display_details=case.invoice.display_details,
        )
        for case in cases
    ]
    tone = tenant.policy.tone_for(reminder_number)
    subject, body = reminder_message(customer.name, tenant.name, lines, tone)
    if call:
        subject = f"Call: {subject}"
    address = call_number if call else customer.email
    assert address is not None  # _contact_decision checked there is a way to reach them

    message = Message(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        customer_id=customer.id,
        channel=VOICE if call else "email",
        direction=Direction.OUTBOUND,
        to_address=address,
        subject=subject,
        body=body,
        status=MessageStatus.DRAFTING,
        idempotency_key=uuid.uuid4().hex,
        reply_token=secrets.token_hex(16),
        attempts=0,
        created_at=now,
    )
    session.add(message)
    for case in cases:
        session.add(
            MessageCase(
                message_id=message.id,
                case_id=case.id,
                tenant_id=tenant.id,
                reminder_number=case.reminders_sent,
            )
        )
