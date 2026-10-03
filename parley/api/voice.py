"""Voice endpoints (spec: "Voice").

Twilio (signed with X-Twilio-Signature):
    POST /v1/voice/twilio/answer   who picked up; returns TwiML (talk or hang up)
    POST /v1/voice/twilio/status   the call's final status
    WS   /v1/voice/twilio/stream   the call's audio (Twilio media stream)

Browser test calls:
    GET  /voice                          the microphone page
    POST /v1/cases/{case_id}/test-call   start a test call about one case (API key)
    WS   /v1/voice/browser?token=...     the page's audio
"""

import json
import logging
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from parley.adapters.channels.voice.browser import BrowserLeg
from parley.adapters.channels.voice.twilio import HANG_UP_TWIML, TwilioStreamLeg, TwilioVoice
from parley.api.dependencies import RuntimeDep, SessionDep, TenantDep
from parley.api.schemas import TestCallOut
from parley.ports.voice import VoiceError
from parley.services.calls import answered, create_test_call, provider_ended
from parley.services.runtime import Runtime
from parley.services.voice_bridge import run_call

log = logging.getLogger(__name__)
router = APIRouter()

VOICE_PAGE = (Path(__file__).parent / "voice.html").read_text(encoding="utf-8")
TwilioSignature = Annotated[str | None, Header(alias="X-Twilio-Signature")]

# Twilio's final call statuses that mean nobody answered.
NOT_ANSWERED = {"busy", "no-answer", "failed", "canceled"}


@router.get("/voice", response_class=HTMLResponse, include_in_schema=False)
def voice_page() -> str:
    return VOICE_PAGE


# --- Twilio ------------------------------------------------------------------------


async def _twilio_form(request: Request, rt: Runtime, signature: str | None) -> dict[str, str]:
    """Check Twilio's signature and return the posted form."""
    if not isinstance(rt.voice, TwilioVoice):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Twilio is not configured")
    # Twilio posts application/x-www-form-urlencoded.
    form = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
    # Twilio signs the URL it called, which is our public URL, not the one this
    # server sees behind a load balancer.
    url = rt.voice.public_url + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    if not rt.voice.valid_signature(url, form, signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad Twilio signature")
    return form


@router.post("/v1/voice/twilio/answer", include_in_schema=False)
async def twilio_answer(
    request: Request, rt: RuntimeDep, token: str, signature: TwilioSignature = None
) -> Response:
    form = await _twilio_form(request, rt, signature)
    talk = await run_in_threadpool(answered, rt, token, form.get("AnsweredBy", "unknown"))
    assert isinstance(rt.voice, TwilioVoice)
    twiml = rt.voice.stream_twiml(token) if talk else HANG_UP_TWIML
    return Response(twiml, media_type="application/xml")


@router.post("/v1/voice/twilio/status", include_in_schema=False)
async def twilio_status(
    request: Request, rt: RuntimeDep, token: str, signature: TwilioSignature = None
) -> Response:
    form = await _twilio_form(request, rt, signature)
    call_status = form.get("CallStatus", "")
    if call_status in NOT_ANSWERED or call_status == "completed":
        await run_in_threadpool(provider_ended, rt, token, call_status)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.websocket("/v1/voice/twilio/stream")
async def twilio_stream(websocket: WebSocket) -> None:
    """Not signed: the stream carries the call's random token (sent to Twilio
    only in the TwiML), and the call must be one we placed and is in progress."""
    rt: Runtime = websocket.app.state.runtime
    await websocket.accept()

    async def receive_text() -> str:
        try:
            return await websocket.receive_text()
        except WebSocketDisconnect:
            return json.dumps({"event": "stop"})

    try:
        leg, token = await TwilioStreamLeg.connect(receive_text, websocket.send_text)
        await run_call(rt, token, leg)
    except VoiceError as exc:
        log.info("Twilio stream ended early: %s", exc)
    finally:
        await _close(websocket)


# --- Browser test calls ----------------------------------------------------------


@router.post("/v1/cases/{case_id}/test-call")
def test_call(
    rt: RuntimeDep, session: SessionDep, tenant: TenantDep, case_id: uuid.UUID
) -> TestCallOut:
    """Talk to the voice agent from the browser, playing the customer of this case.
    The call acts on the real case: a promise or dispute made on it counts."""
    if rt.speech is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no speech model is configured")
    try:
        call = create_test_call(rt, session, tenant, case_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "case not found") from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return TestCallOut(call_id=call.id, websocket_path=f"/v1/voice/browser?token={call.token}")


@router.websocket("/v1/voice/browser")
async def browser_stream(websocket: WebSocket, token: str) -> None:
    rt: Runtime = websocket.app.state.runtime
    if rt.speech is None:
        await websocket.close(code=1011)
        return
    await websocket.accept()

    async def receive() -> bytes | str | None:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        return message.get("bytes") or message.get("text") or b""

    leg = BrowserLeg(receive, websocket.send_bytes, websocket.send_text, rt.speech.output_rate)
    try:
        await leg.start()
        await run_call(rt, token, leg)
    finally:
        await _close(websocket)


async def _close(websocket: WebSocket) -> None:
    with suppress(RuntimeError):  # already closed by the other side
        await websocket.close()
