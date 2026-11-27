import os

import pytest

from openjarvis.tasks.dev_capability import CapabilityError
from openjarvis.tasks.workspace_snapshot import snapshot_files, snapshot_manifest


def test_snapshot_selected_files_and_manifest(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"abc")
    result = snapshot_files(str(tmp_path), ["src/a.py"])
    assert result == {"src/a.py": b"abc"}
    assert (
        snapshot_manifest(result)["src/a.py"]
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_rejects_traversal_symlinks_hardlinks_and_git(tmp_path):
    (tmp_path / "a").write_text("data")
    (tmp_path / "link").symlink_to(tmp_path / "a")
    os.link(tmp_path / "a", tmp_path / "hardlink")
    for path in (
        "../a",
        "link",
        "hardlink",
        ".git/config",
        "preflight-sentinel.txt",
    ):
        with pytest.raises(CapabilityError):
            snapshot_files(str(tmp_path), [path])


def test_rejects_size_overflow_and_duplicate_selection(tmp_path):
    (tmp_path / "a").write_bytes(b"1234")
    for paths, limit in [(["a"], 3), (["a", "a"], 10)]:
        with pytest.raises(CapabilityError):
            snapshot_files(str(tmp_path), paths, max_bytes=limit)
