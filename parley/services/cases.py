"""Opening cases, applying workflow transitions, and pausing."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from parley.core.domain import (
    AgentRunStatus,
    CaseState,
    DisputeStatus,
    EventType,
    InvoiceStatus,
    PromiseStatus,
    TaskKind,
    TaskStatus,
)
from parley.core.messages import InvoiceLine
from parley.core.workflow import Transition, first_reminder_at
from parley.db.models import (
    AgentRun,
    Case,
    Customer,
    Dispute,
    Invoice,
    MessageCase,
    Promise,
    Task,
    Tenant,
)
from parley.services.webhooks import emit


class NotFoundError(LookupError):
    pass


def apply_transition(
    session: Session,
    case: Case,
    transition: Transition,
    now: datetime,
    source_message_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = None,
) -> None:
    """Save a workflow decision onto a case. Sending a reminder is left to the caller,
    because one message can cover several cases. `source_message_id` is the reply
    that caused the change, recorded on any promise it creates; `agent_run_id` is
    the investigation behind any task it creates."""
    reopening = case.state == CaseState.CLOSED and transition.state != CaseState.CLOSED
    closing = case.state != CaseState.CLOSED and transition.state == CaseState.CLOSED
    about = {"case_id": case.id, "customer_id": case.customer_id}

    case.state = transition.state
    case.next_action_at = transition.next_action_at
    case.extra_reminders += transition.grant_extra_reminders

    if transition.state == CaseState.CLOSED:
        case.closed_at = now
        case.closed_reason = transition.close_reason
        _cancel_open_tasks(session, case, now)
        if closing:
            emit(
                session,
                case.tenant_id,
                EventType.CASE_CLOSED,
                {**about, "reason": case.closed_reason},
                now,
            )
    elif reopening:
        case.closed_at = None
        case.closed_reason = None

    if transition.task is not None:
        task_id = uuid.uuid4()
        session.add(
            Task(
                id=task_id,
                tenant_id=case.tenant_id,
                case_id=case.id,
                kind=transition.task,
                summary=transition.task_summary,
                status=TaskStatus.OPEN,
                agent_run_id=agent_run_id,
                created_at=now,
            )
        )
        task_data = {**about, "task_id": task_id, "kind": transition.task}
        emit(session, case.tenant_id, EventType.TASK_CREATED, task_data, now)

    if transition.promise_outcome is not None:
        open_promises = session.scalars(
            select(Promise).where(Promise.case_id == case.id, Promise.status == PromiseStatus.OPEN)
        )
        for promise in open_promises:
            promise.status = transition.promise_outcome
            if promise.status == PromiseStatus.BROKEN:
                broken = {**about, "promise_id": promise.id, "promised_date": promise.promised_date}
                emit(session, case.tenant_id, EventType.PROMISE_BROKEN, broken, now)

    if transition.new_dispute is not None:
        dispute_id = uuid.uuid4()
        session.add(
            Dispute(
                id=dispute_id,
                tenant_id=case.tenant_id,
                case_id=case.id,
                reason=transition.new_dispute,
                status=DisputeStatus.OPEN,
                created_at=now,
            )
        )
        dispute = {**about, "dispute_id": dispute_id, "reason": transition.new_dispute}
        emit(session, case.tenant_id, EventType.DISPUTE_OPENED, dispute, now)

    if transition.investigate is not None:
        session.add(
            AgentRun(
                tenant_id=case.tenant_id,
                case_id=case.id,
                message_id=source_message_id,
                claim=transition.investigate,
                status=AgentRunStatus.QUEUED,
                steps=[],
                created_at=now,
            )
        )

    if transition.new_promise is not None:
        promise_id = uuid.uuid4()
        session.add(
            Promise(
                id=promise_id,
                tenant_id=case.tenant_id,
                case_id=case.id,
                amount=transition.new_promise.amount,
                promised_date=transition.new_promise.promised_date,
                status=PromiseStatus.OPEN,
                source_message_id=source_message_id,
            )
        )
        promised = {
            **about,
            "promise_id": promise_id,
            "promised_date": transition.new_promise.promised_date,
            "amount": transition.new_promise.amount,
        }
        emit(session, case.tenant_id, EventType.PROMISE_CREATED, promised, now)


def _cancel_open_tasks(session: Session, case: Case, now: datetime) -> None:
    open_tasks = session.scalars(
        select(Task).where(Task.case_id == case.id, Task.status == TaskStatus.OPEN)
    )
    for task in open_tasks:
        task.status = TaskStatus.CANCELLED
        task.resolution = f"Case closed: {case.closed_reason}"
        task.resolved_at = now


def open_overdue_cases(session: Session, tenant: Tenant, now: datetime) -> int:
    """Open a Scheduled case for every open invoice past its due date that has none.

    Safe to run from several workers at once: the unique index on `invoice_id`
    makes a second insert for the same invoice do nothing.
    """
    today = now.astimezone(tenant.zone).date()
    has_case = select(Case.id).where(Case.invoice_id == Invoice.id).exists()
    overdue = session.scalars(
        select(Invoice).where(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.OPEN,
            Invoice.amount_due > 0,
            Invoice.due_date < today,
            ~has_case,
        )
    ).all()

    opened = 0
    policy = tenant.policy
    for invoice in overdue:
        statement = (
            insert(Case)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                invoice_id=invoice.id,
                customer_id=invoice.customer_id,
                state=CaseState.SCHEDULED,
                paused=False,
                reminders_sent=0,
                next_action_at=first_reminder_at(invoice.due_date, policy, tenant.zone),
                opened_at=now,
            )
            .on_conflict_do_nothing(index_elements=[Case.invoice_id])
            .returning(Case.id)
        )
        case_id = session.execute(statement).scalar_one_or_none()
        if case_id is not None:
            opened += 1
            opened_data = {
                "case_id": case_id,
                "customer_id": invoice.customer_id,
                "invoice_id": invoice.id,
                "invoice_number": invoice.number,
                "amount_due": invoice.amount_due,
                "currency": invoice.currency,
                "due_date": invoice.due_date,
            }
            emit(session, tenant.id, EventType.CASE_OPENED, opened_data, now)
    return opened


def customer_on_hold(session: Session, customer_id: uuid.UUID) -> bool:
    """Spec: while any case of a customer is Investigating, or waits on a human
    dispute review, no reminder goes to that customer."""
    investigating = select(Case.id).where(
        Case.customer_id == customer_id, Case.state == CaseState.INVESTIGATING
    )
    dispute_review = (
        select(Task.id)
        .join(Case, Case.id == Task.case_id)
        .where(
            Case.customer_id == customer_id,
            Task.kind == TaskKind.REVIEW_DISPUTE,
            Task.status == TaskStatus.OPEN,
        )
    )
    return bool(session.scalar(select(investigating.exists() | dispute_review.exists())))


# --- Pause and resume ------------------------------------------------------------
# Pause is a flag, not a state: the case keeps its state and timer, and the
# worker skips it until it is resumed.


def set_case_paused(
    session: Session, tenant_id: uuid.UUID, case_id: uuid.UUID, paused: bool
) -> Case:
    case = session.scalar(
        select(Case).where(Case.id == case_id, Case.tenant_id == tenant_id).with_for_update()
    )
    if case is None:
        raise NotFoundError(f"case {case_id} not found")
    case.paused = paused
    return case


def set_customer_paused(
    session: Session, tenant_id: uuid.UUID, customer_id: uuid.UUID, paused: bool
) -> Customer:
    customer = session.scalar(
        select(Customer)
        .where(Customer.id == customer_id, Customer.tenant_id == tenant_id)
        .with_for_update()
    )
    if customer is None:
        raise NotFoundError(f"customer {customer_id} not found")
    customer.paused = paused
    return customer


# --- Helpers shared by the message steps ---------------------------------------------


def cases_of_message(session: Session, message_id: uuid.UUID, lock: bool = False) -> list[Case]:
    query = (
        select(Case)
        .join(MessageCase, MessageCase.case_id == Case.id)
        .where(MessageCase.message_id == message_id)
        .order_by(Case.opened_at, Case.id)
    )
    if lock:
        query = query.with_for_update(of=Case)
    return list(session.scalars(query))


def record_amounts(session: Session, message_id: uuid.UUID, cases: list[Case]) -> None:
    """Note each invoice's amount due on the message link, as the text is finalised."""
    amounts = {case.id: case.invoice.amount_due for case in cases}
    links = session.scalars(select(MessageCase).where(MessageCase.message_id == message_id))
    for link in links:
        link.amount_due = amounts.get(link.case_id, link.amount_due)


def trusted_text(*texts: str | None) -> list[str]:
    """Text from the books a draft may quote as it is (see check_draft)."""
    return [text for text in texts if text]


def invoice_line(invoice: Invoice) -> InvoiceLine:
    return InvoiceLine(
        number=invoice.number,
        amount_due=invoice.amount_due,
        currency=invoice.currency,
        due_date=invoice.due_date,
        display_details=invoice.display_details,
    )
