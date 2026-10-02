"""HTTP endpoints (spec: "API and events"). M1 covers sync, cases, pause and tasks."""

import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from parley.adapters.accounting.csv import CsvFormatError
from parley.api.dependencies import RuntimeDep, SessionDep, TenantDep
from parley.api.schemas import (
    CaseDetailOut,
    CaseOut,
    CasePage,
    CustomerOut,
    MessageOut,
    SyncOut,
    TaskOut,
    TaskPage,
)
from parley.core.domain import CaseState, TaskStatus
from parley.db.models import Case, Message, MessageCase, Task
from parley.services.accounting import AdapterConfigError
from parley.services.cases import NotFoundError, set_case_paused, set_customer_paused
from parley.services.sync import SyncError, sync_tenant

router = APIRouter()

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


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
