"""Delivers pending outbound messages from the outbox through the tenant's channel.

Each message is locked, sent and marked `sent` in its own transaction. If the
process dies after sending but before saving, the message stays `pending` and is
sent again with the same idempotency key, which lets the provider drop the copy.
"""

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.core.domain import Direction, MessageStatus
from parley.core.workflow import on_delivery_failed
from parley.db.models import Case, Message, MessageCase
from parley.ports.channel import OutboundMessage
from parley.services.cases import apply_transition
from parley.services.runtime import Runtime

log = logging.getLogger(__name__)

# After this many failed attempts a message is marked failed and a person is asked to look.
MAX_ATTEMPTS = 5


def deliver_pending_messages(rt: Runtime, tenant_id: uuid.UUID) -> int:
    """Try each pending message once. Returns how many were sent."""
    sent = 0
    tried: set[uuid.UUID] = set()
    while True:
        with rt.session_factory.begin() as session:
            message = _lock_next_pending(session, tenant_id, tried)
            if message is None:
                return sent
            tried.add(message.id)
            if _deliver(rt, session, message):
                sent += 1


def _lock_next_pending(
    session: Session, tenant_id: uuid.UUID, tried: set[uuid.UUID]
) -> Message | None:
    query = (
        select(Message)
        .where(
            Message.tenant_id == tenant_id,
            Message.direction == Direction.OUTBOUND,
            Message.status == MessageStatus.PENDING,
        )
        .order_by(Message.created_at, Message.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if tried:
        query = query.where(Message.id.not_in(tried))
    return session.scalar(query)


def _deliver(rt: Runtime, session: Session, message: Message) -> bool:
    now = rt.clock.now()
    message.attempts += 1
    try:
        provider_id = rt.channel.send(
            OutboundMessage(
                idempotency_key=message.idempotency_key,
                to_address=message.to_address,
                subject=message.subject,
                body=message.body,
                reply_token=message.reply_token,
            )
        )
    except Exception as exc:
        log.warning(
            "send failed for message %s (attempt %d): %s", message.id, message.attempts, exc
        )
        message.last_error = str(exc)[:2000]
        if message.attempts >= MAX_ATTEMPTS:
            _give_up(session, message, now)
        return False

    message.status = MessageStatus.SENT
    message.provider_message_id = provider_id
    message.sent_at = now
    message.last_error = None
    return True


def _give_up(session: Session, message: Message, now: datetime) -> None:
    """Mark the message failed and hand each open case it was about to a person."""
    message.status = MessageStatus.FAILED
    cases = session.scalars(
        select(Case)
        .join(MessageCase, MessageCase.case_id == Case.id)
        .where(MessageCase.message_id == message.id)
        .with_for_update()
    )
    for case in cases:
        transition = on_delivery_failed(
            case.view(), f"Reminder could not be delivered: {message.last_error}"
        )
        if transition is not None:
            apply_transition(session, case, transition, now)
