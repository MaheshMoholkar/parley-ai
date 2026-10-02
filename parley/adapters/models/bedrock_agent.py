"""Claude on Amazon Bedrock as an agent model for the harness.

The session keeps the conversation in the provider's own format and only ever
appends to it: each assistant turn is stored exactly as returned (including any
thinking blocks), followed by the tool result. Editing earlier turns would
invalidate the model's reasoning, so nothing is ever rewritten.
"""

from typing import Any

import anthropic
from anthropic import AnthropicBedrockMantle
from anthropic.types import MessageParam, OutputConfigParam, ToolParam
from pydantic import BaseModel

from harness import (
    AgentModelError,
    FinalAnswer,
    Reply,
    TextOnly,
    Tool,
    ToolCall,
    ToolResult,
    Usage,
)
from harness.model import Step
from parley.adapters.models.bedrock import MAX_TOKENS, Effort


class BedrockAgentModel:
    def __init__(
        self,
        region: str,
        model_id: str,
        effort: Effort,
        client: AnthropicBedrockMantle | None = None,
    ) -> None:
        self._client = client or AnthropicBedrockMantle(aws_region=region)
        self.model_id = model_id
        self.effort = effort

    def start(
        self,
        system: str,
        task: str,
        tools: list[Tool],
        final_name: str,
        final_description: str,
        final_type: type[BaseModel],
    ) -> "BedrockAgentSession":
        tool_params: list[ToolParam] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema()}
            for t in tools
        ]
        tool_params.append(
            {
                "name": final_name,
                "description": final_description,
                "input_schema": final_type.model_json_schema(),
                # The tool list never changes during a run, so cache it with the system prompt.
                "cache_control": {"type": "ephemeral"},
            }
        )
        return BedrockAgentSession(self, system, task, tool_params, final_name)


class BedrockAgentSession:
    def __init__(
        self,
        model: BedrockAgentModel,
        system: str,
        task: str,
        tools: list[ToolParam],
        final_name: str,
    ) -> None:
        self.model_id = model.model_id
        self._model = model
        self._system = system
        self._tools = tools
        self._final_name = final_name
        self._messages: list[MessageParam] = [{"role": "user", "content": task}]

    def step(self, message: ToolResult | Reply | None) -> Step:
        if isinstance(message, ToolResult):
            self._messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.call_id,
                            "content": message.content,
                            "is_error": message.is_error,
                        }
                    ],
                }
            )
        elif isinstance(message, Reply):
            self._messages.append({"role": "user", "content": message.text})

        try:
            response = self._model._client.messages.create(
                model=self.model_id,
                max_tokens=MAX_TOKENS,
                system=[
                    {"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=self._messages,
                tools=self._tools,
                # One tool call per turn keeps the run log simple to read and replay.
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                output_config=OutputConfigParam(effort=self._model.effort),
            )
        except anthropic.APIError as exc:
            raise AgentModelError(f"{type(exc).__name__}: {exc}") from exc

        if response.stop_reason == "refusal":
            raise AgentModelError("the model declined the request")
        if response.stop_reason == "max_tokens":
            raise AgentModelError("the response was cut off at the token limit")

        # Append the whole assistant turn unchanged, thinking blocks included.
        self._messages.append({"role": "assistant", "content": response.content})

        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_input_tokens or 0,
            cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
        )
        for block in response.content:
            if block.type == "tool_use":
                tool_input: dict[str, Any] = (
                    dict(block.input) if isinstance(block.input, dict) else {}
                )
                if block.name == self._final_name:
                    return FinalAnswer(block.id, tool_input, usage)
                return ToolCall(block.id, block.name, tool_input, usage)
        text = " ".join(block.text for block in response.content if block.type == "text")
        return TextOnly(text.strip(), usage)
