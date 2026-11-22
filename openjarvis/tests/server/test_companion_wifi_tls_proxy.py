"""Pure input-validation tests for the standard-library Wi-Fi TLS front end."""
from __future__ import annotations

import pytest

from openjarvis.server.companion_wifi_tls_proxy import validate_private_bind_host


@pytest.mark.parametrize("host", ["10.0.0.12", "172.16.0.2", "192.168.1.8", "fd12::1"])
def test_tls_frontend_accepts_only_private_interface_hosts(host):
    assert validate_private_bind_host(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.1.2", "100.64.1.2", "::1", "fe80::1"])
def test_tls_frontend_rejects_wildcard_public_and_link_local_hosts(host):
    with pytest.raises(ValueError):
        validate_private_bind_host(host)
