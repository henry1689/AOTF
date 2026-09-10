"""M0-B1 repo preflight 只读探测测试。

全部仓库来自 sandbox fixture（tmp_path 一次性 git 仓）；DB 无关；无网络；
preflight 对目标仓零写入（test_preflight_is_read_only 整目录字节快照断言）。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

from aotf.git.base import GitCommandError, run_git
from aotf.git.preflight import PreflightReport, preflight

_HEX40 = re.compile(r"[0-9a-f]{40}")


def _tree_hash(root: Path) -> str:
    """整目录（含 .git）逐文件 相对路径+字节 顺序 hash。"""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            digest.update(p.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(p.read_bytes())
    return digest.hexdigest()


def test_clean_repo_reports_clean_with_branch_and_head(sandbox) -> None:
    repo = sandbox.init()
    sha = sandbox.commit_file(repo, "a.txt", content="a\n", msg="add a")
    report = preflight(repo)
    assert isinstance(report, PreflightReport)
    assert report.verdict == "clean"
    assert report.branch == "main"
    assert report.head_commit == sha
    assert _HEX40.fullmatch(report.head_tree or "")
    assert report.reason is None
    assert report.dirty_paths == ()
    assert report.submodules == ()
    assert Path(report.root) == repo.resolve()


def test_preflight_is_read_only_no_bytes_written(sandbox) -> None:
    repo = sandbox.init()
    sandbox.untracked(repo, "u.txt")
    before = _tree_hash(repo)
    preflight(repo)
    preflight(repo)
    assert _tree_hash(repo) == before


def test_dirty_untracked_file(sandbox) -> None:
    repo = sandbox.init()
    sandbox.untracked(repo, "notes.txt")
    report = preflight(repo)
    assert report.verdict == "dirty"
    assert "notes.txt" in report.dirty_paths


def test_dirty_modified_tracked_file(sandbox) -> None:
    repo = sandbox.init()
    sandbox.write(repo, "init.txt", content="changed\n")
    report = preflight(repo)
    assert report.verdict == "dirty"
    assert "init.txt" in report.dirty_paths


def test_dirty_staged_change(sandbox) -> None:
    repo = sandbox.init()
    sandbox.write(repo, "init.txt", content="staged\n")
    sandbox.stage(repo, "init.txt")
    report = preflight(repo)
    assert report.verdict == "dirty"
    assert "init.txt" in report.dirty_paths


def test_dirty_deleted_tracked_file(sandbox) -> None:
    repo = sandbox.init()
    sandbox.delete(repo, "init.txt")
    report = preflight(repo)
    assert report.verdict == "dirty"
    assert "init.txt" in report.dirty_paths


def test_dirty_paths_sorted_and_combined(sandbox) -> None:
    repo = sandbox.init()
    sandbox.commit_file(repo, "mod.txt", content="m1\n", msg="add mod")
    sandbox.write(repo, "mod.txt", content="m2\n")
    sandbox.commit_file(repo, "st.txt", content="s1\n", msg="add st")
    sandbox.write(repo, "st.txt", content="s2\n")
    sandbox.stage(repo, "st.txt")
    sandbox.untracked(repo, "u1.txt")
    sandbox.untracked(repo, "z_dir/u2.txt")
    report = preflight(repo)
    assert report.verdict == "dirty"
    assert report.dirty_paths == (
        "mod.txt", "st.txt", "u1.txt", "z_dir/u2.txt",
    )


def test_plain_dir_is_not_a_repo(sandbox, tmp_path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    report = preflight(d)
    assert report.verdict == "not_a_repo"
    assert report.reason
    assert report.branch is None
    assert report.head_commit is None
    assert report.dirty_paths == ()


def test_bare_repo_is_not_a_repo(sandbox) -> None:
    repo = sandbox.init(bare=True)
    report = preflight(repo)
    assert report.verdict == "not_a_repo"
    assert report.reason and (
        "bare" in report.reason.lower()
        or "work tree" in report.reason.lower()
    )


def test_unborn_repo_no_commits(sandbox) -> None:
    repo = sandbox.init(commit=False)
    report = preflight(repo)
    assert report.verdict == "unborn"
    assert report.branch == "main"
    assert report.head_commit is None
    assert report.head_tree is None
    assert report.reason
    assert report.dirty_paths == ()


def test_detached_head_branch_none_clean(sandbox) -> None:
    repo = sandbox.init()
    sandbox.detach(repo)
    report = preflight(repo)
    assert report.verdict == "clean"
    assert report.branch is None
    assert _HEX40.fullmatch(report.head_commit or "")


def test_submodule_gitlink_listed_clean(sandbox) -> None:
    repo = sandbox.init()
    sandbox.add_submodule_gitlink(repo, "sub")
    report = preflight(repo)
    assert report.submodules == ("sub",)
    assert report.verdict == "clean"


def test_missing_path_raises_valueerror(tmp_path) -> None:
    with pytest.raises(ValueError):
        preflight(tmp_path / "nope")


def test_subdir_input_resolves_to_top_level(sandbox) -> None:
    repo = sandbox.init()
    sub = repo / "deep"
    sub.mkdir()
    report = preflight(sub)
    assert Path(report.root) == repo.resolve()
    assert report.verdict == "clean"


def test_repo_path_with_spaces_and_unicode(sandbox) -> None:
    repo = sandbox.init("子 仓")
    sandbox.commit_file(repo, "名字.txt", content="v1\n", msg="add 名字")
    assert preflight(repo).verdict == "clean"
    sandbox.write(repo, "名字.txt", content="v2\n")
    report = preflight(repo)
    assert report.verdict == "dirty"
    assert report.branch == "main"
    # 非 ASCII 文件名原始返回（core.quotepath=false），不被 C-quote。
    assert "名字.txt" in report.dirty_paths


def test_run_git_returns_trimmed_stdout(sandbox) -> None:
    repo = sandbox.init()
    out = run_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    assert _HEX40.fullmatch(out)


def test_run_git_failure_raises_git_command_error(tmp_path) -> None:
    d = tmp_path / "nonrepo"
    d.mkdir()
    with pytest.raises(GitCommandError) as excinfo:
        run_git(d, "rev-parse", "--verify", "HEAD")
    err = excinfo.value
    assert err.returncode == 128
    assert err.argv[1:] == (
        "-c", "core.quotepath=false", "rev-parse", "--verify", "HEAD",
    )
    assert err.timed_out is False


def test_git_command_error_str_and_timed_out_flag(tmp_path) -> None:
    d = tmp_path / "n"
    d.mkdir()
    with pytest.raises(GitCommandError) as excinfo:
        run_git(d, "rev-parse", "--verify", "HEAD")
    err = excinfo.value
    assert err.timed_out is False
    assert "128" in str(err)
    assert "rev-parse" in str(err)
