"""Phone calls: their records, the agent's tools, and what happens when a call ends.

A reminder call is an outbound message on the "voice" channel. The delivery
step dials it (`place_reminder_call`), the voice bridge carries the
conversation (`parley/services/voice_bridge.py`), and this module does
everything that touches the database:

- `answered`       the provider says who picked up; a machine gets no conversation
- `start_call`     the bridge is about to connect the speech model
- `record_turn`    a finished line of the transcript
- `run_tool`       one tool request from the agent, checked by code (below)
- `finish_call`    the call is over: workflow, audit, transcript

The agent's tools only propose: a promise or dispute goes through the same
checks and state machine as one read from an email (`on_reply`), and no tool
can edit an invoice, record a payment or change a setting. Amounts are given
out only after `confirm_identity`.
"""

import json
import logging
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.collections_ai.voice import voice_system_prompt
from parley.core.domain import (
    CallAudit,
    CallStatus,
    CaseState,
    Direction,
    MessageStatus,
    ReplyIntent,
)
from parley.core.money import MoneyError, format_money, to_minor_units
from parley.core.phone import to_e164
from parley.core.voice import Turn, audit_call
from parley.core.workflow import (
    on_call_audit_failed,
    on_call_not_reached,
    on_reply,
    on_transfer_requested,
)
from parley.db.models import Call, Case, Customer, Message, MessageCase, Promise, Tenant
from parley.ports.voice import VoiceError
from parley.services.cases import apply_transition, cases_of_message, invoice_line
from parley.services.replies import Understood, replies_per_case
from parley.services.runtime import Runtime
from parley.services.tracing import annotate, step

log = logging.getLogger(__name__)

VOICE = "voice"
# A call the provider never reported back on is closed after this long.
STALE_AFTER = timedelta(minutes=15)


# --- Who may be called ----------------------------------------------------------


def callable_number(rt: Runtime, phone: str | None) -> str | None:
    """The customer's number in E.164 form, if calls are set up and this number
    may be called. Demo deployments list the numbers that agreed to be called
    (PARLEY_VOICE_ALLOWED_NUMBERS); "*" allows any number."""
    if rt.voice is None or rt.speech is None:
        return None
    number = to_e164(phone)
    if number is None:
        return None
    if "*" not in rt.voice_allowed_numbers and number not in rt.voice_allowed_numbers:
        return None
    return number


# --- Placing a reminder call ----------------------------------------------------


def place_reminder_call(rt: Runtime, session: Session, message: Message) -> Call:
    """Dial the customer for a pending voice message. Raises VoiceError."""
    if rt.voice is None:
        raise VoiceError("calls are not configured", retryable=False)
    number = callable_number(rt, message.to_address)
    if number is None:
        raise VoiceError(f"{message.to_address} may not be called", retryable=False)
    call = session.scalar(select(Call).where(Call.message_id == message.id))
    if call is None:
        call = _new_call(session, message, number, rt.clock.now(), test=False)
    call.provider_call_id = rt.voice.place_call(number, call.token)
    return call


def create_test_call(rt: Runtime, session: Session, tenant: Tenant, case_id: uuid.UUID) -> Call:
    """A call from the browser page, about one case, with the caller playing the
    customer. It acts on the real case: a promise made on it counts."""
    case = session.scalar(
        select(Case).where(Case.id == case_id, Case.tenant_id == tenant.id).with_for_update()
    )
    if case is None:
        raise LookupError(f"case {case_id} not found")
    if case.state == CaseState.CLOSED:
        raise ValueError("the case is closed")
    now = rt.clock.now()
    message = Message(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        customer_id=case.customer_id,
        channel=VOICE,
        direction=Direction.OUTBOUND,
        to_address="browser",
        subject="Test call from the browser",
        body="",
        status=MessageStatus.SENT,
        idempotency_key=uuid.uuid4().hex,
        attempts=1,
        created_at=now,
        sent_at=now,
    )
    session.add(message)
    session.add(MessageCase(message_id=message.id, case_id=case.id, tenant_id=tenant.id))
    session.flush()
    call = _new_call(session, message, "browser", now, test=True)
    call.status = CallStatus.IN_PROGRESS
    call.answered_by = "human"
    call.answered_at = now
    return call


def _new_call(session: Session, message: Message, number: str, now: datetime, test: bool) -> Call:
    call = Call(
        id=uuid.uuid4(),
        tenant_id=message.tenant_id,
        message_id=message.id,
        customer_id=message.customer_id,
        token=secrets.token_urlsafe(24),
        to_number=number,
        test=test,
        status=CallStatus.PLACED,
        identity_confirmed=False,
        turns=[],
        audit_problems=[],
        created_at=now,
    )
    session.add(call)
    return call


