import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from parley.api.app import create_app
from parley.core.domain import PromiseStatus
from parley.db.models import Case, Promise
from parley.services.due_cases import run_due_cases
from parley.services.runtime import Runtime
from parley.services.tenants import create_tenant
from tests.integration.conftest import invoice_row, write_aging

ROWS = [
    invoice_row("A-1", "Asha", "1000", "2026-01-01"),
    invoice_row("B-1", "Bala", "700", "2026-01-01"),
]


@pytest.fixture
def client(rt: Runtime) -> TestClient:
    return TestClient(create_app(rt))


def new_tenant(rt: Runtime, tmp_path: Path, name: str) -> tuple[uuid.UUID, dict[str, str]]:
    """Create a tenant and return its id and the auth header for it."""
    aging = write_aging(tmp_path / f"{name}.csv", ROWS)
    with rt.session_factory.begin() as session:
        tenant, api_key = create_tenant(
            session, name, "Asia/Kolkata", {"kind": "csv", "invoices_path": str(aging)}
        )
    return tenant.id, {"Authorization": f"Bearer {api_key}"}


def test_health(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_requests_need_a_valid_api_key(client: TestClient) -> None:
    assert client.get("/v1/cases").status_code == 401
    assert client.get("/v1/cases", headers={"Authorization": "Bearer pk_wrong"}).status_code == 401


def test_sync_then_list_and_read_cases(client: TestClient, rt: Runtime, tmp_path: Path) -> None:
    tenant_id, auth = new_tenant(rt, tmp_path, "acme")

    sync = client.post("/v1/sync", headers=auth).json()
    assert (sync["invoices_created"], sync["cases_opened"]) == (2, 2)

    page = client.get("/v1/cases", headers=auth, params={"state": "scheduled"}).json()
    assert page["total"] == 2
    case_id = page["items"][0]["id"]

    run_due_cases(rt, tenant_id)
    detail = client.get(f"/v1/cases/{case_id}", headers=auth).json()
    assert detail["case"]["state"] == "awaiting_reply"
    assert len(detail["messages"]) == 1
    assert detail["messages"][0]["status"] == "drafting"


def test_pause_and_resume(client: TestClient, rt: Runtime, tmp_path: Path) -> None:
    _, auth = new_tenant(rt, tmp_path, "acme")
    client.post("/v1/sync", headers=auth)
    item = client.get("/v1/cases", headers=auth).json()["items"][0]

    assert client.post(f"/v1/cases/{item['id']}/pause", headers=auth).json()["paused"] is True
    assert client.post(f"/v1/cases/{item['id']}/resume", headers=auth).json()["paused"] is False

    customer_id = item["customer"]["id"]
    assert client.post(f"/v1/customers/{customer_id}/pause", headers=auth).json()["paused"] is True


def test_tenants_cannot_see_each_other(client: TestClient, rt: Runtime, tmp_path: Path) -> None:
    _, acme = new_tenant(rt, tmp_path, "acme")
    _, other = new_tenant(rt, tmp_path, "other")
    client.post("/v1/sync", headers=acme)
    case_id = client.get("/v1/cases", headers=acme).json()["items"][0]["id"]

    assert client.get("/v1/cases", headers=other).json()["total"] == 0
    assert client.get(f"/v1/cases/{case_id}", headers=other).status_code == 404
    assert client.post(f"/v1/cases/{case_id}/pause", headers=other).status_code == 404


def test_tasks_list(client: TestClient, rt: Runtime, tmp_path: Path) -> None:
    _, auth = new_tenant(rt, tmp_path, "acme")
    assert client.get("/v1/tasks", headers=auth).json() == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
    }


def test_sync_with_a_bad_file_reports_the_problem(
    client: TestClient, rt: Runtime, tmp_path: Path
) -> None:
    _, auth = new_tenant(rt, tmp_path, "acme")
    (tmp_path / "acme.csv").write_text("invoice_number\nINV-1\n")

    response = client.post("/v1/sync", headers=auth)

    assert response.status_code == 422
    assert "missing columns" in response.json()["detail"]


def test_review_page_is_served(client: TestClient) -> None:
    response = client.get("/review")
    assert response.status_code == 200
    assert "Tasks waiting for a person" in response.text


def test_oversized_bodies_are_refused_before_they_are_read(client: TestClient, rt: Runtime) -> None:
    rt.inbound_secret = "s"

    def chunks():  # type: ignore[no-untyped-def]
        for _ in range(11):
            yield b"x" * (1024 * 1024)

    response = client.post("/v1/inbound/email", content=chunks())
    assert response.status_code == 413


def test_a_source_system_finds_the_case_for_its_own_invoice(
    client: TestClient, rt: Runtime, tmp_path: Path
) -> None:
    _, auth = new_tenant(rt, tmp_path, "acme")
    _, other = new_tenant(rt, tmp_path, "other")  # same invoice numbers, another tenant
    client.post("/v1/sync", headers=auth)
    client.post("/v1/sync", headers=other)

    page = client.get("/v1/cases", headers=auth, params={"invoice_external_id": "A-1"}).json()
    assert page["total"] == 1
    [found] = page["items"]
    assert (found["invoice"]["external_id"], found["invoice"]["source"]) == ("A-1", "csv")

    with rt.session_factory.begin() as session:
        case = session.get_one(Case, found["id"])
        session.add(
            Promise(
                tenant_id=case.tenant_id,
                case_id=case.id,
                amount=50000,
                promised_date=date(2026, 1, 9),
                status=PromiseStatus.OPEN,
            )
        )
    detail = client.get(f"/v1/cases/{found['id']}", headers=auth).json()
    promises = [(p["amount"], p["promised_date"]) for p in detail["promises"]]
    assert promises == [(50000, "2026-01-09")]
    assert detail["disputes"] == []
