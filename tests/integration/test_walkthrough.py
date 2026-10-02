"""M2 acceptance walkthrough (spec, Milestones): reminder, promise, broken promise,
dispute. Runs the whole worker loop day by day with a fake model that drafts
reminders and reads replies; the real check is that code, not the model, drives
every step.

  Mon 5 Jan   reminder 1 (friendly) goes out
  Tue 6 Jan   customer replies "will pay on Friday" -> Promised (9 Jan)
  Sun 11 Jan  promise + 1 day grace has passed, still unpaid -> promise broken;
              Sunday is quiet, so reminder 2 (firm) goes out Mon 12 Jan
  Tue 13 Jan  customer replies with a dispute -> Investigating; the
              investigator reads the contact history and cites the reply;
              a person gets a review task with that evidence, and no
              further reminders go out
"""

import hashlib
import hmac
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from harness import FinalAnswer, ToolCall
from harness.testing import ScriptedModel
from parley.adapters.clock import FakeClock
from parley.adapters.models.fake import FakeCall, FakeModel
from parley.api.app import create_app
from parley.collections_ai.jobs import BriefOut, DraftOut, ReplyReading, ToneVerdict
from parley.core.domain import (
    CaseState,
    Direction,
    MessageStatus,
    PromiseStatus,
    ReplyIntent,
    TaskKind,
)
from parley.db.models import Case, Message, Promise, Task
from parley.services.runtime import Runtime
from parley.services.worker import run_once
from tests.integration.conftest import IST, START, invoice_row, make_tenant, write_aging

SECRET = "walkthrough-secret"


def respond(call: FakeCall) -> DraftOut | ToneVerdict | ReplyReading | BriefOut:
    if call.output_type is BriefOut:
        return BriefOut(brief="Replies within a day.")
    if call.output_type is DraftOut:
        facts = json.loads(call.prompt)
        invoice = facts["invoices"][0]
        return DraftOut(
            subject=f"Invoice {invoice['number']} ({facts['tone']})",
            body=f"Dear {facts['customer_name']},\nInvoice {invoice['number']} for "
            f"{invoice['amount_due']} was due on {invoice['due_date']}.\n{facts['business_name']}",
        )
    if call.output_type is ToneVerdict:
        return ToneVerdict(passed=True, reason="polite")
    if "Friday" in call.prompt:
        return ReplyReading(
            intent=ReplyIntent.PROMISE,
            promised_date=date(2026, 1, 9),
            confidence=0.95,
            summary="Promises to pay on Friday.",
        )
    return ReplyReading(
        intent=ReplyIntent.DISPUTE, confidence=0.9, summary="Says the goods were damaged."
    )


def test_reminder_promise_broken_promise_dispute(
    rt: Runtime, clock: FakeClock, tmp_path: Path
) -> None:
    rt.model = FakeModel(respond)
    rt.agent_model = ScriptedModel(
        [
            ToolCall("t1", "get_contact_history", {}),
            lambda received: FinalAnswer(
                "t2",
                {
                    "result": "dispute_needs_human",
                    "evidence_ids": [json.loads(received[-1].content)[-1]["id"]],  # type: ignore[union-attr]
                    "summary": "Customer says half the boxes arrived damaged.",
                },
            ),
        ]
    )
    rt.inbound_secret = SECRET
    client = TestClient(create_app(rt))
    aging = write_aging(tmp_path / "aging.csv", [invoice_row("A-1", "Asha", "1000", "2026-01-01")])
    make_tenant(rt, aging, approval_mode="none")

    def day(n: int) -> None:
        clock.set(datetime.combine(START.date() + timedelta(days=n), time(10, 0), tzinfo=IST))
        run_once(rt, timedelta(hours=1))

    def reply(text: str) -> None:
        with rt.session_factory() as session:
            token = session.scalars(
                select(Message.reply_token)
                .where(Message.direction == Direction.OUTBOUND)
                .order_by(Message.created_at.desc())
            ).first()
        raw = (
            f"From: asha@example.com\nTo: reply+{token}@replies.example.com\n"
            f"Subject: Re\nMessage-ID: <{text[:8]}@x>\n\n{text}\n"
        ).encode()
        signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        response = client.post(
            "/v1/inbound/email", content=raw, headers={"X-Parley-Signature": signature}
        )
        assert response.json()["outcome"] == "stored"

    def state() -> CaseState:
        with rt.session_factory() as session:
            return session.scalars(select(Case.state)).one()

    def outbound() -> list[Message]:
        with rt.session_factory() as session:
            return list(
                session.scalars(
                    select(Message)
                    .where(Message.direction == Direction.OUTBOUND)
                    .order_by(Message.created_at)
                )
            )

    # Round 1: the first reminder.
    day(0)
    [first] = outbound()
    assert (first.status, first.subject) == (MessageStatus.SENT, "Invoice A-1 (friendly)")
    assert "INR 1,000.00" in first.body

    # Round 2: a promise.
    day(1)
    reply("Sorry for the delay, will pay on Friday.")
    day(1)
    assert state() == CaseState.PROMISED

    # Round 3: the promise is broken; the next reminder waits out Sunday.
    for n in range(2, 7):  # Wed 7 .. Sun 11 Jan
        day(n)
    assert state() == CaseState.SCHEDULED
    with rt.session_factory() as session:
        assert session.scalars(select(Promise.status)).one() == PromiseStatus.BROKEN
    assert len(outbound()) == 1
    day(7)  # Mon 12 Jan
    second = outbound()[1]
    assert second.subject == "Invoice A-1 (firm)"
    assert second.created_at.astimezone(IST).date() == date(2026, 1, 12)

    # Round 4: a dispute stops outreach.
    day(8)
    reply("Half the boxes arrived damaged. We dispute this invoice.")
    for n in range(8, 20):
        day(n)
    assert state() == CaseState.NEEDS_HUMAN
    assert len(outbound()) == 2
    with rt.session_factory() as session:
        [task] = session.scalars(select(Task)).all()
        assert task.kind == TaskKind.REVIEW_DISPUTE
        assert task.agent_run_id is not None
        assert "Evidence: inbound message of 13 Jan 2026" in task.summary
