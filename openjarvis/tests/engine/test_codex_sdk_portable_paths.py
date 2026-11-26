"""Portable runtime path resolution for the Codex SDK adapter."""

from pathlib import Path

from openjarvis.engine import codex_sdk


def test_codex_home_defaults_under_openjarvis_data_root(tmp_path, monkeypatch):
    root = tmp_path / "portable-data"
    monkeypatch.delenv("OPENJARVIS_CODEX_HOME", raising=False)
    monkeypatch.setattr(codex_sdk, "get_config_dir", lambda: root)

    assert codex_sdk._resolve_codex_home() == root / "codex-home"


def test_codex_home_honors_explicit_private_runtime_override(tmp_path, monkeypatch):
    configured = tmp_path / "persistent" / "codex-home"
    monkeypatch.setenv("OPENJARVIS_CODEX_HOME", str(configured))
    monkeypatch.setattr(codex_sdk, "get_config_dir", lambda: tmp_path / "unused")

    assert codex_sdk._resolve_codex_home() == configured.resolve()


def test_codex_python_uses_packaged_runtime_override(tmp_path, monkeypatch):
    configured = tmp_path / "runtime" / "python3"
    monkeypatch.setenv("OPENJARVIS_PYTHON", str(configured))
    monkeypatch.setattr(codex_sdk, "get_config_dir", lambda: tmp_path / "unused")

    assert codex_sdk._resolve_codex_python() == configured.resolve()


def test_codex_python_preserves_existing_venv_and_falls_back_to_current_runtime(
    tmp_path, monkeypatch
):
    root = tmp_path / "openjarvis-home"
    source_python = root / "src" / ".venv" / "bin" / "python"
    source_python.parent.mkdir(parents=True)
    source_python.write_bytes(b"fixture interpreter path")
    monkeypatch.delenv("OPENJARVIS_PYTHON", raising=False)
    monkeypatch.setattr(codex_sdk, "get_config_dir", lambda: root)
    assert codex_sdk._resolve_codex_python() == source_python

    source_python.unlink()
    current_python = Path(tmp_path / "portable" / "python3")
    monkeypatch.setattr(codex_sdk.sys, "executable", str(current_python))
    assert codex_sdk._resolve_codex_python() == current_python.resolve()


def test_non_link_working_directory_uses_configured_openjarvis_home(
    tmp_path, monkeypatch
):
    root = tmp_path / "portable-data"
    monkeypatch.setattr(codex_sdk, "get_config_dir", lambda: root)

    assert codex_sdk._working_directory_for_current_task() == str(root)
