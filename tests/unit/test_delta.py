"""M0-B4 actual delta capture 测试。

全部仓库来自 sandbox fixture（tmp 一次性 git 仓）；capture 只读——对
worktree 与 manifest 零写入；二进制/rename/comment 反例覆盖。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aotf.git.checkpoint import checkpoint_commit
from aotf.git.delta import DeltaError, DeltaReport, capture_delta
from aotf.git.worktree import create_worktree

_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _base_with(sandbox, rels=("a.txt",)) -> Path:
    base = sandbox.init()
    for rel in rels:
        sandbox.commit_file(base, rel, content=f"c-{rel}\n", msg=f"add {rel}")
    return base


def _wt(sandbox, base: Path, branch: str = "task-1",
        allowed: tuple[str, ...] = ("a.txt",)) -> Path:
    return Path(create_worktree(
        base, worktree_path=str(sandbox.base / "aotf" / branch),
        branch=branch, allowed_files=allowed,
    ).worktree_path)


def _cp(wt: Path, allowed: tuple[str, ...] = ("a.txt",)) -> None:
    checkpoint_commit(wt, allowed_files=allowed, message="cp")


def _manifest(wt: Path) -> dict:
    return json.loads(
        open(str(wt) + ".baseline-manifest.json", encoding="utf-8").read()
    )


def test_capture_delta_after_edit(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    _cp(wt)
    rep = capture_delta(wt)
    assert isinstance(rep, DeltaReport)
    assert rep.changes == (("M", "a.txt"),)
    assert rep.checkpoint_commit == sandbox.git(wt, "rev-parse", "HEAD")
    assert rep.actual_tree == sandbox.git(wt, "rev-parse", "HEAD^{tree}")
    assert rep.base_tree == _manifest(wt)["base_tree"]
    assert rep.base_commit == _manifest(wt)["base_commit"]
    assert rep.file_sha256 == {"a.txt": _sha(wt / "a.txt")}
    assert rep.semantic_added == 1
    assert rep.patch_sha256


def test_capture_delta_add_new_file(sandbox) -> None:
    base = _base_with(sandbox)
    allowed = ("a.txt", "new.py")
    wt = _wt(sandbox, base, allowed=allowed)
    sandbox.untracked(wt, "new.py", content="x = 1\ny = 2\n")
    _cp(wt, allowed)
    rep = capture_delta(wt)
    assert rep.changes == (("A", "new.py"),)
    assert rep.file_sha256["new.py"] == _sha(wt / "new.py")
    assert rep.semantic_added == 2


def test_capture_delta_delete(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.delete(wt, "a.txt")
    _cp(wt)
    rep = capture_delta(wt)
    assert rep.changes == (("D", "a.txt"),)
    assert rep.file_sha256 == {}
    assert "a.txt" in rep.diff_stat
    assert rep.renames == ()


def test_capture_delta_empty_when_unchanged(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    rep = capture_delta(wt)
    assert rep.changes == ()
    assert rep.renames == ()
    assert rep.file_sha256 == {}
    assert rep.patch_sha256 == _EMPTY_SHA
    assert rep.semantic_added == 0
    assert rep.diff_stat == ""


def test_delta_out_of_scope_rejected(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)  # manifest allowed == ("a.txt",)
    sandbox.write(wt, "init.txt", content="blocked\n")
    checkpoint_commit(wt, allowed_files=("init.txt",), message="cp")
    with pytest.raises(DeltaError, match="out-of-scope"):
        capture_delta(wt)


def test_delta_rename_reported_and_split(sandbox) -> None:
    base = _base_with(sandbox)
    allowed = ("a.txt", "b.txt")
    wt = _wt(sandbox, base, allowed=allowed)
    sandbox.git(wt, "mv", "a.txt", "b.txt")
    _cp(wt, allowed)
    rep = capture_delta(wt)
    assert rep.renames == (("a.txt", "b.txt"),)
    assert ("A", "b.txt") in rep.changes
    assert ("D", "a.txt") in rep.changes


def test_delta_binary_file(sandbox) -> None:
    base = sandbox.init()
    (base / "bin.dat").write_bytes(b"\x00\x01\xff\xfe")
    sandbox.stage(base, "bin.dat")
    sandbox.git(base, "commit", "-m", "add bin")
    wt = _wt(sandbox, base, allowed=("bin.dat",))
    (wt / "bin.dat").write_bytes(b"\x00\x01\xff\xfe\x00")
    _cp(wt, ("bin.dat",))
    rep = capture_delta(wt)
    assert rep.changes == (("M", "bin.dat"),)
    assert rep.file_sha256["bin.dat"] == _sha(wt / "bin.dat")
    assert rep.patch_sha256 != _EMPTY_SHA
    assert rep.semantic_added == 0


def test_manifest_missing_raises(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    with pytest.raises(DeltaError, match="manifest not found"):
        capture_delta(wt, manifest_path=str(base / "nope.json"))


def test_manifest_invalid_raises(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    payloads = (
        "not json",
        '{"schema_version": 99}',
        '{"schema_version":1,"base_commit":"a","base_tree":"b",'
        '"allowed_files":"a.txt","file_sha256":{}}',
    )
    for payload in payloads:
        bad = sandbox.base / "bad.json"
        bad.write_text(payload, encoding="utf-8", newline="")
        with pytest.raises(DeltaError, match="invalid baseline manifest"):
            capture_delta(wt, manifest_path=bad)


def test_dirty_worktree_rejected(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    with pytest.raises(DeltaError, match="not clean"):
        capture_delta(wt)


def test_capture_multiple_changes_file_sha(sandbox) -> None:
    base = _base_with(sandbox)
    allowed = ("a.txt", "init.txt", "z.txt")
    wt = _wt(sandbox, base, allowed=allowed)
    sandbox.write(wt, "a.txt", content="a2\n")
    sandbox.untracked(wt, "z.txt", content="z1\n")
    _cp(wt, allowed)
    rep = capture_delta(wt)
    assert sorted(rep.file_sha256) == ["a.txt", "z.txt"]
    assert rep.file_sha256["a.txt"] == _sha(wt / "a.txt")
    assert rep.file_sha256["z.txt"] == _sha(wt / "z.txt")


def test_base_tree_matches_manifest(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    _cp(wt)
    rep = capture_delta(wt)
    m = _manifest(wt)
    assert rep.base_tree == m["base_tree"]
    assert rep.base_commit == m["base_commit"]


def test_semantic_added_ignores_comment_lines(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="# head\nc-a.txt\n")
    _cp(wt)
    rep = capture_delta(wt)
    assert rep.changes == (("M", "a.txt"),)
    assert rep.semantic_added == 0


def test_changes_sorted_by_path(sandbox) -> None:
    base = _base_with(sandbox)
    allowed = ("a.txt", "init.txt", "z.txt")
    wt = _wt(sandbox, base, allowed=allowed)
    sandbox.write(wt, "z.txt", content="z2\n")
    sandbox.write(wt, "init.txt", content="init2\n")
    _cp(wt, allowed)
    rep = capture_delta(wt)
    assert tuple(p for _c, p in rep.changes) == ("init.txt", "z.txt")


def test_worktree_missing_raises_valueerror(sandbox) -> None:
    with pytest.raises(ValueError):
        capture_delta(sandbox.base / "nope")
