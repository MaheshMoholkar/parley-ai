"""initial tables

Revision ID: 0001
Revises:
Create Date: 2026-10-02 14:42:31.895023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("default_language", sa.String(length=16), nullable=False),
        sa.Column("policy_overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("adapter_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("api_key_hash", name=op.f("uq_tenants_api_key_hash")),
    )
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_customers_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint(
            "tenant_id",
            "source",
            "external_id",
            name=op.f("uq_customers_tenant_id_source_external_id"),
        ),
    )
    op.create_index(op.f("ix_customers_tenant_id"), "customers", ["tenant_id"], unique=False)
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(length=100), nullable=False),
        sa.Column("amount_due", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "paid",
                "void",
                "removed",
                name="invoicestatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("display_details", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_due >= 0", name=op.f("ck_invoices_amount_due_not_negative")),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_invoices_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_invoices_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
        sa.UniqueConstraint(
            "tenant_id",
            "source",
            "external_id",
            name=op.f("uq_invoices_tenant_id_source_external_id"),
        ),
    )
    op.create_index(op.f("ix_invoices_customer_id"), "invoices", ["customer_id"], unique=False)
    op.create_index(op.f("ix_invoices_tenant_id"), "invoices", ["tenant_id"], unique=False)
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column(
            "direction",
            sa.Enum(
                "outbound",
                "inbound",
                name="direction",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("to_address", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                name="messagestatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("provider_message_id", sa.String(length=300), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_messages_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_messages_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_messages_idempotency_key")),
    )
    op.create_index(op.f("ix_messages_customer_id"), "messages", ["customer_id"], unique=False)
    op.create_index(
        "ix_messages_outbox", "messages", ["tenant_id", "status", "created_at"], unique=False
    )
    op.create_index(op.f("ix_messages_tenant_id"), "messages", ["tenant_id"], unique=False)
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("reference", sa.String(length=300), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_payments_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_payments_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint(
            "tenant_id",
            "source",
            "external_id",
            name=op.f("uq_payments_tenant_id_source_external_id"),
        ),
    )
    op.create_index(op.f("ix_payments_customer_id"), "payments", ["customer_id"], unique=False)
    op.create_index(op.f("ix_payments_tenant_id"), "payments", ["tenant_id"], unique=False)
    op.create_table(
        "cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "scheduled",
                "awaiting_reply",
                "promised",
                "investigating",
                "needs_human",
                "closed",
                name="casestate",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminders_sent", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "closed_reason",
            sa.Enum(
                "paid",
                "void",
                "removed",
                "human",
                name="closereason",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.CheckConstraint(
            "reminders_sent >= 0", name=op.f("ck_cases_reminders_sent_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_cases_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], name=op.f("fk_cases_invoice_id_invoices")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_cases_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cases")),
        sa.UniqueConstraint("invoice_id", name=op.f("uq_cases_invoice_id")),
    )
    op.create_index(op.f("ix_cases_customer_id"), "cases", ["customer_id"], unique=False)
    op.create_index("ix_cases_due", "cases", ["tenant_id", "state", "next_action_at"], unique=False)
    op.create_index(op.f("ix_cases_tenant_id"), "cases", ["tenant_id"], unique=False)
    op.create_table(
        "message_cases",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("reminder_number", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["case_id"], ["cases.id"], name=op.f("fk_message_cases_case_id_cases")
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_message_cases_message_id_messages")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_message_cases_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("message_id", "case_id", name=op.f("pk_message_cases")),
        sa.UniqueConstraint(
            "case_id", "reminder_number", name=op.f("uq_message_cases_case_id_reminder_number")
        ),
    )
    op.create_index(
        op.f("ix_message_cases_tenant_id"), "message_cases", ["tenant_id"], unique=False
    )
    op.create_table(
        "promises",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("promised_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "kept",
                "broken",
                name="promisestatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name=op.f("fk_promises_case_id_cases")),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["messages.id"],
            name=op.f("fk_promises_source_message_id_messages"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_promises_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promises")),
    )
    op.create_index(op.f("ix_promises_case_id"), "promises", ["case_id"], unique=False)
    op.create_index(op.f("ix_promises_tenant_id"), "promises", ["tenant_id"], unique=False)
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "escalation",
                "review_reply",
                "approve_send",
                "verify_payment",
                "review_dispute",
                name="taskkind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "resolved",
                "cancelled",
                name="taskstatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name=op.f("fk_tasks_case_id_cases")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_tasks_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_index(op.f("ix_tasks_case_id"), "tasks", ["case_id"], unique=False)
    op.create_index(op.f("ix_tasks_tenant_id"), "tasks", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tasks_tenant_id"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_case_id"), table_name="tasks")
    op.drop_table("tasks")
    op.drop_index(op.f("ix_promises_tenant_id"), table_name="promises")
    op.drop_index(op.f("ix_promises_case_id"), table_name="promises")
    op.drop_table("promises")
    op.drop_index(op.f("ix_message_cases_tenant_id"), table_name="message_cases")
    op.drop_table("message_cases")
    op.drop_index(op.f("ix_cases_tenant_id"), table_name="cases")
    op.drop_index("ix_cases_due", table_name="cases")
    op.drop_index(op.f("ix_cases_customer_id"), table_name="cases")
    op.drop_table("cases")
    op.drop_index(op.f("ix_payments_tenant_id"), table_name="payments")
    op.drop_index(op.f("ix_payments_customer_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_index(op.f("ix_messages_tenant_id"), table_name="messages")
    op.drop_index("ix_messages_outbox", table_name="messages")
    op.drop_index(op.f("ix_messages_customer_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_invoices_tenant_id"), table_name="invoices")
    op.drop_index(op.f("ix_invoices_customer_id"), table_name="invoices")
    op.drop_table("invoices")
    op.drop_index(op.f("ix_customers_tenant_id"), table_name="customers")
    op.drop_table("customers")
    op.drop_table("tenants")
