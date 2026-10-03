"""The harness on its own, with toy tools: nothing here knows about invoices."""

import json

from pydantic import BaseModel

from harness import FinalAnswer, Limits, TextOnly, Tool, ToolCall, ToolError, run_agent
from harness.model import Reply, ToolResult, Usage
from harness.testing import ScriptedModel


class LookupIn(BaseModel):
    key: str


class Answer(BaseModel):
    value: int
    evidence: list[str]


TABLE = {"a": 1, "b": 2}


def lookup(args: LookupIn) -> dict[str, int]:
    if args.key not in TABLE:
        raise ToolError(f"no row {args.key!r}")
    return {"value": TABLE[args.key]}


LOOKUP = Tool("lookup", "Look up a value by key.", LookupIn, lookup)
LIST = Tool("list_all", "List many rows.", LookupIn, lambda _: [{"n": i} for i in range(50)])


def run(script: list, tools: list[Tool] | None = None, **limits: float) -> tuple:  # type: ignore[type-arg]
    model = ScriptedModel(script)
    result = run_agent(
        model, "system", "task", tools or [LOOKUP, LIST], Answer, limits=Limits(**limits)
    )  # type: ignore[arg-type]
    return result, model.sessions[0].received


def test_tool_call_then_final_answer() -> None:
    def answer_with_looked_up_value(received: list[ToolResult | Reply]) -> FinalAnswer:
        value = json.loads(received[-1].content)["value"]  # type: ignore[union-attr]
        return FinalAnswer("c2", {"value": value, "evidence": ["a"]})

    result, received = run(
        [ToolCall("c1", "lookup", {"key": "a"}, Usage(10, 5)), answer_with_looked_up_value]
    )

    assert result.outcome == "final"
    assert result.final == Answer(value=1, evidence=["a"])
    assert [s.kind for s in result.steps] == ["tool_call", "final"]
    assert received == [ToolResult("c1", '{"value": 1}')]
    assert result.usage.input_tokens == 10


def test_expected_tool_errors_go_back_to_the_model() -> None:
    result, received = run(
        [ToolCall("c1", "lookup", {"key": "zz"}), FinalAnswer("c2", {"value": 0, "evidence": []})]
    )
    assert received[0] == ToolResult("c1", "no row 'zz'", is_error=True)
    assert result.outcome == "final"


def test_bad_tool_input_and_unknown_tools_are_reported() -> None:
    _, received = run(
        [
            ToolCall("c1", "lookup", {"wrong": 1}),
            ToolCall("c2", "delete_everything", {}),
            FinalAnswer("c3", {"value": 0, "evidence": []}),
        ]
    )
    assert received[0].is_error and "Invalid input" in received[0].content  # type: ignore[union-attr]
    assert received[1] == ToolResult("c2", "No tool with that name.", is_error=True)


def test_crashing_tool_is_retried_then_reported() -> None:
    calls = []

    def flaky(_: LookupIn) -> str:
        calls.append(1)
        raise ConnectionError("database away")

    tool = Tool("flaky", "Fails.", LookupIn, flaky)
    _, received = run(
        [ToolCall("c1", "flaky", {"key": "a"}), FinalAnswer("c2", {"value": 0, "evidence": []})],
        [tool],
    )
    assert len(calls) == 3  # first try plus two retries
    assert received[0] == ToolResult(
        "c1", "The tool failed: ConnectionError: database away", is_error=True
    )


def test_long_lists_are_cut() -> None:
    _, received = run(
        [ToolCall("c1", "list_all", {"key": "x"}), FinalAnswer("c2", {"value": 0, "evidence": []})]
    )
    body = json.loads(received[0].content)  # type: ignore[union-attr]
    assert len(body["rows"]) == 20
    assert body["note"] == "Showing 20 of 50 rows. Narrow the search to see others."


def test_invalid_final_answer_can_be_corrected() -> None:
    result, received = run(
        [FinalAnswer("c1", {"value": "lots"}), FinalAnswer("c2", {"value": 3, "evidence": []})]
    )
    assert received[0].is_error and "Invalid submit_answer" in received[0].content  # type: ignore[union-attr]
    assert result.final == Answer(value=3, evidence=[])


def test_plain_text_gets_a_nudge() -> None:
    _, received = run(
        [TextOnly("I think it is 1."), FinalAnswer("c1", {"value": 1, "evidence": []})]
    )
    assert isinstance(received[0], Reply)
    assert "submit_answer" in received[0].text


def test_step_limit_holds() -> None:
    script = [ToolCall(f"c{i}", "lookup", {"key": "a"}) for i in range(20)]
    result, _ = run(script, max_steps=8)
    assert result.outcome == "step_limit"
    assert result.final is None
    assert len(result.steps) == 8


def test_time_limit_holds() -> None:
    ticks = iter([0.0, 1.0, 100.0])
    model = ScriptedModel(
        [ToolCall("c1", "lookup", {"key": "a"}), ToolCall("c2", "lookup", {"key": "a"})]
    )
    result = run_agent(model, "s", "t", [LOOKUP], Answer, monotonic=lambda: next(ticks))
    assert result.outcome == "time_limit"
    assert len(result.steps) == 1


def test_model_failure_ends_the_run() -> None:
    result, _ = run([])  # the scripted session raises when the script is empty
    assert result.outcome == "model_error"
    assert result.steps[-1].kind == "error"


def test_model_turns_and_tool_calls_are_spans() -> None:
    from tests.tracing import collected_spans, named

    exporter = collected_spans()
    result, _ = run(
        [
            ToolCall("c1", "lookup", {"key": "a"}),
            FinalAnswer("c2", {"value": 1, "evidence": ["a"]}),
        ]
    )
    assert result.outcome == "final"
    spans = exporter.get_finished_spans()
    assert len(named(spans, "agent.model_turn")) == 2
    [tool] = named(spans, "agent.tool")
    assert tool.attributes == {"agent.tool": "lookup", "agent.tool_error": False}
