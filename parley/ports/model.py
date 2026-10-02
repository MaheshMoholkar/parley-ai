"""The model interface (spec: "Provider interface").

One method: send a system prompt and a user prompt, get back an instance of a
Pydantic class. The provider constrains the model's output to that class's JSON
schema, so the caller always receives validated, typed data, never free text it
has to parse.

"small" and "large" are tiers; config maps each to a real model id, so a model
can be swapped without code changes.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

Tier = Literal["small", "large"]
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class Completion[T]:
    output: T
    model: str
    usage: TokenUsage
    latency_ms: int


class ModelError(Exception):
    """The call failed: provider error, refusal, cut-off or unparseable output."""


class ModelPort(Protocol):
    def complete(self, tier: Tier, system: str, prompt: str, output_type: type[T]) -> Completion[T]:
        """Run one call. `system` should be stable across calls so it can be cached."""
        ...
