"""M0-B2 worktree create + baseline_manifest 测试。

全部仓库来自 sandbox fixture（tmp 一次性 git 仓）；base 主仓文件/HEAD/
分支零改动有硬断言；manifest 写 worktree 外兄弟文件，绝不进任务树。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aotf.git.preflight import preflight
from aotf.git.worktree import WorktreeError, WorktreeInfo, create_worktree

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _wt(sandbox, branch: str):
    return str(sandbox.base / "aotf" / branch)


def _manifest(info: WorktreeInfo) -> dict:
    return json.loads(open(info.manifest_path, encoding="utf-8").read())


def _sha256_file(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_create_worktree_on_clean_base(sandbox) -> None:
    base = sandbox.init()
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1")
    assert isinstance(info, WorktreeInfo)
    assert info.branch == "task-1"
    assert info.worktree_path == _wt(sandbox, "task-1")
    assert info.base_commit == preflight(base).head_commit
    assert info.manifest_sha256
    assert preflight(info.worktree_path).verdict == "clean"
    assert sandbox.git(info.worktree_path, "symbolic-ref", "--short",
                       "HEAD") == "task-1"


def test_worktree_has_baseline_content(sandbox) -> None:
    base = sandbox.init()
    sandbox.commit_file(base, "a.txt", content="hello\n", msg="add a")
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1")
    assert (Path(info.worktree_path) / "a.txt").read_bytes() == b"hello\n"
    assert (Path(info.worktree_path) / "init.txt").exists()


def test_base_repo_untouched_after_create(sandbox) -> None:
    base = sandbox.init()
    sandbox.commit_file(base, "a.txt", content="hello\n", msg="add a")
    sha0 = preflight(base).head_commit
    branch0 = sandbox.git(base, "symbolic-ref", "--short", "HEAD")
    files_before = {
        rel: _sha256_file(str(base / rel))
        for rel in ("init.txt", "a.txt")
    }
    create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                    branch="task-1")
    report = preflight(base)
    assert report.verdict == "clean"
    assert report.head_commit == sha0
    assert sandbox.git(base, "symbolic-ref", "--short", "HEAD") == branch0
    for rel, h in files_before.items():
        assert _sha256_file(str(base / rel)) == h


def test_manifest_written_with_schema_and_fields(sandbox) -> None:
    base = sandbox.init()
    sandbox.commit_file(base, "a.txt", content="a\n", msg="add a")
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1", at=T0)
    assert info.manifest_path == info.worktree_path + ".baseline-manifest.json"
    m = _manifest(info)
    assert m["schema_version"] == 1
    assert m["branch"] == "task-1"
    assert m["base_commit"] == info.base_commit
    assert m["base_tree"] == info.base_tree
    assert m["worktree_path"] == info.worktree_path
    assert m["allowed_files"] == []
    assert m["submodules"] == []
    assert set(m["tools"]) == {"git", "python"}


def test_manifest_file_sha256_matches_manual_hash(sandbox) -> None:
    base = sandbox.init()
    sandbox.commit_file(base, "a.txt", content="a\n", msg="add a")
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1")
    m = _manifest(info)
    assert sorted(m["file_sha256"]) == ["a.txt", "init.txt"]
    for rel, h in m["file_sha256"].items():
        assert h == _sha256_file(str(Path(info.worktree_path) / rel))


def test_manifest_sha256_closed_loop(sandbox) -> None:
    base = sandbox.init()
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1", at=T0)
    raw = open(info.manifest_path, "rb").read()
    assert hashlib.sha256(raw).hexdigest() == info.manifest_sha256
    # 同一确定性编码（sorted-json + ensure_ascii）重放闭环。
    assert json.dumps(_manifest(info), sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False
                      ).encode("utf-8") == raw


def test_sensitive_or_reserved_filename_keys_survive(sandbox) -> None:
    base = sandbox.init()
    sandbox.commit_file(base, "token", content="t\n", msg="add token")
    sandbox.commit_file(base, "$aotf", content="m\n", msg="add marker name")
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1")
    m = _manifest(info)
    assert m["file_sha256"]["token"]
    assert m["file_sha256"]["$aotf"]


def test_allowed_files_recorded_in_manifest(sandbox) -> None:
    base = sandbox.init()
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1", allowed_files=("a.txt",))
    assert _manifest(info)["allowed_files"] == ["a.txt"]


def test_submodules_recorded_and_gitlink_skipped(sandbox) -> None:
    base = sandbox.init()
    sandbox.add_submodule_gitlink(base, "sub")
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1")
    m = _manifest(info)
    assert m["submodules"] == ["sub"]
    assert "sub" not in m["file_sha256"]


def test_unicode_filename_in_file_sha256(sandbox) -> None:
    base = sandbox.init()
    sandbox.commit_file(base, "名字.txt", content="n\n", msg="add 名字")
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1")
    m = _manifest(info)
    assert m["file_sha256"]["名字.txt"] == _sha256_file(
        str(Path(info.worktree_path) / "名字.txt")
    )


def test_refuse_dirty_base(sandbox) -> None:
    base = sandbox.init()
    sandbox.untracked(base, "u.txt")
    with pytest.raises(WorktreeError, match="dirty"):
        create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                        branch="task-1")
    assert not Path(_wt(sandbox, "task-1")).exists()


def test_refuse_unborn_base(sandbox) -> None:
    base = sandbox.init(commit=False)
    with pytest.raises(WorktreeError, match="no commits"):
        create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                        branch="task-1")


def test_refuse_not_a_repo_base(sandbox) -> None:
    d = sandbox.base / "plain"
    d.mkdir()
    with pytest.raises(WorktreeError, match="not a git work tree"):
        create_worktree(d, worktree_path=_wt(sandbox, "task-1"),
                        branch="task-1")


def test_refuse_existing_branch(sandbox) -> None:
    base = sandbox.init()
    with pytest.raises(WorktreeError, match="branch already exists"):
        create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                        branch="main")
    assert not Path(_wt(sandbox, "task-1")).exists()


def test_refuse_existing_worktree_path(sandbox) -> None:
    base = sandbox.init()
    target = Path(_wt(sandbox, "task-1"))
    target.mkdir(parents=True)
    with pytest.raises(WorktreeError, match="path already exists"):
        create_worktree(base, worktree_path=target, branch="task-1")


def test_refuse_worktree_inside_base_repo(sandbox) -> None:
    base = sandbox.init()
    inside = str(base / "inside")
    with pytest.raises(WorktreeError, match="outside the base"):
        create_worktree(base, worktree_path=inside, branch="task-1")
    assert not (base / "inside").exists()


def test_refuse_base_inside_worktree_path(sandbox) -> None:
    base = sandbox.init()
    with pytest.raises(WorktreeError, match="outside the base"):
        create_worktree(base, worktree_path=str(sandbox.base),
                        branch="task-1")


def test_refuse_relative_worktree_path(sandbox) -> None:
    base = sandbox.init()
    with pytest.raises(WorktreeError, match="absolute"):
        create_worktree(base, worktree_path="relative/path", branch="task-1")


def test_missing_base_path_raises_valueerror(sandbox) -> None:
    with pytest.raises(ValueError):
        create_worktree(sandbox.base / "nope",
                        worktree_path=_wt(sandbox, "task-1"),
                        branch="task-1")


def test_tools_and_created_at_recorded(sandbox) -> None:
    base = sandbox.init()
    info = create_worktree(base, worktree_path=_wt(sandbox, "task-1"),
                           branch="task-1", at=T0)
    m = _manifest(info)
    assert m["tools"]["git"].startswith("git version ")
    assert m["tools"]["python"]
    assert m["created_at"] == "2026-09-01T00:00:00Z"
