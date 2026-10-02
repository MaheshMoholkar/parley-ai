"""Receiving a customer's email reply (spec: "Email (first)").

The reply is matched to its cases, stored as an inbound message in status
`received`, and read later by the worker (see replies.py). Receiving is kept
fast and model-free, so the provider's webhook never times out.

Matching, in order:
1. the reply token in the recipient address (reply+<token>@...), which names
   the exact reminder being answered;
2. otherwise the sender's address, if exactly one customer with open cases
   uses it;
3. otherwise the mail is kept in `unmatched_inbound` for an operator.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from parley.adapters.channels.email_inbound import reply_token
from parley.core.domain import CaseState, Direction, MessageStatus
from parley.db.models import Case, Customer, Message, MessageCase, UnmatchedInbound
from parley.ports.channel import InboundMessage
from parley.services.cases import cases_of_message
from parley.services.runtime import Runtime

log = logging.getLogger(__name__)

Outcome = Literal["stored", "duplicate", "unmatched"]


@dataclass(frozen=True)
class ReceiveResult:
    outcome: Outcome
    message_id: uuid.UUID | None = None
    reason: str = ""


def receive_email(rt: Runtime, email: InboundMessage) -> ReceiveResult:
    now = rt.clock.now()
    with rt.session_factory.begin() as session:
        customer, cases, reason = _match(session, email)
        if customer is None:
            session.add(
                UnmatchedInbound(
                    received_at=now,
                    from_address=email.sender,
                    to_addresses=", ".join(email.recipients),
                    subject=email.subject[:500],
                    body=email.text,
                    reason=reason,
                )
            )
            log.warning("unmatched inbound email from %s: %s", email.sender, reason)
            return ReceiveResult("unmatched", reason=reason)

        if email.provider_message_id and _already_stored(session, customer, email):
            return ReceiveResult("duplicate")

        message = Message(
            id=uuid.uuid4(),
            tenant_id=customer.tenant_id,
            customer_id=customer.id,
            channel="email",
            direction=Direction.INBOUND,
            from_address=email.sender,
            to_address=email.recipients[0] if email.recipients else "",
            subject=email.subject[:500],
            body=email.text,
            # With no open case there is nothing to act on; keep it for the record.
            status=MessageStatus.RECEIVED if cases else MessageStatus.READ,
            idempotency_key=uuid.uuid4().hex,
            provider_message_id=email.provider_message_id or None,
            attempts=0,
            created_at=now,
        )
        session.add(message)
        for case in cases:
            session.add(
                MessageCase(message_id=message.id, case_id=case.id, tenant_id=case.tenant_id)
            )
        return ReceiveResult("stored", message_id=message.id)


def _match(session: Session, email: InboundMessage) -> tuple[Customer | None, list[Case], str]:
    token = reply_token(email.recipients)
    if token:
        original = session.scalar(select(Message).where(Message.reply_token == token))
        if original is not None:
            customer = session.get_one(Customer, original.customer_id)
            cases = [
                c for c in cases_of_message(session, original.id) if c.state != CaseState.CLOSED
            ]
            return customer, cases, ""

    # No usable token: fall back to the sender's address.
    if not email.sender:
        return None, [], "no sender address"
    customers = session.scalars(
        select(Customer)
        .join(Case, Case.customer_id == Customer.id)
        .where(func.lower(Customer.email) == email.sender, Case.state != CaseState.CLOSED)
        .distinct()
    ).all()
    if len(customers) != 1:
        found = "no customer" if not customers else f"{len(customers)} customers"
        return None, [], f"no reply token, and {found} with open cases use {email.sender}"
    customer = customers[0]
    cases = list(
        session.scalars(
            select(Case)
            .where(Case.customer_id == customer.id, Case.state != CaseState.CLOSED)
            .order_by(Case.opened_at, Case.id)
        )
    )
    return customer, cases, ""


def _already_stored(session: Session, customer: Customer, email: InboundMessage) -> bool:
    """Providers retry webhooks, so the same email can arrive twice."""
    existing = select(Message.id).where(
        Message.tenant_id == customer.tenant_id,
        Message.direction == Direction.INBOUND,
        Message.provider_message_id == email.provider_message_id,
    )
    return bool(session.scalar(select(existing.exists())))
