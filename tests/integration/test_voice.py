"""M7: reminder calls end to end, with a scripted speech model and a fake phone.

The acceptance test (spec, Milestones) is `test_a_call_logs_a_promise_that_the_
workflow_honours`: a call reaches the customer, the promise made on it is
checked by code and recorded, and the workflow then treats it like any other
promise (kept when the invoice is paid).
"""

import asyncio
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from parley.adapters.clock import FakeClock
from parley.core.domain import (
    CallAudit,
    CallStatus,
    CaseState,
    MessageStatus,
    PromiseStatus,
    TaskKind,
)
from parley.db.models import Call, Case, Message, Promise, Task, Tenant
from parley.ports.voice import AudioOut, Interrupted, SpeechEvent, TextOut, ToolRequest
from parley.services.calls import answered, provider_ended
from parley.services.runtime import Runtime
from parley.services.voice_bridge import run_call
from parley.services.worker import run_once
from tests.integration.conftest import IST, START, invoice_row, make_tenant, write_aging
from tests.integration.voice_fakes import FakeDialler, FakeLeg, ScriptedSpeech

PHONE = "98123 45678"
NUMBER = "+919812345678"
DAY = timedelta(days=1)

HELLO = TextOut("agent", "Namaste, I am an AI assistant calling for Acme Traders. Is this Asha?")
YES = TextOut("customer", "Haan, main Asha bol rahi hoon.")
CONFIRM = ToolRequest("t1", "confirm_identity", {"confirmed": True, "spoke_with": "Asha"})
INVOICE = ToolRequest("t2", "get_invoice", {})
AMOUNT = TextOut("agent", "Invoice A-1 for INR 1,000.00 was due on 1 January. When can you pay?")
FRIDAY = TextOut("customer", "Friday ko pay kar dungi.")
PROMISE = ToolRequest("t3", "log_promise", {"promised_date": "2026-01-09"})
BYE = TextOut("agent", "Noted, Friday the 9th. Thank you, goodbye.")
END = ToolRequest("t4", "end_call", {"reason": "done"})

PROMISE_CALL: list[SpeechEvent] = [
    AudioOut(b"\x00\x01" * 480),
    HELLO,
    YES,
    CONFIRM,
    INVOICE,
    AMOUNT,
    Interrupted(),
    FRIDAY,
    PROMISE,
    BYE,
    END,
]


def setup_calls(
    rt: Runtime, tmp_path: Path, script: list[SpeechEvent], **policy: object
) -> tuple[Path, FakeDialler, ScriptedSpeech]:
    dialler, speech = FakeDialler(), ScriptedSpeech(script)
    rt.voice, rt.speech = dialler, speech
    rt.voice_allowed_numbers = frozenset({NUMBER})
    aging = write_aging(
        tmp_path / "aging.csv",
        [{**invoice_row("A-1", "Asha", "1000", "2026-01-01"), "phone": PHONE}],
    )
    make_tenant(rt, aging, **{"approval_mode": "none", "call_from_reminder": 1, **policy})
    return aging, dialler, speech


def at(clock: FakeClock, day: int, hour: int = 10) -> None:
    clock.set(datetime.combine(START.date() + timedelta(days=day), time(hour), tzinfo=IST))


def call_row(rt: Runtime) -> Call:
    with rt.session_factory() as session:
        return session.scalars(select(Call)).one()


def case_row(rt: Runtime) -> Case:
    with rt.session_factory() as session:
        return session.scalars(select(Case)).one()


def talk(rt: Runtime, token: str) -> FakeLeg:
    leg = FakeLeg()
    asyncio.run(run_call(rt, token, leg, end_grace=0))
    return leg


