"""A scripted model for testing agents without calling a provider.

Each entry in the script is either a Step, or a function that receives
everything the session has been sent so far and returns a Step. That lets a
test react to tool results, for example "submit the payment id the search
returned".
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from harness.model import AgentModelError, Reply, Step, ToolResult
from harness.tools import Tool

ScriptEntry = Step | Callable[[list[ToolResult | Reply]], Step]


@dataclass
class ScriptedSession:
    script: list[ScriptEntry]
    model_id: str = "scripted"
    received: list[ToolResult | Reply] = field(default_factory=list)

    def step(self, message: ToolResult | Reply | None) -> Step:
        if message is not None:
            self.received.append(message)
        if not self.script:
            raise AgentModelError("script ran out of steps")
        entry = self.script.pop(0)
        return entry(self.received) if callable(entry) else entry


@dataclass
class ScriptedModel:
    script: Sequence[ScriptEntry]
    sessions: list[ScriptedSession] = field(default_factory=list)
    started_with: list[dict[str, object]] = field(default_factory=list)

    def start(
        self,
        system: str,
        task: str,
        tools: list[Tool],
        final_name: str,
        final_description: str,
        final_type: type[BaseModel],
    ) -> ScriptedSession:
        self.started_with.append(
            {"system": system, "task": task, "tools": [t.name for t in tools], "final": final_name}
        )
        session = ScriptedSession(list(self.script))
        self.sessions.append(session)
        return session
