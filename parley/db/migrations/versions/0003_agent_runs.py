"""agent runs

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02 16:11:47.873957
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column(
            "claim",
            sa.Enum(
                "promise",
                "dispute",
                "paid_claim",
                "question",
                "wrong_contact",
                "out_of_office",
                "other",
                name="replyintent",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "done",
                "cancelled",
                name="agentrunstatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column(
            "result",
            sa.Enum(
                "payment_found",
                "partial_payment",
                "payment_not_found",
                "dispute_needs_human",
                "unclear",
                name="findingresult",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("finding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["case_id"], ["cases.id"], name=op.f("fk_agent_runs_case_id_cases")
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_agent_runs_message_id_messages")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_agent_runs_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index(op.f("ix_agent_runs_case_id"), "agent_runs", ["case_id"], unique=False)
    op.create_index(
        "ix_agent_runs_queue", "agent_runs", ["tenant_id", "status", "created_at"], unique=False
    )
    op.create_index(op.f("ix_agent_runs_tenant_id"), "agent_runs", ["tenant_id"], unique=False)
    op.add_column("tasks", sa.Column("agent_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_tasks_agent_run_id_agent_runs"), "tasks", "agent_runs", ["agent_run_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_tasks_agent_run_id_agent_runs"), "tasks", type_="foreignkey")
    op.drop_column("tasks", "agent_run_id")
    op.drop_index(op.f("ix_agent_runs_tenant_id"), table_name="agent_runs")
    op.drop_index("ix_agent_runs_queue", table_name="agent_runs")
    op.drop_index(op.f("ix_agent_runs_case_id"), table_name="agent_runs")
    op.drop_table("agent_runs")
