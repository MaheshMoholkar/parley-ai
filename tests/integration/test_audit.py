from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from parley.adapters.clock import FakeClock
from parley.core.domain import DisputeStatus
from parley.db.models import Case, Dispute, Message
from parley.services.audit import find_policy_violations
from parley.services.runtime import Runtime
from parley.services.worker import run_tenant_once
from tests.integration.conftest import IST, invoice_row, make_tenant, write_aging


def one_sent_reminder(rt: Runtime, tmp_path: Path) -> tuple:  # type: ignore[type-arg]
    aging = write_aging(tmp_path / "aging.csv", [invoice_row("A-1", "Asha", "1000", "2026-01-01")])
    tenant_id = make_tenant(rt, aging, approval_mode="none")
    run_tenant_once(rt, tenant_id, None, timedelta(hours=1))
    return tenant_id


def test_a_clean_log_has_no_violations(rt: Runtime, tmp_path: Path) -> None:
    tenant_id = one_sent_reminder(rt, tmp_path)
    with rt.session_factory() as session:
        assert find_policy_violations(session, tenant_id) == []


def test_tampered_messages_are_caught(rt: Runtime, tmp_path: Path, clock: FakeClock) -> None:
    tenant_id = one_sent_reminder(rt, tmp_path)
    with rt.session_factory.begin() as session:
        message = session.scalars(select(Message)).one()
        message.body = message.body.replace("INR 1,000.00", "INR 1,500.00")
        message.sent_at = datetime(2026, 1, 4, 11, 0, tzinfo=IST)  # a Sunday
        case = session.scalars(select(Case)).one()
        session.add(
            Dispute(
                tenant_id=tenant_id,
                case_id=case.id,
                reason="x",
                status=DisputeStatus.OPEN,
                created_at=datetime(2026, 1, 3, tzinfo=IST),
            )
        )

    with rt.session_factory() as session:
        problems = find_policy_violations(session, tenant_id)
    assert any("quiet hours" in p for p in problems)
    assert any("does not match any amount due" in p for p in problems)
    assert any("open dispute" in p for p in problems)
