"""Whole-case eval: six debtor personas, each played by the model, over a
simulated 30 days of the real worker loop (spec: "Whole cases").

Each day at 10:00: any payment the persona makes lands in the books, replies
written the day before arrive by email, and the worker runs. Every reminder
sent that day is shown to the persona model, whose reply arrives the next day.
At the end the case's state, its tasks and the audit of the message log are
scored.
"""

import csv
import tempfile
import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select

from evalkit import Example, Rule, Scorer, load_jsonl
from evals.sandbox import runtime
from harness import AgentModel
from parley.adapters.clock import FakeClock
from parley.core.domain import Direction, MessageStatus, PromiseStatus
from parley.db.models import AgentRun, Case, Message, Promise, Task
from parley.db.session import SessionFactory
from parley.ports.channel import InboundMessage
from parley.ports.model import ModelPort
from parley.services.audit import find_policy_violations
from parley.services.inbound import receive_email
from parley.services.metrics import tenant_metrics
from parley.services.tenants import create_tenant
from parley.services.worker import run_tenant_once

DATASET = Path(__file__).parent / "datasets" / "personas.jsonl"
PERSONA_PROMPT = (Path(__file__).parent / "prompts" / "persona.md").read_text(encoding="utf-8")
IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 1, 5, 10, 0, tzinfo=IST)
DAYS = 30
RULES = [Rule("final_state_correct", "no_drop"), Rule("policy_violations", "max", 0)]
COUNT_METRICS = frozenset({"policy_violations"})


class PersonaReply(BaseModel):
    reply: str = Field(default="", description="The email body, or empty for no reply")


def load() -> list[Example]:
    return load_jsonl(DATASET)


def make_task(
    session_factory: SessionFactory,
    model: ModelPort,
    agent_for: Callable[[Example], AgentModel | None],
    persona_model: ModelPort | None = None,
) -> Callable[[Example], dict[str, Any]]:
    """`model` runs the service (drafting, reading); `persona_model` plays the
    customer (defaults to `model`)."""
    customer_model = persona_model or model

    def task(example: Example) -> dict[str, Any]:
        rt = runtime(session_factory, START, model=model, agent_model=agent_for(example))
        assert isinstance(rt.clock, FakeClock)
        with tempfile.TemporaryDirectory() as tmp:
            aging = Path(tmp) / "aging.csv"
            payments = Path(tmp) / "payments.csv"
            _write_books(
                aging,
                payments,
                amount_due="10000.00",
                paid_before=example.input.get("paid_before_start", False),
            )
            with rt.session_factory.begin() as session:
                tenant, _ = create_tenant(
                    session,
                    f"Eval {example.id} {uuid.uuid4().hex[:6]}",
                    "Asia/Kolkata",
                    {"kind": "csv", "invoices_path": str(aging), "payments_path": str(payments)},
                    {"approval_mode": "none"},
                )
                tenant_id = tenant.id

            transcript: list[tuple[str, str]] = []  # (who, text)
            replies_due: list[tuple[str, str]] = []  # (reply token, text) for tomorrow
            seen: set[uuid.UUID] = set()
            for day in range(DAYS):
                rt.clock.set(
                    datetime.combine(START.date() + timedelta(days=day), time(10, 0), tzinfo=IST)
                )
                if example.input.get("pays_on_day") == day:
                    _write_books(aging, payments, amount_due="0", paid_before=False)
                for token, text in replies_due:
                    receive_email(rt, _email(token, text, f"{example.id}-{day}-{len(transcript)}"))
                replies_due = []
                run_tenant_once(rt, tenant_id, None, timedelta(minutes=1))

                for message in _new_sent(rt.session_factory, tenant_id, seen):
                    transcript.append(("reminder", f"Subject: {message.subject}\n{message.body}"))
                    reply = _persona_reply(customer_model, example, transcript, rt.clock.now())
                    if reply:
                        transcript.append(("you", reply))
                        replies_due.append((message.reply_token or "", reply))

            return _outcome(rt.session_factory, tenant_id, transcript)

    return task


