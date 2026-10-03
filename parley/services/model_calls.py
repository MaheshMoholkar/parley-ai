"""Runs model jobs and logs every call (spec: "Every call is logged with prompt
version, tokens, cost and latency")."""

import uuid
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy.orm import Session

from parley.collections_ai.prompts import Prompt
from parley.db.models import ModelCall
from parley.ports.model import Completion, ModelError, Tier, TokenUsage
from parley.services.runtime import Runtime

# Cache writes cost 1.25x the input price and cache reads 0.1x.
CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.1


def run_job[T: BaseModel](
    rt: Runtime,
    session: Session,
    tenant_id: uuid.UUID,
    message_id: uuid.UUID | None,
    prompt_version: str,
    tier: Tier,
    job: Callable[[], tuple[Completion[T], Prompt]],
) -> Completion[T]:
    """Call `job` and record the call, whether it succeeds or raises ModelError."""
    now = rt.clock.now()
    try:
        completion, _ = job()
    except ModelError as exc:
        session.add(_row(tenant_id, message_id, prompt_version, tier, now, error=str(exc)))
        raise
    session.add(
        _row(
            tenant_id,
            message_id,
            prompt_version,
            tier,
            now,
            model=completion.model,
            usage=completion.usage,
            latency_ms=completion.latency_ms,
            cost=estimate_cost_micro_usd(rt, completion.model, completion.usage),
        )
    )
    return completion


def estimate_cost_micro_usd(rt: Runtime, model: str, usage: TokenUsage) -> int:
    """Cost in millionths of a dollar. Prices are per million tokens, so tokens
    times price is already in micro-dollars."""
    input_price, output_price = rt.model_prices.get(model, (0.0, 0.0))
    input_cost = input_price * (
        usage.input_tokens
        + CACHE_WRITE_FACTOR * usage.cache_write_tokens
        + CACHE_READ_FACTOR * usage.cache_read_tokens
    )
    return round(input_cost + output_price * usage.output_tokens)


def _row(
    tenant_id: uuid.UUID,
    message_id: uuid.UUID | None,
    prompt_version: str,
    tier: Tier,
    now: datetime,
    model: str | None = None,
    usage: TokenUsage | None = None,
    latency_ms: int = 0,
    cost: int = 0,
    error: str | None = None,
) -> ModelCall:
    usage = usage or TokenUsage(0, 0)
    return ModelCall(
        tenant_id=tenant_id,
        message_id=message_id,
        prompt_version=prompt_version,
        tier=tier,
        model=model,
        ok=error is None,
        error=error,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_micro_usd=cost,
        latency_ms=latency_ms,
        created_at=now,
    )
