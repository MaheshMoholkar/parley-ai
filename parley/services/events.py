"""Events pushed by the source system (`POST /v1/events`; spec: "Events").

An event is a hint that something changed, not a fact to apply: the source
system stays the owner of invoice facts. So each new event is recorded, and the
worker runs a full sync for that tenant on its next round instead of waiting
for the sync interval. That keeps one code path (sync) for every change, and a
lost or out-of-order event can never leave the core with wrong numbers.

Event ids are unique per tenant, so a repeated delivery is recognised and ignored.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from parley.core.domain import SourceEventType
from parley.db.models import InboundEvent, Tenant


def record_event(
    session: Session,
    tenant: Tenant,
    event_id: str,
    event_type: SourceEventType,
    data: dict[str, Any],
    now: datetime,
) -> bool:
    """Store the event. Returns False if this event id was already received."""
    statement = (
        insert(InboundEvent)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            event_id=event_id,
            type=event_type,
            data=data,
            received_at=now,
        )
        .on_conflict_do_nothing(index_elements=[InboundEvent.tenant_id, InboundEvent.event_id])
        .returning(InboundEvent.id)
    )
    return session.execute(statement).scalar_one_or_none() is not None


def latest_event_at(session: Session, tenant_id: uuid.UUID) -> datetime | None:
    """When the tenant's newest source event arrived, if any."""
    return session.scalar(
        select(func.max(InboundEvent.received_at)).where(InboundEvent.tenant_id == tenant_id)
    )
