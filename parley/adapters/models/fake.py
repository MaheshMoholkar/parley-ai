"""A model for tests and demos: answers come from a function you supply."""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from parley.ports.model import Completion, ModelError, T, Tier, TokenUsage


@dataclass(frozen=True)
class FakeCall:
    tier: Tier
    system: str
    prompt: str
    output_type: type[BaseModel]


# Given the call, return an instance of call.output_type (or raise ModelError).
Responder = Callable[[FakeCall], BaseModel]


class FakeModel:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.calls: list[FakeCall] = []

    def complete(self, tier: Tier, system: str, prompt: str, output_type: type[T]) -> Completion[T]:
        call = FakeCall(tier, system, prompt, output_type)
        self.calls.append(call)
        output = self.responder(call)
        if not isinstance(output, output_type):
            raise ModelError(f"fake responder returned {type(output).__name__}")
        return Completion(
            output=output,
            model=f"fake-{tier}",
            usage=TokenUsage(input_tokens=100, output_tokens=50),
            latency_ms=1,
        )
