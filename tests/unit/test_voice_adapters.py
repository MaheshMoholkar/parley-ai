"""Audio conversion, the Twilio adapter, and the Nova Sonic event protocol,
without a network: Twilio's API is a MockTransport and Nova Sonic's stream a fake."""

import asyncio
import base64
import hashlib
import hmac
import json
import math
from array import array
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx2 as httpx
import pytest

from parley.adapters.channels.voice.audio import pcm16_to_ulaw, resample, ulaw_to_pcm16
from parley.adapters.channels.voice.twilio import TwilioStreamLeg, TwilioVoice
from parley.adapters.models.nova_sonic import NovaSonic
from parley.ports.voice import AudioOut, Interrupted, TextOut, ToolRequest, ToolSpec, VoiceError

# --- Audio -------------------------------------------------------------------------


def tone(rate: int, ms: int, amplitude: int = 8000) -> bytes:
    count = rate * ms // 1000
    return array(
        "h", (int(amplitude * math.sin(2 * math.pi * 440 * i / rate)) for i in range(count))
    ).tobytes()


def samples(pcm: bytes) -> array[int]:
    out = array("h")
    out.frombytes(pcm)
    return out


def test_ulaw_round_trip_is_close() -> None:
    pcm = tone(8000, 20)
    back = ulaw_to_pcm16(pcm16_to_ulaw(pcm))
    assert len(pcm16_to_ulaw(pcm)) == 160  # one byte per sample
    errors = [abs(a - b) for a, b in zip(samples(pcm), samples(back), strict=True)]
    assert max(errors) < 300  # μ-law keeps about 13 bits of an 8000-high wave


def test_resampling_changes_the_length_and_keeps_the_shape() -> None:
    pcm = tone(24000, 20)
    down = resample(pcm, 24000, 8000)
    assert len(down) == len(pcm) // 3
    up = resample(down, 8000, 16000)
    assert len(up) == 2 * len(down)
    assert max(samples(up)) > 6000  # still a loud tone, not silence or noise
    assert resample(pcm, 16000, 16000) is pcm


# --- Twilio --------------------------------------------------------------------------


def twilio(handler: Any) -> TwilioVoice:
    return TwilioVoice(
        "AC123", "secret", "+15550001111", "https://parley.example", httpx.MockTransport(handler)
    )


def test_place_call_asks_twilio_to_ring_and_call_back() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"sid": "CA999"})

    assert twilio(handler).place_call("+919812345678", "tok") == "CA999"
    form = {k: v[0] for k, v in parse_qs(sent[0].content.decode()).items()}
    assert sent[0].url.path == "/2010-04-01/Accounts/AC123/Calls.json"
    assert form["To"] == "+919812345678"
    assert form["Url"] == "https://parley.example/v1/voice/twilio/answer?token=tok"
    assert form["StatusCallback"] == "https://parley.example/v1/voice/twilio/status?token=tok"
    assert form["MachineDetection"] == "Enable"


def test_twilio_errors_say_whether_a_retry_is_safe() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "invalid number"})

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(VoiceError) as error:
        twilio(refused).place_call("+919812345678", "t")
    assert error.value.retryable is False and "400" in str(error.value)

    with pytest.raises(VoiceError) as error:
        twilio(timeout).place_call("+919812345678", "t")
    assert error.value.retryable is False  # Twilio may already be dialling


def test_twilio_signatures_cover_the_url_and_every_parameter() -> None:
    voice = twilio(lambda r: httpx.Response(200))
    url = "https://parley.example/v1/voice/twilio/answer?token=tok"
    params = {"CallSid": "CA1", "AnsweredBy": "human"}
    payload = url + "AnsweredByhumanCallSidCA1"
    good = base64.b64encode(hmac.new(b"secret", payload.encode(), hashlib.sha1).digest()).decode()

    assert voice.valid_signature(url, params, good)
    assert not voice.valid_signature(url, {**params, "AnsweredBy": "machine_start"}, good)
    assert not voice.valid_signature(url + "x", params, good)
    assert not voice.valid_signature(url, params, None)


