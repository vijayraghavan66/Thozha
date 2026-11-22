"""Small standard-library TLS front end for the loopback-only companion gateway.

This process intentionally contains no Jarvis credentials or authorization logic.
It terminates TLS on one configured private interface and forwards the HTTP byte
stream only to 127.0.0.1:8001, where the allowlisted companion gateway validates
the device token and routes. Keep this front end separate from the main API.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import ssl
import sys
from pathlib import Path

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8001
MAX_CONNECTIONS = 32
MAX_BYTES_PER_DIRECTION = 12 * 1024 * 1024
IDLE_TIMEOUT_SECONDS = 120


def validate_private_bind_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("TLS proxy bind host must be a private IP literal") from exc
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        raise ValueError("TLS proxy bind host must be a private interface address")
    if isinstance(address, ipaddress.IPv4Address):
        allowed = address.is_private and not address.is_reserved
    else:
        allowed = address in ipaddress.ip_network("fc00::/7")
    if not allowed:
        raise ValueError("TLS proxy bind host must be RFC1918 or IPv6 ULA")
    return str(address)


def require_supported_runtime() -> None:
    if sys.version_info < (3, 14) or ssl.OPENSSL_VERSION_INFO < (3, 6, 0):
        raise RuntimeError("Wi-Fi TLS frontend requires Python 3.14+ with OpenSSL 3.6+ on this Mac")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    total = 0
    while True:
        data = await asyncio.wait_for(reader.read(64 * 1024), timeout=IDLE_TIMEOUT_SECONDS)
        if not data:
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()
            return
        total += len(data)
        if total > MAX_BYTES_PER_DIRECTION:
            raise ValueError("companion request stream exceeded the byte budget")
        writer.write(data)
        await writer.drain()


async def serve(host: str, port: int, certificate: Path, private_key: Path, *, debug: bool = False) -> None:
    require_supported_runtime()
    bind_host = validate_private_bind_host(host)
    if port != 8443:
        raise ValueError("The local companion TLS frontend uses fixed port 8443")
    if not certificate.is_file() or not private_key.is_file():
        raise ValueError("TLS identity files are unavailable")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificate), str(private_key))
    context.set_alpn_protocols(["http/1.1"])
    active = asyncio.Semaphore(MAX_CONNECTIONS)

    async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        remote = client_writer.get_extra_info("peername")
        remote_ip = remote[0] if isinstance(remote, tuple) and remote else ""
        try:
            peer = ipaddress.ip_address(remote_ip)
            if peer.is_loopback or peer.is_link_local or peer.is_multicast or peer.is_unspecified or peer.is_global:
                raise PermissionError("client is outside private address ranges")
            async with active:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT), timeout=3,
                )
                client_to_upstream = asyncio.create_task(_pipe(client_reader, upstream_writer))
                upstream_to_client = asyncio.create_task(_pipe(upstream_reader, client_writer))
                try:
                    await asyncio.gather(client_to_upstream, upstream_to_client)
                except Exception:
                    client_to_upstream.cancel()
                    upstream_to_client.cancel()
                    await asyncio.gather(client_to_upstream, upstream_to_client, return_exceptions=True)
                    raise
        except Exception as exc:  # diagnostics contain only an exception type
            if debug:
                print(f"local TLS proxy closed connection ({type(exc).__name__})", file=sys.stderr, flush=True)
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except Exception:
                    pass
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(
        handle,
        host=bind_host,
        port=port,
        ssl=context,
        ssl_handshake_timeout=10,
        limit=64 * 1024,
        backlog=64,
    )
    print(f"Local TLS frontend listening on private interface port {port}", flush=True)
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Private-interface TLS frontend for the companion gateway")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--debug", action="store_true", help="Log connection exception types only")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.host, args.port, Path(args.certificate), Path(args.private_key), debug=args.debug))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Local TLS frontend stopped ({type(exc).__name__}).", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - interactive runner
    raise SystemExit(main())
