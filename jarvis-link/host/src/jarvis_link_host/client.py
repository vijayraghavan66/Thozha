"""Outbound-only signed HTTP client for the Jarvis Link relay."""
from __future__ import annotations

import json
import secrets
import re
import time
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

import httpx

from .crypto import DeviceIdentity, b64u, device_id_for_signing_key


class RelayClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: str = "relay_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def compact_json(body: Mapping[str, Any] | None) -> bytes:
    if body is None:
        return b""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


class RelayClient:
    """Only initiates HTTPS requests/WebSockets; never accepts public inbound sockets."""
    def __init__(self, *, base_url: str, tenant_id: str, device_id: str,
                 identity: DeviceIdentity, http_client: httpx.Client | None = None) -> None:
        parsed = urlsplit(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("relay URL must be HTTPS (HTTP is allowed only for local simulation)")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", tenant_id):
            raise ValueError("tenant ID must use 8–64 relay-compatible characters")
        if not re.fullmatch(r"[0-9a-f]{32}", device_id) or device_id_for_signing_key(identity.signing_public_der) != device_id:
            raise ValueError("device ID does not match its signing identity")
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.device_id = device_id
        self.identity = identity
        self._owned_client = http_client is None
        # Honor managed-system HTTP(S)_PROXY settings for remote HTTPS relay
        # traffic. Loopback simulation remains direct and never enters a proxy.
        self.http = http_client or httpx.Client(timeout=10.0, follow_redirects=False, trust_env=not local_http)

    def signed_request(self, method: str, path_and_query: str, body: Mapping[str, Any] | None = None) -> httpx.Response:
        if not path_and_query.startswith("/") or "\r" in path_and_query or "\n" in path_and_query:
            raise ValueError("path_and_query must be an absolute URL path")
        raw = compact_json(body)
        timestamp_ms = int(time.time() * 1000)
        nonce = b64u(secrets.token_bytes(32))
        signature = self.identity.sign_request(
            tenant_id=self.tenant_id,
            device_id=self.device_id,
            timestamp_ms=timestamp_ms,
            nonce_b64url=nonce,
            method=method,
            path_and_query=path_and_query,
            body=raw,
        )
        headers = {
            "Accept": "application/json",
            "X-Jarvis-Link-Tenant": self.tenant_id,
            "X-Jarvis-Link-Device": self.device_id,
            "X-Jarvis-Link-Timestamp": str(timestamp_ms),
            "X-Jarvis-Link-Nonce": nonce,
            "X-Jarvis-Link-Signature": signature,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self.http.request(method.upper(), self.base_url + path_and_query, content=raw, headers=headers)
        if response.status_code >= 400:
            # Do not echo relay-provided text that might contain input data.
            code = "relay_error"
            try:
                code = response.json().get("error", {}).get("code", code)
            except Exception:
                pass
            raise RelayClientError(f"relay returned HTTP {response.status_code} ({code})", status_code=response.status_code, code=str(code))
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except Exception as exc:
            raise RelayClientError("relay returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RelayClientError("relay returned an invalid response object")
        return value

    def bootstrap_host(self, *, bootstrap_secret: str, device_name: str, certificate: Mapping[str, Any]) -> dict[str, Any]:
        """One-time, unsigned tenant bootstrap authorised by the relay operator's bootstrap secret."""
        raw = compact_json({"tenant_id": self.tenant_id, "device_name": device_name, "certificate": dict(certificate)})
        response = self.http.post(
            self.base_url + "/v1/enrollment/bootstrap", content=raw,
            headers={"Content-Type": "application/json", "X-Jarvis-Link-Tenant": self.tenant_id,
                     "X-Jarvis-Link-Bootstrap-Key": bootstrap_secret},
        )
        if response.status_code >= 400:
            raise RelayClientError(f"relay returned HTTP {response.status_code} (bootstrap)")
        return self._json(response)

    def create_enrollment_invitation(self, *, device_role: str = "companion", expires_in_seconds: int = 600) -> dict[str, Any]:
        if device_role not in {"host", "companion"} or not 60 <= expires_in_seconds <= 3600:
            raise ValueError("invalid enrollment role or expiry")
        response = self.signed_request("POST", "/v1/enrollment/invitations", {
            "tenant_id": self.tenant_id,
            "device_role": device_role,
            "expires_in_seconds": expires_in_seconds,
        })
        return self._json(response)

    def pending_enrollments(self) -> list[dict[str, Any]]:
        value = self._json(self.signed_request("GET", "/v1/enrollment/pending"))
        pending = value.get("enrollments")
        if not isinstance(pending, list) or any(not isinstance(item, dict) for item in pending):
            raise RelayClientError("relay enrollment list has an invalid shape")
        return pending

    def certify_device(self, certificate: Mapping[str, Any]) -> dict[str, Any]:
        return self._json(self.signed_request("POST", "/v1/enrollment/certify", {"certificate": dict(certificate)}))

    def device_certificate(self, device_id: str) -> dict[str, Any]:
        """Fetch the current active certificate for a tenant device."""
        if not re.fullmatch(r"[0-9a-f]{32}", device_id):
            raise ValueError("invalid device ID")
        value = self._json(self.signed_request("GET", f"/v1/devices/{device_id}/certificate"))
        certificate = value.get("certificate")
        if not isinstance(certificate, dict):
            raise RelayClientError("relay returned an invalid device certificate")
        return certificate

    def revoke_device(self, device_id: str, reason_code: str = "owner_revoked") -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", device_id):
            raise ValueError("invalid device ID")
        return self._json(self.signed_request(
            "POST", f"/v1/devices/{device_id}/revoke", {"reason_code": reason_code},
        ))

    def submit_envelope(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        return self._json(self.signed_request("POST", "/v1/messages", dict(envelope)))

    def inbox(self, *, device_id: str, limit: int = 20, control_only: bool = False) -> list[dict[str, Any]]:
        if not re.fullmatch(r"[0-9a-f]{32}", device_id) or not 1 <= limit <= 50:
            raise ValueError("invalid inbox recipient or limit")
        params = {'limit': str(limit)}
        if control_only:
            params['filter'] = 'control'
        path = f"/v1/devices/{device_id}/inbox?{urlencode(params)}"
        value = self._json(self.signed_request("GET", path))
        messages = value.get("messages")
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
            raise RelayClientError("relay inbox has an invalid shape")
        return messages

    def ack(self, *, message_id: str, status: str, result_digest: str = "") -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", message_id) or status not in {"received", "rejected"}:
            raise ValueError("invalid message ACK")
        if result_digest and not re.fullmatch(r"[0-9a-f]{64}", result_digest):
            raise ValueError("invalid ACK result digest")
        return self._json(self.signed_request(
            "POST", f"/v1/messages/{message_id}/ack", {"status": status, "result_digest": result_digest},
        ))

    def close(self) -> None:
        if self._owned_client:
            self.http.close()


_SAFE_RELAY_CODE = re.compile(r"[A-Z][A-Z_]{0,39}")


def relay_failure_log_fields(failure: BaseException) -> dict[str, Any]:
    """Structured, non-sensitive diagnostics for a relay failure: HTTP status and a whitelisted error code only.

    Relay bodies, headers, signatures and tokens are never included. Codes must be fixed uppercase identifiers
    (for example TIMESTAMP_SKEW or INVALID_SIGNATURE); anything else is dropped.
    """
    fields: dict[str, Any] = {}
    status = getattr(failure, "status_code", None)
    if status is None:
        response = getattr(failure, "response", None)  # websockets handshake rejection carries the response
        status = getattr(response, "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        fields["relay_status"] = status
    code = getattr(failure, "code", None)
    if isinstance(code, str) and _SAFE_RELAY_CODE.fullmatch(code):
        fields["relay_code"] = code
    return fields
