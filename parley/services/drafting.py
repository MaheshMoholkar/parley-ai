"""The worker step that writes queued reminders (status `drafting`).

For each message: the model drafts it, code checks it (amounts, dates, invoice
numbers, banned phrases), and the small model judges its tone. A draft that
fails is regenerated once. If it fails again, the fixed template is used instead
and a person must approve it. Otherwise the tenant's approval mode decides
whether a person approves it before it goes to the outbox.

Without a model configured, the fixed template written when the reminder was
queued is used as it is.
"""

import logging
import uuid
from dataclasses import dataclass
from functools import partial

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.collections_ai.jobs import (
    DRAFT_PROMPT,
    TONE_PROMPT,
    DraftRequest,
    draft_reminder,
    judge_tone,
)
from parley.core.checks import check_draft
from parley.core.domain import Direction, MessageStatus, TaskKind, TaskStatus
from parley.core.messages import InvoiceLine
from parley.core.money import format_money
from parley.db.models import Customer, Message, MessageCase, Task, Tenant
from parley.ports.model import ModelError, ModelPort
from parley.services.cases import cases_of_message, invoice_line
from parley.services.model_calls import run_job
from parley.services.runtime import Runtime

log = logging.getLogger(__name__)

MAX_DRAFT_ATTEMPTS = 2  # the first draft, plus one regeneration


@dataclass(frozen=True)
class _Draft:
    subject: str
    body: str
    prompt_version: str | None
    problems: list[str]


def draft_queued_messages(rt: Runtime, tenant_id: uuid.UUID) -> int:
    """Draft every queued message of the tenant. Returns how many were drafted.

    Each message is locked while it is drafted (other workers skip it), and its
    result is saved in the same transaction.
    """
    drafted = 0
    tried: set[uuid.UUID] = set()
    while True:
        with rt.session_factory.begin() as session:
            query = (
                select(Message)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.direction == Direction.OUTBOUND,
                    Message.status == MessageStatus.DRAFTING,
                )
                .order_by(Message.created_at, Message.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if tried:
                query = query.where(Message.id.not_in(tried))
            message = session.scalar(query)
            if message is None:
                return drafted
            tried.add(message.id)
            _draft_message(rt, session, message)
            drafted += 1


def _draft_message(rt: Runtime, session: Session, message: Message) -> None:
    tenant = session.get_one(Tenant, message.tenant_id)
    customer = session.get_one(Customer, message.customer_id)
    cases = cases_of_message(session, message.id)
    lines = [invoice_line(case.invoice) for case in cases]
    reminder_number = max(
        (
            n or 1
            for n in session.scalars(
                select(MessageCase.reminder_number).where(MessageCase.message_id == message.id)
            )
        ),
        default=1,
    )
    tone = tenant.policy.tone_for(reminder_number)

    if rt.model is None:
        draft = _Draft(message.subject, message.body, None, [])
        failed_checks = False
    else:
        request = DraftRequest(
            business_name=tenant.name,
            customer_name=customer.name,
            language=customer.language or tenant.default_language,
            tone=tone,
            customer_brief=customer.brief,
            lines=lines,
            payment_link=tenant.payment_link,
        )
        draft = _draft_with_model(rt, rt.model, session, message, request)
        failed_checks = bool(draft.problems)
        if failed_checks:
            # Fall back to the template that was written when the reminder was
            # queued; it always passes the checks.
            log.warning("draft for message %s failed twice: %s", message.id, draft.problems)
        else:
            message.subject = draft.subject
            message.body = draft.body
            message.prompt_version = draft.prompt_version

    total = sum(line.amount_due for line in lines)
    if failed_checks:
        summary = (
            "The drafted reminder failed its checks twice, so the standard template is "
            "shown instead. Problems: " + "; ".join(draft.problems)
        )
        _ask_for_approval(session, message, cases[0].id, summary, rt)
    elif tenant.policy.needs_approval(total):
        summary = f"Approve reminder to {customer.name}: {_describe(lines)}."
        _ask_for_approval(session, message, cases[0].id, summary, rt)
    else:
        message.status = MessageStatus.PENDING


def _draft_with_model(
    rt: Runtime, model: ModelPort, session: Session, message: Message, request: DraftRequest
) -> _Draft:
    problems: list[str] = []
    for _ in range(MAX_DRAFT_ATTEMPTS):
        try:
            completion = run_job(
                rt,
                session,
                message.tenant_id,
                message.id,
                DRAFT_PROMPT,
                "large",
                partial(draft_reminder, model, request),
            )
            subject, body = completion.output.subject.strip(), completion.output.body.strip()
            problems = check_draft(subject, body, request.lines)
            if not problems:
                verdict = run_job(
                    rt,
                    session,
                    message.tenant_id,
                    message.id,
                    TONE_PROMPT,
                    "small",
                    partial(judge_tone, model, request.tone, subject, body),
                ).output
                if not verdict.passed:
                    problems = [f"tone judge: {verdict.reason}"]
        except ModelError as exc:
            problems = [f"model error: {exc}"]
        if not problems:
            return _Draft(subject, body, DRAFT_PROMPT, [])
    return _Draft("", "", None, problems)


def _ask_for_approval(
    session: Session, message: Message, case_id: uuid.UUID, summary: str, rt: Runtime
) -> None:
    message.status = MessageStatus.AWAITING_APPROVAL
    session.add(
        Task(
            tenant_id=message.tenant_id,
            case_id=case_id,
            message_id=message.id,
            kind=TaskKind.APPROVE_SEND,
            summary=summary,
            status=TaskStatus.OPEN,
            created_at=rt.clock.now(),
        )
    )


def _describe(lines: list[InvoiceLine]) -> str:
    return ", ".join(
        f"{line.number} ({format_money(line.amount_due, line.currency)})" for line in lines
    )
