"""The agent loop.

1. Open a session with the system prompt, the task and the tool list.
2. The model asks for one tool; the harness runs it and sends back the result.
3. Repeat until the model gives a final answer or a limit is hit.

Every step is recorded, so a run can be stored, replayed and scored. What to do
with the final answer is the caller's job; the harness only checks its shape.

Each model turn and each tool call is also an OpenTelemetry span (a no-op
unless the application sets up tracing), nested in whatever span the caller
has open.
"""

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from opentelemetry import trace
from pydantic import BaseModel, ValidationError

from harness.model import (
    AgentModel,
    AgentModelError,
    FinalAnswer,
    Reply,
    TextOnly,
    ToolCall,
    ToolResult,
    Usage,
)
from harness.tools import Tool, ToolError

Outcome = Literal["final", "step_limit", "time_limit", "model_error"]

_tracer = trace.get_tracer("harness")

NUDGE = "Reply with a tool call: use one of the tools, or call {final} to give your answer."


@dataclass(frozen=True)
class Limits:
    max_steps: int = 8  # model turns per run
    tool_retries: int = 2  # extra attempts when a tool raises an unexpected error
    max_rows: int = 20  # list results longer than this are cut
    max_seconds: float = 60.0  # wall-clock time per run


@dataclass(frozen=True)
class StepRecord:
    """One model turn and what the harness did with it."""

    number: int
    kind: Literal["tool_call", "final", "text", "error"]
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    is_error: bool = False
    usage: Usage = field(default_factory=Usage)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult[T: BaseModel]:
    outcome: Outcome
    final: T | None
    steps: list[StepRecord]
    model_id: str = ""
    error: str = ""

    @property
    def usage(self) -> Usage:
        return Usage(
            input_tokens=sum(s.usage.input_tokens for s in self.steps),
            output_tokens=sum(s.usage.output_tokens for s in self.steps),
            cache_read_tokens=sum(s.usage.cache_read_tokens for s in self.steps),
            cache_write_tokens=sum(s.usage.cache_write_tokens for s in self.steps),
        )


def run_agent[T: BaseModel](
    model: AgentModel,
    system: str,
    task: str,
    tools: list[Tool],
    final_type: type[T],
    final_name: str = "submit_answer",
    final_description: str = "Give your final answer. Call this exactly once, at the end.",
    limits: Limits | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> RunResult[T]:
    limits = limits or Limits()
    by_name = {tool.name: tool for tool in tools}
    if final_name in by_name:
        raise ValueError(f"tool name {final_name!r} is reserved for the final answer")

    deadline = monotonic() + limits.max_seconds
    steps: list[StepRecord] = []
    try:
        session = model.start(system, task, tools, final_name, final_description, final_type)
    except AgentModelError as exc:
        return RunResult("model_error", None, steps, error=str(exc))

    message: ToolResult | Reply | None = None
    for number in range(1, limits.max_steps + 1):
        if monotonic() > deadline:
            return RunResult("time_limit", None, steps, session.model_id)
        try:
            with _tracer.start_as_current_span("agent.model_turn") as span:
                step = session.step(message)
                span.set_attributes(
                    {
                        "agent.step": number,
                        "gen_ai.request.model": session.model_id,
                        "gen_ai.usage.input_tokens": step.usage.input_tokens,
                        "gen_ai.usage.output_tokens": step.usage.output_tokens,
                    }
                )
        except AgentModelError as exc:
            steps.append(StepRecord(number, "error", output=str(exc), is_error=True))
            return RunResult("model_error", None, steps, session.model_id, error=str(exc))

        match step:
            case FinalAnswer():
                try:
                    final = final_type.model_validate(step.input)
                except ValidationError as exc:
                    # Tell the model what was wrong; it may try again within the limit.
                    problem = f"Invalid {final_name}: {exc.errors(include_url=False)}"
                    steps.append(
                        StepRecord(
                            number, "final", final_name, step.input, problem, True, step.usage
                        )
                    )
                    message = ToolResult(step.call_id, problem, is_error=True)
                    continue
                steps.append(StepRecord(number, "final", final_name, step.input, usage=step.usage))
                return RunResult("final", final, steps, session.model_id)

            case ToolCall():
                with _tracer.start_as_current_span("agent.tool") as span:
                    output, is_error = _run_tool(by_name.get(step.name), step.input, limits)
                    span.set_attributes({"agent.tool": step.name, "agent.tool_error": is_error})
                steps.append(
                    StepRecord(
                        number, "tool_call", step.name, step.input, output, is_error, step.usage
                    )
                )
                message = ToolResult(step.call_id, output, is_error)

            case TextOnly():
                steps.append(StepRecord(number, "text", output=step.text, usage=step.usage))
                message = Reply(NUDGE.format(final=final_name))

    return RunResult("step_limit", None, steps, session.model_id)


def _run_tool(tool: Tool | None, raw_input: dict[str, Any], limits: Limits) -> tuple[str, bool]:
    """Run one tool call and return (result text, is_error). Never raises."""
    if tool is None:
        return "No tool with that name.", True
    try:
        parsed = tool.input_type.model_validate(raw_input)
    except ValidationError as exc:
        return f"Invalid input: {exc.errors(include_url=False)}", True

    last_error = ""
    for _ in range(1 + limits.tool_retries):
        try:
            result = tool.handler(parsed)
        except ToolError as exc:
            return str(exc), True  # an expected answer, such as "not found": no retry
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        return _to_text(result, limits.max_rows), False
    return f"The tool failed: {last_error}", True


def _to_text(result: Any, max_rows: int) -> str:
    """JSON text for the model; long lists are cut to max_rows with a note."""
    if isinstance(result, list) and len(result) > max_rows:
        result = {
            "rows": result[:max_rows],
            "note": f"Showing {max_rows} of {len(result)} rows. Narrow the search to see others.",
        }
    return json.dumps(result, ensure_ascii=False, default=str, sort_keys=True)
