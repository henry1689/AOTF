"""AOTF repo preflight 只读探测（M0-B1）。

对给定仓库做机械只读体检（spec §8.1 步骤 1–2：branch/HEAD/status/
submodule），产出 typed PreflightReport。dirty 主仓「暂停/等待/另定
baseline」的决策不归本模块（编排层 policy，B2+）；这里只如实报告。

只读保证：全部探测经 base.run_git（GIT_OPTIONAL_LOCKS=0，不写 index/
refs/.git 任何字节）；本模块不写文件、不新建对象、不触发 clone/submodule
命令（gitlink 探测 = ls-files --stage 纯读 index）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aotf.git.base import GitCommandError, run_git

__all__ = ["PreflightReport", "preflight"]

_NOT_A_REPO = "not a git work tree"
_NOT_INSIDE = "not inside a git work tree (bare or nested repo)"
_UNBORN = "no commits yet"


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """仓库只读探测结果。verdict ∈ {clean, dirty, unborn, not_a_repo}。

    - root：仓顶绝对路径（平台规范形式）；not_a_repo 时为输入路径规范形；
    - branch：HEAD 分支名；detached 为 None；
    - head_commit / head_tree：HEAD commit/tree 40-hex；unborn/not_a_repo 为 None；
    - dirty_paths：dirty 涉及文件相对路径（sorted）；非 dirty 为空元组；
    - submodules：index 中 mode=160000 gitlink 路径（sorted）；
    - reason：not_a_repo/unborn 人类可读原因；clean/dirty 为 None。
    """

    root: str
    verdict: str
    branch: str | None
    head_commit: str | None
    head_tree: str | None
    dirty_paths: tuple[str, ...]
    submodules: tuple[str, ...]
    reason: str | None


def _not_a_repo(root: str, reason: str) -> PreflightReport:
    return PreflightReport(root, "not_a_repo", None, None, None, (), (), reason)


def _unborn(root: str, branch: str | None) -> PreflightReport:
    return PreflightReport(root, "unborn", branch, None, None, (), (), _UNBORN)


def preflight(repo_path: str | Path, *, timeout: float = 30.0) -> PreflightReport:
    """只读探测 repo_path 返回 typed PreflightReport。

    路径不存在或非目录抛 ValueError；git 缺失/超时按环境错误上抛
    GitCommandError，不伪装成 not_a_repo。
    """
    path = Path(repo_path)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"repo path not found or not a directory: {repo_path}")
    resolved = os.path.normpath(str(path.resolve()))

    try:
        inside = run_git(path, "rev-parse", "--is-inside-work-tree",
                         timeout=timeout).strip()
    except GitCommandError as exc:
        if exc.returncode is None:
            raise
        # 仅当 git 自述「非 git 仓库」才判 not_a_repo；其余（dubious
        # ownership/权限/损坏等环境性 rc!=0）按环境错误上抛，不伪装。
        if b"not a git repository" not in exc.stderr:
            raise
        return _not_a_repo(resolved, _NOT_A_REPO)
    if inside != "true":
        return _not_a_repo(resolved, _NOT_INSIDE)

    root = os.path.normpath(
        run_git(path, "rev-parse", "--show-toplevel", timeout=timeout)
    )
    top = Path(root)

    branch: str | None = None
    try:
        branch = run_git(top, "symbolic-ref", "--quiet", "--short", "HEAD",
                         timeout=timeout).strip()
    except GitCommandError:
        branch = None

    head_commit: str | None = None
    try:
        head_commit = run_git(top, "rev-parse", "--verify", "HEAD^{commit}",
                              timeout=timeout).strip()
    except GitCommandError:
        head_commit = None
    if head_commit is None:
        # unborn 无 HEAD：无从 diff，index/工作区内容不在此报告（编排层
        # 须先经 baseline 建立才谈 dirty；语义定案于此）。
        return _unborn(root, branch)

    try:
        head_tree = run_git(top, "rev-parse", "--verify", "HEAD^{tree}",
                            timeout=timeout).strip()
    except GitCommandError:
        head_tree = None

    dirty: set[str] = set()
    for argv in (("diff", "--name-only"), ("diff", "--cached", "--name-only")):
        try:
            out = run_git(top, *argv, timeout=timeout)
        except GitCommandError:
            out = ""
        dirty.update(line for line in out.splitlines() if line)
    untracked = run_git(top, "ls-files", "--others", "--exclude-standard",
                        timeout=timeout)
    dirty.update(line for line in untracked.splitlines() if line)
    dirty_paths = tuple(sorted(dirty))

    subs: list[str] = []
    for line in run_git(top, "ls-files", "--stage", timeout=timeout).splitlines():
        if line.startswith("160000 "):
            subs.append(line.partition("\t")[2])
    submodules = tuple(sorted(subs))

    return PreflightReport(
        root=root,
        verdict="dirty" if dirty_paths else "clean",
        branch=branch,
        head_commit=head_commit,
        head_tree=head_tree,
        dirty_paths=dirty_paths,
        submodules=submodules,
        reason=None,
    )