def test_a_call_logs_a_promise_that_the_workflow_honours(
    rt: Runtime, clock: FakeClock, tmp_path: Path
) -> None:
    aging, dialler, speech = setup_calls(rt, tmp_path, PROMISE_CALL)

    run_once(rt, sync_interval=DAY)  # the first reminder is a call: dialled now
    [(number, token)] = dialler.placed
    assert number == NUMBER
    with rt.session_factory() as session:
        message = session.scalars(select(Message)).one()
        assert (message.channel, message.status) == ("voice", MessageStatus.SENT)
    assert case_row(rt).state == CaseState.AWAITING_REPLY

    assert answered(rt, token, "human")
    leg = talk(rt, token)

    session = speech.sessions[0]
    assert "AI assistant" in session.system_prompt and "INR" not in session.system_prompt
    assert session.audio_in == 640  # 160 samples at 8 kHz became 320 at 16 kHz
    assert leg.played == 320  # 480 samples at 24 kHz became 160 at 8 kHz (320 bytes)
    assert leg.cleared == 1  # the customer talked over the agent
    assert session.result("get_invoice")["invoices"][0]["amount_due"] == "INR 1,000.00"
    assert session.result("log_promise")["recorded"] is True

    call = call_row(rt)
    assert (call.status, call.audit, call.audit_problems) == (
        CallStatus.ANSWERED,
        CallAudit.PASSED,
        [],
    )
    assert [t["role"] for t in call.turns][:4] == ["agent", "customer", "tool", "tool"]
    with rt.session_factory() as session_:
        body = session_.get_one(Message, call.message_id).body
    assert "Customer: Friday ko pay kar dungi." in body
    assert "[log_promise" in body

    assert case_row(rt).state == CaseState.PROMISED
    # Friday 9 Jan: the customer pays; the source shows the invoice paid.
    at(clock, 4)
    write_aging(aging, [{**invoice_row("A-1", "Asha", "0", "2026-01-01"), "phone": PHONE}])
    run_once(rt, sync_interval=timedelta(0))
    assert (case_row(rt).state, case_row(rt).closed_reason) == (CaseState.CLOSED, "paid")
    with rt.session_factory() as session_:
        assert session_.scalars(select(Promise.status)).one() == PromiseStatus.KEPT


def test_amounts_are_withheld_until_the_identity_is_confirmed(rt: Runtime, tmp_path: Path) -> None:
    script: list[SpeechEvent] = [
        HELLO,
        ToolRequest("t1", "get_invoice", {}),
        ToolRequest("t2", "log_promise", {"promised_date": "2026-01-09"}),
        END,
    ]
    _, dialler, speech = setup_calls(rt, tmp_path, script)
    run_once(rt, sync_interval=DAY)
    token = dialler.placed[0][1]
    answered(rt, token, "human")
    talk(rt, token)

    session = speech.sessions[0]
    assert session.result("get_invoice")["ok"] is False
    assert session.result("log_promise")["ok"] is False
    assert case_row(rt).state == CaseState.AWAITING_REPLY


def test_a_promise_the_rules_reject_is_not_recorded(rt: Runtime, tmp_path: Path) -> None:
    far = ToolRequest("t3", "log_promise", {"promised_date": "2026-06-01"})
    _, dialler, speech = setup_calls(rt, tmp_path, [HELLO, YES, CONFIRM, far, END])
    run_once(rt, sync_interval=DAY)
    token = dialler.placed[0][1]
    answered(rt, token, "human")
    talk(rt, token)

    result = speech.sessions[0].result("log_promise")
    assert result["recorded"] is False and "days away" in result["reason"]
    assert case_row(rt).state == CaseState.NEEDS_HUMAN


def test_an_answering_machine_gets_no_message_and_the_call_is_retried(
    rt: Runtime, clock: FakeClock, tmp_path: Path
) -> None:
    _, dialler, speech = setup_calls(rt, tmp_path, PROMISE_CALL)
    run_once(rt, sync_interval=DAY)
    token = dialler.placed[0][1]

    assert not answered(rt, token, "machine_start")
    assert call_row(rt).status == CallStatus.NOT_REACHED
    case = case_row(rt)
    assert case.state == CaseState.SCHEDULED
    assert case.next_action_at == clock.now() + timedelta(days=1)
    assert speech.sessions == []  # the agent never spoke

    at(clock, 1)  # the next day: called again (reminder 2)
    run_once(rt, sync_interval=DAY)
    assert len(dialler.placed) == 2


