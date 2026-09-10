"""AOTF worktree create + baseline_manifest（M0-B2）。

spec §8.1 建立规则：从已确认 commit 创建专用分支与独立 worktree，并把
baseline_manifest.json（commit/tree、逐文件 hash、批准范围、工具链版本）
写在 AOTF 管理根（默认 worktree 兄弟文件），不放进任务树——防后续 delta/
checkpoint 把 manifest 卷入。

设计裁决：
- 门禁复用 preflight：base 仅 clean 可建；dirty（§8.1 rule 2 默认暂停）、
  unborn、not_a_repo 均拒绝（WorktreeError）。人工「等待/另定 baseline/
  取消」决策归编排层。
- 隔离守卫：worktree_path 必须绝对、不存在、与 base 工作树双向不嵌套。
- 异常域：policy 拒绝 → 本模块 WorktreeError（独立领域异常，不扩冻结 16
  码、不冒充 AotfError）；git 机械失败 → GitCommandError 上抛。
- main 工作树文件/HEAD/分支零改动；新分支 ref + .git/worktrees/ 元数据
  写入是 git worktree add 内建行为（任务分支，不 merge/push main）。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aotf.git.base import GitCommandError, run_git
from aotf.git.preflight import preflight

__all__ = ["WorktreeError", "WorktreeInfo", "create_worktree"]

_NOT_A_REPO = "base repo is not a git work tree"
_UNBORN = "base repo has no commits"
_DIRTY = "base repo working tree is dirty; baseline must be clean"
_RELATIVE = "worktree path must be absolute"
_PATH_EXISTS = "worktree path already exists"
_NESTED = "worktree path must be outside the base repo working tree"
_BRANCH_EXISTS = "branch already exists"


class WorktreeError(Exception):
    """worktree 建立的 policy 拒绝（非状态机错误）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    """成功建立的任务 worktree 事实。"""

    branch: str
    base_commit: str
    base_tree: str
    worktree_path: str
    manifest_path: str
    manifest_sha256: str


def _nested(child: str, parent: str) -> bool:
    """child 是否位于 parent 之下（Windows 大小写不敏感）。"""
    try:
        common = os.path.commonpath(
            [os.path.normcase(child), os.path.normcase(parent)]
        )
    except ValueError:
        return False
    return common == os.path.normcase(parent)


def _collect_file_sha256(worktree: str, timeout: float) -> dict[str, str]:
    """枚举 worktree 内已跟踪普通文件并逐文件 sha256（跳 gitlink）。"""
    raw = run_git(worktree, "ls-files", "-z", timeout=timeout)
    rels = [r for r in raw.split("\0") if r]
    files: dict[str, str] = {}
    for rel in sorted(rels):
        path = Path(worktree) / rel
        if not path.is_file():
            continue  # gitlink(160000)/异常条目不参与 baseline 文件 hash
        try:
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise WorktreeError(f"cannot read tracked file {rel}: {exc}") \
                from exc
    return files


def _manifest_bytes(payload: dict) -> bytes:
    """确定性 sorted-json 编码（ensure_ascii：任意文件名安全）。

    不用 canonical：canonical 的敏感键/reserved 守卫与 NFC 规范用于载荷
    字段语义；仓内任意跟踪文件名作为本 manifest 的键是自由标识符，不受
    该守卫约束，且保持原始字节（非 ASCII 用 \\u 转义、NFC 不改写）。
    """
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def create_worktree(
    base_repo: str | Path,
    *,
    worktree_path: str | Path,
    branch: str,
    allowed_files: tuple[str, ...] = (),
    at: datetime | None = None,
    timeout: float = 30.0,
) -> WorktreeInfo:
    """从 base 的 HEAD 建专用分支 worktree 并写 baseline manifest。

    base 必须 clean 且已有 commit；失败逐项抛 WorktreeError；git 机械
    失败抛 GitCommandError；base 路径无效抛 ValueError（preflight 传播）。
    """
    report = preflight(base_repo, timeout=timeout)
    if report.verdict == "not_a_repo":
        raise WorktreeError(_NOT_A_REPO)
    if report.verdict == "unborn":
        raise WorktreeError(_UNBORN)
    if report.verdict == "dirty":
        raise WorktreeError(_DIRTY)
    assert report.head_commit is not None and report.head_tree is not None

    wt = os.path.normpath(str(Path(worktree_path)))
    if not Path(wt).is_absolute():
        raise WorktreeError(_RELATIVE)
    # 嵌套先于 lexists：worktree 为 base 祖先（必然已存在）时须落 NESTED
    # 而非 PATH_EXISTS；`_nested(root, wt)` 方向因此可达（非死代码）。
    if _nested(wt, report.root) or _nested(report.root, wt):
        raise WorktreeError(_NESTED)
    if os.path.lexists(wt):
        raise WorktreeError(_PATH_EXISTS)

    ref = f"refs/heads/{branch}"
    try:
        run_git(base_repo, "show-ref", "--verify", "--quiet", ref,
                timeout=timeout)
    except GitCommandError:
        pass
    else:
        raise WorktreeError(_BRANCH_EXISTS)

    try:
        Path(wt).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreeError(f"cannot create worktree root: {exc}") from exc
    run_git(base_repo, "worktree", "add", "-b", branch, wt,
            report.head_commit, timeout=timeout)

    file_sha256 = _collect_file_sha256(wt, timeout)

    if at is None:
        at = datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    created_at = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = {
        "schema_version": 1,
        "branch": branch,
        "base_commit": report.head_commit,
        "base_tree": report.head_tree,
        "worktree_path": wt,
        "allowed_files": sorted(allowed_files),
        "submodules": list(report.submodules),
        "file_sha256": file_sha256,
        "tools": {
            "git": run_git(base_repo, "--version", timeout=timeout).strip(),
            "python": platform.python_version(),
        },
        "created_at": created_at,
    }
    manifest_path = wt + ".baseline-manifest.json"
    manifest_bytes = _manifest_bytes(payload)
    Path(manifest_path).write_bytes(manifest_bytes)
    return WorktreeInfo(
        branch=branch,
        base_commit=report.head_commit,
        base_tree=report.head_tree,
        worktree_path=wt,
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
