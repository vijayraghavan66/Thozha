"""Device-authenticated push-token registration against the real workerd relay.

Security properties: a device can set or clear only its own token; only the tenant host can read a token;
malformed tokens and extra fields are refused; a revoked device cannot register.
"""
import json
import uuid

import httpx
import pytest

from jarvis_link_host.client import RelayClientError
from sim_companion import SimCompanion  # noqa: F401
from test_relay_integration import World, relay_url  # noqa: F401

VALID_TOKEN = "fcm-" + "A" * 150  # matches the relay's token grammar and length


def _put(world, device, token_body, path_device=None):
    client = device.client if hasattr(device, "client") else world.relay
    path = f"/v1/devices/{path_device or device.device_id}/push-token"
    return client.signed_request("PUT", path, token_body)


def test_companion_registers_its_own_token_and_only_the_host_can_read_it(relay_url):  # noqa: F811
    world = World(relay_url)
    companion = world.companion
    assert _put(world, companion, {"token": VALID_TOKEN}).json()["status"] == "registered"
    token = world.relay.signed_request("GET", f"/v1/devices/{companion.device_id}/push-token").json()["token"]
    assert token == VALID_TOKEN
    with pytest.raises(RelayClientError):  # the companion itself cannot read its token back
        companion.client.signed_request("GET", f"/v1/devices/{companion.device_id}/push-token")


def test_a_device_cannot_register_a_token_for_another_device(relay_url):  # noqa: F811
    world = World(relay_url)
    other = world.enroll_companion()
    with pytest.raises(RelayClientError) as exc:
        world.companion.client.signed_request("PUT", f"/v1/devices/{other.device_id}/push-token", {"token": VALID_TOKEN})
    assert "403" in str(exc.value)
    with pytest.raises(RelayClientError):
        world.companion.client.signed_request("DELETE", f"/v1/devices/{other.device_id}/push-token")


def test_malformed_tokens_and_extra_fields_are_refused(relay_url):  # noqa: F811
    world = World(relay_url)
    for body in ({"token": "short"}, {"token": "x" * 50 + " spaces here" + "x" * 60}, {"token": VALID_TOKEN, "extra": 1}):
        with pytest.raises(RelayClientError) as exc:
            _put(world, world.companion, body)
        assert "422" in str(exc.value)


def test_clear_then_revoked_device_is_refused(relay_url):  # noqa: F811
    world = World(relay_url)
    _put(world, world.companion, {"token": VALID_TOKEN})
    world.companion.client.signed_request("DELETE", f"/v1/devices/{world.companion.device_id}/push-token")
    with pytest.raises(RelayClientError):
        world.relay.signed_request("GET", f"/v1/devices/{world.companion.device_id}/push-token")
    _put(world, world.companion, {"token": VALID_TOKEN})
    world.relay.revoke_device(world.companion.device_id)
    with pytest.raises(RelayClientError):
        _put(world, world.companion, {"token": VALID_TOKEN})