def test_the_stream_leg_speaks_twilio_media_messages() -> None:
    incoming = [
        {"event": "connected"},
        {
            "event": "start",
            "start": {"streamSid": "MZ1", "callSid": "CA1", "customParameters": {"token": "tok"}},
        },
        {
            "event": "media",
            "media": {"payload": base64.b64encode(pcm16_to_ulaw(tone(8000, 20))).decode()},
        },
        {"event": "stop"},
    ]
    sent: list[dict[str, Any]] = []

    async def receive_text() -> str:
        return json.dumps(incoming.pop(0))

    async def send_text(text: str) -> None:
        sent.append(json.loads(text))

    async def run() -> None:
        leg, token = await TwilioStreamLeg.connect(receive_text, send_text)
        assert (token, leg.stream_sid, leg.call_sid) == ("tok", "MZ1", "CA1")
        audio = await leg.receive()
        assert audio is not None and len(audio) == 320  # 160 samples of 16-bit PCM
        assert await leg.receive() is None  # "stop": the caller hung up
        await leg.play(tone(8000, 20))
        await leg.clear()

    asyncio.run(run())
    assert sent[0]["event"] == "media" and sent[0]["streamSid"] == "MZ1"
    assert len(base64.b64decode(sent[0]["media"]["payload"])) == 160
    assert sent[1] == {"event": "clear", "streamSid": "MZ1"}


# --- Nova Sonic ------------------------------------------------------------------------


class FakeStream:
    """Stands in for the SDK's two-way stream."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.sent: list[dict[str, Any]] = []
        self.replies = replies
        self.input_stream = SimpleNamespace(send=self._send, close=self._close)

    async def _send(self, chunk: Any) -> None:
        self.sent.append(json.loads(chunk.value.bytes_)["event"])

    async def _close(self) -> None:
        self.sent.append({"closed": True})

    async def await_output(self) -> tuple[None, Any]:
        replies = list(self.replies)

        async def receive() -> Any:
            if not replies:
                return None
            payload = json.dumps({"event": replies.pop(0)}).encode()
            return SimpleNamespace(value=SimpleNamespace(bytes_=payload))

        return None, SimpleNamespace(receive=receive)


class FakeClient:
    def __init__(self, stream: FakeStream) -> None:
        self.stream = stream

    async def invoke_model_with_bidirectional_stream(self, request: Any) -> FakeStream:
        assert request.model_id == "amazon.nova-2-sonic-v1:0"
        return self.stream


def speculative(role: str) -> dict[str, Any]:
    stage = json.dumps({"generationStage": "SPECULATIVE"})
    return {"contentStart": {"role": role, "additionalModelFields": stage}}


def final(role: str) -> dict[str, Any]:
    stage = json.dumps({"generationStage": "FINAL"})
    return {"contentStart": {"role": role, "additionalModelFields": stage}}


def test_nova_sonic_session_protocol() -> None:
    replies = [
        {"completionStart": {}},
        {"contentStart": {"role": "USER"}},
        {"textOutput": {"content": "Hello?"}},
        speculative("ASSISTANT"),
        {"textOutput": {"content": "Hi, I am an AI assistant."}},  # spoken draft: skipped
        final("ASSISTANT"),
        {"textOutput": {"content": "Hi, I am an AI assistant."}},
        {"audioOutput": {"content": base64.b64encode(b"\x01\x00" * 4).decode()}},
        {"textOutput": {"content": '{ "interrupted" : true }'}},
        {"toolUse": {"toolUseId": "t1", "toolName": "get_invoice", "content": "{}"}},
        {"contentEnd": {"stopReason": "INTERRUPTED"}},
        {"completionEnd": {}},
        {"textOutput": {"content": "never read"}},
    ]
    stream = FakeStream(replies)
    sonic = NovaSonic("ap-south-1", client=FakeClient(stream))
    tools = [ToolSpec("get_invoice", "Invoices", {"type": "object"})]

    async def run() -> list[Any]:
        session = await sonic.start("You are polite.", tools, "hi")
        await session.send_audio(b"\x00\x00" * 160)
        await session.send_tool_result("t1", {"ok": True})
        events: AsyncIterator[Any] = session.events()
        received = [event async for event in events]
        await session.close()
        return received

    received = asyncio.run(run())
    assert received == [
        TextOut("customer", "Hello?"),
        TextOut("agent", "Hi, I am an AI assistant."),
        AudioOut(b"\x01\x00" * 4),
        Interrupted(),
        ToolRequest("t1", "get_invoice", {}),
        Interrupted(),
    ]

    kinds = [next(iter(event)) for event in stream.sent]
    assert kinds[:3] == ["sessionStart", "promptStart", "contentStart"]
    prompt_start = stream.sent[1]["promptStart"]
    assert prompt_start["audioOutputConfiguration"]["sampleRateHertz"] == 24000
    assert prompt_start["audioOutputConfiguration"]["voiceId"] == "kiara"
    spec = prompt_start["toolConfiguration"]["tools"][0]["toolSpec"]
    assert spec["name"] == "get_invoice" and json.loads(spec["inputSchema"]["json"]) == {
        "type": "object"
    }
    assert stream.sent[3]["textInput"]["content"] == "You are polite."
    assert "audioInput" in kinds and "toolResult" in kinds
    assert kinds[-4:] == ["contentEnd", "promptEnd", "sessionEnd", "closed"]
