"""Tenant metrics (spec: `GET /v1/metrics`): how collection is going and what
the model costs per case.

Cost per case divides all model spend (single calls and investigator runs) by
the number of cases that used the model at least once. A call is attributed to
the cases of the message it was about; a reminder covering three invoices
counts once for each of them.
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from parley.core.domain import CaseState, CloseReason, PromiseStatus
from parley.db.models import AgentRun, Case, Invoice, MessageCase, ModelCall, Promise, Tenant


@dataclass
class TierUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_micro_usd: int = 0
    avg_latency_ms: float = 0.0


@dataclass
class TenantMetrics:
    cases_by_state: dict[str, int]
    promises_made: int
    promises_kept: int
    promises_broken: int
    promise_kept_rate: float | None  # kept / (kept + broken)
    cases_collected: int  # closed because the source showed them paid
    avg_days_to_collect: float | None  # from due date to close, for collected cases
    model_cost_micro_usd: int
    cases_using_model: int
    cost_per_case_micro_usd: float | None
    # Share of input tokens served from the prompt cache (cheaper); None before any call.
    cache_read_share: float | None
    usage_by_tier: dict[str, TierUsage] = field(default_factory=dict)


def tenant_metrics(
    session: Session, tenant_id: uuid.UUID, since: datetime | None = None
) -> TenantMetrics:
    tenant = session.get_one(Tenant, tenant_id)
    case_filter = [Case.tenant_id == tenant_id]
    if since is not None:
        case_filter.append(Case.opened_at >= since)

    by_state = Counter(
        {
            str(state): count
            for state, count in session.execute(
                select(Case.state, func.count()).where(*case_filter).group_by(Case.state)
            )
        }
    )

    promise_counts = Counter(
        str(status)
        for status in session.scalars(
            select(Promise.status).join(Case, Case.id == Promise.case_id).where(*case_filter)
        )
    )
    kept, broken = promise_counts[PromiseStatus.KEPT], promise_counts[PromiseStatus.BROKEN]

    collected = session.execute(
        select(Case.closed_at, Invoice.due_date)
        .join(Invoice, Invoice.id == Case.invoice_id)
        .where(*case_filter, Case.state == CaseState.CLOSED, Case.closed_reason == CloseReason.PAID)
    ).all()
    days = [
        (closed_at.astimezone(tenant.zone).date() - due).days
        for closed_at, due in collected
        if closed_at is not None  # always set on a closed case; checked for the type checker
    ]

    usage, total_cost, model_cases = _model_usage(session, tenant_id, since)
    all_input = sum(
        u.input_tokens + u.cache_read_tokens + u.cache_write_tokens for u in usage.values()
    )

    return TenantMetrics(
        cases_by_state=dict(by_state),
        promises_made=sum(promise_counts.values()),
        promises_kept=kept,
        promises_broken=broken,
        promise_kept_rate=kept / (kept + broken) if kept + broken else None,
        cases_collected=len(collected),
        avg_days_to_collect=sum(days) / len(days) if days else None,
        model_cost_micro_usd=total_cost,
        cases_using_model=len(model_cases),
        cost_per_case_micro_usd=total_cost / len(model_cases) if model_cases else None,
        cache_read_share=(
            sum(u.cache_read_tokens for u in usage.values()) / all_input if all_input else None
        ),
        usage_by_tier=usage,
    )


def _model_usage(
    session: Session, tenant_id: uuid.UUID, since: datetime | None
) -> tuple[dict[str, TierUsage], int, set[uuid.UUID]]:
    usage: dict[str, TierUsage] = {}
    latency: dict[str, list[int]] = {}
    cases: set[uuid.UUID] = set()
    total = 0

    calls = select(ModelCall).where(ModelCall.tenant_id == tenant_id)
    if since is not None:
        calls = calls.where(ModelCall.created_at >= since)
    message_ids: set[uuid.UUID] = set()
    for call in session.scalars(calls):
        bucket = usage.setdefault(call.tier, TierUsage())
        _add(
            bucket,
            call.input_tokens,
            call.output_tokens,
            call.cache_read_tokens,
            call.cache_write_tokens,
            call.cost_micro_usd,
        )
        latency.setdefault(call.tier, []).append(call.latency_ms)
        total += call.cost_micro_usd
        if call.message_id is not None:
            message_ids.add(call.message_id)
    if message_ids:
        cases.update(
            session.scalars(
                select(MessageCase.case_id).where(MessageCase.message_id.in_(message_ids))
            )
        )

    runs = select(AgentRun).where(AgentRun.tenant_id == tenant_id, AgentRun.model.is_not(None))
    if since is not None:
        runs = runs.where(AgentRun.created_at >= since)
    for run in session.scalars(runs):
        agent = usage.setdefault("investigator", TierUsage())
        _add(agent, run.input_tokens, run.output_tokens, 0, 0, run.cost_micro_usd)
        total += run.cost_micro_usd
        cases.add(run.case_id)

    for tier, values in latency.items():
        usage[tier].avg_latency_ms = sum(values) / len(values)
    return usage, total, cases


def _add(u: TierUsage, inp: int, out: int, cache_read: int, cache_write: int, cost: int) -> None:
    u.calls += 1
    u.input_tokens += inp
    u.output_tokens += out
    u.cache_read_tokens += cache_read
    u.cache_write_tokens += cache_write
    u.cost_micro_usd += cost