def _write_books(aging: Path, payments: Path, amount_due: str, paid_before: bool) -> None:
    with aging.open("w", newline="") as f:
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
        writer.writerow(
            ["INV-1", "C1", "Asha Stores", "asha@example.com", amount_due, "INR", "2025-12-31"]
        )
    with payments.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["payment_id", "customer_id", "amount", "currency", "paid_on", "reference"])
        if paid_before:
            # Recorded in the books, but not yet matched to the invoice.
            writer.writerow(["P1", "C1", "10000.00", "INR", "2026-01-02", "NEFT UTR 403912345678"])


def _email(token: str, text: str, msg_id: str) -> InboundMessage:
    return InboundMessage(
        provider_message_id=f"<{msg_id}@persona>",
        sender="asha@example.com",
        recipients=(f"reply+{token}@replies.example.com",),
        subject="Re: reminder",
        text=text,
    )


def _new_sent(
    session_factory: SessionFactory, tenant_id: uuid.UUID, seen: set[uuid.UUID]
) -> list[Message]:
    with session_factory() as session:
        sent = session.scalars(
            select(Message)
            .where(
                Message.tenant_id == tenant_id,
                Message.direction == Direction.OUTBOUND,
                Message.status == MessageStatus.SENT,
            )
            .order_by(Message.created_at)
        ).all()
    new = [m for m in sent if m.id not in seen]
    seen.update(m.id for m in new)
    return new


def _persona_reply(
    model: ModelPort, example: Example, transcript: list[tuple[str, str]], now: datetime
) -> str:
    system = PERSONA_PROMPT.format(description=example.input["description"])
    history = "\n\n".join(f"[{who}]\n{text}" for who, text in transcript)
    today = f"{now.astimezone(IST):%A %d %B %Y}"
    prompt = f"Today is {today}.\n\n{history}\n\nYour reply to the latest reminder:"
    return model.complete("small", system, prompt, PersonaReply).output.reply.strip()


def _outcome(
    session_factory: SessionFactory, tenant_id: uuid.UUID, transcript: list[tuple[str, str]]
) -> dict[str, Any]:
    with session_factory() as session:
        case = session.scalars(select(Case).where(Case.tenant_id == tenant_id)).one()
        tasks = [
            str(t.kind) for t in session.scalars(select(Task).where(Task.tenant_id == tenant_id))
        ]
        promises = [
            str(p.status)
            for p in session.scalars(select(Promise).where(Promise.tenant_id == tenant_id))
        ]
        runs = [
            str(r.result)
            for r in session.scalars(select(AgentRun).where(AgentRun.tenant_id == tenant_id))
        ]
        usage = tenant_metrics(session, tenant_id).usage_by_tier
        return {
            "final_state": str(case.state),
            "tasks": tasks,
            "promises": promises,
            "investigations": runs,
            "violations": find_policy_violations(session, tenant_id),
            # The case's model cost and latency by tier ("small", "large", "investigator").
            "cost_usd": {tier: u.cost_micro_usd / 1_000_000 for tier, u in usage.items()},
            "latency_ms": {tier: u.avg_latency_ms for tier, u in usage.items()},
            "transcript": transcript,
        }


def score(example: Example, output: dict[str, Any]) -> dict[str, float | bool | int]:
    events = set(output["tasks"]) | set(output["investigations"])
    if PromiseStatus.BROKEN.value in output["promises"]:
        events.add("promise_broken")
    must_have = example.expected["must_have"]
    return {
        "final_state_correct": output["final_state"] in example.expected["final_states"]
        and all(item in events for item in must_have),
        "policy_violations": len(output["violations"]),
        # Averaged over personas, these are the cost per case.
        "cost_usd": sum(output["cost_usd"].values()),
        **{f"cost_usd_{tier}": cost for tier, cost in output["cost_usd"].items()},
    }


SCORERS: list[Scorer] = [score]
