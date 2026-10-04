"""A person resolving a task (spec: `POST /v1/tasks/{id}/resolve`).

approve_send tasks act on the waiting message:
  approve  send it as it is
  edit     send an edited subject and body
  reject   do not send it; the case carries on as if this reminder was skipped
Approved and edited messages must still pass the output checks.

All other tasks act on the case:
  resume   go back to chasing (one more reminder if the case was at its limit)
  close    stop chasing this invoice
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.core.checks import check_draft
from parley.core.domain import (
    DisputeStatus,
    MessageStatus,
    TaskAction,
    TaskKind,
    TaskStatus,
)
from parley.core.workflow import WorkflowError, on_task_resolved
from parley.db.models import Case, Dispute, Message, Task, Tenant
from parley.services.cases import (
    NotFoundError,
    apply_transition,
    cases_of_message,
    invoice_line,
    record_amounts,
)
from parley.services.sendable import stale_reason


class TaskError(ValueError):
    """The action is not allowed, with the reasons."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def resolve_task(
    session: Session,
    tenant: Tenant,
    task_id: uuid.UUID,
    action: TaskAction,
    now: datetime,
    subject: str | None = None,
    body: str | None = None,
    note: str = "",
) -> Task:
    task = session.scalar(
        select(Task).where(Task.id == task_id, Task.tenant_id == tenant.id).with_for_update()
    )
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    if task.status != TaskStatus.OPEN:
        raise TaskError([f"task is already {task.status}"])

    if task.kind == TaskKind.APPROVE_SEND:
        _resolve_approval(session, task, action, subject, body)
    else:
        _resolve_case_task(session, tenant, task, action, now)

    task.status = TaskStatus.RESOLVED
    task.resolution = f"{action}: {note}".strip().rstrip(":")
    task.resolved_at = now
    return task


def _resolve_approval(
    session: Session, task: Task, action: TaskAction, subject: str | None, body: str | None
) -> None:
    if task.message_id is None:
        raise TaskError(["approval task has no message"])
    message = session.get_one(Message, task.message_id, with_for_update=True)
    if message.status != MessageStatus.AWAITING_APPROVAL:
        raise TaskError([f"message is {message.status}, not awaiting approval"])

    match action:
        case TaskAction.REJECT:
            message.status = MessageStatus.REJECTED
            return
        case TaskAction.EDIT:
            if not subject or not body:
                raise TaskError(["edit needs a subject and a body"])
            message.subject, message.body = subject.strip(), body.strip()
            message.prompt_version = None  # written by a person
        case TaskAction.APPROVE:
            pass
        case _:
            raise TaskError([f"{action} does not apply to an approval task"])

    cases = cases_of_message(session, message.id)
    problems = check_draft(message.subject, message.body, [invoice_line(c.invoice) for c in cases])
    if problems:
        raise TaskError(problems)
    reason = stale_reason(session, message)
    if reason is not None:
        # The worker withdraws it on its next round; the reviewer is told why now.
        raise TaskError([f"this reminder should no longer be sent: {reason}"])
    record_amounts(session, message.id, cases)
    message.status = MessageStatus.PENDING


def _resolve_case_task(
    session: Session, tenant: Tenant, task: Task, action: TaskAction, now: datetime
) -> None:
    case = session.get_one(Case, task.case_id, with_for_update=True)
    try:
        transition = on_task_resolved(case.view(), task.kind, action, tenant.policy, now)
    except WorkflowError as exc:
        raise TaskError([str(exc)]) from None
    if transition is not None:
        apply_transition(session, case, transition, now)

    if task.kind == TaskKind.REVIEW_DISPUTE:
        open_disputes = session.scalars(
            select(Dispute).where(Dispute.case_id == case.id, Dispute.status == DisputeStatus.OPEN)
        )
        for dispute in open_disputes:
            dispute.status = DisputeStatus.RESOLVED
            dispute.resolved_at = now
