import hashlib
import json
import time

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from jarvis_link_host.client import RelayClient
from jarvis_link_host.crypto import DeviceIdentity, device_id_for_signing_key, request_signing_bytes


def test_signed_request_uses_exact_tenant_path_and_body_bytes():
    identity = DeviceIdentity.generate()
    device_id = device_id_for_signing_key(identity.signing_public_der)
    seen = {}

    def handler(request: httpx.Request):
        seen["request"] = request
        raw = request.read()
        ts = int(request.headers["X-Jarvis-Link-Timestamp"])
        nonce = request.headers["X-Jarvis-Link-Nonce"]
        signed = request_signing_bytes(
            "tenant-1", device_id, ts, nonce, request.method,
            request.url.raw_path.decode("ascii"), raw,
        )
        identity.signing_private.public_key().verify(
            __import__("base64").urlsafe_b64decode(request.headers["X-Jarvis-Link-Signature"] + "=" * (-len(request.headers["X-Jarvis-Link-Signature"]) % 4)),
            signed, ec.ECDSA(hashes.SHA256()),
        )
        return httpx.Response(200, json={"messages": [], "next_cursor": None})

    transport = httpx.MockTransport(handler)
    client = RelayClient(
        base_url="https://relay.invalid", tenant_id="tenant-1", device_id=device_id,
        identity=identity, http_client=httpx.Client(transport=transport),
    )
    result = client.signed_request("GET", f"/v1/devices/{device_id}/inbox?limit=20")
    assert result.status_code == 200
    request = seen["request"]
    assert request.headers["X-Jarvis-Link-Tenant"] == "tenant-1"
    assert "Authorization" not in request.headers
    assert request.headers["X-Jarvis-Link-Device"] == device_id
    client.http.close()


def test_relay_url_must_be_https_outside_local_simulation():
    identity = DeviceIdentity.generate()
    device_id = device_id_for_signing_key(identity.signing_public_der)
    try:
        RelayClient(base_url="http://relay.example", tenant_id="tenant-1", device_id=device_id, identity=identity)
        assert False, "public HTTP relay URL should fail"
    except ValueError as exc:
        assert "HTTPS" in str(exc)
