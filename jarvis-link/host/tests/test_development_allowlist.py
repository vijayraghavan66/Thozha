"""Explicit development-scope enrollment is tested only against temporary allowlists."""
from __future__ import annotations

import json
import importlib.util
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis_link_host.crypto import DeviceIdentity, EnvelopeError, device_id_for_signing_key, issue_device_certificate
from jarvis_link_host.host import (
    authorize_development_companion,
    development_companions,
    revoke_development_companion,
)


def _certificate(root: DeviceIdentity, *, tenant: str = "tenant-test", device_role: str = "companion"):
    companion_signing = DeviceIdentity.generate()
    host_id = device_id_for_signing_key(root.signing_public_der)
    cert = issue_device_certificate(
        host=root,
        tenant_id=tenant,
        host_device_id=host_id,
        signing_public_b64u=companion_signing.signing_public_b64u,
        encryption_public_b64u=companion_signing.encryption_public_b64u,
        device_role=device_role,
    )
    return cert, host_id


def test_owner_must_explicitly_authorize_existing_host_certified_device(tmp_path):
    root = DeviceIdentity.generate()
    cert, host_id = _certificate(root)
    allowlist = tmp_path / "development-companions.json"

    with pytest.raises(PermissionError, match="explicit Mac-owner authorization"):
        authorize_development_companion(
            device_id=cert.device_id,
            certificate=cert,
            trusted_host_signing_public_der=root.signing_public_der,
            expected_tenant_id="tenant-test",
            expected_issuer_device_id=host_id,
            owner_authorized=False,
            path=allowlist,
        )
    assert not allowlist.exists()

    assert authorize_development_companion(
        device_id=cert.device_id,
        certificate=cert,
        trusted_host_signing_public_der=root.signing_public_der,
        expected_tenant_id="tenant-test",
        expected_issuer_device_id=host_id,
        owner_authorized=True,
        path=allowlist,
    ) is True
    assert development_companions(allowlist) == {cert.device_id}
    assert stat.S_IMODE(allowlist.stat().st_mode) == 0o600
    assert authorize_development_companion(
        device_id=cert.device_id,
        certificate=cert,
        trusted_host_signing_public_der=root.signing_public_der,
        expected_tenant_id="tenant-test",
        expected_issuer_device_id=host_id,
        owner_authorized=True,
        path=allowlist,
    ) is False


def test_enrollment_rejects_untrusted_wrong_role_wrong_tenant_and_expired_certificates(tmp_path):
    root = DeviceIdentity.generate()
    other_root = DeviceIdentity.generate()
    cert, host_id = _certificate(root)
    allowlist = tmp_path / "allowlist.json"
    common = {
        "device_id": cert.device_id,
        "trusted_host_signing_public_der": root.signing_public_der,
        "expected_tenant_id": "tenant-test",
        "expected_issuer_device_id": host_id,
        "owner_authorized": True,
        "path": allowlist,
    }
    with pytest.raises(EnvelopeError, match="trusted host"):
        authorize_development_companion(
            certificate=cert, trusted_host_signing_public_der=other_root.signing_public_der, **{k: v for k, v in common.items() if k != "trusted_host_signing_public_der"},
        )
    with pytest.raises(EnvelopeError, match="tenant"):
        authorize_development_companion(certificate=cert, **{**common, "expected_tenant_id": "other-tenant"})
    wrong_role, _ = _certificate(root, device_role="host")
    with pytest.raises(EnvelopeError, match="role"):
        authorize_development_companion(
            certificate=wrong_role,
            **{**common, "device_id": wrong_role.device_id},
        )
    with pytest.raises(EnvelopeError, match="validity"):
        authorize_development_companion(
            certificate=cert, now_ms=cert.expires_at_unix_ms + 1, **common,
        )
    assert not allowlist.exists()


