"""The voice bridge: carries one live call between the customer and the
speech model (spec: "Voice service").

Two loops run side by side for the length of the call:

    customer audio ──▶ resample ──▶ speech model                 (`_listen`)
    speech model ──▶ audio: resample ──▶ customer                (`_speak`)
                 ──▶ transcript line: saved, shown on the browser page
                 ──▶ tool request: run by code, result sent back
                 ──▶ interruption: drop audio the customer has not heard yet

The call ends when the customer hangs up, the agent calls `end_call`, or the
time limit passes. Whatever ends it, `finish_call` then runs the workflow and
the transcript audit.

Database work (tools, transcript lines) is synchronous code, so it runs in a
worker thread (`asyncio.to_thread`) and never blocks the audio.
"""

import asyncio
import logging
from contextlib import suppress

from parley.adapters.channels.voice.audio import resample
from parley.collections_ai.voice import VOICE_TOOLS
from parley.ports.voice import (
    AudioOut,
    CallLeg,
    Interrupted,
    SpeechSession,
    TextOut,
    ToolRequest,
)
from parley.services.calls import CallStart, finish_call, record_turn, run_tool, start_call
from parley.services.runtime import Runtime

log = logging.getLogger(__name__)

# After end_call, wait this long so the goodbye already sent to the phone is heard.
END_GRACE_SECONDS = 2.5


async def run_call(
    rt: Runtime, token: str, leg: CallLeg, end_grace: float = END_GRACE_SECONDS
) -> None:
    """Hold the conversation for the call with this token, then finish it."""
    if rt.speech is None:
        log.error("a call was connected but no speech model is configured")
        await leg.hang_up()
        return
    start = await asyncio.to_thread(start_call, rt, token)
    if start is None:
        log.warning("a call stream arrived for an unknown or finished call")
        await leg.hang_up()
        return

    reached = True
    session: SpeechSession | None = None
    try:
        session = await rt.speech.start(start.system_prompt, VOICE_TOOLS, start.language)
        bridge = _Bridge(rt, start, leg, session, rt.speech.input_rate, rt.speech.output_rate)
        await asyncio.wait_for(bridge.run(end_grace), timeout=rt.voice_max_seconds)
    except TimeoutError:
        log.info("call %s reached its time limit", start.call_id)
    except Exception:
        log.exception("call %s failed", start.call_id)
    finally:
        if session is not None:
            await session.close()
        with suppress(Exception):
            await leg.hang_up()
        await asyncio.to_thread(finish_call, rt, token, reached)


class _Bridge:
    def __init__(
        self,
        rt: Runtime,
        start: CallStart,
        leg: CallLeg,
        session: SpeechSession,
        model_in_rate: int,
        model_out_rate: int,
    ) -> None:
        self.rt = rt
        self.call_id = start.call_id
        self.leg = leg
        self.session = session
        self.model_in_rate = model_in_rate
        self.model_out_rate = model_out_rate

    async def run(self, end_grace: float) -> None:
        """Run both loops; when either stops (hang-up, end_call), stop the other."""
        tasks = [
            asyncio.create_task(self._listen()),
            asyncio.create_task(self._speak(end_grace)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raise an error from the loop that stopped
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _listen(self) -> None:
        while (pcm := await self.leg.receive()) is not None:
            await self.session.send_audio(resample(pcm, self.leg.in_rate, self.model_in_rate))

    async def _speak(self, end_grace: float) -> None:
        async for event in self.session.events():
            match event:
                case AudioOut(pcm=pcm):
                    await self.leg.play(resample(pcm, self.model_out_rate, self.leg.out_rate))
                case Interrupted():
                    await self.leg.clear()
                case TextOut(role=role, text=text):
                    await asyncio.to_thread(record_turn, self.rt, self.call_id, role, text)
                    await self.leg.show(role, text)
                case ToolRequest(tool_use_id=tool_use_id, name=name, arguments=arguments):
                    result = await asyncio.to_thread(
                        run_tool, self.rt, self.call_id, name, arguments
                    )
                    await self.leg.show(
                        "tool", f"{name}: {'ok' if result.get('ok') else 'refused'}"
                    )
                    if result.get("end"):
                        await asyncio.sleep(end_grace)
                        return
                    await self.session.send_tool_result(tool_use_id, result)
