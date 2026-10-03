"""Amazon Nova 2 Sonic on Bedrock: the speech-to-speech model for calls.

Nova Sonic is reached over one two-way stream per call. Both directions carry
small JSON events. What we send:

    sessionStart, promptStart (voice, audio formats, tools)
    the system prompt as a SYSTEM text block
    one long USER audio block, fed 16 kHz PCM chunks while the call lasts
    a TOOL block for each tool result
    promptEnd, sessionEnd

What comes back: the agent's speech as 24 kHz PCM (`audioOutput`), text for both
sides of the conversation (`textOutput`), tool requests (`toolUse`), and an
"interrupted" signal when the customer talks over the agent. The agent's text
arrives twice: a SPECULATIVE version while it speaks and a FINAL one; only the
FINAL one goes into the transcript.

This module needs AWS credentials with bedrock:InvokeModelWithBidirectionalStream.
It is covered by tests against a fake stream; real calls need AWS access.
"""

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import boto3
from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient
from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from smithy_aws_core.identity import AWSCredentialsIdentity

from parley.ports.voice import (
    AudioOut,
    Interrupted,
    SpeechEvent,
    TextOut,
    ToolRequest,
    ToolSpec,
)

log = logging.getLogger(__name__)

MODEL_ID = "amazon.nova-2-sonic-v1:0"
INPUT_RATE = 16000
OUTPUT_RATE = 24000
# Voice per language. Check the ids against the Nova 2 Sonic voice list for
# your region; both can be changed with PARLEY_VOICE_IDS.
DEFAULT_VOICES = {"en": "kiara", "hi": "kiara", "hinglish": "kiara"}


class Boto3Credentials:
    """Lets the Bedrock streaming SDK use boto3's credential chain (environment,
    profile, or the container's role), so voice finds credentials the same way
    as every other AWS call in this app. boto3 refreshes temporary credentials."""

    def __init__(self) -> None:
        self._credentials = boto3.Session().get_credentials()

    async def get_identity(self, *, properties: Mapping[str, Any]) -> AWSCredentialsIdentity:
        if self._credentials is None:
            raise RuntimeError("no AWS credentials found for Nova Sonic")
        frozen = self._credentials.get_frozen_credentials()
        if not frozen.access_key or not frozen.secret_key:
            raise RuntimeError("incomplete AWS credentials for Nova Sonic")
        return AWSCredentialsIdentity(
            access_key_id=frozen.access_key,
            secret_access_key=frozen.secret_key,
            session_token=frozen.token,
        )

    async def invalidate(self) -> None:
        return None


class NovaSonic:
    input_rate = INPUT_RATE
    output_rate = OUTPUT_RATE

    def __init__(
        self,
        region: str,
        model_id: str = MODEL_ID,
        voices: Mapping[str, str] | None = None,
        client: Any = None,
    ) -> None:
        self.model_id = model_id
        self.region = region
        self.voices = dict(voices or DEFAULT_VOICES)
        self._client = client  # built on first use: the SDK's config is resolved asynchronously

    async def start(
        self, system_prompt: str, tools: list[ToolSpec], language: str
    ) -> "NovaSonicSession":
        if self._client is None:
            config = await AsyncBedrockRuntimeConfig.resolve(
                region=self.region, aws_credentials_identity_resolver=Boto3Credentials()
            )
            self._client = AsyncBedrockRuntimeClient(config=config)
        stream = await self._client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model_id)
        )
        voice = self.voices.get(language) or self.voices.get("en") or "kiara"
        session = NovaSonicSession(stream)
        await session.open(system_prompt, tools, voice)
        return session


