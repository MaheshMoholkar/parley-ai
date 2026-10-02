from pathlib import Path

from sqlalchemy import select

from parley.adapters.channels.dry_run import DryRunChannel
from parley.core.domain import CaseState, MessageStatus, TaskKind
from parley.db.models import Case, Message, Task
from parley.ports.channel import OutboundMessage
from parley.services.delivery import MAX_ATTEMPTS, deliver_pending_messages
from parley.services.due_cases import run_due_cases
from parley.services.runtime import Runtime
from parley.services.sync import sync_tenant
from tests.integration.conftest import invoice_row, make_tenant, write_aging

ROW = invoice_row("A-1", "Asha", "1000", "2026-01-01")


def queue_one_reminder(rt: Runtime, tmp_path: Path) -> None:
    tenant_id = make_tenant(rt, write_aging(tmp_path / "aging.csv", [ROW]))
    sync_tenant(rt, tenant_id)
    run_due_cases(rt, tenant_id)


def the_message(rt: Runtime) -> Message:
    with rt.session_factory() as session:
        return session.scalars(select(Message)).one()


def test_pending_message_is_sent_once(rt: Runtime, tmp_path: Path, channel: DryRunChannel) -> None:
    queue_one_reminder(rt, tmp_path)
    tenant_id = the_message(rt).tenant_id

    assert deliver_pending_messages(rt, tenant_id) == 1
    assert deliver_pending_messages(rt, tenant_id) == 0  # nothing left pending

    message = the_message(rt)
    assert message.status == MessageStatus.SENT
    assert message.provider_message_id == f"dry-run:{message.idempotency_key}"
    assert list(channel.sent) == [message.idempotency_key]


class FailingChannel:
    name = "failing"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: OutboundMessage) -> str:
        self.calls += 1
        raise ConnectionError("provider unavailable")


def test_failed_send_is_retried_then_handed_to_a_human(rt: Runtime, tmp_path: Path) -> None:
    queue_one_reminder(rt, tmp_path)
    tenant_id = the_message(rt).tenant_id
    rt.channel = failing = FailingChannel()

    for _ in range(MAX_ATTEMPTS - 1):
        deliver_pending_messages(rt, tenant_id)
        assert the_message(rt).status == MessageStatus.PENDING

    deliver_pending_messages(rt, tenant_id)

    message = the_message(rt)
    assert failing.calls == MAX_ATTEMPTS
    assert message.status == MessageStatus.FAILED
    assert message.last_error == "provider unavailable"
    with rt.session_factory() as session:
        assert session.scalars(select(Case)).one().state == CaseState.NEEDS_HUMAN
        assert session.scalars(select(Task)).one().kind == TaskKind.ESCALATION


class SendsThenCrashes:
    """The provider accepts the message, then our process fails before saving."""

    name = "flaky"

    def __init__(self, provider: DryRunChannel) -> None:
        self.provider = provider
        self.crash = True

    def send(self, message: OutboundMessage) -> str:
        provider_id = self.provider.send(message)
        if self.crash:
            self.crash = False
            raise TimeoutError("lost the response")
        return provider_id


def test_retry_after_a_lost_response_reuses_the_idempotency_key(
    rt: Runtime, tmp_path: Path, channel: DryRunChannel
) -> None:
    queue_one_reminder(rt, tmp_path)
    tenant_id = the_message(rt).tenant_id
    rt.channel = SendsThenCrashes(channel)

    deliver_pending_messages(rt, tenant_id)
    deliver_pending_messages(rt, tenant_id)

    assert the_message(rt).status == MessageStatus.SENT
    assert len(channel.sent) == 1  # the provider saw the same key twice and kept one
