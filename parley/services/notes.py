"""Write-back: notes about collection activity, added to the invoice in the
source system (spec: "Write-back").

The same events that go to the tenant's webhook (webhooks.emit) also queue a
note here, in the same transaction as the change, when the tenant has turned
write-back on (`"write_notes": true` in its adapter settings). A worker step,
`write_source_notes`, then adds each note to its invoice through the adapter's
`add_note`, oldest first.

Like webhooks, delivery is at least once: each note carries its id as an
idempotency key, a failure is retried after 1, 2, 4 ... minutes (at most 6 hours
apart), and a note is given up after MAX_ATTEMPTS, or at once when the source
refuses it or the adapter cannot write notes at all.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.core.domain import EventType, NoteStatus
from parley.core.notes import note_text
from parley.db.models import Case, Invoice, SourceNote, Tenant
from parley.ports.accounting import NoteError, NoteWriter
from parley.services.runtime import Runtime
from parley.services.tracing import annotate, step

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 10
MAX_BACKOFF = timedelta(hours=6)


def writes_notes(tenant: Tenant) -> bool:
    return tenant.adapter_config.get("write_notes") is True


def queue_notes(
    session: Session, tenant: Tenant, event_type: EventType, data: dict[str, Any], now: datetime
) -> None:
    """Queue a note on each invoice the event is about, if the tenant writes back."""
    if not writes_notes(tenant):
        return
    case_ids = data.get("case_ids") or ([data["case_id"]] if data.get("case_id") else [])
    for case_id in case_ids:
        case = session.get(Case, case_id)
        if case is None or case.tenant_id != tenant.id:
            continue
        text = note_text(event_type, data, case.invoice.currency)
        if text is None:
            continue
        session.add(
            SourceNote(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                invoice_id=case.invoice_id,
                text=text,
                status=NoteStatus.PENDING,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
            )
        )


def write_source_notes(rt: Runtime, tenant_id: uuid.UUID) -> int:
    """Write every note that is due, oldest first. Stops at the first failure
    that may pass, so a source that is down costs one attempt per round.
    Returns how many were written."""
    written = 0
    writer: NoteWriter | None = None
    while True:
        with rt.session_factory.begin() as session:
            now = rt.clock.now()
            note = session.scalar(
                select(SourceNote)
                .where(
                    SourceNote.tenant_id == tenant_id,
                    SourceNote.status == NoteStatus.PENDING,
                    SourceNote.next_attempt_at <= now,
                )
                .order_by(SourceNote.seq)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if note is None:
                return written
            if writer is None:
                adapter = rt.accounting_for(session.get_one(Tenant, tenant_id))
                if not isinstance(adapter, NoteWriter):
                    _give_up_all(
                        session, tenant_id, f"the {adapter.source} adapter cannot write notes"
                    )
                    return written
                writer = adapter
            invoice = session.get_one(Invoice, note.invoice_id)
            with step("write_note", tenant_id=tenant_id, invoice_id=invoice.id) as span:
                ok = _write(writer, note, invoice, now)
                annotate(span, status=note.status, error=note.last_error)
            if not ok:
                if note.status == NoteStatus.PENDING:
                    return written  # may pass later: try again next round
                continue  # refused for good: go on with the next note
            written += 1


def _write(writer: NoteWriter, note: SourceNote, invoice: Invoice, now: datetime) -> bool:
    note.attempts += 1
    try:
        writer.add_note(invoice.external_id, note.text, idempotency_key=str(note.id))
    except NoteError as exc:
        _failed(note, str(exc), retryable=exc.retryable, now=now)
        return False
    except Exception as exc:  # a network error or a bug: worth another try
        _failed(note, f"{type(exc).__name__}: {exc}", retryable=True, now=now)
        return False
    note.status = NoteStatus.WRITTEN
    note.written_at = now
    note.last_error = None
    return True


def _failed(note: SourceNote, error: str, retryable: bool, now: datetime) -> None:
    log.warning("note %s (attempt %d) failed: %s", note.id, note.attempts, error)
    note.last_error = error[:2000]
    if not retryable or note.attempts >= MAX_ATTEMPTS:
        note.status = NoteStatus.FAILED
    else:
        note.next_attempt_at = now + min(timedelta(minutes=2 ** (note.attempts - 1)), MAX_BACKOFF)


def _give_up_all(session: Session, tenant_id: uuid.UUID, reason: str) -> None:
    """The adapter has no write-back at all: no note can ever be written."""
    log.warning("tenant %s has write_notes on, but %s", tenant_id, reason)
    pending = session.scalars(
        select(SourceNote).where(
            SourceNote.tenant_id == tenant_id, SourceNote.status == NoteStatus.PENDING
        )
    )
    for note in pending:
        note.status = NoteStatus.FAILED
        note.last_error = reason