class NovaSonicSession:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._prompt = str(uuid.uuid4())
        self._audio = str(uuid.uuid4())
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def open(self, system_prompt: str, tools: list[ToolSpec], voice: str) -> None:
        await self._send(
            {"sessionStart": {"inferenceConfiguration": {"maxTokens": 1024, "temperature": 0.4}}}
        )
        await self._send(
            {
                "promptStart": {
                    "promptName": self._prompt,
                    "textOutputConfiguration": {"mediaType": "text/plain"},
                    "audioOutputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": OUTPUT_RATE,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "voiceId": voice,
                        "encoding": "base64",
                        "audioType": "SPEECH",
                    },
                    "toolUseOutputConfiguration": {"mediaType": "application/json"},
                    "toolConfiguration": {
                        "tools": [
                            {
                                "toolSpec": {
                                    "name": tool.name,
                                    "description": tool.description,
                                    "inputSchema": {"json": json.dumps(tool.input_schema)},
                                }
                            }
                            for tool in tools
                        ]
                    },
                }
            }
        )
        await self._text_block("SYSTEM", system_prompt, interactive=False)
        await self._send(
            {
                "contentStart": {
                    "promptName": self._prompt,
                    "contentName": self._audio,
                    "type": "AUDIO",
                    "interactive": True,
                    "role": "USER",
                    "audioInputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": INPUT_RATE,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "audioType": "SPEECH",
                        "encoding": "base64",
                    },
                }
            }
        )
        # On an outbound call the agent speaks first; this cue starts it.
        await self._text_block("USER", "(The call has been answered.)", interactive=True)

    async def send_audio(self, pcm: bytes) -> None:
        await self._send(
            {
                "audioInput": {
                    "promptName": self._prompt,
                    "contentName": self._audio,
                    "content": base64.b64encode(pcm).decode(),
                }
            }
        )

    async def send_tool_result(self, tool_use_id: str, result: Mapping[str, Any]) -> None:
        name = str(uuid.uuid4())
        await self._send(
            {
                "contentStart": {
                    "promptName": self._prompt,
                    "contentName": name,
                    "interactive": False,
                    "type": "TOOL",
                    "role": "TOOL",
                    "toolResultInputConfiguration": {
                        "toolUseId": tool_use_id,
                        "type": "TEXT",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    },
                }
            }
        )
        await self._send(
            {
                "toolResult": {
                    "promptName": self._prompt,
                    "contentName": name,
                    "content": json.dumps(result),
                }
            }
        )
        await self._send({"contentEnd": {"promptName": self._prompt, "contentName": name}})

    async def events(self) -> AsyncIterator[SpeechEvent]:
        _, output = await self._stream.await_output()
        role, final = "", True
        while True:
            chunk = await output.receive()
            if chunk is None or chunk.value.bytes_ is None:
                return
            event: dict[str, Any] = json.loads(chunk.value.bytes_)["event"]
            if "contentStart" in event:
                start = event["contentStart"]
                role = start.get("role", "")
                fields = json.loads(start.get("additionalModelFields") or "{}")
                final = fields.get("generationStage", "FINAL") == "FINAL"
            elif "textOutput" in event:
                text = event["textOutput"].get("content", "")
                if _is_interruption(text):
                    yield Interrupted()
                elif role == "ASSISTANT" and final:
                    yield TextOut("agent", text)
                elif role == "USER":
                    yield TextOut("customer", text)
            elif "audioOutput" in event:
                yield AudioOut(base64.b64decode(event["audioOutput"]["content"]))
            elif "toolUse" in event:
                use = event["toolUse"]
                arguments = json.loads(use.get("content") or "{}")
                yield ToolRequest(use["toolUseId"], use["toolName"], arguments)
            elif "contentEnd" in event:
                if event["contentEnd"].get("stopReason") == "INTERRUPTED":
                    yield Interrupted()
            elif "completionEnd" in event:
                return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._send(
                {"contentEnd": {"promptName": self._prompt, "contentName": self._audio}}
            )
            await self._send({"promptEnd": {"promptName": self._prompt}})
            await self._send({"sessionEnd": {}})
            await self._stream.input_stream.close()
        except Exception:
            log.warning("closing the Nova Sonic stream failed", exc_info=True)

    async def _text_block(self, role: str, text: str, interactive: bool) -> None:
        name = str(uuid.uuid4())
        await self._send(
            {
                "contentStart": {
                    "promptName": self._prompt,
                    "contentName": name,
                    "type": "TEXT",
                    "interactive": interactive,
                    "role": role,
                    "textInputConfiguration": {"mediaType": "text/plain"},
                }
            }
        )
        await self._send(
            {"textInput": {"promptName": self._prompt, "contentName": name, "content": text}}
        )
        await self._send({"contentEnd": {"promptName": self._prompt, "contentName": name}})

    async def _send(self, event: dict[str, Any]) -> None:
        payload = json.dumps({"event": event}).encode()
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=payload)
        )
        async with self._send_lock:
            await self._stream.input_stream.send(chunk)


def _is_interruption(text: str) -> bool:
    """Nova Sonic signals barge-in as the text `{ "interrupted" : true }`."""
    if '"interrupted"' not in text:
        return False
    try:
        return bool(json.loads(text).get("interrupted"))
    except (ValueError, AttributeError):
        return False
