from __future__ import annotations

import json
import os
import secrets
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class Account03LedgerError(RuntimeError):
    """The authoritative Account 03 publishing ledger is unavailable or invalid."""


class Account03LedgerClient:
    def __init__(self, base_url: str, token: str, timeout: float = 12.0):
        parsed = urlparse(base_url.strip())
        host = (parsed.hostname or "").lower()
        local_hosts = {"127.0.0.1", "localhost", "::1"}

        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and host in local_hosts
        ):
            raise Account03LedgerError(
                "Ledger URL must use HTTPS; HTTP is permitted only for local testing."
            )
        if not parsed.netloc or parsed.query or parsed.fragment:
            raise Account03LedgerError("Invalid Account 03 ledger URL.")
        if len(token) < 32:
            raise Account03LedgerError("Account 03 ledger token must be at least 32 characters.")
        if not 1 <= timeout <= 60:
            raise Account03LedgerError("Ledger timeout must be between 1 and 60 seconds.")

        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "Account03LedgerClient":
        url = os.getenv("ACCOUNT03_LEDGER_API_URL", "").strip()
        token = os.getenv("ACCOUNT03_LEDGER_API_TOKEN", "").strip()
        timeout_text = os.getenv("ACCOUNT03_LEDGER_TIMEOUT_SECONDS", "12")

        if not url:
            raise Account03LedgerError("ACCOUNT03_LEDGER_API_URL is not configured.")
        if not token:
            raise Account03LedgerError("ACCOUNT03_LEDGER_API_TOKEN is not configured.")

        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise Account03LedgerError(
                "ACCOUNT03_LEDGER_TIMEOUT_SECONDS must be numeric."
            ) from exc

        return cls(url, token, timeout)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = None if payload is None else json.dumps(
            payload, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

        headers = {
            "Accept": "application/json",
            "Cache-Control": "no-store",
            "Authorization": f"Bearer {self.token}",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = int(response.status)
                raw = response.read(16384)
        except HTTPError as exc:
            status = int(exc.code)
            raw = exc.read(16384)
        except (URLError, TimeoutError, OSError) as exc:
            raise Account03LedgerError(
                f"Account 03 ledger request failed: {type(exc).__name__}"
            ) from exc

        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Account03LedgerError(
                f"Account 03 ledger returned invalid JSON (HTTP {status})."
            ) from exc

        if not isinstance(body, dict):
            raise Account03LedgerError("Account 03 ledger returned an invalid response.")

        return status, body

    def healthcheck(self) -> None:
        status, body = self._request("GET", "/health")
        if status != 200 or body.get("ok") is not True:
            raise Account03LedgerError(
                f"Account 03 ledger health check failed (HTTP {status})."
            )

    def reserve(self, signal: dict[str, Any]) -> dict[str, Any]:
        reservation_id = secrets.token_hex(20)
        payload = {
            "reservation_id": reservation_id,
            "account_id": "account_03",
            "signal": signal,
        }

        status, body = self._request("POST", "/v1/reservations", payload)

        if status == 201 and body.get("reserved") is True:
            if body.get("reservation_id") != reservation_id:
                raise Account03LedgerError("Ledger returned a mismatched reservation ID.")
            return body

        if status == 409 and body.get("reserved") is False:
            return body

        raise Account03LedgerError(
            f"Account 03 reservation failed (HTTP {status}): "
            f"{body.get('error', body.get('reason', 'unexpected_response'))}"
        )

    def finalize(self, reservation_id: str, status: str) -> dict[str, Any]:
        if status not in {"published", "publish_uncertain"}:
            raise Account03LedgerError("Invalid reservation finalization status.")

        if not reservation_id or not reservation_id.isalnum():
            raise Account03LedgerError("Invalid reservation ID.")

        http_status, body = self._request(
            "POST",
            f"/v1/reservations/{reservation_id}/finalize",
            {"status": status},
        )

        if http_status == 200 and body.get("finalized") is True:
            return body

        raise Account03LedgerError(
            f"Account 03 finalization failed (HTTP {http_status}): "
            f"{body.get('error', body.get('reason', 'unexpected_response'))}"
        )
