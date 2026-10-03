"""Database connections.

Usage pattern across the app:

    with session_factory.begin() as session:
        ...  # reads and writes

`begin()` opens a transaction that commits when the block ends normally and
rolls back if an exception escapes it, so a half-finished change is never saved.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

SessionFactory = sessionmaker[Session]


def make_engine(database_url: str) -> Engine:
    # pool_pre_ping checks a pooled connection is alive before using it, which
    # avoids errors after the database restarts.
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> SessionFactory:
    # expire_on_commit=False keeps loaded values readable after commit, so an API
    # handler can return an object it just saved.
    return sessionmaker(engine, expire_on_commit=False)
