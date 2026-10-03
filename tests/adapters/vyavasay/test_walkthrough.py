"""M6 acceptance (spec, Milestones): the collections walkthrough with Vyavasay
as the source instead of a CSV file.

  Mon 5 Jan   two overdue invoices sync in. SI-2 has an open credit note, so
              the reminder asks only for what is left; one reminder covers both
  Tue 6 Jan   the customer promises to pay on Friday
  Fri 9 Jan   Vyavasay records a part payment on SI-1: the amount drops, the case
              stays open and the promise is still waiting
  Sat 10 Jan  the rest is paid: both cases close as paid and the promises are kept
"""

import hashlib
import hmac
import json
from datetime import date, datetime, time, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from parley.adapters.accounting.vyavasay import VyavasayAccountingAdapter, VyavasayClient
from parley.adapters.clock import FakeClock
from parley.adapters.models.fake import FakeCall, FakeModel
from parley.api.app import create_app
from parley.collections_ai.jobs import BriefOut, DraftOut, ReplyReading, ToneVerdict
from parley.core.domain import CaseState, CloseReason, Direction, PromiseStatus, ReplyIntent
from parley.db.models import Case, Invoice, Message, Promise, Tenant
from parley.services.runtime import Runtime
from parley.services.tenants import create_tenant
from parley.services.worker import run_once
from tests.adapters.vyavasay.fake_server import FakeVyavasay
from tests.integration.conftest import IST, START

SECRET = "walkthrough-secret"


def respond(call: FakeCall) -> DraftOut | ToneVerdict | ReplyReading | BriefOut:
    if call.output_type is BriefOut:
        return BriefOut(brief="Promises and pays.")
    if call.output_type is DraftOut:
        facts = json.loads(call.prompt)
        rows = "\n".join(
            f"Invoice {i['number']}: {i['amount_due']}, due {i['due_date']}."
            for i in facts["invoices"]
        )
        return DraftOut(subject="Payment reminder", body=f"Dear {facts['customer_name']},\n{rows}")
    if call.output_type is ToneVerdict:
        return ToneVerdict(passed=True, reason="polite")
    return ReplyReading(
        intent=ReplyIntent.PROMISE,
        promised_date=date(2026, 1, 9),
        confidence=0.95,
        summary="Promises to pay on Friday.",
    )


def test_walkthrough_with_vyavasay_as_the_source(rt: Runtime, clock: FakeClock) -> None:
    server = FakeVyavasay()
    server.add_party("p1", "Asha Traders", email="asha@example.com")
    server.add_invoice("i1", "SI-1", "p1", "1000.00", "2026-01-01")
    server.add_invoice("i2", "SI-2", "p1", "500.00", "2026-01-02")
    server.sale_returns.append(
        {
            "salesInvoiceId": "i2",
            "balanceAmount": "200.00",
            "applicationStatus": "open",
            "status": "posted",
            "currencyCode": "INR",
        }
    )

    def accounting_for(tenant: Tenant) -> VyavasayAccountingAdapter:
        # A fresh adapter per call, as in production, so credit notes are re-read.
        client = VyavasayClient(
            "https://vyavasay.test", token="api-token", transport=server.transport()
        )
        return VyavasayAccountingAdapter(client)

    rt.accounting_for = accounting_for
    rt.model = FakeModel(respond)
    rt.inbound_secret = SECRET
    with rt.session_factory.begin() as session:
        create_tenant(
            session,
            name="Acme Traders",
            timezone="Asia/Kolkata",
            adapter_config={"kind": "vyavasay", "base_url": "https://vyavasay.test"},
            policy_overrides={"approval_mode": "none"},
        )
    api = TestClient(create_app(rt))

    def day(n: int) -> None:
        clock.set(datetime.combine(START.date() + timedelta(days=n), time(10, 0), tzinfo=IST))
        run_once(rt, timedelta(hours=1))

    def cases() -> dict[str, Case]:
        with rt.session_factory() as session:
            rows = session.execute(select(Invoice.number, Case).join(Case.invoice)).all()
            return {number: case for number, case in rows}

    def outbound() -> list[Message]:
        with rt.session_factory() as session:
            query = select(Message).where(Message.direction == Direction.OUTBOUND)
            return list(session.scalars(query))

    # Mon: one reminder for both invoices, net of the credit note.
    day(0)
    [reminder] = outbound()
    assert "Invoice SI-1: INR 1,000.00" in reminder.body
    assert "Invoice SI-2: INR 300.00" in reminder.body

    # Tue: the customer promises Friday.
    raw = (
        f"From: asha@example.com\nTo: reply+{reminder.reply_token}@replies.example.com\n"
        "Subject: Re\nMessage-ID: <r1@x>\n\nWill pay on Friday.\n"
    ).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    response = api.post("/v1/inbound/email", content=raw, headers={"X-Parley-Signature": signature})
    assert response.json()["outcome"] == "stored"
    day(1)
    assert {c.state for c in cases().values()} == {CaseState.PROMISED}

    # Fri: a part payment shows up in Vyavasay.
    server.pay("i1", "600.00", "2026-01-09")
    day(4)
    with rt.session_factory() as session:
        amounts = dict(session.execute(select(Invoice.number, Invoice.amount_due)).all())
        assert amounts == {"SI-1": 40000, "SI-2": 30000}
    assert {c.state for c in cases().values()} == {CaseState.PROMISED}

    # Sat: the rest is paid, and every case closes.
    server.pay("i1", "400.00", "2026-01-10")
    server.pay("i2", "300.00", "2026-01-10")
    day(5)
    assert {(c.state, c.closed_reason) for c in cases().values()} == {
        (CaseState.CLOSED, CloseReason.PAID)
    }
    with rt.session_factory() as session:
        assert set(session.scalars(select(Promise.status))) == {PromiseStatus.KEPT}
    assert len(outbound()) == 1