def find_call(session: Session, token: str) -> Call | None:
    if not token:
        return None
    return session.scalar(select(Call).where(Call.token == token))


# --- During the call --------------------------------------------------------------


def answered(rt: Runtime, token: str, answered_by: str) -> bool:
    """The provider reports who picked up. Returns True if the agent should talk.
    An answering machine (or fax) gets no message: the call ends and the
    workflow tries again later."""
    with rt.session_factory.begin() as session:
        call = _lock_call(session, token)
        if call is None or call.status != CallStatus.PLACED:
            return False
        call.answered_by = answered_by
        if answered_by.startswith("machine") or answered_by == "fax":
            _finish(rt, session, call, reached=False)
            return False
        call.status = CallStatus.IN_PROGRESS
        call.answered_at = rt.clock.now()
        return True


@dataclass(frozen=True)
class CallStart:
    call_id: uuid.UUID
    system_prompt: str
    language: str


def start_call(rt: Runtime, token: str, test: bool) -> CallStart | None:
    """The audio is connected; returns what the speech model needs, or None if
    this call should not be talking: an unknown token, a call that ended or
    already has its audio connected, or a phone call's token used on the browser
    page (or the other way round). So a token that leaks (it appears in URLs
    and so in access logs) cannot be used to open a second conversation."""
    with rt.session_factory.begin() as session:
        call = _lock_call(session, token)
        if call is None or call.status != CallStatus.IN_PROGRESS:
            return None
        if call.test != test or call.connected_at is not None:
            return None
        call.connected_at = rt.clock.now()
        tenant = session.get_one(Tenant, call.tenant_id)
        customer = session.get_one(Customer, call.customer_id)
        language = customer.language or tenant.default_language
        prompt = voice_system_prompt(tenant.name, customer.name, language)
        return CallStart(call.id, prompt, language)


def record_turn(rt: Runtime, call_id: uuid.UUID, role: str, text: str) -> None:
    with rt.session_factory.begin() as session:
        call = session.get_one(Call, call_id, with_for_update=True)
        call.turns = [*call.turns, _turn(rt, role, text)]


