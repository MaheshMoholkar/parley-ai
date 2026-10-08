"""HTTP client for the Vyavasay REST API.

Authentication, whichever the tenant is set up for:
- an API token, sent as `Authorization: Bearer <token>`;
- a dedicated Vyavasay user (phone and password): log in, switch the session
  to the right Vyavasay tenant, and use that session token. A 401 (expired
  session) triggers one fresh login and a retry.
"""

from collections.abc import Iterator
from typing import Any

import httpx2 as httpx

PAGE_SIZE = 200  # Vyavasay's maximum


class VyavasayError(RuntimeError):
    pass


class VyavasayClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        phone: str | None = None,
        password: str | None = None,
        vyavasay_tenant_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not token and not (phone and password and vyavasay_tenant_id):
            raise VyavasayError("needs either a token, or phone, password and vyavasay_tenant_id")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )
        self._fixed_token = token
        self._login = (phone, password, vyavasay_tenant_id)
        self._session_token: str | None = None

    def get(self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> Any:
        """GET a JSON resource; returns None for 404."""
        response = self._request(path, params)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise VyavasayError(f"GET {path} failed: {response.status_code} {response.text[:300]}")
        return response.json()

    def get_all(self, path: str, params: list[tuple[str, Any]]) -> Iterator[dict[str, Any]]:
        """Every item of a paged list endpoint ({items, total, limit, offset})."""
        offset = 0
        while True:
            page = self.get(path, [*params, ("limit", PAGE_SIZE), ("offset", offset)])
            if page is None:
                return
            items = page.get("items", [])
            yield from items
            offset += len(items)
            if not items or offset >= int(page.get("total", 0)):
                return

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        """POST JSON and return the response, whatever its status (the caller
        decides what each status means). Network errors raise httpx errors."""
        return self._request(path, None, method="POST", body=body, headers=headers)

    def _request(
        self,
        path: str,
        params: Any,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        def send() -> httpx.Response:
            return self._http.request(
                method,
                path,
                params=params,
                json=body,
                headers={**(headers or {}), **self._auth_header()},
            )

        response = send()
        if response.status_code == 401 and self._fixed_token is None:
            self._session_token = None  # the session expired: log in again, once
            response = send()
        return response

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._fixed_token or self._session()}"}

    def _session(self) -> str:
        if self._session_token is None:
            phone, password, tenant_id = self._login
            login = self._http.post(
                "/v1/auth/login", json={"phone": phone, "password": password, "isPersistent": True}
            )
            if login.status_code != 200:
                raise VyavasayError(f"login failed: {login.status_code}")
            token = str(login.json()["token"])
            switch = self._http.post(
                "/v1/auth/switch-tenant",
                json={"tenantId": tenant_id},
                headers={"Authorization": f"Bearer {token}"},
            )
            if switch.status_code != 200:
                raise VyavasayError(f"switching to tenant {tenant_id} failed: {switch.status_code}")
            self._session_token = token
        return self._session_token
