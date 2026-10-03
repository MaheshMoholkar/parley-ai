"""Posts one webhook over HTTPS. Kept apart from the outbox logic so tests can
swap in a fake receiver."""

import httpx2 as httpx

TIMEOUT_SECONDS = 10.0


def post_webhook(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST the body and return the HTTP status code. Network errors raise."""
    response = httpx.post(
        url,
        content=body,
        headers={"Content-Type": "application/json", **headers},
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
    )
    return response.status_code
