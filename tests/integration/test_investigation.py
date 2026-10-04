"""M3 acceptance (spec, Milestones): a seeded "we already paid" claim returns the
right finding with valid evidence ids, and the step cap holds.

The agent model is scripted, so these tests check the harness, the tools, the
run log and code's handling of the finding, not the model's judgement (that is
what the M4 evals are for).
"""

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from harness import FinalAnswer, ToolCall
from harness.model import Reply, ToolResult
from harness.testing import ScriptedModel
from parley.adapters.models.fake import FakeCall, FakeModel
from parley.collections_ai.jobs import BriefOut, ReplyReading
from parley.core.domain import (
    AgentRunStatus,
    CaseState,
    Direction,
    FindingResult,
    ReplyIntent,
    TaskKind,
)
from parley.db.models import AgentRun, Case, Invoice, Message, Payment, Task
from parley.ports.channel import InboundMessage
from parley.services.inbound import receive_email
from parley.services.investigation import run_investigations
from parley.services.replies import read_received_replies
from parley.services.runtime import Runtime
from parley.services.worker import run_tenant_once
from tests.integration.conftest import invoice_row, make_tenant, write_aging

PAYMENTS = """payment_id,customer_id,customer_name,amount,currency,paid_on,reference
P-1,Asha,Asha,1000.00,INR,2026-01-02,UTR123
P-2,Asha,Asha,250.00,INR,2025-11-10,old payment
P-3,Bala,Bala,1000.00,INR,2026-01-02,someone else's
"""


def paid_claim(rt: Runtime, tmp_path: Path) -> None:
    """Asha is reminded about A-1 and replies that she has already paid."""
    aging = write_aging(
        tmp_path / "aging.csv",
        [
            invoice_row("A-1", "Asha", "1000", "2026-01-01"),
            invoice_row("B-1", "Bala", "500", "2026-01-01"),
        ],
    )
    payments = tmp_path / "payments.csv"
    payments.write_text(PAYMENTS)
    tenant_id = make_tenant(rt, aging, payments, approval_mode="none")
    run_tenant_once(rt, tenant_id, None, timedelta(hours=1))

    with rt.session_factory() as session:
        token = session.scalars(
            select(Message.reply_token).where(Message.to_address == "asha@example.com")
        ).one()
    receive_email(
        rt,
        InboundMessage(
            provider_message_id="<claim@x>",
            sender="asha@example.com",
            recipients=(f"reply+{token}@replies.example.com",),
            subject="Re: reminder",
            text="We paid this on 2 Jan by NEFT, UTR123. Please check.",
        ),
    )

    def read_as_paid_claim(call: FakeCall) -> ReplyReading | BriefOut:
        if call.output_type is BriefOut:
            return BriefOut(brief="Says they paid by NEFT.")
        return ReplyReading(intent=ReplyIntent.PAID_CLAIM, confidence=0.95, summary="Says paid.")

    rt.model = FakeModel(read_as_paid_claim)
    read_received_replies(rt, tenant_id)
    rt.model = None


def last_rows(received: list[ToolResult | Reply]) -> list[dict[str, str]]:
    content = received[-1].content  # type: ignore[union-attr]
    return json.loads(content)  # type: ignore[no-any-return]


def asha_case(rt: Runtime) -> Case:
    with rt.session_factory() as session:
        return session.scalars(select(Case).join(Invoice).where(Invoice.number == "A-1")).one()


def the_run(rt: Runtime) -> AgentRun:
    with rt.session_factory() as session:
        return session.scalars(select(AgentRun)).one()


def tasks(rt: Runtime) -> list[Task]:
    with rt.session_factory() as session:
        return list(session.scalars(select(Task)))


def test_already_paid_claim_finds_the_payment(rt: Runtime, tmp_path: Path) -> None:
    paid_claim(rt, tmp_path)
    tenant_id = asha_case(rt).tenant_id
    rt.agent_model = agent = ScriptedModel(
        [
            ToolCall(
                "c1",
                "search_payments",
                {"from_date": "2025-12-01", "to_date": "2026-01-05", "near_amount": "1000"},
            ),
            lambda received: FinalAnswer(
                "c2",
                {
                    "result": "payment_found",
                    "evidence_ids": [last_rows(received)[0]["id"]],
                    "summary": "Says paid on 2 Jan (UTR123); a matching payment is recorded.",
                },
            ),
        ]
    )

    assert run_investigations(rt, tenant_id) == 1

    # The search saw only Asha's payments in the range: not Bala's, not November's.
    rows = last_rows(agent.sessions[0].received[:1])
    assert [(r["amount"], r["reference"]) for r in rows] == [("INR 1,000.00", "UTR123")]
    # The claim text reached the agent, inside its untrusted-input tags.
    assert "<customer_message>\nWe paid this on 2 Jan" in str(agent.started_with[0]["task"])

    run = the_run(rt)
    assert (run.status, run.outcome, run.result) == (
        AgentRunStatus.DONE,
        "final",
        FindingResult.PAYMENT_FOUND,
    )
    assert [s["kind"] for s in run.steps] == ["tool_call", "final"]
    assert run.prompt_version == "investigate.v1"

    assert asha_case(rt).state == CaseState.NEEDS_HUMAN
    [task] = tasks(rt)
    assert (task.kind, task.agent_run_id) == (TaskKind.VERIFY_PAYMENT, run.id)
    assert "Evidence: payment of INR 1,000.00 on 02 Jan 2026 (ref UTR123)." in task.summary

    # Nothing in the books changed: a person confirms before anything is closed.
    with rt.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 3
        assert session.scalar(select(Invoice.amount_due).where(Invoice.number == "A-1")) == 100000


