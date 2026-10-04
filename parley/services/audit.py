"""Audits the outbound message log of a tenant for policy violations.

Used by the evals ("no policy violation in the log") and by tests, and safe to
run in production as a periodic check. It looks only at what was actually
sent, so it catches mistakes regardless of which part of the code made them.

Checks, per sent reminder:
- sent inside quiet hours or on a quiet day (tenant timezone);
- more messages to one customer in 7 days than the weekly cap;
- amounts, due dates, invoice numbers or banned phrases that fail the output
  checks (against the amount owed when the message text was finalised);
- sent after one of its cases was closed;
- sent while the customer had an open dispute;
- a case reminded more times than its limit.
"""

import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from parley.core.checks import check_draft
from parley.core.domain import Direction, MessageStatus
from parley.core.policy import CONTACT_WINDOW
from parley.db.models import Case, Customer, Dispute, Message, MessageCase, Tenant
from parley.services.cases import invoice_line, trusted_text


def find_policy_violations(session: Session, tenant_id: uuid.UUID) -> list[str]:
    tenant = session.get_one(Tenant, tenant_id)
    policy = tenant.policy
    sent = list(
        session.scalars(
            select(Message)
            .where(
                Message.tenant_id == tenant_id,
                Message.direction == Direction.OUTBOUND,
                Message.status == MessageStatus.SENT,
            )
            .order_by(func.coalesce(Message.sent_at, Message.created_at))
        )
    )
    problems: list[str] = []
    by_customer: dict[uuid.UUID, list[datetime]] = defaultdict(list)

    for message in sent:
        # When the customer was contacted: sending can come well after queueing
        # (approval, quiet hours).
        when = message.sent_at or message.created_at
        local = when.astimezone(tenant.zone)
        label = f"message {message.id} ({local:%d %b %H:%M})"
        if policy.quiet_hours.is_quiet(local):
            problems.append(f"{label}: sent in quiet hours")
        by_customer[message.customer_id].append(when)

        rows = session.execute(
            select(Case, MessageCase.amount_due)
            .join(MessageCase, MessageCase.case_id == Case.id)
            .where(MessageCase.message_id == message.id)
        ).all()
        cases = [case for case, _ in rows]
        # Check against the amount owed when the text was written, not today's.
        lines = [
            replace(invoice_line(case.invoice), amount_due=amount)
            if amount is not None
            else invoice_line(case.invoice)
            for case, amount in rows
        ]
        customer = session.get_one(Customer, message.customer_id)
        trusted = trusted_text(tenant.name, customer.name, tenant.payment_link)
        for problem in check_draft(message.subject, message.body, lines, trusted):
            problems.append(f"{label}: {problem}")
        for case in cases:
            if case.closed_at is not None and case.closed_at < when:
                problems.append(f"{label}: sent after case {case.invoice.number} was closed")

        disputes = session.scalars(
            select(Dispute)
            .join(Case, Case.id == Dispute.case_id)
            .where(Case.customer_id == message.customer_id, Dispute.created_at < when)
        )
        for dispute in disputes:
            if dispute.resolved_at is None or dispute.resolved_at > when:
                problems.append(f"{label}: sent while the customer had an open dispute")
                break

    for customer_id, times in by_customer.items():
        for i, start in enumerate(times):
            in_window = [t for t in times[i:] if t < start + CONTACT_WINDOW]
            if len(in_window) > policy.max_contacts_per_week:
                name = session.get_one(Customer, customer_id).name
                problems.append(
                    f"{name}: {len(in_window)} messages within 7 days from {start:%d %b} "
                    f"(cap {policy.max_contacts_per_week})"
                )
                break

    for case in session.scalars(select(Case).where(Case.tenant_id == tenant_id)):
        if case.reminders_sent > policy.max_reminders + case.extra_reminders:
            problems.append(f"case {case.invoice.number}: {case.reminders_sent} reminders sent")
    return problems
