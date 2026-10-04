"""The voice endpoints: Twilio's signed callbacks and media stream, and the
browser test call."""

import base64
import hashlib
import hmac
import json
from pathlib import Path

import httpx2 as httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from parley.adapters.channels.voice.audio import pcm16_to_ulaw
from parley.adapters.channels.voice.twilio import TwilioVoice
from parley.api.app import create_app
from parley.core.domain import CallStatus, CaseState
from parley.db.models import Call, Case
from parley.ports.voice import ToolRequest
from parley.services.runtime import Runtime
from parley.services.tenants import create_tenant
from parley.services.worker import run_once
from tests.integration.conftest import invoice_row, write_aging
from tests.integration.test_voice import CONFIRM, DAY, END, HELLO, NUMBER, PHONE, YES
from tests.integration.voice_fakes import ScriptedSpeech

PUBLIC = "https://parley.example"


def setup(rt: Runtime, tmp_path: Path, speech: ScriptedSpeech) -> tuple[TestClient, str]:
    def twilio_api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"sid": "CA42"})

    rt.voice = TwilioVoice(
        "AC1", "auth-token", "+15550001111", PUBLIC, httpx.MockTransport(twilio_api)
    )
    rt.speech = speech
    rt.voice_allowed_numbers = frozenset({NUMBER})
    aging = write_aging(
        tmp_path / "aging.csv",
        [{**invoice_row("A-1", "Asha", "1000", "2026-01-01"), "phone": PHONE}],
    )
    with rt.session_factory.begin() as session:
        _, api_key = create_tenant(
            session,
            "Acme Traders",
            "Asia/Kolkata",
            {"kind": "csv", "invoices_path": str(aging)},
            policy_overrides={"approval_mode": "none", "call_from_reminder": 1},
        )
    return TestClient(create_app(rt)), api_key


def twilio_post(client: TestClient, path: str, form: dict[str, str]) -> httpx.Response:
    payload = PUBLIC + path + "".join(f"{k}{form[k]}" for k in sorted(form))
    digest = hmac.new(b"auth-token", payload.encode(), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode()
    return client.post(path, data=form, headers={"X-Twilio-Signature": signature})


def call_row(rt: Runtime) -> Call:
    with rt.session_factory() as session:
        return session.scalars(select(Call)).one()


def test_a_phone_call_through_the_twilio_endpoints(rt: Runtime, tmp_path: Path) -> None:
    speech = ScriptedSpeech([HELLO, YES, CONFIRM, END])
    client, _ = setup(rt, tmp_path, speech)
    run_once(rt, sync_interval=DAY)
    call = call_row(rt)
    assert call.provider_call_id == "CA42"

    # A forged callback is refused.
    forged = client.post(
        f"/v1/voice/twilio/answer?token={call.token}",
        data={"AnsweredBy": "human"},
        headers={"X-Twilio-Signature": "bad"},
    )
    assert forged.status_code == 403

    answer = twilio_post(
        client, f"/v1/voice/twilio/answer?token={call.token}", {"AnsweredBy": "human"}
    )
    assert answer.status_code == 200
    assert '<Stream url="wss://parley.example/v1/voice/twilio/stream">' in answer.text
    assert f'value="{call.token}"' in answer.text

    with client.websocket_connect("/v1/voice/twilio/stream") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        start = {"streamSid": "MZ1", "callSid": "CA42", "customParameters": {"token": call.token}}
        ws.send_text(json.dumps({"event": "start", "start": start}))
        media = base64.b64encode(pcm16_to_ulaw(b"\x00\x00" * 160)).decode()
        ws.send_text(json.dumps({"event": "media", "media": {"payload": media}}))
        # The agent says its lines and ends the call; the server closes the socket.
        while True:
            try:
                ws.receive_text()
            except Exception:
                break

    assert call_row(rt).status == CallStatus.ANSWERED
    status = twilio_post(
        client, f"/v1/voice/twilio/status?token={call.token}", {"CallStatus": "completed"}
    )
    assert status.status_code == 204
    assert call_row(rt).status == CallStatus.ANSWERED  # the bridge already finished it


def test_twilio_answering_machine_hangs_up(rt: Runtime, tmp_path: Path) -> None:
    client, _ = setup(rt, tmp_path, ScriptedSpeech([]))
    run_once(rt, sync_interval=DAY)
    token = call_row(rt).token
    answer = twilio_post(
        client, f"/v1/voice/twilio/answer?token={token}", {"AnsweredBy": "machine_start"}
    )
    assert answer.text == "<Response><Hangup/></Response>"
    assert call_row(rt).status == CallStatus.NOT_REACHED


def test_a_browser_test_call(rt: Runtime, tmp_path: Path) -> None:
    promise = ToolRequest("t3", "log_promise", {"promised_date": "2026-01-09"})
    speech = ScriptedSpeech([HELLO, YES, CONFIRM, promise, END])
    client, api_key = setup(rt, tmp_path, speech)
    rt.voice_allowed_numbers = frozenset()  # no phone calls: email reminders
    run_once(rt, sync_interval=DAY)
    with rt.session_factory() as session:
        case_id = session.scalars(select(Case.id)).one()
    auth = {"Authorization": f"Bearer {api_key}"}

    assert client.post(f"/v1/cases/{case_id}/test-call").status_code == 401
    started = client.post(f"/v1/cases/{case_id}/test-call", headers=auth)
    assert started.status_code == 200

    shown = []
    with client.websocket_connect(started.json()["websocket_path"]) as ws:
        assert json.loads(ws.receive_text()) == {
            "type": "start",
            "in_rate": 16000,
            "out_rate": 24000,
        }
        ws.send_bytes(b"\x00\x00" * 320)
        while True:
            message = json.loads(ws.receive_text())
            if message["type"] == "end":
                break
            shown.append((message["role"], message["text"]))

    assert shown[0] == ("agent", HELLO.text)
    assert ("tool", "log_promise: ok") in shown
    with rt.session_factory() as session:
        assert session.scalars(select(Case.state)).one() == CaseState.PROMISED
        assert session.scalars(select(Call.test)).one() is True

    # A spent token cannot start another conversation.
    with client.websocket_connect(started.json()["websocket_path"]) as ws:
        ws.receive_text()  # "start"
        assert json.loads(ws.receive_text()) == {"type": "end"}


def test_a_phone_calls_token_cannot_open_a_browser_conversation(
    rt: Runtime, tmp_path: Path
) -> None:
    client, _ = setup(rt, tmp_path, ScriptedSpeech([HELLO, END]))
    run_once(rt, sync_interval=DAY)
    token = call_row(rt).token
    twilio_post(client, f"/v1/voice/twilio/answer?token={token}", {"AnsweredBy": "human"})

    with client.websocket_connect(f"/v1/voice/browser?token={token}") as ws:
        ws.receive_text()  # "start"
        assert json.loads(ws.receive_text()) == {"type": "end"}
    assert call_row(rt).connected_at is None  # the real stream can still connect