def test_revocation_requires_explicit_owner_action_and_only_removes_development_scope(tmp_path):
    root = DeviceIdentity.generate()
    cert, host_id = _certificate(root)
    allowlist = tmp_path / "development-companions.json"
    authorize_development_companion(
        device_id=cert.device_id,
        certificate=cert,
        trusted_host_signing_public_der=root.signing_public_der,
        expected_tenant_id="tenant-test",
        expected_issuer_device_id=host_id,
        owner_authorized=True,
        path=allowlist,
    )
    with pytest.raises(PermissionError, match="explicit Mac-owner authorization"):
        revoke_development_companion(device_id=cert.device_id, owner_authorized=False, path=allowlist)
    assert development_companions(allowlist) == {cert.device_id}
    assert revoke_development_companion(device_id=cert.device_id, owner_authorized=True, path=allowlist)
    assert development_companions(allowlist) == set()
    # The certificate remains cryptographically valid; revocation here removes only dev scope.
    assert cert.device_id not in development_companions(allowlist)


def test_malformed_or_symlink_allowlist_fails_closed(tmp_path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(["not-a-device-id"]), encoding="utf-8")
    root = DeviceIdentity.generate()
    cert, host_id = _certificate(root)
    kwargs = dict(
        device_id=cert.device_id,
        certificate=cert,
        trusted_host_signing_public_der=root.signing_public_der,
        expected_tenant_id="tenant-test",
        expected_issuer_device_id=host_id,
        owner_authorized=True,
    )
    with pytest.raises(ValueError, match="invalid entries"):
        authorize_development_companion(path=malformed, **kwargs)
    target = tmp_path / "target.json"
    target.write_text("[]", encoding="utf-8")
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        authorize_development_companion(path=alias, **kwargs)
    assert target.read_text(encoding="utf-8") == "[]"


def test_operator_commands_require_explicit_flag_and_exact_device_repetition():
    script = Path(__file__).resolve().parents[1] / "scripts" / "link_host_admin.py"
    spec = importlib.util.spec_from_file_location("jarvis_development_allowlist_cli_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    no_flag = SimpleNamespace(approve_local=False, device_id="a" * 32, confirm_device_id="a" * 32)
    with pytest.raises(SystemExit, match="--approve-local"):
        module._require_development_owner_confirmation(no_flag)
    wrong_repeat = SimpleNamespace(approve_local=True, device_id="a" * 32, confirm_device_id="b" * 32)
    with pytest.raises(SystemExit, match="repeat the exact"):
        module._require_development_owner_confirmation(wrong_repeat)
    confirmed = SimpleNamespace(approve_local=True, device_id="a" * 32, confirm_device_id="a" * 32)
    assert module._require_development_owner_confirmation(confirmed) is None
    assert "authorize-development" in module.COMMANDS
    assert "revoke-development" in module.COMMANDS


def test_cli_authorizes_only_the_current_host_certified_companion_in_temp_allowlist(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "link_host_admin.py"
    spec = importlib.util.spec_from_file_location("jarvis_development_enrollment_cli_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    root = DeviceIdentity.generate()
    cert, host_id = _certificate(root)
    allowlist = tmp_path / "development-companions.json"

    class Reply:
        def json(self):
            return {"certificate": cert.to_dict()}

    class Relay:
        def signed_request(self, *_args, **_kwargs):
            return Reply()

    relay = Relay()
    state = {"tenant_id": "tenant-test", "host_device_id": host_id}
    monkeypatch.setattr(module, "DEVELOPMENT_ALLOWLIST_PATH", allowlist)
    monkeypatch.setattr(module, "_relay", lambda _args, _path: (state, relay, root))
    args = SimpleNamespace(
        approve_local=True,
        confirm_device_id=cert.device_id,
        device_id=cert.device_id,
    )
    result = module.cmd_authorize_development(args, tmp_path / "host.json")
    assert result["status"] == "authorized_for_development"
    assert result["pairing_changed"] is False
    assert result["relay_certificate_changed"] is False
    assert development_companions(allowlist) == {cert.device_id}
