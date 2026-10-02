"""What the harness needs from a model provider.

A provider starts a session; the session keeps the provider's own conversation
history (append-only, so any reasoning blocks the provider returns stay valid)
and answers each turn with one of three steps: call a tool, give the final
answer, or (by mistake) reply with plain text.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from harness.tools import Tool


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    input: dict[str, Any]
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class FinalAnswer:
    call_id: str
    input: dict[str, Any]  # validated by the harness against the final answer type
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class TextOnly:
    text: str
    usage: Usage = field(default_factory=Usage)


Step = ToolCall | FinalAnswer | TextOnly


@dataclass(frozen=True)
class ToolResult:
    """Sent back after a ToolCall or a rejected FinalAnswer."""

    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Reply:
    """Sent back after a TextOnly step, to steer the model back to using tools."""

    text: str


class AgentModelError(Exception):
    """The provider failed or declined; the run ends."""


class AgentSession(Protocol):
    model_id: str

    def step(self, message: ToolResult | Reply | None) -> Step:
        """Send `message` (None on the first turn) and return the model's next step."""
        ...


class AgentModel(Protocol):
    def start(
        self,
        system: str,
        task: str,
        tools: list[Tool],
        final_name: str,
        final_description: str,
        final_type: type[BaseModel],
    ) -> AgentSession:
        """Open a session. The final answer is offered as one more tool named
        `final_name` whose input schema is `final_type`'s JSON schema."""
        ...
