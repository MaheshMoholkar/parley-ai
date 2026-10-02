"""A throwaway database for evals that need one (investigator, personas).

Every example gets its own tenant, so examples can run in parallel in one
database without seeing each other. Point PARLEY_EVAL_DATABASE_URL at a
database you do not mind filling with eval data.
"""

import os
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config

from harness import AgentModel
from parley.adapters.channels.dry_run import DryRunChannel
from parley.adapters.clock import FakeClock
from parley.db.session import SessionFactory, make_engine, make_session_factory
from parley.ports.model import ModelPort
from parley.services.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "postgresql+psycopg://parley:parley@localhost:5432/parley_eval"


def eval_database_url() -> str:
    return os.environ.get("PARLEY_EVAL_DATABASE_URL", DEFAULT_URL)


def prepare_database(url: str | None = None) -> SessionFactory:
    """Bring the eval database up to the latest migration and connect to it."""
    url = url or eval_database_url()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "parley/db/migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return make_session_factory(make_engine(url))


def runtime(
    session_factory: SessionFactory,
    start: datetime,
    model: ModelPort | None = None,
    agent_model: AgentModel | None = None,
) -> Runtime:
    return Runtime(
        session_factory=session_factory,
        clock=FakeClock(start),
        channel=DryRunChannel(),
        model=model,
        agent_model=agent_model,
    )
