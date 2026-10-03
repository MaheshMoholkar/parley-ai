"""The worker step that reads received replies and acts on them.

The small model reads each reply. If it is unsure, the large model reads it
again; if that is still unsure, a person takes it. The model only proposes what
the reply says: code checks every field (dates, amounts, invoice numbers), and
the state machine decides what happens. No reply can edit an invoice, record a
payment or change a setting, whatever it says.

Without a model configured, every reply goes to a person.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date
from functools import partial

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.collections_ai.jobs import (
    BRIEF_PROMPT,
    READ_PROMPT,
    ReplyReading,
    read_reply,
    update_brief,
)
from parley.core.checks import check_brief
from parley.core.domain import CaseState, Direction, MessageStatus, ReplyIntent
from parley.core.money import MoneyError, to_minor_units
from parley.core.workflow import Reply, on_reply
from parley.db.models import Case, Customer, Message, Tenant
from parley.ports.model import ModelError, ModelPort
from parley.services.cases import apply_transition, cases_of_message
from parley.services.model_calls import run_job
from parley.services.runtime import Runtime

log = logging.getLogger(__name__)

# Below this, a reading is not trusted: re-read with the large model, then a person.
MIN_CONFIDENCE = 0.75


def read_received_replies(rt: Runtime, tenant_id: uuid.UUID) -> int:
    """Read every received reply of the tenant. Returns how many were read."""
    done = 0
    tried: set[uuid.UUID] = set()
    while True:
        with rt.session_factory.begin() as session:
            query = (
                select(Message)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.direction == Direction.INBOUND,
                    Message.status == MessageStatus.RECEIVED,
                )
                .order_by(Message.created_at, Message.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if tried:
                query = query.where(Message.id.not_in(tried))
            message = session.scalar(query)
            if message is None:
                return done
            tried.add(message.id)
            _handle_reply(rt, session, message)
            done += 1


def _handle_reply(rt: Runtime, session: Session, message: Message) -> None:
    now = rt.clock.now()
    tenant = session.get_one(Tenant, message.tenant_id)
    customer = session.get_one(Customer, message.customer_id)
    cases = [
        c for c in cases_of_message(session, message.id, lock=True) if c.state != CaseState.CLOSED
    ]

    reply = _understand(rt, session, message, cases, now.astimezone(tenant.zone).date())
    if reply.language:
        customer.language = reply.language

    for case, case_reply in replies_per_case(reply, cases):
        transition = on_reply(case.view(), case_reply, tenant.policy, now, tenant.zone)
        apply_transition(session, case, transition, now, source_message_id=message.id)

    if rt.model is not None:
        _update_brief(rt, rt.model, session, message, customer, reply)

    message.status = MessageStatus.READ
    message.prompt_version = READ_PROMPT if rt.model is not None else None


@dataclass(frozen=True)
class Understood:
    """What a reply says, after code has checked the model's reading."""

    intent: ReplyIntent
    summary: str
    promised_date: date | None = None
    promised_amount: int | None = None
    invoice_numbers: frozenset[str] = frozenset()
    language: str | None = None


def _understand(
    rt: Runtime, session: Session, message: Message, cases: list[Case], today: date
) -> Understood:
    if rt.model is None:
        return Understood(ReplyIntent.OTHER, "No model configured; please read the reply.")
    numbers = [case.invoice.number for case in cases]
    reading = _read(rt, rt.model, session, message, numbers, today)
    if reading is None:
        return Understood(ReplyIntent.OTHER, "The reply could not be read automatically.")
    if reading.confidence < MIN_CONFIDENCE:
        return Understood(
            ReplyIntent.OTHER, f"Unclear reply: {reading.summary}", language=reading.language
        )

    # Check the proposed fields against the cases; never trust them as given.
    known = set(numbers)
    named = frozenset(n for n in reading.invoice_numbers if n in known)
    amount = None
    if reading.promised_amount:
        currency = cases[0].invoice.currency if cases else "INR"
        try:
            amount = to_minor_units(reading.promised_amount, currency)
        except MoneyError:
            return Understood(
                ReplyIntent.OTHER,
                f"Promise with an unreadable amount: {reading.summary}",
                language=reading.language,
            )
    return Understood(
        reading.intent,
        reading.summary,
        promised_date=reading.promised_date,
        promised_amount=amount,
        invoice_numbers=named,
        language=reading.language,
    )


def _read(
    rt: Runtime,
    model: ModelPort,
    session: Session,
    message: Message,
    numbers: list[str],
    today: date,
) -> ReplyReading | None:
    """Small model first; the large model only if the small one is unsure."""
    reading = None
    for tier in ("small", "large"):
        try:
            reading = run_job(
                rt,
                session,
                message.tenant_id,
                message.id,
                READ_PROMPT,
                tier,
                partial(read_reply, model, tier, today, numbers, message.body),
            ).output
        except ModelError as exc:
            log.warning("reading reply %s with the %s model failed: %s", message.id, tier, exc)
            continue
        if reading.confidence >= MIN_CONFIDENCE:
            return reading
    return reading


def _update_brief(
    rt: Runtime,
    model: ModelPort,
    session: Session,
    message: Message,
    customer: Customer,
    reply: Understood,
) -> None:
    """Refresh the customer brief. A failed or invalid update keeps the old brief:
    the brief is a convenience, never a reason to stop handling the reply."""
    try:
        new_brief = run_job(
            rt,
            session,
            message.tenant_id,
            message.id,
            BRIEF_PROMPT,
            "small",
            partial(
                update_brief,
                model,
                customer.brief,
                message.body,
                f"{reply.intent}: {reply.summary}",
            ),
        ).output.brief.strip()
    except ModelError as exc:
        log.warning("brief update for customer %s failed: %s", customer.id, exc)
        return
    problems = check_brief(new_brief)
    if problems:
        log.warning("brief update for customer %s rejected: %s", customer.id, problems)
        return
    customer.brief = new_brief


def replies_per_case(reply: Understood, cases: list[Case]) -> list[tuple[Case, Reply]]:
    """Decide which cases the reply is about, and what it says to each."""
    targets = [c for c in cases if c.invoice.number in reply.invoice_numbers] or cases

    if (
        reply.intent == ReplyIntent.PROMISE
        and reply.promised_amount is not None
        and len(targets) > 1
    ):
        total = sum(c.invoice.amount_due for c in targets)
        if reply.promised_amount != total:
            # A part payment across several invoices: a person decides how it splits.
            summary = f"Promise of part of the total across several invoices: {reply.summary}"
            return [(c, Reply(ReplyIntent.OTHER, summary=summary)) for c in targets]
        return [(c, _reply(reply, amount=None)) for c in targets]  # full amount of each

    return [(c, _reply(reply, amount=reply.promised_amount)) for c in targets]


def _reply(reply: Understood, amount: int | None) -> Reply:
    return Reply(
        intent=reply.intent,
        promised_date=reply.promised_date,
        promised_amount=amount,
        summary=reply.summary,
    )
