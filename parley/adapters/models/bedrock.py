"""Claude on Amazon Bedrock, through the Anthropic SDK's Bedrock client.

Credentials come from the standard AWS chain (environment, profile, or the ECS
task role). Model ids on Bedrock carry an "anthropic." prefix.
"""

import time
from typing import Literal

import anthropic
from anthropic import AnthropicBedrockMantle
from anthropic.types import OutputConfigParam
from pydantic import ValidationError

from parley.ports.model import Completion, ModelError, T, Tier, TokenUsage

Effort = Literal["low", "medium", "high", "xhigh", "max"]

# Thinking and the answer both count toward this limit. Drafts and readings are
# short, so this is generous; hitting it is reported as an error, not truncated.
MAX_TOKENS = 16000


class BedrockModel:
    def __init__(
        self,
        region: str,
        model_ids: dict[Tier, str],
        effort: dict[Tier, Effort],
        client: AnthropicBedrockMantle | None = None,
    ) -> None:
        self._client = client or AnthropicBedrockMantle(aws_region=region)
        self._model_ids = model_ids
        self._effort = effort

    def complete(self, tier: Tier, system: str, prompt: str, output_type: type[T]) -> Completion[T]:
        model_id = self._model_ids[tier]
        started = time.monotonic()
        try:
            response = self._client.messages.parse(
                model=model_id,
                max_tokens=MAX_TOKENS,
                # The system prompt (policy and instructions) is the same on every
                # call, so mark it for prompt caching.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
                output_config=OutputConfigParam(effort=self._effort[tier]),
                output_format=output_type,
            )
        except anthropic.APIError as exc:
            raise ModelError(f"{type(exc).__name__}: {exc}") from exc
        except (ValidationError, ValueError) as exc:
            # The model's output did not fit the requested shape (for example a
            # confidence of 1.5, or text instead of JSON). Treat it like any
            # other failed call; never let it escape as a crash.
            raise ModelError(f"invalid output: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.stop_reason == "refusal":
            raise ModelError("the model declined the request")
        if response.stop_reason == "max_tokens":
            raise ModelError("the output was cut off at the token limit")
        parsed = response.parsed_output
        if parsed is None:
            raise ModelError("the model returned no structured output")

        usage = response.usage
        return Completion(
            output=parsed,
            model=model_id,
            usage=TokenUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
            ),
            latency_ms=latency_ms,
        )
