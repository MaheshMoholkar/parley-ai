"""The parley command: creating a tenant without printing its credentials."""

import json
from pathlib import Path

import pytest

from parley import cli
from parley.services.runtime import Runtime
from tests.integration.conftest import invoice_row, write_aging


def test_create_tenant_can_save_credentials_instead_of_printing_them(
    rt: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    saved: dict[str, str] = {}

    def store(name: str, value: str) -> str:
        saved[name] = value
        return f"arn:aws:secretsmanager:x:1:secret:{name}"

    monkeypatch.setattr(cli, "build_runtime", lambda settings: rt)
    monkeypatch.setattr(cli, "store_secret", store)
    aging = write_aging(tmp_path / "a.csv", [invoice_row("A-1", "Asha", "1", "2026-01-01")])

    cli.main(
        ["create-tenant", "--name", "Acme", "--invoices-csv", str(aging),
         "--save-to-secret", "parley/tenants/acme"]
    )  # fmt: skip

    credentials = json.loads(saved["parley/tenants/acme"])
    assert credentials["api_key"].startswith("pk_")
    printed = capsys.readouterr().out
    assert credentials["api_key"] not in printed
    assert credentials["webhook_secret"] not in printed
    assert "saved to arn:aws:secretsmanager" in printed