def test_step_cap_holds(rt: Runtime, tmp_path: Path) -> None:
    paid_claim(rt, tmp_path)
    rt.agent_model = ScriptedModel([ToolCall(f"c{i}", "get_promises", {}) for i in range(30)])

    run_investigations(rt, asha_case(rt).tenant_id)

    run = the_run(rt)
    assert (run.outcome, run.result, len(run.steps)) == ("step_limit", FindingResult.UNCLEAR, 8)
    [task] = tasks(rt)
    assert "used all its steps" in task.summary


def test_invented_evidence_is_set_aside(rt: Runtime, tmp_path: Path) -> None:
    paid_claim(rt, tmp_path)
    rt.agent_model = ScriptedModel(
        [
            FinalAnswer(
                "c1",
                {
                    "result": "payment_found",
                    "evidence_ids": ["P-1", "4f0c0000-0000-0000-0000-000000000000"],
                    "summary": "Paid.",
                },
            )
        ]
    )

    run_investigations(rt, asha_case(rt).tenant_id)

    assert the_run(rt).result == FindingResult.UNCLEAR
    assert "cited records that do not exist (P-1, 4f0c0000" in tasks(rt)[0].summary


def test_another_customers_payment_is_not_valid_evidence(rt: Runtime, tmp_path: Path) -> None:
    paid_claim(rt, tmp_path)
    with rt.session_factory() as session:
        balas = str(
            session.scalars(select(Payment.id).where(Payment.reference == "someone else's")).one()
        )
    rt.agent_model = ScriptedModel(
        [
            FinalAnswer(
                "c1", {"result": "payment_found", "evidence_ids": [balas], "summary": "Paid."}
            )
        ]
    )

    run_investigations(rt, asha_case(rt).tenant_id)

    assert the_run(rt).result == FindingResult.UNCLEAR


def test_payment_found_without_a_payment_is_not_accepted(rt: Runtime, tmp_path: Path) -> None:
    paid_claim(rt, tmp_path)
    invoice_id = str(asha_case(rt).invoice_id)
    rt.agent_model = ScriptedModel(
        [
            FinalAnswer(
                "c1", {"result": "payment_found", "evidence_ids": [invoice_id], "summary": "Paid."}
            )
        ]
    )

    run_investigations(rt, asha_case(rt).tenant_id)

    assert the_run(rt).result == FindingResult.UNCLEAR
    assert "without citing a payment" in tasks(rt)[0].summary


def test_tools_only_see_this_customer(rt: Runtime, tmp_path: Path) -> None:
    paid_claim(rt, tmp_path)
    rt.agent_model = agent = ScriptedModel(
        [
            ToolCall("c1", "get_invoice", {"invoice_number": "B-1"}),  # Bala's invoice
            ToolCall("c2", "get_invoice", {"invoice_number": "A-1"}),
            FinalAnswer("c3", {"result": "unclear", "evidence_ids": [], "summary": "Unsure."}),
        ]
    )

    run_investigations(rt, asha_case(rt).tenant_id)

    refused, found = agent.sessions[0].received[:2]
    assert refused == ToolResult(
        "c1", "This customer has no invoice numbered 'B-1'.", is_error=True
    )
    assert json.loads(found.content)["amount_due"] == "INR 1,000.00"  # type: ignore[union-attr]


def test_run_is_cancelled_if_the_case_moved_on(rt: Runtime, tmp_path: Path) -> None:
    paid_claim(rt, tmp_path)
    with rt.session_factory.begin() as session:
        case = session.scalars(select(Case).join(Invoice).where(Invoice.number == "A-1")).one()
        case.state = CaseState.CLOSED  # e.g. the sync saw the invoice paid meanwhile
    rt.agent_model = agent = ScriptedModel([])

    run_investigations(rt, asha_case(rt).tenant_id)

    assert the_run(rt).status == AgentRunStatus.CANCELLED
    assert agent.sessions == []
    assert tasks(rt) == []


def test_outbound_messages_are_never_sent_by_the_investigator(rt: Runtime, tmp_path: Path) -> None:
    paid_claim(rt, tmp_path)
    with rt.session_factory() as session:
        before = session.scalar(
            select(func.count()).select_from(Message).where(Message.direction == Direction.OUTBOUND)
        )
    rt.agent_model = ScriptedModel(
        [FinalAnswer("c1", {"result": "payment_not_found", "evidence_ids": [], "summary": "None."})]
    )
    run_investigations(rt, asha_case(rt).tenant_id)
    with rt.session_factory() as session:
        after = session.scalar(
            select(func.count()).select_from(Message).where(Message.direction == Direction.OUTBOUND)
        )
    assert after == before
    assert "Check the bank" in tasks(rt)[0].summary


def test_a_payment_too_small_for_the_invoice_is_not_payment_found(
    rt: Runtime, tmp_path: Path
) -> None:
    paid_claim(rt, tmp_path)
    with rt.session_factory() as session:
        old = str(
            session.scalars(select(Payment.id).where(Payment.reference == "old payment")).one()
        )
    rt.agent_model = ScriptedModel(
        [FinalAnswer("c1", {"result": "payment_found", "evidence_ids": [old], "summary": "Paid."})]
    )

    run_investigations(rt, asha_case(rt).tenant_id)

    assert the_run(rt).result == FindingResult.UNCLEAR
    assert "add up to less than the amount due" in tasks(rt)[0].summary
