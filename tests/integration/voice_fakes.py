"""Test doubles for calls: a scripted speech model, a phone leg, and a dialler."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from parley.ports.voice import SpeechEvent, ToolRequest, ToolSpec


class ScriptedSpeech:
    """A speech model that plays a fixed script: lines for the agent and the
    customer, and tool requests. Each tool request waits for its result before
    the script goes on, as the real model does."""

    input_rate = 16000
    output_rate = 24000

    def __init__(self, script: list[SpeechEvent]) -> None:
        self.script = script
        self.sessions: list[ScriptedSession] = []

    async def start(
        self, system_prompt: str, tools: list[ToolSpec], language: str
    ) -> "ScriptedSession":
        session = ScriptedSession(self.script, system_prompt, [t.name for t in tools], language)
        self.sessions.append(session)
        return session


@dataclass
class ScriptedSession:
    script: list[SpeechEvent]
    system_prompt: str
    tools: list[str]
    language: str
    audio_in: int = 0
    results: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    closed: bool = False

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_in += len(pcm)

    async def send_tool_result(self, tool_use_id: str, result: Mapping[str, Any]) -> None:
        self.results[tool_use_id] = result

    async def events(self) -> AsyncIterator[SpeechEvent]:
        for event in self.script:
            yield event
            await asyncio.sleep(0)  # let the other loop run, as real audio would

    async def close(self) -> None:
        self.closed = True

    def result(self, name: str) -> Mapping[str, Any]:
        for event in self.script:
            if isinstance(event, ToolRequest) and event.name == name:
                return self.results[event.tool_use_id]
        raise KeyError(name)


class FakeLeg:
    """The customer's phone: sends one chunk of audio, then stays on the line
    until the agent hangs up."""

    in_rate = 8000
    out_rate = 8000

    def __init__(self) -> None:
        self.played = 0
        self.cleared = 0
        self.shown: list[tuple[str, str]] = []
        self._sent = False
        self._hung_up = asyncio.Event()

    async def receive(self) -> bytes | None:
        if not self._sent:
            self._sent = True
            return b"\x00\x00" * 160
        await self._hung_up.wait()
        return None

    async def play(self, pcm: bytes) -> None:
        self.played += len(pcm)

    async def clear(self) -> None:
        self.cleared += 1

    async def show(self, role: str, text: str) -> None:
        self.shown.append((role, text))

    async def hang_up(self) -> None:
        self._hung_up.set()


@dataclass
class FakeDialler:
    placed: list[tuple[str, str]] = field(default_factory=list)
    transfers: list[tuple[str, str]] = field(default_factory=list)

    def place_call(self, to_number: str, token: str) -> str:
        self.placed.append((to_number, token))
        return f"CA{len(self.placed)}"

    def transfer(self, provider_call_id: str, to_number: str) -> None:
        self.transfers.append((provider_call_id, to_number))

    def hang_up(self, provider_call_id: str) -> None:
        return None
