# Development-scope companion enrollment

Development scope is a separate privilege from normal companion pairing. A paired companion cannot grant itself development access through a Link message. The host checks `~/.jarvis-link-test/development-companions.json` and rejects development tasks when the device ID is absent.

## Owner-authorized enrollment

Use this only after the companion has completed the normal pairing flow and the host has certified it. The command fetches the current certificate from the configured relay, verifies the host signature, tenant, device ID, companion role, issuer and validity window, then atomically adds that one device to the development allowlist.

```sh
python host/scripts/link_host_admin.py authorize-development \
  --relay http://127.0.0.1:8787 \
  --device-id <32-hex-companion-id> \
  --approve-local \
  --confirm-device-id <same-32-hex-companion-id>
```

Both owner-confirmation flags are required, and the repeated ID must match exactly. The command changes only the development allowlist. It does not issue or replace pairing credentials, certify a device, or alter the relay certificate.

## Revoke development scope

```sh
python host/scripts/link_host_admin.py revoke-development \
  --device-id <32-hex-companion-id> \
  --approve-local \
  --confirm-device-id <same-32-hex-companion-id>
```

This removes only development scope; normal pairing and the relay certificate remain intact. The running host reads the allowlist for each new `task.submit`, so removal blocks subsequent development requests without restarting services. Existing task capabilities remain bound to their supervisor task and become unusable after task cancellation, expiry or revocation.

## Approval boundary after enrollment

Allowlisting only permits the paired device to submit a development-scoped request. It does not authorize project writes. Codex must present one exact bounded plan; the user must approve it through the supervisor approval flow. The capability issued afterward is restricted to the approved workspace, paths, operations, quotas and lifetime. Codex shell/file sandbox approvals remain independent. Actions outside the plan require a separate authorization.

Enrollment has not been run against the live allowlist as part of this implementation. Tests use temporary files and synthetic certificates only.
