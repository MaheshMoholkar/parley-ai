"""Outbound events (spec: "Outbound events").

`emit` writes an event into the `outbound_events` outbox inside the caller's
transaction, so an event exists exactly when the change it reports was saved.
`deliver_webhooks` (a worker step) posts pending events to the tenant's webhook
URL.

What a receiver gets:

    POST <webhook_url>
    X-Parley-Event-Id: <uuid>        the same on every retry; ignore ids already seen
    X-Parley-Event-Type: case.opened
    X-Parley-Signature: sha256=<hex HMAC-SHA256 of the body with the webhook secret>

    {"id": "...", "type": "case.opened", "created_at": "...", "data": {...}}

Delivery is at least once: any 2xx counts as delivered; anything else, or no
answer, is retried with growing gaps (1, 2, 4 ... minutes, at most 6 hours
apart) and given up after MAX_ATTEMPTS. Events are posted oldest first, but a
retried event can arrive after a newer one, so receivers should not rely on order.
"""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.core.domain import EventType, WebhookStatus
from parley.db.models import OutboundEvent, Tenant
from parley.services.notes import queue_notes
from parley.services.runtime import Runtime
from parley.services.tracing import annotate, step

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 10
MAX_BACKOFF = timedelta(hours=6)


def emit(
    session: Session,
    tenant_id: uuid.UUID,
    event_type: EventType,
    data: dict[str, Any],
    now: datetime,
) -> None:
    """Queue an event for the tenant's webhook, and a note for the source system
    if the tenant writes back (services/notes.py). Without a webhook URL no event
    is queued, so tenants without webhooks build up no backlog."""
    tenant = session.get_one(Tenant, tenant_id)
    queue_notes(session, tenant, event_type, data, now)
    if not tenant.webhook_url:
        return
    session.add(
        OutboundEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            type=event_type,
            data=_jsonable(data),
            status=WebhookStatus.PENDING,
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
    )


def deliver_webhooks(rt: Runtime, tenant_id: uuid.UUID) -> int:
    """Post every event that is due, oldest first. Stops at the first failure,
    so an unreachable receiver costs one attempt per round, not one per event.
    Returns how many were delivered."""
    delivered = 0
    while True:
        with rt.session_factory.begin() as session:
            now = rt.clock.now()
            event = session.scalar(
                select(OutboundEvent)
                .where(
                    OutboundEvent.tenant_id == tenant_id,
                    OutboundEvent.status == WebhookStatus.PENDING,
                    OutboundEvent.next_attempt_at <= now,
                )
                .order_by(OutboundEvent.seq)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return delivered
            tenant = session.get_one(Tenant, tenant_id)
            with step(
                "post_webhook",
                tenant_id=tenant_id,
                event_id=event.id,
                event_type=event.type,
                attempt=event.attempts + 1,
            ) as span:
                posted = _post(rt, tenant, event, now)
                annotate(span, status=event.status, error=event.last_error)
            if not posted:
                return delivered
            delivered += 1


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(rt: Runtime, tenant: Tenant, event: OutboundEvent, now: datetime) -> bool:
    event.attempts += 1
    if not tenant.webhook_url:  # removed after the event was queued
        event.status = WebhookStatus.FAILED
        event.last_error = "the tenant no longer has a webhook URL"
        return False

    body = json.dumps(
        {
            "id": str(event.id),
            "type": event.type,
            "created_at": event.created_at.isoformat(),
            "data": event.data,
        },
        separators=(",", ":"),
    ).encode()
    headers = {
        "X-Parley-Event-Id": str(event.id),
        "X-Parley-Event-Type": event.type,
        "X-Parley-Signature": sign(body, tenant.webhook_secret),
    }
    try:
        status_code = rt.post_webhook(tenant.webhook_url, body, headers)
        error = None if 200 <= status_code < 300 else f"receiver answered {status_code}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    if error is None:
        event.status = WebhookStatus.DELIVERED
        event.delivered_at = now
        event.last_error = None
        return True

    log.warning("webhook %s (attempt %d) failed: %s", event.id, event.attempts, error)
    event.last_error = error[:2000]
    if event.attempts >= MAX_ATTEMPTS:
        event.status = WebhookStatus.FAILED
    else:
        event.next_attempt_at = now + min(timedelta(minutes=2 ** (event.attempts - 1)), MAX_BACKOFF)
    return False


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    """Ids, dates and enums become strings, so the event can be stored as JSON."""
    loaded: dict[str, Any] = json.loads(json.dumps(data, default=str))
    return loaded
