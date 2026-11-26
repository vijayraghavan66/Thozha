"""Pure input-validation tests for the standard-library Wi-Fi TLS front end."""
from __future__ import annotations

import pytest

from openjarvis.server.companion_wifi_tls_proxy import (
    validate_gateway_port_pair,
    validate_private_bind_host,
)


@pytest.mark.parametrize("host", ["10.0.0.12", "172.16.0.2", "192.168.1.8", "fd12::1"])
def test_tls_frontend_accepts_only_private_interface_hosts(host):
    assert validate_private_bind_host(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.1.2", "100.64.1.2", "::1", "fe80::1"])
def test_tls_frontend_rejects_wildcard_public_and_link_local_hosts(host):
    with pytest.raises(ValueError):
        validate_private_bind_host(host)


@pytest.mark.parametrize("tls_port,upstream_port", [(8443, 8001), (8444, 8002)])
def test_tls_frontend_accepts_supported_port_pairs(tls_port, upstream_port):
    assert validate_gateway_port_pair(tls_port) == (tls_port, upstream_port)
    assert validate_gateway_port_pair(tls_port, upstream_port) == (tls_port, upstream_port)


@pytest.mark.parametrize("tls_port,upstream_port", [(8444, 8001), (8443, 8002), (8445, 8003)])
def test_tls_frontend_rejects_unsupported_or_mismatched_pairs(tls_port, upstream_port):
    with pytest.raises(ValueError, match="supported TLS/loopback port pair"):
        validate_gateway_port_pair(tls_port, upstream_port)