def test_no_answer_reschedules_and_late_callbacks_change_nothing(
    rt: Runtime, tmp_path: Path
) -> None:
    _, dialler, _ = setup_calls(rt, tmp_path, PROMISE_CALL)
    run_once(rt, sync_interval=DAY)
    token = dialler.placed[0][1]

    provider_ended(rt, token, "no-answer")
    assert case_row(rt).state == CaseState.SCHEDULED
    assert not answered(rt, token, "human")  # a late or replayed callback
    provider_ended(rt, token, "completed")
    assert call_row(rt).status == CallStatus.NOT_REACHED


def test_a_call_that_breaks_a_voice_rule_goes_to_a_person(rt: Runtime, tmp_path: Path) -> None:
    rude = TextOut("agent", "Pay INR 1,000.00 now or we take legal action.")
    _, dialler, _ = setup_calls(rt, tmp_path, [HELLO, YES, CONFIRM, rude, END])
    run_once(rt, sync_interval=DAY)
    token = dialler.placed[0][1]
    answered(rt, token, "human")
    talk(rt, token)

    call = call_row(rt)
    assert call.audit == CallAudit.FAILED
    assert call.audit_problems == ["banned phrase: 'legal action'"]
    with rt.session_factory() as session:
        task = session.scalars(select(Task)).one()
    assert task.kind == TaskKind.REVIEW_CALL
    assert case_row(rt).state == CaseState.AWAITING_REPLY  # the case keeps its place


def test_calls_wait_for_allowed_hours(rt: Runtime, clock: FakeClock, tmp_path: Path) -> None:
    _, dialler, _ = setup_calls(rt, tmp_path, PROMISE_CALL, approval_mode="all")
    run_once(rt, sync_interval=DAY)  # queued; waits for approval
    with rt.session_factory.begin() as session:
        task = session.scalars(select(Task)).one()
        assert task.summary.startswith("Approve call to Asha")
        session.get_one(Message, task.message_id).status = MessageStatus.PENDING  # approved late

    at(clock, 0, hour=21)  # approved in the evening: quiet hours
    run_once(rt, sync_interval=DAY)
    assert dialler.placed == []
    at(clock, 1, hour=9)
    run_once(rt, sync_interval=DAY)
    assert len(dialler.placed) == 1


def test_numbers_not_allowed_get_email_instead(rt: Runtime, tmp_path: Path) -> None:
    setup_calls(rt, tmp_path, PROMISE_CALL)
    rt.voice_allowed_numbers = frozenset({"+919800000000"})  # someone else
    run_once(rt, sync_interval=DAY)
    with rt.session_factory() as session:
        message = session.scalars(select(Message)).one()
    assert (message.channel, message.to_address) == ("email", "asha@example.com")


def test_transfer_to_a_person_hands_over_the_call(rt: Runtime, tmp_path: Path) -> None:
    transfer = ToolRequest("t3", "transfer_to_human", {"reason": "wants a discount"})
    _, dialler, speech = setup_calls(rt, tmp_path, [HELLO, YES, CONFIRM, transfer, END])
    with rt.session_factory.begin() as session:
        session.scalars(select(Tenant)).one().voice_transfer_number = "+919899999999"
    run_once(rt, sync_interval=DAY)
    token = dialler.placed[0][1]
    answered(rt, token, "human")
    talk(rt, token)

    assert speech.sessions[0].result("transfer_to_human")["transferred"] is True
    assert dialler.transfers == [("CA1", "+919899999999")]
    assert case_row(rt).state == CaseState.NEEDS_HUMAN


@pytest.mark.parametrize("already_paid", [False, True])
def test_disputes_and_paid_claims_on_a_call_go_to_the_investigator(
    rt: Runtime, tmp_path: Path, already_paid: bool
) -> None:
    dispute = ToolRequest(
        "t3", "log_dispute", {"reason": "Half the boxes were damaged", "already_paid": already_paid}
    )
    _, dialler, _ = setup_calls(rt, tmp_path, [HELLO, YES, CONFIRM, dispute, END])
    run_once(rt, sync_interval=DAY)
    token = dialler.placed[0][1]
    answered(rt, token, "human")
    talk(rt, token)
    assert case_row(rt).state == CaseState.INVESTIGATING
