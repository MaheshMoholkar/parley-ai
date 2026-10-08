"""Write-back to Vyavasay (spec: "Write-back"): notes about collection activity
on each invoice, through the collection-activity endpoint the spec asks Vyavasay to
add. Runs against the fake server."""

from datetime import datetime, time, timedelta

from sqlalchemy import select

from parley.adapters.accounting.vyavasay import VyavasayAccountingAdapter, VyavasayClient
from parley.adapters.clock import FakeClock
from parley.core.domain import NoteStatus
from parley.db.models import SourceNote, Tenant
from parley.ports.accounting import AccountingPort
from parley.services.notes import MAX_ATTEMPTS, write_source_notes
from parley.services.runtime import Runtime
from parley.services.tenants import create_tenant
from parley.services.worker import run_once
from tests.adapters.vyavasay.fake_server import FakeVyavasay
from tests.integration.conftest import IST, START

HOUR = timedelta(hours=1)


def setup(rt: Runtime, server: FakeVyavasay, write_notes: bool = True) -> Tenant:
    server.add_party("p1", "Asha Traders", email="asha@example.com")
    server.add_invoice("i1", "SI-1", "p1", "1000.00", "2026-01-01")

    def accounting_for(tenant: Tenant) -> AccountingPort:
        client = VyavasayClient(
            "https://vyavasay.test", token="api-token", transport=server.transport()
        )
        return VyavasayAccountingAdapter(client)

    rt.accounting_for = accounting_for
    with rt.session_factory.begin() as session:
        tenant, _ = create_tenant(
            session,
            name="Acme Traders",
            timezone="Asia/Kolkata",
            adapter_config={"kind": "vyavasay", "base_url": "x", "write_notes": write_notes},
            policy_overrides={"approval_mode": "none"},
        )
        return tenant


def notes(rt: Runtime) -> list[SourceNote]:
    with rt.session_factory() as session:
        return list(session.scalars(select(SourceNote).order_by(SourceNote.seq)))


def test_collection_activity_is_noted_on_the_invoice(rt: Runtime, clock: FakeClock) -> None:
    server = FakeVyavasay(notes_endpoint=True)
    setup(rt, server)

    run_once(rt, HOUR)  # case opens, reminder 1 goes out
    assert server.notes == [
        ("i1", "Collections: the invoice is overdue; reminders will follow the usual schedule."),
        ("i1", "Collections: reminder sent by email."),
    ]

    server.pay("i1", "1000.00", "2026-01-06")
    clock.set(datetime.combine(START.date() + timedelta(days=1), time(10), tzinfo=IST))
    run_once(rt, timedelta(0))
    assert server.notes[-1] == ("i1", "Collections: closed, the invoice is paid.")
    assert {n.status for n in notes(rt)} == {NoteStatus.WRITTEN}


def test_a_source_that_is_down_gets_each_note_once_it_is_back(
    rt: Runtime, clock: FakeClock
) -> None:
    server = FakeVyavasay(notes_endpoint=True, notes_down=True)
    tenant = setup(rt, server)
    run_once(rt, HOUR)
    assert server.notes == []
    first = notes(rt)[0]
    assert (first.status, first.attempts) == (NoteStatus.PENDING, 1)
    assert "503" in (first.last_error or "")

    server.notes_down = False
    clock.advance(timedelta(minutes=1))
    assert write_source_notes(rt, tenant.id) == 2
    assert len(server.notes) == 2  # each once: the idempotency key stops repeats


def test_until_vyavasay_has_the_endpoint_notes_fail_without_retrying(rt: Runtime) -> None:
    server = FakeVyavasay(notes_endpoint=False)
    setup(rt, server)
    run_once(rt, HOUR)
    failed = notes(rt)
    assert {n.status for n in failed} == {NoteStatus.FAILED}
    assert all(n.attempts == 1 for n in failed)
    assert "no notes endpoint yet" in (failed[0].last_error or "")


def test_write_back_is_off_unless_the_tenant_turns_it_on(rt: Runtime) -> None:
    server = FakeVyavasay(notes_endpoint=True)
    setup(rt, server, write_notes=False)
    run_once(rt, HOUR)
    assert notes(rt) == [] and server.notes == []


def test_a_note_is_given_up_after_the_last_retry(rt: Runtime, clock: FakeClock) -> None:
    server = FakeVyavasay(notes_endpoint=True, notes_down=True)
    tenant = setup(rt, server)
    run_once(rt, HOUR)
    for _ in range(MAX_ATTEMPTS):
        clock.advance(timedelta(hours=6))
        write_source_notes(rt, tenant.id)
    first = notes(rt)[0]
    assert (first.status, first.attempts) == (NoteStatus.FAILED, MAX_ATTEMPTS)
