"""Review fixes: messages can be withdrawn; a call's audio connects once

Messages can be withdrawn: queued, but no longer right to send. Calls record
when their audio stream connected, so a call accepts only one.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-04 08:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MESSAGE_STATUSES_V2 = [
    "drafting", "awaiting_approval", "pending", "sent", "failed", "rejected", "received", "read",
]  # fmt: skip
MESSAGE_STATUSES_V3 = [*MESSAGE_STATUSES_V2, "withdrawn"]


def _quoted(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _message_statuses(values: list[str]) -> None:
    op.drop_constraint(op.f("ck_messages_messagestatus"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_messagestatus"), "messages", f"status IN ({_quoted(values)})"
    )


def upgrade() -> None:
    _message_statuses(MESSAGE_STATUSES_V3)
    op.add_column("calls", sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "connected_at")
    op.execute("UPDATE messages SET status = 'rejected' WHERE status = 'withdrawn'")
    _message_statuses(MESSAGE_STATUSES_V2)
