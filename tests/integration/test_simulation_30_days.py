"""M1 acceptance test (spec, Milestones): "A CSV of 20 invoices produces the right
cases and due actions across a simulated 30 days."

Approval is switched off and no model is configured, so reminders use the fixed
template and go out as soon as they are due. The worker runs once a day at 10:00
India time, from Monday 5 January 2026 to
Wednesday 4 February 2026, with the default policy:
first reminder 3 days after the due date, 5 days between reminders, at most 4
reminders, at most 2 messages per customer per week, no contact 20:00-09:00 or
on Sundays.

Expected results were worked out by hand from those rules:

  Asha    A-1        reminders 5, 10, 15, 20 Jan; escalated Sun 25 Jan
  Bala    B-1, B-2   same dates as Asha, both invoices in each message
  Chitra  C-1        5, 10 Jan; paid on 12 Jan, case closed
  Deepak  D-1        due 8 Jan; first reminder due Sun 11 Jan, so sent Mon 12;
                     then 17, 22, 27 Jan; escalated 1 Feb
  Esha    E-1        due 25 Jan; reminders 28 Jan and 2 Feb
  Farid   F-1        no email address: escalated on 5 Jan, nothing sent
  Gita    G-1        customer paused before the first run: nothing sent
  Hari    H-1, H-2   H-1 on 5 Jan, H-2 on 7 Jan; the weekly cap then holds H-1
                     until 12 Jan, when both go together; then 17 and 22 Jan;
                     both escalated 27 Jan
  Indira  I-1        5, 10 Jan; missing from the file from 15 Jan, case closed
  Jay     J-1        5, 10, 15, 20 Jan; part-paid on 12 Jan, so the 15 Jan
                     reminder shows the new amount; escalated 25 Jan
  Kiran   K-1        amount due 0: never a case
  Lata    L-1        due 10 Feb: never a case
  Mohan   M-1..M-3   5, 10, 15, 20 Jan, all three invoices together; escalated 25 Jan
  Nisha   N-1        case paused until 19 Jan; then 19, 24, 29 Jan and 3 Feb
  Om      O-1        due 10 Jan; 13 Jan; next due Sun 18 Jan so sent Mon 19;
                     then 24, 29 Jan; escalated 3 Feb
  Priya   P-1        5, 10, 15, 20 Jan; escalated 25 Jan; paid 28 Jan, so the
                     case closes and the escalation task is cancelled
"""

import csv
import shutil
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

from parley.adapters.clock import FakeClock
from parley.core.domain import CaseState, CloseReason, MessageStatus, TaskKind, TaskStatus
from parley.db.models import Case, Customer, Invoice, Message, MessageCase, Task
from parley.services.audit import find_policy_violations
from parley.services.cases import set_case_paused, set_customer_paused
from parley.services.runtime import Runtime
from parley.services.sync import sync_tenant
from parley.services.worker import run_once
from tests.integration.conftest import IST, START, make_tenant

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aging_20.csv"
SYNC_EVERY_RUN = timedelta(minutes=1)


