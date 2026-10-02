"""amount due on message links

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02 16:27:12.285238
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_cases", sa.Column("amount_due", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("message_cases", "amount_due")
