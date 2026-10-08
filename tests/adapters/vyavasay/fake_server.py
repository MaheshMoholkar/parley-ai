"""An in-memory stand-in for the Vyavasay REST API, served through
httpx's MockTransport so the real client code runs unchanged.

Only the endpoints and filters the adapter uses are implemented, in the shapes
Vyavasay's OpenAPI contract gives them. Tests add records with the helper
methods and can expire the session token to test re-login.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

PHONE, PASSWORD, VY_TENANT = "9800000000", "secret", "vy-tenant-1"


@dataclass
class FakeVyavasay:
    invoices: dict[str, dict[str, Any]] = field(default_factory=dict)
    parties: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: list[dict[str, Any]] = field(default_factory=list)
    sale_returns: list[dict[str, Any]] = field(default_factory=list)
    api_token: str = "api-token"
    # Write-back: entries added through the (proposed) collection-activity endpoint, as
    # (invoice id, text). The endpoint is missing until `notes_endpoint` is set;
    # `notes_down` makes it answer 503.
    notes: list[tuple[str, str]] = field(default_factory=list)
    notes_endpoint: bool = False
    notes_down: bool = False
    _note_keys: set[str] = field(default_factory=set)
    logins: int = 0
    requests: list[str] = field(default_factory=list)
    _session_tokens: set[str] = field(default_factory=set)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- Test data ------------------------------------------------------------------------

    def add_party(self, party_id: str, name: str, email: str | None = None) -> None:
        self.parties[party_id] = {
            "id": party_id,
            "name": name,
            "displayName": None,
            "email": email,
            "phone": None,
        }

    def add_invoice(
        self,
        invoice_id: str,
        number: str,
        party_id: str,
        total: str,
        due: str | None,
        *,
        balance: str | None = None,
        invoice_date: str = "2025-12-01",
        status: str = "posted",
        payment_status: str = "unpaid",
    ) -> None:
        self.invoices[invoice_id] = {
            "invoice": {
                "id": invoice_id,
                "invoiceNumber": number,
                "customerId": party_id,
                "customerName": self.parties[party_id]["name"],
                "invoiceDate": invoice_date,
                "dueDate": due,
                "currencyCode": "INR",
                "totalAmount": total,
                "status": status,
                "updatedAt": "2026-01-01T00:00:00Z",
            },
            "balanceAmount": balance if balance is not None else total,
            "paymentStatus": payment_status,
        }

    def pay(self, invoice_id: str, amount: str, paid_on: str, **extra: Any) -> None:
        """Record a posted incoming payment and reduce the invoice's balance."""
        row = self.invoices[invoice_id]
        left = float(row["balanceAmount"]) - float(amount)
        row["balanceAmount"] = f"{max(left, 0):.2f}"
        row["paymentStatus"] = "paid" if left <= 0 else "partial"
        self.payments.append(
            {
                "id": f"pay-{len(self.payments) + 1}",
                "paymentNumber": f"PAY-{len(self.payments) + 1:04d}",
                "paymentDate": paid_on,
                "direction": "in",
                "partyId": row["invoice"]["customerId"],
                "amount": amount,
                "reference": None,
                "status": "posted",
                "chequeStatus": None,
                **extra,
            }
        )

    def expire_sessions(self) -> None:
        self._session_tokens.clear()

    # --- HTTP -----------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if request.method == "POST" and path == "/v1/auth/login":
            body = json.loads(request.content)
            if (body["phone"], body["password"]) != (PHONE, PASSWORD):
                return httpx.Response(401, json={"error": "bad credentials"})
            self.logins += 1
            token = f"session-{self.logins}"
            return httpx.Response(200, json={"token": token})
        if request.method == "POST" and path == "/v1/auth/switch-tenant":
            body = json.loads(request.content)
            token = _bearer(request)
            if body["tenantId"] != VY_TENANT or not token.startswith("session-"):
                return httpx.Response(403, json={"error": "no access"})
            self._session_tokens.add(token)
            return httpx.Response(200, json={"tenantId": VY_TENANT})

        token = _bearer(request)
        if token != self.api_token and token not in self._session_tokens:
            return httpx.Response(401, json={"error": "unauthorised"})
        if request.method == "POST":
            return self._post(path, request)
        return self._get(path, request.url.params)

    def _post(self, path: str, request: httpx.Request) -> httpx.Response:
        match path.strip("/").split("/"):
            case ["v1", "sales-invoices", invoice_id, "collection-activity"] if self.notes_endpoint:
                if self.notes_down:
                    return httpx.Response(503, json={"error": "maintenance"})
                if invoice_id not in self.invoices:
                    return httpx.Response(404, json={"error": "not found"})
                key = request.headers["idempotency-key"]
                if key in self._note_keys:
                    return httpx.Response(409, json={"error": "duplicate"})
                self._note_keys.add(key)
                self.notes.append((invoice_id, json.loads(request.content)["text"]))
                return httpx.Response(201, json={"id": key})
        return httpx.Response(404, json={"error": f"no route {path}"})

    def _get(self, path: str, params: httpx.QueryParams) -> httpx.Response:
        parts = path.strip("/").split("/")
        match parts:
            case ["v1", "sales-invoices"]:
                statuses = params.get_list("paymentStatus")
                rows = [
                    r
                    for r in self.invoices.values()
                    if r["invoice"]["status"] == params.get("status", r["invoice"]["status"])
                    and (not statuses or r["paymentStatus"] in statuses)
                ]
                return _page(rows, params)
            case ["v1", "sales-invoices", invoice_id]:
                row = self.invoices.get(invoice_id)
                if row is None:
                    return httpx.Response(404, json={"error": "not found"})
                balance = {
                    "balanceAmount": row["balanceAmount"],
                    "paymentStatus": row["paymentStatus"],
                }
                return httpx.Response(200, json={"invoice": row["invoice"], "balance": balance})
            case ["v1", "parties", party_id]:
                party = self.parties.get(party_id)
                return httpx.Response(200, json=party) if party else httpx.Response(404)
            case ["v1", "payments"]:
                rows = [
                    p
                    for p in self.payments
                    if p["direction"] == params.get("direction")
                    and p["status"] == params.get("status")
                    and p["paymentDate"] >= params.get("fromDate", "")
                    and p["partyId"] == params.get("partyId", p["partyId"])
                ]
                return _page(rows, params)
            case ["v1", "sale-returns"]:
                rows = [
                    s
                    for s in self.sale_returns
                    if s["status"] == params.get("status")
                    and s["applicationStatus"] == params.get("applicationStatus")
                ]
                return _page(rows, params)
        return httpx.Response(404, json={"error": f"no route {path}"})


def _bearer(request: httpx.Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ")


def _page(rows: list[dict[str, Any]], params: httpx.QueryParams) -> httpx.Response:
    limit, offset = int(params.get("limit", 50)), int(params.get("offset", 0))
    page = {"items": rows[offset : offset + limit], "total": len(rows), "limit": limit}
    return httpx.Response(200, json={**page, "offset": offset})