def edit_aging(
    path: Path, number: str, amount_due: str | None = None, remove: bool = False
) -> None:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0])
    if remove:
        rows = [r for r in rows if r["invoice_number"] != number]
    else:
        for r in rows:
            if r["invoice_number"] == number:
                r["amount_due"] = amount_due or r["amount_due"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def jan(day: int) -> date:
    return date(2026, 1, day)


def feb(day: int) -> date:
    return date(2026, 2, day)


def test_twenty_invoices_over_thirty_days(rt: Runtime, clock: FakeClock, tmp_path: Path) -> None:
    aging = tmp_path / "aging.csv"
    shutil.copy(FIXTURE, aging)
    tenant_id = make_tenant(rt, aging, approval_mode="none")

    # Before the first run: pause customer Gita, and Nisha's case.
    sync_tenant(rt, tenant_id)
    with rt.session_factory.begin() as session:
        gita = session.scalars(select(Customer).where(Customer.external_id == "C07")).one()
        set_customer_paused(session, tenant_id, gita.id, paused=True)
        nisha_case = session.scalars(
            select(Case).join(Invoice).where(Invoice.number == "N-1")
        ).one()
        set_case_paused(session, tenant_id, nisha_case.id, paused=True)

    # Changes in the source system, applied on the morning of the given day.
    source_changes = {
        jan(12): lambda: (edit_aging(aging, "C-1", "0"), edit_aging(aging, "J-1", "400.00")),
        jan(15): lambda: edit_aging(aging, "I-1", remove=True),
        jan(28): lambda: edit_aging(aging, "P-1", "0"),
    }

    for day in range(31):
        today = START.date() + timedelta(days=day)
        clock.set(datetime.combine(today, time(10, 0), tzinfo=IST))
        if today in source_changes:
            source_changes[today]()
        if today == jan(19):
            with rt.session_factory.begin() as session:
                set_case_paused(session, tenant_id, nisha_case.id, paused=False)
        run_once(rt, SYNC_EVERY_RUN)

    with rt.session_factory() as session:
        _check_messages(session)
        _check_cases(session)
        _check_tasks(session)
        assert find_policy_violations(session, tenant_id) == []


def _check_messages(session) -> None:  # type: ignore[no-untyped-def]
    # customer name -> list of (date sent, invoice numbers in that message)
    sent: dict[str, list[tuple[date, list[str]]]] = defaultdict(list)
    rows = session.execute(
        select(Message, Customer.name, Invoice.number)
        .join(Customer, Customer.id == Message.customer_id)
        .join(MessageCase, MessageCase.message_id == Message.id)
        .join(Case, Case.id == MessageCase.case_id)
        .join(Invoice, Invoice.id == Case.invoice_id)
        .order_by(Message.created_at, Invoice.number)
    ).all()
    by_message: dict[object, tuple[str, date, list[str]]] = {}
    for message, customer_name, number in rows:
        assert message.status == MessageStatus.SENT
        local = message.created_at.astimezone(IST)
        # Never on a Sunday or in quiet hours.
        assert local.weekday() != 6
        assert time(9, 0) <= local.time() < time(20, 0)
        entry = by_message.setdefault(message.id, (customer_name.split()[0], local.date(), []))
        entry[2].append(number)
    for name, day, numbers in by_message.values():
        sent[name].append((day, numbers))

    five_reminders = [jan(5), jan(10), jan(15), jan(20)]
    expected: dict[str, list[tuple[date, list[str]]]] = {
        "Asha": [(d, ["A-1"]) for d in five_reminders],
        "Bala": [(d, ["B-1", "B-2"]) for d in five_reminders],
        "Chitra": [(jan(5), ["C-1"]), (jan(10), ["C-1"])],
        "Deepak": [(d, ["D-1"]) for d in (jan(12), jan(17), jan(22), jan(27))],
        "Esha": [(jan(28), ["E-1"]), (feb(2), ["E-1"])],
        "Hari": [
            (jan(5), ["H-1"]),
            (jan(7), ["H-2"]),
            (jan(12), ["H-1", "H-2"]),
            (jan(17), ["H-1", "H-2"]),
            (jan(22), ["H-1", "H-2"]),
        ],
        "Indira": [(jan(5), ["I-1"]), (jan(10), ["I-1"])],
        "Jay": [(d, ["J-1"]) for d in five_reminders],
        "Mohan": [(d, ["M-1", "M-2", "M-3"]) for d in five_reminders],
        "Nisha": [(d, ["N-1"]) for d in (jan(19), jan(24), jan(29), feb(3))],
        "Om": [(d, ["O-1"]) for d in (jan(13), jan(19), jan(24), jan(29))],
        "Priya": [(d, ["P-1"]) for d in five_reminders],
    }
    assert dict(sent) == expected

    # Jay part-paid on 12 Jan: later reminders use the new amount.
    jay_bodies = session.scalars(
        select(Message.body)
        .join(Customer, Customer.id == Message.customer_id)
        .where(Customer.name == "Jay Builders")
        .order_by(Message.created_at)
    ).all()
    assert "INR 1,000.00" in jay_bodies[1]
    assert "INR 400.00" in jay_bodies[2]

    # Tone steps up: friendly, firm, final, final.
    asha_bodies = session.scalars(
        select(Message.body)
        .join(Customer, Customer.id == Message.customer_id)
        .where(Customer.name == "Asha Stores")
        .order_by(Message.created_at)
    ).all()
    assert "friendly reminder" in asha_bodies[0]
    assert "still unpaid" in asha_bodies[1]
    assert "final reminder" in asha_bodies[2] and "final reminder" in asha_bodies[3]


def _check_cases(session) -> None:  # type: ignore[no-untyped-def]
    states = {
        number: (state, reason, reminders)
        for number, state, reason, reminders in session.execute(
            select(Invoice.number, Case.state, Case.closed_reason, Case.reminders_sent).join(
                Case, Case.invoice_id == Invoice.id
            )
        )
    }
    human = (CaseState.NEEDS_HUMAN, None, 4)
    assert states == {
        "A-1": human,
        "B-1": human,
        "B-2": human,
        "C-1": (CaseState.CLOSED, CloseReason.PAID, 2),
        "D-1": human,
        "E-1": (CaseState.AWAITING_REPLY, None, 2),
        "F-1": (CaseState.NEEDS_HUMAN, None, 0),
        "G-1": (CaseState.SCHEDULED, None, 0),
        "H-1": human,
        "H-2": human,
        "I-1": (CaseState.CLOSED, CloseReason.REMOVED, 2),
        "J-1": human,
        "M-1": human,
        "M-2": human,
        "M-3": human,
        "N-1": (CaseState.AWAITING_REPLY, None, 4),
        "O-1": human,
        "P-1": (CaseState.CLOSED, CloseReason.PAID, 4),
    }  # K-1 (paid) and L-1 (not yet due) never get a case


def _check_tasks(session) -> None:  # type: ignore[no-untyped-def]
    tasks = {
        (number, kind, status)
        for number, kind, status in session.execute(
            select(Invoice.number, Task.kind, Task.status)
            .join(Case, Case.id == Task.case_id)
            .join(Invoice, Invoice.id == Case.invoice_id)
        )
    }
    escalated = ["A-1", "B-1", "B-2", "D-1", "F-1", "H-1", "H-2", "J-1", "M-1", "M-2", "M-3", "O-1"]
    expected = {(n, TaskKind.ESCALATION, TaskStatus.OPEN) for n in escalated}
    expected.add(("P-1", TaskKind.ESCALATION, TaskStatus.CANCELLED))
    assert tasks == expected
