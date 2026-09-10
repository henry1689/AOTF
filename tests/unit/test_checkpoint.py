"""M0-B3 checkpoint commit 测试。

全部仓库来自 sandbox fixture（tmp 一次性 git 仓）；base main 主仓零改动有
硬断言；越界拒绝不 stage/不 commit（HEAD 不变断言）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aotf.git.checkpoint import (
    COMMIT_EMAIL,
    COMMIT_NAME,
    CheckpointError,
    CheckpointInfo,
    OutOfScopeError,
    checkpoint_commit,
)
from aotf.git.preflight import preflight
from aotf.git.worktree import create_worktree

_HEX40 = re.compile(r"[0-9a-f]{40}")


def _base_with(sandbox, rels=("a.txt",)) -> Path:
    base = sandbox.init()
    for rel in rels:
        sandbox.commit_file(base, rel, content=f"{rel}\n", msg=f"add {rel}")
    return base


def _wt(sandbox, base: Path, branch: str = "task-1") -> Path:
    return Path(create_worktree(
        base, worktree_path=str(sandbox.base / "aotf" / branch),
        branch=branch,
    ).worktree_path)


def test_checkpoint_commits_approved_edit(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    head0 = sandbox.git(wt, "rev-parse", "HEAD")
    sandbox.write(wt, "a.txt", content="v2\n")
    info = checkpoint_commit(wt, allowed_files=("a.txt",))
    assert isinstance(info, CheckpointInfo)
    assert info.base_commit == head0
    assert info.changed_files == ("a.txt",)
    assert info.commit_sha == sandbox.git(wt, "rev-parse", "HEAD")
    assert info.commit_sha != head0
    assert preflight(wt).verdict == "clean"


def test_checkpoint_adds_approved_untracked_file(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.untracked(wt, "new.py", content="x = 1\n")
    info = checkpoint_commit(wt, allowed_files=("new.py",))
    assert info.changed_files == ("new.py",)
    assert (wt / "new.py").exists()
    assert preflight(wt).verdict == "clean"


def test_checkpoint_commits_approved_deletion(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.delete(wt, "a.txt")
    info = checkpoint_commit(wt, allowed_files=("a.txt",))
    assert info.changed_files == ("a.txt",)
    assert not (wt / "a.txt").exists()
    assert preflight(wt).verdict == "clean"
    assert "a.txt" not in sandbox.git(wt, "ls-files").splitlines()


def test_out_of_scope_modified_refused_untouched(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    head0 = sandbox.git(wt, "rev-parse", "HEAD")
    sandbox.write(wt, "a.txt", content="v2\n")
    with pytest.raises(OutOfScopeError) as excinfo:
        checkpoint_commit(wt, allowed_files=())
    assert excinfo.value.offending == ("a.txt",)
    assert sandbox.git(wt, "rev-parse", "HEAD") == head0
    assert "a.txt" in preflight(wt).dirty_paths
    assert sandbox.git(wt, "diff", "--cached", "--name-only") == ""


def test_out_of_scope_untracked_refused(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.untracked(wt, "u.py", content="y\n")
    with pytest.raises(OutOfScopeError) as excinfo:
        checkpoint_commit(wt, allowed_files=())
    assert excinfo.value.offending == ("u.py",)
    assert (wt / "u.py").exists()
    assert sandbox.git(wt, "diff", "--cached", "--name-only") == ""


def test_no_changes_raises_checkpoint_error(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    head0 = sandbox.git(wt, "rev-parse", "HEAD")
    with pytest.raises(CheckpointError, match="no actual changes"):
        checkpoint_commit(wt, allowed_files=("a.txt",))
    assert sandbox.git(wt, "rev-parse", "HEAD") == head0


def test_mixed_allowed_and_blocked_lists_blocked(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    head0 = sandbox.git(wt, "rev-parse", "HEAD")
    sandbox.write(wt, "a.txt", content="v2\n")
    sandbox.write(wt, "init.txt", content="blocked\n")
    with pytest.raises(OutOfScopeError) as excinfo:
        checkpoint_commit(wt, allowed_files=("a.txt",))
    assert excinfo.value.offending == ("init.txt",)
    assert sandbox.git(wt, "rev-parse", "HEAD") == head0
    assert sandbox.git(wt, "diff", "--cached", "--name-only") == ""


def test_checkpoint_identity_is_controller(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    checkpoint_commit(wt, allowed_files=("a.txt",), message="ident")
    assert sandbox.git(wt, "log", "-1", "--format=%an|%ae") == (
        f"{COMMIT_NAME}|{COMMIT_EMAIL}"
    )


def test_checkpoint_message_recorded(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    checkpoint_commit(wt, allowed_files=("a.txt",), message="fix the thing")
    assert sandbox.git(wt, "log", "-1", "--format=%s") == "fix the thing"


def test_checkpoint_tree_and_commit_hex(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    info = checkpoint_commit(wt, allowed_files=("a.txt",))
    assert _HEX40.fullmatch(info.commit_sha)
    assert _HEX40.fullmatch(info.tree)
    assert info.tree == sandbox.git(wt, "rev-parse", "HEAD^{tree}")
    assert info.commit_sha == sandbox.git(wt, "rev-parse", "HEAD")


def test_checkpoint_with_prestaged_change(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    sandbox.stage(wt, "a.txt")
    info = checkpoint_commit(wt, allowed_files=("a.txt",))
    assert info.changed_files == ("a.txt",)
    assert preflight(wt).verdict == "clean"


def test_base_main_repo_untouched(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    main_head0 = preflight(base).head_commit
    main_branch0 = sandbox.git(base, "symbolic-ref", "--short", "HEAD")
    sandbox.write(wt, "a.txt", content="v2\n")
    checkpoint_commit(wt, allowed_files=("a.txt",))
    report = preflight(base)
    assert report.verdict == "clean"
    assert report.head_commit == main_head0
    assert sandbox.git(base, "symbolic-ref", "--short", "HEAD") == main_branch0


def test_refuse_non_repo_worktree(sandbox) -> None:
    d = sandbox.base / "plain"
    d.mkdir()
    with pytest.raises(CheckpointError, match="not a git work tree"):
        checkpoint_commit(d, allowed_files=())


def test_refuse_missing_worktree_path(sandbox) -> None:
    with pytest.raises(ValueError):
        checkpoint_commit(sandbox.base / "nope", allowed_files=())


def test_default_message(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    checkpoint_commit(wt, allowed_files=("a.txt",))
    assert sandbox.git(wt, "log", "-1", "--format=%s") == "aotf checkpoint"


def test_allowed_superset_accepted(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    info = checkpoint_commit(
        wt, allowed_files=("a.txt", "zz-nonexistent.txt"),
    )
    assert info.changed_files == ("a.txt",)
    assert preflight(wt).verdict == "clean"


def test_rename_both_paths_checked_out_of_scope(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    head0 = sandbox.git(wt, "rev-parse", "HEAD")
    sandbox.git(wt, "mv", "a.txt", "b.txt")
    # 仅批准目标路径 b.txt：源 a.txt 的删除必须视为越界（--no-renames）。
    with pytest.raises(OutOfScopeError) as excinfo:
        checkpoint_commit(wt, allowed_files=("b.txt",))
    assert excinfo.value.offending == ("a.txt",)
    assert sandbox.git(wt, "rev-parse", "HEAD") == head0


def test_prestaged_deletion_commits(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.delete(wt, "a.txt")
    sandbox.stage(wt, "a.txt")
    info = checkpoint_commit(wt, allowed_files=("a.txt",))
    assert info.changed_files == ("a.txt",)
    assert preflight(wt).verdict == "clean"
    assert "a.txt" not in sandbox.git(wt, "ls-files").splitlines()
