"""M2 drafting, approvals, replies

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02 15:39:06.410161
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MESSAGE_STATUSES_V1 = ["pending", "sent", "failed"]
MESSAGE_STATUSES_V2 = [
    "drafting", "awaiting_approval", "pending", "sent", "failed", "rejected", "received", "read",
]  # fmt: skip


def _quoted(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "unmatched_inbound",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("to_addresses", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unmatched_inbound")),
    )
    op.create_table(
        "model_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_model_calls_message_id_messages")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_model_calls_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_calls")),
    )
    op.create_index(op.f("ix_model_calls_message_id"), "model_calls", ["message_id"], unique=False)
    op.create_index(op.f("ix_model_calls_tenant_id"), "model_calls", ["tenant_id"], unique=False)
    op.create_table(
        "disputes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "resolved",
                name="disputestatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name=op.f("fk_disputes_case_id_cases")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_disputes_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_disputes")),
    )
    op.create_index(op.f("ix_disputes_case_id"), "disputes", ["case_id"], unique=False)
    op.create_index(op.f("ix_disputes_tenant_id"), "disputes", ["tenant_id"], unique=False)
    op.add_column(
        "cases", sa.Column("extra_reminders", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("messages", sa.Column("from_address", sa.String(length=320), nullable=True))
    op.add_column("messages", sa.Column("reply_token", sa.String(length=64), nullable=True))
    op.create_unique_constraint(op.f("uq_messages_reply_token"), "messages", ["reply_token"])
    op.add_column("tasks", sa.Column("message_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_tasks_message_id_messages"), "tasks", "messages", ["message_id"], ["id"]
    )
    op.add_column("tenants", sa.Column("payment_link", sa.String(length=500), nullable=True))

    # Autogenerate does not see new enum values, so this CHECK is replaced by hand.
    op.drop_constraint(op.f("ck_messages_messagestatus"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_messagestatus"), "messages", f"status IN ({_quoted(MESSAGE_STATUSES_V2)})"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_messages_messagestatus"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_messagestatus"), "messages", f"status IN ({_quoted(MESSAGE_STATUSES_V1)})"
    )
    op.drop_column("tenants", "payment_link")
    op.drop_constraint(op.f("fk_tasks_message_id_messages"), "tasks", type_="foreignkey")
    op.drop_column("tasks", "message_id")
    op.drop_constraint(op.f("uq_messages_reply_token"), "messages", type_="unique")
    op.drop_column("messages", "reply_token")
    op.drop_column("messages", "from_address")
    op.drop_column("cases", "extra_reminders")
    op.drop_index(op.f("ix_disputes_tenant_id"), table_name="disputes")
    op.drop_index(op.f("ix_disputes_case_id"), table_name="disputes")
    op.drop_table("disputes")
    op.drop_index(op.f("ix_model_calls_tenant_id"), table_name="model_calls")
    op.drop_index(op.f("ix_model_calls_message_id"), table_name="model_calls")
    op.drop_table("model_calls")
    op.drop_table("unmatched_inbound")
