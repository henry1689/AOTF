"""AOTF 控制器 checkpoint commit（M0-B3）。

spec §8.2：Implementer 编辑结束后（worktree dirty：改/增/删未提交），
控制器机械地枚举 actual changes → 范围验证 actual⊆allowed（越界抛
OutOfScopeError，不 stage/不 commit）→ 只 stage 批准子集 → 以固定控制器
身份提交 checkpoint → 提交后 worktree 必须 clean（EvidenceRunner 的不可变
post-edit snapshot 面）。

设计裁决：
- 工作前提：Implementer 不自行 commit（变化以未提交 diff + untracked 存在）。
  若 Agent 违反自行提交，其 commit 已入任务分支（不在本包可回改范围），
  本包对提交后仍存在的未提交变化照常处理。
- 身份固定：-c user.name=aotf-controller / -c user.email=…（控制器署名，
  不依赖 base/global 身份）。
- 越界/空 diff：越界 → OutOfScopeError（编排层据此入 SAFE_HALT）；无实际
  变化 → CheckpointError（空 delta）；git 机械失败 → GitCommandError；
  worktree 非仓/无 commit → CheckpointError。异常域同 B1/B2 先例（不扩冻结
  16 码、不冒充 AotfError）。
- main 主仓零改动：本模块只写任务分支（worktree 的 HEAD/ref）；delta/patch
  计算归 B4，archive/清理归 B5，tasks.actual_tree 落库归编排层。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aotf.git.base import GitCommandError, run_git
from aotf.git.preflight import preflight

__all__ = [
    "COMMIT_NAME",
    "COMMIT_EMAIL",
    "CheckpointError",
    "OutOfScopeError",
    "CheckpointInfo",
    "checkpoint_commit",
]

COMMIT_NAME = "aotf-controller"
COMMIT_EMAIL = "aotf-controller@local.invalid"


class CheckpointError(Exception):
    """checkpoint 的 policy 拒绝（非状态机错误）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class OutOfScopeError(CheckpointError):
    """actual 文件越出 allowed_files（编排层据此入 SAFE_HALT）。"""

    def __init__(self, offending: tuple[str, ...]) -> None:
        self.offending = tuple(sorted(offending))
        first = self.offending[0] if self.offending else ""
        super().__init__(
            f"out-of-scope files: {len(self.offending)} ({first!r} first)"
        )


@dataclass(frozen=True, slots=True)
class CheckpointInfo:
    """成功 checkpoint 的事实。tree = HEAD^{tree}（actual_tree 语义）。"""

    base_commit: str
    commit_sha: str
    tree: str
    changed_files: tuple[str, ...]
    message: str


def _name_set(repo: str, argv: tuple[str, ...], timeout: float) -> set[str]:
    # 仓库与 HEAD 已在上方验证存在；此处 git 失败即真错误，直接上抛，
    # 不吞（吞错会让 actual 缺项导致部分提交）。
    out = run_git(repo, *argv, timeout=timeout)
    return {line for line in out.splitlines() if line}


def checkpoint_commit(
    worktree: str | Path,
    *,
    allowed_files: tuple[str, ...] = (),
    message: str = "aotf checkpoint",
    timeout: float = 30.0,
) -> CheckpointInfo:
    """在任务 worktree 上创建控制器 checkpoint commit。

    越界抛 OutOfScopeError（不 stage/不 commit）；空 diff 抛 CheckpointError；
    git 机械失败抛 GitCommandError；路径无效抛 ValueError。
    """
    path = Path(worktree)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"worktree path not found or not a directory: {worktree}")
    try:
        inside = run_git(path, "rev-parse", "--is-inside-work-tree",
                         timeout=timeout).strip()
    except GitCommandError as exc:
        if exc.returncode is None:
            raise
        inside = "false"
    if inside != "true":
        raise CheckpointError("worktree is not a git work tree")
    try:
        base_commit = run_git(path, "rev-parse", "--verify", "HEAD^{commit}",
                              timeout=timeout).strip()
    except GitCommandError as exc:
        if exc.returncode is None:
            raise
        raise CheckpointError("worktree has no commits") from exc

    # --no-renames：rename 折叠会让源删除漏出 actual（绕过越界门且
    # changed_files 漏报源路径），显式展开使删除/新增以独立路径呈现。
    tracked = _name_set(path, ("diff", "--no-renames", "--name-only", "HEAD"),
                        timeout)
    untracked = _name_set(
        path, ("ls-files", "--others", "--exclude-standard"), timeout
    )
    actual = tuple(sorted(tracked | untracked))
    if not actual:
        raise CheckpointError("no actual changes to checkpoint")

    allowed = set(allowed_files)
    offending = tuple(sorted(a for a in actual if a not in allowed))
    if offending:
        raise OutOfScopeError(offending)

    # pre-staged 删除（index 与工作区均已无此文件）对 `git add` 是 fatal
    # pathspec；剔除已完整 stage 的删除，其余走 add（commit 会携带删除）。
    staged_deletions = _name_set(
        path, ("diff", "--no-renames", "--cached", "--name-only",
               "--diff-filter=D", "HEAD"),
        timeout,
    )
    to_add = tuple(a for a in actual if a not in staged_deletions)
    if to_add:
        run_git(path, "add", "--", *to_add, timeout=timeout)
    staged = _name_set(
        path, ("diff", "--no-renames", "--cached", "--name-only", "HEAD"),
        timeout,
    )
    if staged != set(actual):
        raise CheckpointError("staged set mismatch after add")

    run_git(path, "-c", f"user.name={COMMIT_NAME}",
            "-c", f"user.email={COMMIT_EMAIL}",
            "-c", "commit.gpgsign=false",
            "commit", "-m", message, timeout=timeout)
    commit_sha = run_git(path, "rev-parse", "--verify", "HEAD^{commit}",
                         timeout=timeout).strip()
    tree = run_git(path, "rev-parse", "--verify", "HEAD^{tree}",
                   timeout=timeout).strip()
    if preflight(path, timeout=timeout).verdict != "clean":
        raise CheckpointError("worktree not clean after checkpoint")
    return CheckpointInfo(
        base_commit=base_commit,
        commit_sha=commit_sha,
        tree=tree,
        changed_files=actual,
        message=message,
    )
