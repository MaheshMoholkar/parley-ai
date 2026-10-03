"""Events in (`POST /v1/events`) and webhooks out (the outbound events outbox)."""

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from parley.adapters.clock import FakeClock
from parley.api.app import create_app
from parley.core.domain import WebhookStatus
from parley.db.models import Invoice, OutboundEvent, Tenant
from parley.services.runtime import Runtime
from parley.services.tenants import create_tenant, set_webhook_url
from parley.services.webhooks import MAX_ATTEMPTS, deliver_webhooks
from parley.services.worker import run_once
from tests.integration.conftest import invoice_row, write_aging

DAY = timedelta(days=1)


@dataclass
class Receiver:
    """A webhook receiver that records what it is sent and answers `status`."""

    status: int = 200
    posts: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=list)

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        self.posts.append((url, body, headers))
        return self.status

    def types(self) -> list[str]:
        return [json.loads(body)["type"] for _, body, _ in self.posts]


@dataclass
class Setup:
    tenant_id: uuid.UUID
    api_key: str
    secret: str
    aging: Path


def setup_tenant(rt: Runtime, tmp_path: Path, webhook_url: str | None = None) -> Setup:
    aging = write_aging(tmp_path / "aging.csv", [invoice_row("A-1", "Asha", "1000", "2026-01-01")])
    with rt.session_factory.begin() as session:
        tenant, api_key = create_tenant(
            session,
            "Acme",
            "Asia/Kolkata",
            {"kind": "csv", "invoices_path": str(aging)},
            policy_overrides={"approval_mode": "none"},
            webhook_url=webhook_url,
        )
        return Setup(tenant.id, api_key, tenant.webhook_secret, aging)


def signed(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- Events in -------------------------------------------------------------------------


def post_event(client: TestClient, setup: Setup, body: bytes, signature: str | None = None) -> int:
    headers = {"Authorization": f"Bearer {setup.api_key}"}
    headers["X-Parley-Signature"] = signature or signed(body, setup.secret)
    response = client.post("/v1/events", content=body, headers=headers)
    return response.status_code if response.status_code != 202 else response.json()["outcome"]


def test_events_need_the_api_key_and_a_valid_signature(rt: Runtime, tmp_path: Path) -> None:
    setup = setup_tenant(rt, tmp_path)
    client = TestClient(create_app(rt))
    body = json.dumps({"id": "e1", "type": "invoice.updated"}).encode()

    assert client.post("/v1/events", content=body).status_code == 401
    assert post_event(client, setup, body, signature="sha256=00") == 401
    assert post_event(client, setup, body, signature=signed(body, "other secret")) == 401
    bad_type = json.dumps({"id": "e2", "type": "invoice.deleted"}).encode()
    assert post_event(client, setup, bad_type) == 422

    assert post_event(client, setup, body) == "accepted"
    assert post_event(client, setup, body) == "duplicate"


def test_an_event_makes_the_next_worker_round_sync(
    rt: Runtime, clock: FakeClock, tmp_path: Path
) -> None:
    setup = setup_tenant(rt, tmp_path)
    client = TestClient(create_app(rt))
    run_once(rt, sync_interval=DAY)  # first sync

    def amount_due() -> int:
        with rt.session_factory() as session:
            return session.scalars(select(Invoice.amount_due)).one()

    write_aging(setup.aging, [invoice_row("A-1", "Asha", "400", "2026-01-01")])
    clock.advance(timedelta(minutes=5))
    run_once(rt, sync_interval=DAY)
    assert amount_due() == 100000  # the interval has not passed, so no sync

    body = json.dumps({"id": "pay-1", "type": "payment.recorded", "data": {"id": "p1"}}).encode()
    assert post_event(client, setup, body) == "accepted"
    clock.advance(timedelta(minutes=1))
    run_once(rt, sync_interval=DAY)
    assert amount_due() == 40000

    # The event is used up: the next round does not sync again.
    with rt.session_factory() as session:
        synced_at = session.get_one(Tenant, setup.tenant_id).last_synced_at
    clock.advance(timedelta(minutes=1))
    run_once(rt, sync_interval=DAY)
    with rt.session_factory() as session:
        assert session.get_one(Tenant, setup.tenant_id).last_synced_at == synced_at


# --- Webhooks out ----------------------------------------------------------------------


def test_events_are_signed_and_posted_once(rt: Runtime, tmp_path: Path) -> None:
    receiver = Receiver()
    rt.post_webhook = receiver
    setup = setup_tenant(rt, tmp_path, webhook_url="https://host.example/hooks")

    run_once(rt, sync_interval=DAY)  # opens the case and sends the first reminder
    run_once(rt, sync_interval=DAY)

    assert receiver.types() == ["case.opened", "message.sent"]
    url, body, headers = receiver.posts[0]
    event = json.loads(body)
    assert url == "https://host.example/hooks"
    assert headers["X-Parley-Event-Id"] == event["id"]
    assert headers["X-Parley-Event-Type"] == "case.opened"
    assert headers["X-Parley-Signature"] == signed(body, setup.secret)
    assert event["data"]["invoice_number"] == "A-1"
    assert event["data"]["amount_due"] == 100000


def test_a_failing_receiver_is_retried_with_backoff_then_given_up(
    rt: Runtime, clock: FakeClock, tmp_path: Path
) -> None:
    receiver = Receiver(status=500)
    rt.post_webhook = receiver
    setup = setup_tenant(rt, tmp_path, webhook_url="https://host.example/hooks")
    run_once(rt, sync_interval=DAY)
    # A round stops at the first failure, so a dead receiver gets one attempt per round.
    assert receiver.types() == ["case.opened"]

    deliver_webhooks(rt, setup.tenant_id)
    assert receiver.types() == ["case.opened", "message.sent"]  # case.opened is not due yet

    clock.advance(timedelta(minutes=1))
    deliver_webhooks(rt, setup.tenant_id)
    assert len(receiver.posts) == 3  # one retry: the round stops at its first failure

    receiver.status = 204
    clock.advance(timedelta(minutes=2))
    assert deliver_webhooks(rt, setup.tenant_id) == 2  # both events now go through
    assert len({json.loads(b)["id"] for _, b, _ in receiver.posts}) == 2

    # A receiver that never answers: given up after MAX_ATTEMPTS.
    def down(url: str, body: bytes, headers: dict[str, str]) -> int:
        raise ConnectionError("connection refused")

    rt.post_webhook = down
    write_aging(setup.aging, [])  # the invoice is gone: case.closed
    run_once(rt, sync_interval=timedelta(0))
    for _ in range(MAX_ATTEMPTS):
        clock.advance(timedelta(hours=6))
        deliver_webhooks(rt, setup.tenant_id)
    with rt.session_factory() as session:
        closed = session.scalars(
            select(OutboundEvent).where(OutboundEvent.type == "case.closed")
        ).one()
    assert (closed.status, closed.attempts) == (WebhookStatus.FAILED, MAX_ATTEMPTS)
    assert closed.last_error == "ConnectionError: connection refused"


def test_no_webhook_url_means_no_events(rt: Runtime, tmp_path: Path) -> None:
    setup = setup_tenant(rt, tmp_path)
    run_once(rt, sync_interval=DAY)
    with rt.session_factory() as session:
        assert session.scalars(select(OutboundEvent)).all() == []

    with rt.session_factory.begin() as session:
        set_webhook_url(session, setup.tenant_id, "https://host.example/hooks")
    with rt.session_factory.begin() as session, pytest.raises(ValueError, match="https"):
        set_webhook_url(session, setup.tenant_id, "ftp://host.example")
