"""Twilio: places calls and carries their audio (spec: "Voice").

How a call runs:

1. `place_call` asks Twilio's REST API to ring the customer. Twilio waits until
   it knows whether a person or an answering machine picked up, then asks
   `{public_url}/v1/voice/twilio/answer` what to do.
2. For a person, the answer is TwiML that connects the call's audio to our
   WebSocket (`/v1/voice/twilio/stream`) as a media stream; for a machine, a
   hang-up.
3. On the stream, `TwilioStreamLeg` turns Twilio's JSON messages (μ-law audio
   at 8 kHz, base64) into PCM and back.
4. When the call ends, Twilio posts its final status to
   `/v1/voice/twilio/status`.

Every request from Twilio is signed (X-Twilio-Signature) with the account's
auth token; `valid_signature` checks it.
"""

import base64
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx2 as httpx

from parley.adapters.channels.voice.audio import pcm16_to_ulaw, ulaw_to_pcm16
from parley.ports.voice import VoiceError

API_BASE = "https://api.twilio.com/2010-04-01"
RING_SECONDS = 30
# Hard limit on a call's length, enforced by Twilio as well as by the bridge.
MAX_CALL_SECONDS = 480


class TwilioVoice:
    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        public_url: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.public_url = public_url.rstrip("/")
        self._http = httpx.Client(
            base_url=f"{API_BASE}/Accounts/{account_sid}",
            auth=(account_sid, auth_token),
            timeout=15.0,
            transport=transport,
        )

    # --- CallPlacer --------------------------------------------------------------

    def place_call(self, to_number: str, token: str) -> str:
        form = {
            "To": to_number,
            "From": self.from_number,
            "Url": f"{self.public_url}/v1/voice/twilio/answer?token={token}",
            "StatusCallback": f"{self.public_url}/v1/voice/twilio/status?token={token}",
            # Wait for answering-machine detection before asking for the answer TwiML.
            "MachineDetection": "Enable",
            "Timeout": str(RING_SECONDS),
            "TimeLimit": str(MAX_CALL_SECONDS),
        }
        call = self._post("/Calls.json", form)
        return str(call["sid"])

    def transfer(self, provider_call_id: str, to_number: str) -> None:
        twiml = f"<Response><Dial>{escape(to_number)}</Dial></Response>"
        self._post(f"/Calls/{provider_call_id}.json", {"Twiml": twiml})

    def hang_up(self, provider_call_id: str) -> None:
        self._post(f"/Calls/{provider_call_id}.json", {"Status": "completed"})

    def _post(self, path: str, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._http.post(path, data=form)
        except httpx.TimeoutException as exc:
            # Twilio may have acted on the request; retrying could ring twice.
            raise VoiceError(f"Twilio timed out: {exc}", retryable=False) from None
        except httpx.TransportError as exc:
            raise VoiceError(f"could not reach Twilio: {exc}", retryable=True) from None
        if response.status_code >= 400:
            # Twilio refused, so nothing happened; 4xx will fail again, but the
            # retry limit bounds that and the error is kept for a person.
            raise VoiceError(
                f"Twilio answered {response.status_code}: {response.text[:300]}",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        result: dict[str, Any] = response.json()
        return result

    # --- Webhooks from Twilio ----------------------------------------------------

    def valid_signature(self, url: str, params: Mapping[str, str], signature: str | None) -> bool:
        """Twilio signs the full URL followed by each POST parameter (name then
        value, sorted by name) with HMAC-SHA1 under the auth token."""
        if not signature:
            return False
        payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
        digest = hmac.new(self.auth_token.encode(), payload.encode(), hashlib.sha1).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), signature)

    def stream_twiml(self, token: str) -> str:
        """Connect the answered call's audio to our WebSocket."""
        ws_url = self.public_url.replace("https://", "wss://").replace("http://", "ws://")
        return (
            "<Response><Connect>"
            f"<Stream url={quoteattr(ws_url + '/v1/voice/twilio/stream')}>"
            f'<Parameter name="token" value={quoteattr(token)}/>'
            "</Stream></Connect></Response>"
        )


HANG_UP_TWIML = "<Response><Hangup/></Response>"


class TwilioStreamLeg:
    """A live call's audio over a Twilio media stream WebSocket.

    Built with the WebSocket's receive and send functions, so this adapter does
    not depend on the web framework.
    """

    in_rate = 8000
    out_rate = 8000

    def __init__(
        self,
        receive_text: Callable[[], Awaitable[str]],
        send_text: Callable[[str], Awaitable[None]],
        stream_sid: str,
        call_sid: str,
    ) -> None:
        self._receive_text = receive_text
        self._send_text = send_text
        self.stream_sid = stream_sid
        self.call_sid = call_sid
        self._ended = False

    @classmethod
    async def connect(
        cls,
        receive_text: Callable[[], Awaitable[str]],
        send_text: Callable[[str], Awaitable[None]],
    ) -> tuple["TwilioStreamLeg", str]:
        """Read Twilio's opening messages and return the leg and the call token
        (sent as a custom parameter of the stream)."""
        while True:
            message = json.loads(await receive_text())
            if message.get("event") == "start":
                start = message["start"]
                token = str(start.get("customParameters", {}).get("token", ""))
                leg = cls(receive_text, send_text, start["streamSid"], start["callSid"])
                return leg, token
            if message.get("event") == "stop":
                raise VoiceError("the stream stopped before it started", retryable=False)

    async def receive(self) -> bytes | None:
        while not self._ended:
            message = json.loads(await self._receive_text())
            match message.get("event"):
                case "media":
                    return ulaw_to_pcm16(base64.b64decode(message["media"]["payload"]))
                case "stop":
                    self._ended = True
        return None

    async def play(self, pcm: bytes) -> None:
        payload = base64.b64encode(pcm16_to_ulaw(pcm)).decode()
        await self._send(
            {"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}}
        )

    async def clear(self) -> None:
        await self._send({"event": "clear", "streamSid": self.stream_sid})

    async def show(self, role: str, text: str) -> None:
        return None  # a phone has no screen

    async def hang_up(self) -> None:
        self._ended = True  # the route closes the socket, which ends the call

    async def _send(self, message: dict[str, Any]) -> None:
        await self._send_text(json.dumps(message))
