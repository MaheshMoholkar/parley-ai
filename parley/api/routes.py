"""HTTP endpoints (spec: "API and events")."""

import hashlib
import hmac
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from parley.adapters.accounting.csv import CsvFormatError
from parley.adapters.channels.email_inbound import parse_email
from parley.api.dependencies import RuntimeDep, SessionDep, TenantDep
from parley.api.schemas import (
    CaseDetailOut,
    CaseOut,
    CasePage,
    CustomerOut,
    EventIn,
    EventOut,
    InboundOut,
    MessageOut,
    MetricsOut,
    ResolveTaskIn,
    SyncOut,
    TaskOut,
    TaskPage,
)
from parley.core.domain import CaseState, TaskStatus
from parley.db.models import Case, Message, MessageCase, Task
from parley.services.accounting import AdapterConfigError
from parley.services.cases import NotFoundError, set_case_paused, set_customer_paused
from parley.services.events import record_event
from parley.services.inbound import receive_email
from parley.services.metrics import tenant_metrics
from parley.services.sync import SyncError, sync_tenant
from parley.services.tasks import TaskError, resolve_task

router = APIRouter()

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


REVIEW_PAGE = (Path(__file__).parent / "review.html").read_text(encoding="utf-8")


@router.get("/review", response_class=HTMLResponse, include_in_schema=False)
def review_page() -> str:
    """The minimal review screen. The page itself is public; everything it shows
    comes from the API, which needs the tenant's API key."""
    return REVIEW_PAGE


@router.get("/healthz")
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.post("/v1/sync")
def sync_now(rt: RuntimeDep, tenant: TenantDep) -> SyncOut:
    """Pull invoices and payments through the tenant's adapter now."""
    try:
        result = sync_tenant(rt, tenant.id)
    except (CsvFormatError, AdapterConfigError, SyncError) as exc:
        # The source data or adapter settings are wrong; nothing was saved.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    return SyncOut(**asdict(result))


MAX_EVENT_BYTES = 256 * 1024
MAX_EMAIL_BYTES = 10 * 1024 * 1024


