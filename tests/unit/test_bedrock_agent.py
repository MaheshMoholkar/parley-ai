"""The Bedrock agent session against a stub client: request shape, step mapping,
and the append-only history that keeps thinking blocks valid."""

from typing import Any

import pytest
from anthropic.types import Message
from pydantic import BaseModel

from harness import AgentModelError, FinalAnswer, TextOnly, Tool, ToolCall, ToolResult
from parley.adapters.models.bedrock_agent import BedrockAgentModel


class In(BaseModel):
    key: str


class Out(BaseModel):
    answer: str


def message(content: list[dict[str, Any]], stop_reason: str = "tool_use") -> Message:
    return Message.model_validate(
        {
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": "anthropic.claude-opus-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 100},
        }
    )


class StubClient:
    def __init__(self, responses: list[Message]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Message:
        # Copy the history as it was at call time; the session appends to it later.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def test_session_maps_steps_and_only_appends() -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-1"}
    client = StubClient(
        [
            message(
                [
                    thinking,
                    {"type": "tool_use", "id": "tu1", "name": "lookup", "input": {"key": "a"}},
                ]
            ),
            message([{"type": "text", "text": "Done thinking."}], stop_reason="end_turn"),
            message(
                [{"type": "tool_use", "id": "tu2", "name": "finish", "input": {"answer": "1"}}]
            ),
        ]
    )
    model = BedrockAgentModel("ap-south-1", "anthropic.claude-opus-5-5", "medium", client=client)  # type: ignore[arg-type]
    tool = Tool("lookup", "Look up.", In, lambda _: {})
    session = model.start("system text", "the task", [tool], "finish", "Finish.", Out)

    first = session.step(None)
    assert isinstance(first, ToolCall)
    assert (first.call_id, first.name, first.input) == ("tu1", "lookup", {"key": "a"})
    assert first.usage.cache_read_tokens == 100

    second = session.step(ToolResult("tu1", '{"v": 1}'))
    assert isinstance(second, TextOnly) and second.text == "Done thinking."

    third = session.step(None)
    assert isinstance(third, FinalAnswer) and third.input == {"answer": "1"}

    request = client.requests[0]
    assert request["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert [t["name"] for t in request["tools"]] == ["lookup", "finish"]
    assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}

    # The second request carries the first assistant turn unchanged (thinking
    # block and all), followed by the tool result: history is only appended.
    history = client.requests[1]["messages"]
    assert history[0] == {"role": "user", "content": "the task"}
    assert history[1]["role"] == "assistant"
    assert history[1]["content"][0].type == "thinking"
    assert history[1]["content"][0].signature == "sig-1"
    assert history[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "tu1",
        "content": '{"v": 1}',
        "is_error": False,
    }
    assert client.requests[2]["messages"][:3] == history


def test_refusal_ends_the_session() -> None:
    client = StubClient([message([], stop_reason="refusal")])
    model = BedrockAgentModel("ap-south-1", "m", "medium", client=client)  # type: ignore[arg-type]
    session = model.start("s", "t", [], "finish", "Finish.", Out)
    with pytest.raises(AgentModelError, match="declined"):
        session.step(None)
