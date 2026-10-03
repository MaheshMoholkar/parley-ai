"""The voice interfaces (spec: "Voice").

Two outside systems take part in a call:

- a telephony provider, which dials the customer and streams the call's audio
  (`CallPlacer`);
- a speech-to-speech model, which listens, talks, and asks for tools
  (`SpeechModel` and the `SpeechSession` it opens for each call).

Audio crossing these interfaces is always 16-bit little-endian mono PCM at the
sample rate the speech model states (`input_rate`, `output_rate`). Converting
to and from the telephone's own format is the telephony adapter's job.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class VoiceError(RuntimeError):
    """A call could not be placed or changed.

    `retryable` is False when the provider may already have acted (for example
    the request timed out after it was sent): retrying could ring the customer
    twice, so a person decides instead.
    """

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class CallPlacer(Protocol):
    def place_call(self, to_number: str, token: str) -> str:
        """Start ringing `to_number` and return the provider's call id. `token`
        identifies the call when the provider connects its audio back to us."""
        ...

    def transfer(self, provider_call_id: str, to_number: str) -> None:
        """Hand a live call over to a person's phone."""
        ...

    def hang_up(self, provider_call_id: str) -> None: ...


# --- Speech model ---------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # JSON Schema of the tool's input.
    input_schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AudioOut:
    pcm: bytes  # at the model's output_rate


@dataclass(frozen=True)
class TextOut:
    """A finished line of the transcript: what the agent said, or what the
    model heard the customer say."""

    role: Literal["agent", "customer"]
    text: str


@dataclass(frozen=True)
class ToolRequest:
    tool_use_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Interrupted:
    """The customer started talking over the agent: drop audio not yet played."""


SpeechEvent = AudioOut | TextOut | ToolRequest | Interrupted


class SpeechSession(Protocol):
    async def send_audio(self, pcm: bytes) -> None:
        """Customer audio at the model's input_rate."""
        ...

    async def send_tool_result(self, tool_use_id: str, result: Mapping[str, Any]) -> None: ...

    def events(self) -> AsyncIterator[SpeechEvent]:
        """What the model produces, until the session ends."""
        ...

    async def close(self) -> None: ...


class SpeechModel(Protocol):
    input_rate: int
    output_rate: int

    async def start(
        self, system_prompt: str, tools: list[ToolSpec], language: str
    ) -> SpeechSession: ...


# --- The caller's side of a call ------------------------------------------------


class CallLeg(Protocol):
    """The customer's end of a live call: a phone line, or a browser microphone
    for test calls. Audio is 16-bit mono PCM at the leg's own rates."""

    in_rate: int  # sample rate of audio from the customer
    out_rate: int  # sample rate the leg wants for audio to the customer

    async def receive(self) -> bytes | None:
        """The next chunk of the customer's audio; None once they hang up."""
        ...

    async def play(self, pcm: bytes) -> None: ...

    async def clear(self) -> None:
        """Drop audio sent to `play` that the customer has not heard yet."""
        ...

    async def show(self, role: str, text: str) -> None:
        """A line of transcript, for legs that can display it (the browser)."""
        ...

    async def hang_up(self) -> None: ...
