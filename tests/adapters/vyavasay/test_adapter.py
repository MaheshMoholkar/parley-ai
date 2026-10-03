"""The Vyavasay adapter against a fake Vyavasay server: authentication,
paging, and how Vyavasay's records are turned into the core's."""

from datetime import date

import pytest

from parley.adapters.accounting import AdapterConfigError
from parley.adapters.accounting.vyavasay import from_config
from parley.adapters.accounting.vyavasay.adapter import VyavasayAccountingAdapter
from parley.adapters.accounting.vyavasay.client import PAGE_SIZE, VyavasayClient, VyavasayError
from parley.core.domain import InvoiceStatus
from tests.adapters.vyavasay.fake_server import PASSWORD, PHONE, VY_TENANT, FakeVyavasay

BASE = "https://vyavasay.test"


@pytest.fixture
def server() -> FakeVyavasay:
    server = FakeVyavasay()
    server.add_party("p1", "Asha Traders", email="asha@example.com")
    return server


def adapter_for(server: FakeVyavasay) -> VyavasayAccountingAdapter:
    client = VyavasayClient(BASE, token=server.api_token, transport=server.transport())
    return VyavasayAccountingAdapter(client)


def test_open_invoices_are_mapped_into_minor_units() -> None:
    server = FakeVyavasay()
    server.add_party("p1", "Asha Traders")
    server.add_invoice("i1", "SI-1", "p1", "1180.00", "2026-01-01", balance="680.50")
    server.add_invoice("i2", "SI-2", "p1", "500.00", "2026-01-10", payment_status="paid")

    [invoice] = adapter_for(server).list_open_invoices()

    assert invoice.external_id == "i1"
    assert invoice.number == "SI-1"
    assert invoice.customer_external_id == "p1"
    assert invoice.amount_due == 68050
    assert invoice.currency == "INR"
    assert invoice.due_date == date(2026, 1, 1)
    assert invoice.status == InvoiceStatus.OPEN
    assert invoice.display_details == ""


def test_every_page_is_read(server: FakeVyavasay) -> None:
    for n in range(PAGE_SIZE + 5):
        server.add_invoice(f"i{n}", f"SI-{n}", "p1", "100.00", "2026-01-01")

    invoices = adapter_for(server).list_open_invoices()

    assert len(invoices) == PAGE_SIZE + 5
    assert len({i.external_id for i in invoices}) == PAGE_SIZE + 5


def test_an_open_credit_note_is_taken_off_the_amount_due(server: FakeVyavasay) -> None:
    server.add_invoice("i1", "SI-1", "p1", "1000.00", "2026-01-01")
    server.add_invoice("i2", "SI-2", "p1", "300.00", "2026-01-01")
    for invoice_id, balance in (("i1", "250.00"), ("i2", "300.00")):
        server.sale_returns.append(
            {
                "salesInvoiceId": invoice_id,
                "balanceAmount": balance,
                "applicationStatus": "open",
                "status": "posted",
                "currencyCode": "INR",
            }
        )

    adapter = adapter_for(server)
    [invoice] = adapter.list_open_invoices()  # SI-2 is fully credited, so not open

    assert (invoice.number, invoice.amount_due) == ("SI-1", 75000)
    assert invoice.display_details == "after an unapplied credit note"
    assert adapter.get_invoice("i2").status == InvoiceStatus.PAID  # type: ignore[union-attr]


def test_a_missing_due_date_falls_back_to_the_invoice_date(server: FakeVyavasay) -> None:
    server.add_invoice("i1", "SI-1", "p1", "100.00", None, invoice_date="2025-12-15")

    [invoice] = adapter_for(server).list_open_invoices()

    assert invoice.due_date == date(2025, 12, 15)
    assert "invoice date is used" in invoice.display_details


def test_get_invoice_reports_cancelled_as_void_and_missing_as_none(server: FakeVyavasay) -> None:
    server.add_invoice("i1", "SI-1", "p1", "100.00", "2026-01-01", status="cancelled")
    adapter = adapter_for(server)

    assert adapter.get_invoice("i1").status == InvoiceStatus.VOID  # type: ignore[union-attr]
    assert adapter.get_invoice("nope") is None


def test_customers_and_payments(server: FakeVyavasay) -> None:
    server.add_invoice("i1", "SI-1", "p1", "1000.00", "2026-01-01")
    server.pay("i1", "400.00", "2026-01-03", reference="UTR123")
    server.pay("i1", "100.00", "2026-01-04", chequeStatus="deposited")  # not cleared yet
    server.pay("i1", "100.00", "2026-01-05", chequeStatus="cleared")
    server.pay("i1", "50.00", "2025-12-01")  # before the window
    adapter = adapter_for(server)

    customer = adapter.get_customer("p1")
    assert customer is not None
    assert (customer.name, customer.email, customer.phone) == (
        "Asha Traders",
        "asha@example.com",
        None,
    )
    assert adapter.get_customer("nobody") is None

    payments = adapter.list_payments_since(date(2026, 1, 1), customer_external_id="p1")
    assert [(p.amount, p.paid_on, p.reference) for p in payments] == [
        (40000, date(2026, 1, 3), "UTR123"),
        (10000, date(2026, 1, 5), "PAY-0003"),  # no reference: the payment number
    ]


def test_logs_in_switches_tenant_and_logs_in_again_when_the_session_expires(
    server: FakeVyavasay,
) -> None:
    client = VyavasayClient(
        BASE,
        phone=PHONE,
        password=PASSWORD,
        vyavasay_tenant_id=VY_TENANT,
        transport=server.transport(),
    )
    assert client.get("/v1/parties/p1")["name"] == "Asha Traders"
    assert server.requests[:2] == ["POST /v1/auth/login", "POST /v1/auth/switch-tenant"]

    server.expire_sessions()
    assert client.get("/v1/parties/p1")["name"] == "Asha Traders"
    assert server.logins == 2


def test_bad_credentials_and_server_errors_raise(server: FakeVyavasay) -> None:
    wrong = VyavasayClient(
        BASE,
        phone=PHONE,
        password="wrong",
        vyavasay_tenant_id=VY_TENANT,
        transport=server.transport(),
    )
    with pytest.raises(VyavasayError, match="login failed"):
        wrong.get("/v1/parties/p1")

    bad_token = VyavasayClient(BASE, token="expired", transport=server.transport())
    with pytest.raises(VyavasayError, match="401"):
        bad_token.get("/v1/parties/p1")


def test_config_resolves_secret_references(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VY_TOKEN", "t0ken")
    adapter = from_config({"kind": "vyavasay", "base_url": BASE, "token": "env:VY_TOKEN"})
    assert adapter.source == "vyavasay"

    with pytest.raises(AdapterConfigError, match="base_url"):
        from_config({"kind": "vyavasay"})
    with pytest.raises(AdapterConfigError, match="MISSING"):
        from_config({"kind": "vyavasay", "base_url": BASE, "token": "env:MISSING"})
    with pytest.raises(AdapterConfigError, match="needs either"):
        from_config({"kind": "vyavasay", "base_url": BASE, "phone": PHONE})
