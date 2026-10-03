"""GET /v1/metrics on a small worked scenario.

Two invoices due 1 Jan, reminders on Mon 5 Jan. Bala pays on 6 Jan without
replying. Asha replies on 6 Jan promising to pay on 7 Jan, and does.

Every fake model call reports 100 input and 50 output tokens. At $4/$20 per
million tokens for the large tier and $2/$10 for the small one, a large call
costs 1400 micro-dollars and a small call 700. The calls are: two drafts
(large), two tone checks (small), one reply reading and one brief update
(small) = 2*1400 + 4*700 = 5600, over 2 cases = 2800 per case.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from parley.adapters.clock import FakeClock
from parley.adapters.models.fake import FakeCall, FakeModel
from parley.api.app import create_app
from parley.collections_ai.jobs import BriefOut, DraftOut, ReplyReading, ToneVerdict
from parley.core.domain import ReplyIntent
from parley.db.models import Message
from parley.ports.channel import InboundMessage
from parley.services.inbound import receive_email
from parley.services.runtime import Runtime
from parley.services.tenants import create_tenant
from parley.services.worker import run_tenant_once
from tests.integration.conftest import IST, START, invoice_row, write_aging


def respond(call: FakeCall) -> DraftOut | ToneVerdict | ReplyReading | BriefOut:
    if call.output_type is DraftOut:
        invoice = json.loads(call.prompt)["invoices"][0]
        return DraftOut(
            subject=f"Invoice {invoice['number']}",
            body=f"Invoice {invoice['number']}: {invoice['amount_due']}, due {invoice['due_date']}",
        )
    if call.output_type is ToneVerdict:
        return ToneVerdict(passed=True, reason="ok")
    if call.output_type is BriefOut:
        return BriefOut(brief="Keeps promises.")
    return ReplyReading(
        intent=ReplyIntent.PROMISE,
        promised_date=date(2026, 1, 7),
        confidence=0.95,
        summary="Will pay on the 7th.",
    )


def test_metrics_report_collection_and_cost_per_case(
    rt: Runtime, clock: FakeClock, tmp_path: Path
) -> None:
    rt.model = FakeModel(respond)
    rt.model_prices = {"fake-large": (4.0, 20.0), "fake-small": (2.0, 10.0)}
    aging = tmp_path / "aging.csv"
    asha = invoice_row("A-1", "Asha", "1000", "2026-01-01")
    bala = invoice_row("B-1", "Bala", "700", "2026-01-01")
    write_aging(aging, [asha, bala])
    with rt.session_factory.begin() as session:
        tenant, key = create_tenant(
            session,
            "Acme",
            "Asia/Kolkata",
            {"kind": "csv", "invoices_path": str(aging)},
            {"approval_mode": "none"},
        )
        tenant_id = tenant.id

    def day(n: int) -> None:
        clock.set(datetime.combine(START.date() + timedelta(days=n), time(10, 0), tzinfo=IST))
        run_tenant_once(rt, tenant_id, None, timedelta(minutes=1))

    day(0)  # both reminded
    with rt.session_factory() as session:
        token = session.scalars(
            select(Message.reply_token).where(Message.to_address == "asha@example.com")
        ).one()
    receive_email(
        rt,
        InboundMessage(
            "<p@x>",
            "asha@example.com",
            (f"reply+{token}@replies.example.com",),
            "Re",
            "Will pay on the 7th",
        ),
    )
    write_aging(aging, [asha, {**bala, "amount_due": "0"}])
    day(1)  # promise recorded; Bala's invoice paid
    write_aging(aging, [{**asha, "amount_due": "0"}, {**bala, "amount_due": "0"}])
    day(2)  # Asha pays as promised

    client = TestClient(create_app(rt))
    m = client.get("/v1/metrics", headers={"Authorization": f"Bearer {key}"}).json()

    assert m["cases_by_state"] == {"closed": 2}
    assert (m["promises_made"], m["promises_kept"], m["promises_broken"]) == (1, 1, 0)
    assert m["promise_kept_rate"] == 1.0
    assert m["cases_collected"] == 2
    assert m["avg_days_to_collect"] == 5.5  # Bala 5 days after due, Asha 6
    assert m["model_cost_micro_usd"] == 5600
    assert m["cases_using_model"] == 2
    assert m["cost_per_case_micro_usd"] == 2800
    assert m["usage_by_tier"]["large"]["calls"] == 2
    assert m["usage_by_tier"]["small"]["calls"] == 4
    assert m["cache_read_share"] == 0.0
