"""Investigator eval: seeded ledgers with a known answer (evals/datasets/investigations.jsonl).

Each example sets up its own tenant: the invoices, the payments in the books
(some from other customers, some noise), and the customer's claim. The real
investigation step runs, and the stored run is scored: was the finding right,
did it cite exactly the right payments, did it stay within the step cap.
"""

import csv
import tempfile
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from evalkit import Example, Rule, Scorer, load_jsonl
from evals.sandbox import runtime
from harness import AgentModel
from parley.core.domain import AgentRunStatus, CaseState, Direction, MessageStatus, ReplyIntent
from parley.db.models import AgentRun, Case, Invoice, Message, MessageCase, Payment
from parley.db.session import SessionFactory
from parley.services.investigation import run_investigations
from parley.services.sync import sync_tenant
from parley.services.tenants import create_tenant

DATASET = Path(__file__).parent / "datasets" / "investigations.jsonl"
TODAY = datetime(2026, 1, 10, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
RULES = [
    Rule("result_correct", "no_drop"),
    Rule("evidence_correct", "no_drop"),
    Rule("over_step_cap", "max", 0),
]
COUNT_METRICS = frozenset({"over_step_cap"})


def load() -> list[Example]:
    return load_jsonl(DATASET)


def make_task(
    session_factory: SessionFactory, agent_for: Callable[[Example], AgentModel]
) -> Callable[[Example], dict[str, Any]]:
    """`agent_for` gives the agent model for an example (the real one in evals,
    a scripted one in the plumbing tests)."""

    def task(example: Example) -> dict[str, Any]:
        rt = runtime(session_factory, TODAY, agent_model=agent_for(example))
        with tempfile.TemporaryDirectory() as tmp:
            tenant_id = _seed(rt.session_factory, Path(tmp), example)
            sync_tenant(rt, tenant_id)
            _queue_claim(rt.session_factory, tenant_id, example, rt.clock.now())
            run_investigations(rt, tenant_id)
        return _read_run(rt.session_factory, tenant_id)

    return task


def _seed(session_factory: SessionFactory, folder: Path, example: Example) -> uuid.UUID:
    invoices = folder / "aging.csv"
    with invoices.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "invoice_number",
                "customer_id",
                "customer_name",
                "email",
                "amount_due",
                "currency",
                "due_date",
            ]
        )
        for inv in example.input["invoices"]:
            writer.writerow(
                [
                    inv["number"],
                    "C1",
                    "Asha Stores",
                    "asha@example.com",
                    inv["amount_due"],
                    "INR",
                    inv["due_date"],
                ]
            )
        # Another customer, so the books hold payments that are not Asha's.
        writer.writerow(
            ["OTHER-1", "C2", "Bala Traders", "bala@example.com", "100.00", "INR", "2025-12-31"]
        )
    payments = folder / "payments.csv"
    with payments.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["payment_id", "customer_id", "amount", "currency", "paid_on", "reference"])
        for p in example.input["payments"]:
            customer = "C1" if p["customer"] == "self" else "C2"
            writer.writerow([p["ref"], customer, p["amount"], "INR", p["paid_on"], p["reference"]])
    with session_factory.begin() as session:
        tenant, _ = create_tenant(
            session,
            f"Eval {example.id}",
            "Asia/Kolkata",
            {"kind": "csv", "invoices_path": str(invoices), "payments_path": str(payments)},
        )
        return tenant.id


def _queue_claim(
    session_factory: SessionFactory, tenant_id: uuid.UUID, example: Example, now: datetime
) -> None:
    """Put the case where a read reply would leave it: Investigating, with the
    claim stored as an inbound message and a queued run."""
    with session_factory.begin() as session:
        case = session.scalars(
            select(Case)
            .join(Invoice)
            .where(Case.tenant_id == tenant_id, Invoice.number == example.input["case_invoice"])
        ).one()
        case.state = CaseState.INVESTIGATING
        case.next_action_at = None
        message = Message(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            customer_id=case.customer_id,
            channel="email",
            direction=Direction.INBOUND,
            from_address="asha@example.com",
            to_address="",
            subject="Re: reminder",
            body=example.input["text"],
            status=MessageStatus.READ,
            idempotency_key=uuid.uuid4().hex,
            attempts=0,
            created_at=now - timedelta(hours=1),
        )
        session.add(message)
        session.flush()  # the run and the link refer to the message, so insert it first
        session.add(MessageCase(message_id=message.id, case_id=case.id, tenant_id=tenant_id))
        session.add(
            AgentRun(
                tenant_id=tenant_id,
                case_id=case.id,
                message_id=message.id,
                claim=ReplyIntent(example.input["claim"]),
                status=AgentRunStatus.QUEUED,
                steps=[],
                created_at=now,
            )
        )


def _read_run(session_factory: SessionFactory, tenant_id: uuid.UUID) -> dict[str, Any]:
    with session_factory() as session:
        run = session.scalars(select(AgentRun).where(AgentRun.tenant_id == tenant_id)).one()
        cited = (run.finding or {}).get("evidence_ids", [])
        refs = []
        for raw in cited:
            try:
                record_id = uuid.UUID(raw)
            except ValueError:
                refs.append(f"invalid:{raw}")
                continue
            payment = session.get(Payment, record_id)
            refs.append(payment.external_id if payment else "non-payment")
        return {
            "result": str(run.result),
            "model_result": (run.finding or {}).get("result"),
            "cited": refs,
            "outcome": run.outcome,
            "steps": len(run.steps),
            "cost_micro_usd": run.cost_micro_usd,
        }


def score(example: Example, output: dict[str, Any]) -> dict[str, float | bool | int]:
    expected_refs = example.expected["evidence_refs"]
    cited_payments = {
        r for r in output["cited"] if r not in ("non-payment",) and not r.startswith("invalid:")
    }
    scores: dict[str, float | bool | int] = {
        "result_correct": output["result"] == example.expected["result"],
        "over_step_cap": int(output["steps"] > 8),
        "steps": output["steps"],
    }
    if expected_refs is not None:
        scores["evidence_correct"] = cited_payments == set(expected_refs)
    return scores


SCORERS: list[Scorer] = [score]
