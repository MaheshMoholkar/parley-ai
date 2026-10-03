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

from parley.core.domain import Direction, EventType, MessageStatus
from parley.core.workflow import on_delivery_failed
from parley.db.models import Case, Message, MessageCase, Tenant
from parley.ports.channel import OutboundMessage
from parley.ports.voice import VoiceError
from parley.services.calls import VOICE, place_reminder_call
from parley.services.cases import apply_transition, cases_of_message
from parley.services.runtime import Runtime
from parley.services.tracing import annotate, step
from parley.services.webhooks import emit

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
            with step(
                "deliver_message",
                tenant_id=tenant_id,
                message_id=message.id,
                channel=message.channel,
                attempt=message.attempts + 1,
            ) as span:
                delivered = _deliver(rt, session, message)
                annotate(span, status=message.status, error=message.last_error)
            if delivered:
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
    if message.channel == VOICE and _quiet_now(session, message, now):
        return False  # a call waits for allowed hours, however long approval took
    message.attempts += 1
    try:
        if message.channel == VOICE:
            provider_id = place_reminder_call(rt, session, message).provider_call_id
        else:
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
        # A call that may already be ringing is never retried: a person decides.
        unsafe_to_retry = isinstance(exc, VoiceError) and not exc.retryable
        if unsafe_to_retry or message.attempts >= MAX_ATTEMPTS:
            _give_up(session, message, now)
        return False

    message.status = MessageStatus.SENT
    message.provider_message_id = provider_id
    message.sent_at = now
    message.last_error = None
    data = {
        "message_id": message.id,
        "customer_id": message.customer_id,
        "case_ids": [case.id for case in cases_of_message(session, message.id)],
        "channel": message.channel,
    }
    emit(session, message.tenant_id, EventType.MESSAGE_SENT, data, now)
    return True


def _quiet_now(session: Session, message: Message, now: datetime) -> bool:
    tenant = session.get_one(Tenant, message.tenant_id)
    return tenant.policy.quiet_hours.is_quiet(now.astimezone(tenant.zone))


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
