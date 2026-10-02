"""Fixtures for tests that need PostgreSQL.

The schema is built by running the real migrations once per test session, and
every table is emptied after each test. Point PARLEY_TEST_DATABASE_URL at a
throwaway database; its contents are deleted.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from parley.adapters.channels.dry_run import DryRunChannel
from parley.adapters.clock import FakeClock
from parley.db.models import Base
from parley.db.session import SessionFactory, make_engine, make_session_factory
from parley.services.runtime import Runtime
from parley.services.tenants import create_tenant

TEST_DATABASE_URL = os.environ.get(
    "PARLEY_TEST_DATABASE_URL", "postgresql+psycopg://parley:parley@localhost:5432/parley_test"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
IST = ZoneInfo("Asia/Kolkata")
# Monday 5 January 2026, 10:00 in India.
START = datetime(2026, 1, 5, 10, 0, tzinfo=IST)

INVOICE_HEADER = [
    "invoice_number",
    "customer_id",
    "customer_name",
    "email",
    "phone",
    "amount_due",
    "currency",
    "due_date",
    "details",
]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "parley/db/migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    engine = make_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[SessionFactory]:
    yield make_session_factory(engine)
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def channel() -> DryRunChannel:
    return DryRunChannel()


@pytest.fixture
def rt(session_factory: SessionFactory, clock: FakeClock, channel: DryRunChannel) -> Runtime:
    return Runtime(session_factory=session_factory, clock=clock, channel=channel)


def invoice_row(number: str, customer: str, amount_due: str, due_date: str) -> dict[str, str]:
    """One aging-report row; `write_aging` fills in the other columns."""
    return {
        "invoice_number": number,
        "customer_name": customer,
        "amount_due": amount_due,
        "due_date": due_date,
    }


def write_aging(path: Path, rows: list[dict[str, str]]) -> Path:
    """Write an aging-report CSV. Each row needs at least invoice_number,
    customer_name, amount_due and due_date; other columns get sensible defaults."""
    lines = [",".join(INVOICE_HEADER)]
    for row in rows:
        full = {"currency": "INR", "email": f"{row['customer_name'].lower()}@example.com", **row}
        lines.append(",".join(full.get(col, "") for col in INVOICE_HEADER))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_tenant(rt: Runtime, invoices_path: Path, **policy: Any) -> uuid.UUID:
    with rt.session_factory.begin() as session:
        tenant, _ = create_tenant(
            session,
            name="Acme Traders",
            timezone="Asia/Kolkata",
            adapter_config={"kind": "csv", "invoices_path": str(invoices_path)},
            policy_overrides=policy,
        )
        return tenant.id
