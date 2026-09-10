"""AOTF controller-owned snapshot/mutation 执行测试。"""

from __future__ import annotations

import hashlib

import pytest

from aotf.controller import (ControllerMutationError, FileMutation,
                             apply_mutations, snapshot_files)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_snapshot_and_controller_replace(tmp_path) -> None:
    before = b"x = 1\n"
    target = tmp_path / "a.py"
    target.write_bytes(before)
    snap = snapshot_files(str(tmp_path), ("a.py",))
    assert snap[0].sha256 == _sha(before)
    assert snap[0].content == "x = 1\n"

    changed = apply_mutations(str(tmp_path), ("a.py",), (
        FileMutation("a.py", "replace", _sha(before), "x = 2\n"),
    ))
    assert changed == ("a.py",)
    assert target.read_text(encoding="utf-8") == "x = 2\n"


def test_controller_create_and_delete(tmp_path) -> None:
    old = tmp_path / "old.txt"
    old.write_bytes(b"old\n")
    changed = apply_mutations(
        str(tmp_path), ("new.txt", "old.txt"), (
            FileMutation("new.txt", "create", None, "new\n"),
            FileMutation("old.txt", "delete", _sha(b"old\n"), None),
        ))
    assert changed == ("new.txt", "old.txt")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert not old.exists()


def test_controller_validates_whole_batch_before_writing(tmp_path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"a0")
    b.write_bytes(b"b0")
    with pytest.raises(ControllerMutationError, match="preimage hash mismatch"):
        apply_mutations(str(tmp_path), ("a.txt", "b.txt"), (
            FileMutation("a.txt", "replace", _sha(b"a0"), "a1"),
            FileMutation("b.txt", "replace", "0" * 64, "b1"),
        ))
    assert a.read_text(encoding="utf-8") == "a0"
    assert b.read_text(encoding="utf-8") == "b0"


@pytest.mark.parametrize("path", ["../evil", "/abs", ".git/config",
                                   "dir\\evil"])
def test_controller_rejects_unsafe_or_control_paths(tmp_path, path) -> None:
    with pytest.raises(ControllerMutationError):
        apply_mutations(str(tmp_path), (path,), (
            FileMutation(path, "create", None, "x"),
        ))


def test_controller_rejects_out_of_scope_duplicate_and_limits(tmp_path) -> None:
    (tmp_path / "a.txt").write_bytes(b"a")
    with pytest.raises(ControllerMutationError, match="outside approval"):
        apply_mutations(str(tmp_path), ("a.txt",), (
            FileMutation("b.txt", "create", None, "b"),
        ))
    with pytest.raises(ControllerMutationError, match="duplicate mutation"):
        apply_mutations(str(tmp_path), ("a.txt",), (
            FileMutation("a.txt", "replace", _sha(b"a"), "a1"),
            FileMutation("a.txt", "replace", _sha(b"a"), "a2"),
        ))
    with pytest.raises(ControllerMutationError, match="byte limit"):
        apply_mutations(str(tmp_path), ("a.txt",), (
            FileMutation("a.txt", "replace", _sha(b"a"), "too large"),
        ), max_total_bytes=2)


def test_snapshot_rejects_binary_and_reports_missing(tmp_path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"\xff")
    with pytest.raises(ControllerMutationError, match="non-UTF-8"):
        snapshot_files(str(tmp_path), ("bin.dat",))
    missing = snapshot_files(str(tmp_path), ("new.txt",))
    assert missing[0].exists is False
    assert missing[0].sha256 is None and missing[0].content is None
