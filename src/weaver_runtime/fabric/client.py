"""Low-level Fabric/OneLake REST client: requests, retries, and pagination."""

from __future__ import annotations

import json
import ssl
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FabricClientError(RuntimeError):
    """Raised when a Fabric or OneLake REST request fails."""


GET_STATUSES = {200}
TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_REQUEST_ATTEMPTS = 5
REQUEST_TIMEOUT = 120

#: Substrings of transient transport/TLS failures worth retrying.
TRANSIENT_URLERROR_REASONS = (
    "timed out",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "eof occurred in violation of protocol",
    "bad handshake",
    "ssl",
    "broken pipe",
    "temporary failure in name resolution",
)


def _is_transient_urlerror(reason: object) -> bool:
    if isinstance(reason, (ssl.SSLError, TimeoutError, ConnectionError)):
        return True
    text = str(reason).lower()
    return any(needle in text for needle in TRANSIENT_URLERROR_REASONS)


def request_bytes(
    method: str,
    url: str,
    token: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    expected_statuses: set[int] | None = None,
) -> tuple[bytes, dict[str, str], int]:
    """Call a REST endpoint and return response bytes, headers, and status."""

    expected_statuses = expected_statuses or GET_STATUSES
    request_headers = {
        "Authorization": f"Bearer {token}",
        **(headers or {}),
    }

    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                response_body = response.read()
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                if response.status not in expected_statuses:
                    if (
                        response.status in TRANSIENT_HTTP_STATUSES
                        and attempt < MAX_REQUEST_ATTEMPTS
                    ):
                        time.sleep(min(2**attempt, 10))
                        continue
                    raise FabricClientError(
                        f"{method} {url} returned HTTP {response.status}: "
                        f"{response_body.decode('utf-8', errors='replace')}"
                    )
                return response_body, response_headers, response.status
        except HTTPError as exc:
            response_body = exc.read()
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            if exc.code in expected_statuses:
                return response_body, response_headers, exc.code
            if exc.code in TRANSIENT_HTTP_STATUSES and attempt < MAX_REQUEST_ATTEMPTS:
                time.sleep(min(2**attempt, 10))
                continue
            detail = response_body.decode("utf-8", errors="replace")
            raise FabricClientError(f"{method} {url} returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            if attempt < MAX_REQUEST_ATTEMPTS and _is_transient_urlerror(exc.reason):
                time.sleep(min(2**attempt, 10))
                continue
            raise FabricClientError(f"{method} {url} failed: {exc.reason}") from exc
        except OSError as exc:
            # socket.timeout, ssl.SSLError, ConnectionError, etc. can surface
            # directly (and socket.timeout is not TimeoutError on Python 3.9).
            if attempt < MAX_REQUEST_ATTEMPTS:
                time.sleep(min(2**attempt, 10))
                continue
            raise FabricClientError(f"{method} {url} failed: {exc}") from exc

    raise FabricClientError(f"{method} {url} failed after {MAX_REQUEST_ATTEMPTS} attempts")


def request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    expected_statuses: set[int] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str], int]:
    """Call a JSON REST endpoint and return payload, headers, and status."""

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if body is not None else {}),
    }
    response_body, response_headers, status = request_bytes(
        method,
        url,
        token,
        body=body,
        headers=headers,
        expected_statuses=expected_statuses,
    )
    return (
        json.loads(response_body.decode("utf-8")) if response_body else None,
        response_headers,
        status,
    )


def fabric_url(api_base_url: str, path: str) -> str:
    """Return an absolute Fabric API URL under ``/v1``."""

    return f"{api_base_url.rstrip('/')}/v1/{path.lstrip('/')}"


def paged_values(url: str, token: str) -> list[dict[str, Any]]:
    """Return every item across a paginated Fabric list endpoint."""

    values: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        payload, _, _ = request_json("GET", next_url, token)
        payload = payload or {}
        values.extend(payload.get("value", []))
        next_url = payload.get("continuationUri") or payload.get("nextLink") or ""
    return values
