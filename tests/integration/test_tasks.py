"""Resolving tasks through the API: approving drafts, resuming and closing cases."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from parley.adapters.clock import FakeClock
from parley.api.app import create_app
from parley.core.domain import CaseState, CloseReason, MessageStatus, TaskStatus
from parley.db.models import Case, Message
from parley.services.runtime import Runtime
from parley.services.tenants import create_tenant
from parley.services.worker import run_tenant_once
from tests.integration.conftest import invoice_row, write_aging

ROW = invoice_row("A-1", "Asha", "1234.50", "2026-01-01")


@pytest.fixture
def client(rt: Runtime) -> TestClient:
    return TestClient(create_app(rt))


def new_tenant(rt: Runtime, tmp_path: Path, **policy: object) -> tuple[uuid.UUID, dict[str, str]]:
    aging = write_aging(tmp_path / "aging.csv", [ROW])
    with rt.session_factory.begin() as session:
        tenant, key = create_tenant(
            session, "Acme", "Asia/Kolkata", {"kind": "csv", "invoices_path": str(aging)}, policy
        )
    return tenant.id, {"Authorization": f"Bearer {key}"}


def run_round(rt: Runtime, tenant_id: uuid.UUID) -> None:
    run_tenant_once(rt, tenant_id, None, timedelta(hours=1))


def open_task(client: TestClient, auth: dict[str, str]) -> dict[str, str]:
    [task] = client.get("/v1/tasks", headers=auth).json()["items"]
    return task  # type: ignore[no-any-return]


def message_status(rt: Runtime) -> MessageStatus:
    with rt.session_factory() as session:
        return session.scalars(select(Message.status)).one()


def the_case(rt: Runtime) -> Case:
    with rt.session_factory() as session:
        return session.scalars(select(Case)).one()


def resolve(client: TestClient, auth: dict[str, str], task_id: str, **body: str):  # type: ignore[no-untyped-def]
    return client.post(f"/v1/tasks/{task_id}/resolve", headers=auth, json=body)


def test_approve_sends_the_draft(client: TestClient, rt: Runtime, tmp_path: Path) -> None:
    tenant_id, auth = new_tenant(rt, tmp_path)
    run_round(rt, tenant_id)
    task = open_task(client, auth)
    assert task["kind"] == "approve_send"

    response = resolve(client, auth, task["id"], action="approve")

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert message_status(rt) == MessageStatus.PENDING
    run_round(rt, tenant_id)
    assert message_status(rt) == MessageStatus.SENT


def test_an_edit_with_a_wrong_amount_is_refused(
    client: TestClient, rt: Runtime, tmp_path: Path
) -> None:
    tenant_id, auth = new_tenant(rt, tmp_path)
    run_round(rt, tenant_id)
    task = open_task(client, auth)

    bad = resolve(
        client,
        auth,
        task["id"],
        action="edit",
        subject="Invoice A-1",
        body="Please pay INR 999.00 for invoice A-1, due 01 Jan 2026.",
    )
    assert bad.status_code == 409
    assert "amount 'INR 999.00' does not match any amount due" in bad.json()["detail"]
    assert message_status(rt) == MessageStatus.AWAITING_APPROVAL

    good = resolve(
        client,
        auth,
        task["id"],
        action="edit",
        subject="Invoice A-1",
        body="Please pay INR 1,234.50 for invoice A-1, due 01 Jan 2026.",
    )
    assert good.status_code == 200
    assert message_status(rt) == MessageStatus.PENDING


def test_reject_skips_the_reminder(
    client: TestClient, rt: Runtime, tmp_path: Path, clock: FakeClock
) -> None:
    tenant_id, auth = new_tenant(rt, tmp_path)
    run_round(rt, tenant_id)

    resolve(client, auth, open_task(client, auth)["id"], action="reject", note="spoke by phone")

    assert message_status(rt) == MessageStatus.REJECTED
    # The case carries on: after the gap, the next reminder is queued.
    clock.advance(timedelta(days=5))
    run_round(rt, tenant_id)
    with rt.session_factory() as session:
        assert len(session.scalars(select(Message)).all()) == 2
    assert the_case(rt).reminders_sent == 2


def test_resuming_an_escalation_allows_one_more_reminder(
    client: TestClient, rt: Runtime, tmp_path: Path
) -> None:
    tenant_id, auth = new_tenant(rt, tmp_path, max_reminders=1, approval_mode="none")
    run_round(rt, tenant_id)  # reminder 1
    with rt.session_factory.begin() as session:  # pretend the reply wait is over
        session.scalars(select(Case)).one().next_action_at = rt.clock.now()
    run_round(rt, tenant_id)  # limit reached: escalated
    assert the_case(rt).state == CaseState.NEEDS_HUMAN

    resolve(client, auth, open_task(client, auth)["id"], action="resume")
    run_round(rt, tenant_id)

    case = the_case(rt)
    assert (case.state, case.reminders_sent, case.extra_reminders) == (
        CaseState.AWAITING_REPLY,
        2,
        1,
    )


def test_close_stops_chasing_and_sync_does_not_reopen(
    client: TestClient, rt: Runtime, tmp_path: Path
) -> None:
    tenant_id, auth = new_tenant(rt, tmp_path, max_reminders=1, approval_mode="none")
    run_round(rt, tenant_id)
    with rt.session_factory.begin() as session:
        session.scalars(select(Case)).one().next_action_at = rt.clock.now()
    run_round(rt, tenant_id)
    task = open_task(client, auth)

    resolve(client, auth, task["id"], action="close", note="written off")
    run_round(rt, tenant_id)  # syncs again; the invoice is still open in the file

    case = the_case(rt)
    assert (case.state, case.closed_reason) == (CaseState.CLOSED, CloseReason.HUMAN)
    assert (
        client.get("/v1/tasks", headers=auth, params={"status": "resolved"}).json()["items"][0][
            "resolution"
        ]
        == "close: written off"
    )


def test_a_task_can_only_be_resolved_once(client: TestClient, rt: Runtime, tmp_path: Path) -> None:
    tenant_id, auth = new_tenant(rt, tmp_path)
    run_round(rt, tenant_id)
    task_id = open_task(client, auth)["id"]
    assert resolve(client, auth, task_id, action="approve").status_code == 200
    again = resolve(client, auth, task_id, action="reject")
    assert again.status_code == 409
    assert again.json()["detail"] == [f"task is already {TaskStatus.RESOLVED}"]


def test_wrong_action_for_the_task_kind_is_refused(
    client: TestClient, rt: Runtime, tmp_path: Path
) -> None:
    tenant_id, auth = new_tenant(rt, tmp_path)
    run_round(rt, tenant_id)
    response = resolve(client, auth, open_task(client, auth)["id"], action="resume")
    assert response.status_code == 409