async def read_body(request: Request, limit: int) -> bytes:
    """The request body, refused with 413 as soon as it passes `limit` bytes.
    Read in chunks, so an oversized upload is never held in memory whole."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "request too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "request too large")
    return bytes(body)


async def event_body(request: Request) -> bytes:
    return await read_body(request, MAX_EVENT_BYTES)


@router.post("/v1/events", status_code=status.HTTP_202_ACCEPTED)
def receive_source_event(
    rt: RuntimeDep,
    session: SessionDep,
    tenant: TenantDep,
    body: Annotated[bytes, Depends(event_body)],
    signature: Annotated[str | None, Header(alias="X-Parley-Signature")] = None,
) -> EventOut:
    """Receive `invoice.created`, `invoice.updated`, `invoice.voided` or
    `payment.recorded` from the source system.

    Needs the tenant's API key, and the body signed with the tenant's webhook
    secret: `X-Parley-Signature: sha256=<hex HMAC-SHA256 of the body>`. Each
    new event makes the worker sync this tenant on its next round; a repeated
    event id is acknowledged and ignored.
    """
    if not valid_signature(body, signature, tenant.webhook_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing signature")
    try:
        event = EventIn.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False)
        ) from None
    new = record_event(session, tenant, event.id, event.type, event.data, rt.clock.now())
    return EventOut(outcome="accepted" if new else "duplicate")


@router.get("/v1/cases")
def list_cases(
    session: SessionDep,
    tenant: TenantDep,
    state: CaseState | None = None,
    customer_id: uuid.UUID | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> CasePage:
    conditions = [Case.tenant_id == tenant.id]
    if state is not None:
        conditions.append(Case.state == state)
    if customer_id is not None:
        conditions.append(Case.customer_id == customer_id)

    total = session.scalar(select(func.count()).select_from(Case).where(*conditions)) or 0
    cases = session.scalars(
        select(Case)
        .where(*conditions)
        .options(selectinload(Case.invoice), selectinload(Case.customer))
        .order_by(Case.opened_at.desc(), Case.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return CasePage(
        items=[CaseOut.model_validate(c) for c in cases], total=total, limit=limit, offset=offset
    )


@router.get("/v1/cases/{case_id}")
def get_case(session: SessionDep, tenant: TenantDep, case_id: uuid.UUID) -> CaseDetailOut:
    """One case with its messages and tasks."""
    case = session.scalar(select(Case).where(Case.id == case_id, Case.tenant_id == tenant.id))
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "case not found")
    messages = session.scalars(
        select(Message)
        .join(MessageCase, MessageCase.message_id == Message.id)
        .where(MessageCase.case_id == case.id)
        .order_by(Message.created_at)
    ).all()
    tasks = session.scalars(
        select(Task).where(Task.case_id == case.id).order_by(Task.created_at)
    ).all()
    return CaseDetailOut(
        case=CaseOut.model_validate(case),
        messages=[MessageOut.model_validate(m) for m in messages],
        tasks=[TaskOut.model_validate(t) for t in tasks],
    )


@router.post("/v1/cases/{case_id}/pause")
def pause_case(session: SessionDep, tenant: TenantDep, case_id: uuid.UUID) -> CaseOut:
    return _set_case_paused(session, tenant.id, case_id, paused=True)


@router.post("/v1/cases/{case_id}/resume")
def resume_case(session: SessionDep, tenant: TenantDep, case_id: uuid.UUID) -> CaseOut:
    return _set_case_paused(session, tenant.id, case_id, paused=False)


@router.post("/v1/customers/{customer_id}/pause")
def pause_customer(session: SessionDep, tenant: TenantDep, customer_id: uuid.UUID) -> CustomerOut:
    return _set_customer_paused(session, tenant.id, customer_id, paused=True)


@router.post("/v1/customers/{customer_id}/resume")
def resume_customer(session: SessionDep, tenant: TenantDep, customer_id: uuid.UUID) -> CustomerOut:
    return _set_customer_paused(session, tenant.id, customer_id, paused=False)


@router.get("/v1/metrics")
def metrics(session: SessionDep, tenant: TenantDep, since: datetime | None = None) -> MetricsOut:
    """Cases by state, promises kept, days to collect, and model cost per case.
    `since` limits it to cases opened (and model calls made) from that time."""
    return MetricsOut.model_validate(asdict(tenant_metrics(session, tenant.id, since)))


@router.get("/v1/tasks")
def list_tasks(
    session: SessionDep,
    tenant: TenantDep,
    task_status: Annotated[TaskStatus, Query(alias="status")] = TaskStatus.OPEN,
    limit: Limit = 50,
    offset: Offset = 0,
) -> TaskPage:
    conditions = [Task.tenant_id == tenant.id, Task.status == task_status]
    total = session.scalar(select(func.count()).select_from(Task).where(*conditions)) or 0
    tasks = session.scalars(
        select(Task)
        .where(*conditions)
        .order_by(Task.created_at, Task.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return TaskPage(
        items=[TaskOut.model_validate(t) for t in tasks], total=total, limit=limit, offset=offset
    )


@router.post("/v1/tasks/{task_id}/resolve")
def resolve(
    session: SessionDep, rt: RuntimeDep, tenant: TenantDep, task_id: uuid.UUID, body: ResolveTaskIn
) -> TaskOut:
    """Approve, edit or reject a draft; or resume or close the case behind a task."""
    try:
        task = resolve_task(
            session,
            tenant,
            task_id,
            body.action,
            rt.clock.now(),
            body.subject,
            body.body,
            body.note,
        )
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found") from None
    except TaskError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.problems) from None
    return TaskOut.model_validate(task)


@router.post("/v1/inbound/email", status_code=status.HTTP_202_ACCEPTED)
async def inbound_email(
    request: Request,
    rt: RuntimeDep,
    signature: Annotated[str | None, Header(alias="X-Parley-Signature")] = None,
) -> InboundOut:
    """Receive one raw email (MIME) forwarded from SES receiving.

    Not authenticated by API key: the forwarder signs the raw body with the
    shared inbound secret, sent as `X-Parley-Signature: sha256=<hex HMAC>`.
    """
    if not rt.inbound_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "inbound email is not configured")
    raw = await read_body(request, MAX_EMAIL_BYTES)
    if not valid_signature(raw, signature, rt.inbound_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing signature")

    # Parsing untrusted mail and the database work are both blocking, so they
    # run off the event loop, which keeps serving other requests and calls.
    email = await run_in_threadpool(parse_email, raw)
    result = await run_in_threadpool(receive_email, rt, email)
    return InboundOut(outcome=result.outcome, message_id=result.message_id, reason=result.reason)


def valid_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # compare_digest takes the same time whatever the input, so the signature
    # cannot be guessed byte by byte from response times.
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _set_case_paused(
    session: Session, tenant_id: uuid.UUID, case_id: uuid.UUID, paused: bool
) -> CaseOut:
    try:
        return CaseOut.model_validate(set_case_paused(session, tenant_id, case_id, paused))
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "case not found") from None


def _set_customer_paused(
    session: Session, tenant_id: uuid.UUID, customer_id: uuid.UUID, paused: bool
) -> CustomerOut:
    try:
        return CustomerOut.model_validate(
            set_customer_paused(session, tenant_id, customer_id, paused)
        )
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "customer not found") from None