def run_tool(
    rt: Runtime, call_id: uuid.UUID, name: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Run one tool the agent asked for and return what the agent is told.
    Every result has "ok"; a refused request says why, so the agent can tell
    the customer a colleague will follow up."""
    with (
        step("call_tool", call_id=call_id, tool=name) as span,
        rt.session_factory.begin() as session,
    ):
        call = session.get_one(Call, call_id, with_for_update=True)
        tool = _TOOLS.get(name)
        if tool is None:
            result: dict[str, Any] = {"ok": False, "error": f"there is no tool called {name}"}
        else:
            try:
                result = tool(_ToolContext(rt, session, call), arguments)
            except (KeyError, TypeError, ValueError) as exc:
                result = {"ok": False, "error": f"bad input: {exc}"}
        call.turns = [
            *call.turns,
            {
                **_turn(rt, "tool", json.dumps(dict(arguments), ensure_ascii=False)[:500]),
                "tool": name,
                "ok": bool(result.get("ok")),
            },
        ]
        annotate(span, ok=bool(result.get("ok")))
        return result


@dataclass
class _ToolContext:
    rt: Runtime
    session: Session
    call: Call

    @property
    def now(self) -> datetime:
        return self.rt.clock.now()

    @property
    def tenant(self) -> Tenant:
        return self.session.get_one(Tenant, self.call.tenant_id)

    def open_cases(self) -> list[Case]:
        cases = cases_of_message(self.session, self.call.message_id, lock=True)
        return [case for case in cases if case.state != CaseState.CLOSED]

    def apply_reply(self, understood: Understood) -> list[str]:
        """Send what the customer said through the state machine, as for an email
        reply. Returns the reasons any case went to a person instead."""
        tenant = self.tenant
        problems = []
        for case, reply in replies_per_case(understood, self.open_cases()):
            transition = on_reply(case.view(), reply, tenant.policy, self.now, tenant.zone)
            apply_transition(
                self.session, case, transition, self.now, source_message_id=self.call.message_id
            )
            if transition.task is not None:
                problems.append(transition.task_summary)
        return problems


IDENTITY_FIRST = {"ok": False, "error": "Confirm you are speaking with the customer first."}


def _confirm_identity(ctx: _ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    if args.get("confirmed") is True:
        ctx.call.identity_confirmed = True
        return {"ok": True}
    problems = ctx.apply_reply(
        Understood(
            ReplyIntent.WRONG_CONTACT, f"Wrong person on a call: {args.get('spoke_with', '')}"
        )
    )
    return {
        "ok": False,
        "next": "Apologise, do not discuss the invoices, and end the call.",
        "noted": problems,
    }


def _get_invoice(ctx: _ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    if not ctx.call.identity_confirmed:
        return IDENTITY_FIRST
    lines = [invoice_line(case.invoice) for case in ctx.open_cases()]
    if not lines:
        return {"ok": True, "invoices": [], "note": "Nothing is overdue any more."}
    result: dict[str, Any] = {
        "ok": True,
        "invoices": [
            {
                "number": line.number,
                "amount_due": format_money(line.amount_due, line.currency),
                "due_date": f"{line.due_date:%d %B %Y}",
                "details": line.display_details,
            }
            for line in lines
        ],
    }
    if len(lines) > 1 and len({line.currency for line in lines}) == 1:
        result["total"] = format_money(sum(line.amount_due for line in lines), lines[0].currency)
    return result


def _log_promise(ctx: _ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    if not ctx.call.identity_confirmed:
        return IDENTITY_FIRST
    if _promises_on_call(ctx):
        return {
            "ok": False,
            "recorded": False,
            "error": "A promise is already recorded on this call.",
        }
    cases = ctx.open_cases()
    if not cases:
        return {"ok": False, "recorded": False, "error": "Nothing is overdue any more."}
    try:
        promised = date.fromisoformat(str(args["promised_date"]))
    except ValueError:
        return {"ok": False, "recorded": False, "error": "Ask for the date again."}
    amount = None
    if args.get("amount"):
        try:
            amount = to_minor_units(str(args["amount"]), cases[0].invoice.currency)
        except MoneyError:
            return {"ok": False, "recorded": False, "error": "Ask for the amount again."}
    known = {case.invoice.number for case in cases}
    named = frozenset(n for n in args.get("invoice_numbers") or [] if n in known)
    problems = ctx.apply_reply(
        Understood(
            ReplyIntent.PROMISE,
            "Promise made on a call.",
            promised_date=promised,
            promised_amount=amount,
            invoice_numbers=named,
        )
    )
    if problems:
        return {"ok": False, "recorded": False, "reason": "; ".join(problems)}
    return {"ok": True, "recorded": True, "promised_date": promised.isoformat()}


def _log_dispute(ctx: _ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    if not ctx.call.identity_confirmed:
        return IDENTITY_FIRST
    cases = ctx.open_cases()
    known = {case.invoice.number for case in cases}
    intent = ReplyIntent.PAID_CLAIM if args.get("already_paid") else ReplyIntent.DISPUTE
    reason = str(args["reason"]).strip()[:500] or "No reason given."
    ctx.apply_reply(
        Understood(
            intent,
            f"On a call: {reason}",
            invoice_numbers=frozenset(n for n in args.get("invoice_numbers") or [] if n in known),
        )
    )
    return {"ok": True, "say": "Tell the customer a colleague will look into it."}


def _send_payment_link(ctx: _ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    tenant = ctx.tenant
    customer = ctx.session.get_one(Customer, ctx.call.customer_id)
    if not tenant.payment_link:
        return {
            "ok": False,
            "error": "There is no payment link; say a colleague will send details.",
        }
    if not customer.email:
        return {"ok": False, "error": "We have no email address for the customer."}
    message = Message(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        customer_id=customer.id,
        channel="email",
        direction=Direction.OUTBOUND,
        to_address=customer.email,
        subject=f"Payment link from {tenant.name}",
        body=(
            f"Dear {customer.name},\n\nAs requested on our call, you can pay here:\n"
            f"{tenant.payment_link}\n\nRegards,\n{tenant.name}\n"
        ),
        # Asked for by the customer and holds no amounts, so it skips drafting
        # and approval.
        status=MessageStatus.PENDING,
        idempotency_key=uuid.uuid4().hex,
        reply_token=secrets.token_hex(16),
        attempts=0,
        created_at=ctx.now,
    )
    ctx.session.add(message)
    for case in ctx.open_cases():
        ctx.session.add(MessageCase(message_id=message.id, case_id=case.id, tenant_id=tenant.id))
    return {"ok": True, "say": f"The link is on its way to {customer.email}."}


def _transfer_to_human(ctx: _ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    reason = str(args.get("reason", "")).strip()[:500]
    for case in ctx.open_cases():
        transition = on_transfer_requested(case.view(), reason)
        if transition is not None:
            apply_transition(ctx.session, case, transition, ctx.now)
    number = ctx.tenant.voice_transfer_number
    if number and ctx.call.provider_call_id and ctx.rt.voice is not None:
        try:
            ctx.rt.voice.transfer(ctx.call.provider_call_id, number)
            return {"ok": True, "transferred": True, "say": "Say you are connecting them now."}
        except VoiceError as exc:
            log.warning("transferring call %s failed: %s", ctx.call.id, exc)
    return {"ok": True, "transferred": False, "say": "Say a colleague will call them back."}


def _end_call(ctx: _ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    if args.get("reason") == "answering_machine":
        ctx.call.answered_by = "machine (heard by the agent)"
    return {"ok": True, "end": True}


def _promises_on_call(ctx: _ToolContext) -> bool:
    query = select(Promise.id).where(Promise.source_message_id == ctx.call.message_id)
    return ctx.session.scalar(query.limit(1)) is not None


_TOOLS: dict[str, Callable[[_ToolContext, Mapping[str, Any]], dict[str, Any]]] = {
    "confirm_identity": _confirm_identity,
    "get_invoice": _get_invoice,
    "log_promise": _log_promise,
    "log_dispute": _log_dispute,
    "send_payment_link": _send_payment_link,
    "transfer_to_human": _transfer_to_human,
    "end_call": _end_call,
}


# --- After the call -----------------------------------------------------------------


def finish_call(rt: Runtime, token: str, reached: bool) -> None:
    """The call is over. `reached` is False when nobody (or a machine) answered.
    Safe to call more than once: only the first call does anything."""
    with rt.session_factory.begin() as session:
        call = _lock_call(session, token)
        if call is not None:
            _finish(rt, session, call, reached)


def provider_ended(rt: Runtime, token: str, provider_status: str) -> None:
    """The provider's final status for the call. A call that was never answered
    (busy, no answer, failed, or completed while still ringing) was not
    reached. An answered call is finished by the bridge."""
    with rt.session_factory.begin() as session:
        call = _lock_call(session, token)
        if call is not None and call.status == CallStatus.PLACED:
            log.info("call %s ended unanswered: %s", call.id, provider_status)
            _finish(rt, session, call, reached=False)


def finish_stale_calls(rt: Runtime, tenant_id: uuid.UUID) -> int:
    """Close calls the provider never reported back on (a lost callback, a
    stream that never connected), so their cases do not wait forever."""
    now = rt.clock.now()
    with rt.session_factory.begin() as session:
        stale = session.scalars(
            select(Call)
            .where(
                Call.tenant_id == tenant_id,
                Call.status.in_([CallStatus.PLACED, CallStatus.IN_PROGRESS]),
                Call.created_at < now - STALE_AFTER - timedelta(seconds=rt.voice_max_seconds),
            )
            .with_for_update(skip_locked=True)
        ).all()
        for call in stale:
            _finish(rt, session, call, reached=call.status == CallStatus.IN_PROGRESS)
        return len(stale)


def _finish(rt: Runtime, session: Session, call: Call, reached: bool) -> None:
    if call.ended_at is not None:
        return
    now = rt.clock.now()
    call.ended_at = now
    machine = (call.answered_by or "").startswith("machine")
    if not reached or machine:
        call.status = CallStatus.NOT_REACHED
        if not call.test:
            for case in cases_of_message(session, call.message_id, lock=True):
                transition = on_call_not_reached(case.view(), now)
                if transition is not None:
                    apply_transition(session, case, transition, now)
        return

    call.status = CallStatus.ANSWERED
    cases = cases_of_message(session, call.message_id, lock=True)
    promised = session.scalars(
        select(Promise.amount).where(Promise.source_message_id == call.message_id)
    ).all()
    problems = audit_call(
        [_as_turn(t) for t in call.turns], [invoice_line(c.invoice) for c in cases], promised
    )
    call.audit = CallAudit.FAILED if problems else CallAudit.PASSED
    call.audit_problems = problems
    message = session.get_one(Message, call.message_id)
    message.body = transcript_text(call.turns)
    if problems and cases:
        transition = on_call_audit_failed(cases[0].view(), "; ".join(problems))
        if transition is not None:
            apply_transition(session, cases[0], transition, now)


def transcript_text(turns: list[dict[str, Any]]) -> str:
    """The call as plain text, stored as the message body for the contact log."""
    lines = []
    for turn in turns:
        if turn["role"] == "tool":
            lines.append(
                f"[{turn['tool']} {turn['text']} -> {'ok' if turn.get('ok') else 'refused'}]"
            )
        else:
            lines.append(f"{turn['role'].capitalize()}: {turn['text']}")
    return "\n".join(lines)


def _as_turn(raw: dict[str, Any]) -> Turn:
    return Turn(raw["role"], raw.get("text", ""), raw.get("tool", ""), bool(raw.get("ok")))


def _turn(rt: Runtime, role: str, text: str) -> dict[str, Any]:
    return {"role": role, "text": text, "at": rt.clock.now().isoformat()}


def _lock_call(session: Session, token: str) -> Call | None:
    if not token:
        return None
    return session.scalar(select(Call).where(Call.token == token).with_for_update())
