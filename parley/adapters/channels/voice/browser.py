"""A test call from a browser microphone (spec, M7 build order: "browser
microphone" first).

The page `/voice` sends the microphone as binary WebSocket frames of 16-bit PCM
at 16 kHz, and plays back binary frames of 16-bit PCM at the speech model's
output rate. Text frames carry JSON: the transcript as it happens, "clear"
when the customer interrupts, and "end".
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

BROWSER_INPUT_RATE = 16000


class BrowserLeg:
    in_rate = BROWSER_INPUT_RATE

    def __init__(
        self,
        receive: Callable[[], Awaitable[bytes | str | None]],
        send_bytes: Callable[[bytes], Awaitable[None]],
        send_text: Callable[[str], Awaitable[None]],
        out_rate: int,
    ) -> None:
        self._receive = receive
        self._send_bytes = send_bytes
        self._send_text = send_text
        self.out_rate = out_rate
        self._ended = False

    async def start(self) -> None:
        """Tell the page the sample rates to use."""
        await self._send({"type": "start", "in_rate": self.in_rate, "out_rate": self.out_rate})

    async def receive(self) -> bytes | None:
        while not self._ended:
            frame = await self._receive()
            if frame is None:  # the page closed the socket
                self._ended = True
            elif isinstance(frame, bytes):
                return frame
            elif json.loads(frame).get("type") == "hang_up":
                self._ended = True
        return None

    async def play(self, pcm: bytes) -> None:
        await self._send_bytes(pcm)

    async def clear(self) -> None:
        await self._send({"type": "clear"})

    async def show(self, role: str, text: str) -> None:
        await self._send({"type": "transcript", "role": role, "text": text})

    async def hang_up(self) -> None:
        if not self._ended:
            self._ended = True
            await self._send({"type": "end"})

    async def _send(self, message: dict[str, Any]) -> None:
        await self._send_text(json.dumps(message))
