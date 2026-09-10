"""AOTF worktree archive / 清理与保留（M0-B5）。

spec §8.3：任务（成功 checkpoint 或失败未提交）结束后先把证据与唯一变更
归档，再按保留策略清理 worktree；清理前总能恢复 manifest 与 patch（§20.3
#16）；失败不 --force 丢变更；回滚只作用任务 worktree/分支，不 reset 主仓。

- archive 独立于 commit 状态：dirty（未 checkpoint 失败态）也可归档——patch
  用 `git diff <base_commit> --`（工作区含 index 未提交 diff vs base）；
- untracked 逐文件拷入归档 untracked/（git diff 不含 untracked，防 --force
  丢新增）；force 因此数据无损；
- dirty 判定 = preflight dirty **或** `git status --porcelain` 非空（gitlink
  子模块错配态不误报 clean）；
- remove：dirty 无 force 直接拒绝（零副作用，不落归档）；force/clean 才
  archive → 经 **peer worktree** 作 cwd 执行 `git worktree remove`（Windows
  无法删自身 cwd）；delete_branch=True 须 base_repo（detached 分支前置拒）；
- 归档只写 archive_root/<wt-name>/（AOTF 管理根，控制面目录；非 B6 formal
  artifact store）；对 worktree/base 只读；main 工作树/HEAD/文件零改动；
- 失败原子性：任何归档中异常先回滚已建归档目录再上抛。

编码：manifest 逐字节拷贝（拷贝 sha==源 sha）；patch 存盘 = run_git 文本
（去单个尾 \\n）还原一个尾 \\n 后 surrogateescape 编码——等价 git 原始输出
bytes，patch_sha256 即该原始 diff bytes 的 sha。

异常域：policy/完整性拒绝 → CleanupError；git 机械失败 → GitCommandError；
路径无效 → ValueError。不扩冻结 16 码。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aotf.git.base import GitCommandError, run_git
from aotf.git.preflight import preflight

__all__ = [
    "CleanupError", "ArchiveInfo", "CleanupInfo",
    "archive_worktree", "remove_worktree",
]


class CleanupError(Exception):
    """worktree 收尾的 policy/完整性拒绝（非状态机错误）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ArchiveInfo:
    """任务归档事实。patch_sha256 = git 原始 diff bytes 的 sha。"""

    archive_dir: str
    manifest_path: str
    manifest_sha256: str
    patch_path: str
    patch_sha256: str
    untracked_dir: str
    dirty: bool
    branch: str | None
    base_commit: str
    archived_at: str


@dataclass(frozen=True, slots=True)
class CleanupInfo:
    """清理完成事实。"""

    worktree: str
    archive: ArchiveInfo
    branch: str | None
    removed: bool
    delete_branch: bool


def _nested(child: str, parent: str) -> bool:
    try:
        common = os.path.commonpath(
            [os.path.normcase(child), os.path.normcase(parent)]
        )
    except ValueError:
        return False
    return common == os.path.normcase(parent)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _as_utc(at: datetime | None) -> str:
    if at is None:
        at = datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_base_commit(manifest_path: Path) -> str:
    if not manifest_path.exists() or not manifest_path.is_file():
        raise CleanupError("baseline manifest not found")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CleanupError(f"invalid baseline manifest: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(
        data.get("base_commit"), str
    ):
        raise CleanupError("invalid baseline manifest")
    return data["base_commit"]


def _porcelain_changed(path: Path, timeout: float) -> bool:
    out = run_git(path, "status", "--porcelain", timeout=timeout)
    return any(line.strip() for line in out.splitlines())


def _dirty(report, path: Path, timeout: float) -> bool:
    return report.verdict == "dirty" or _porcelain_changed(path, timeout)


def _prepare(path: Path, timeout: float):
    """共同前置：确认 worktree + 取 branch/verdict/root。"""
    try:
        inside = run_git(path, "rev-parse", "--is-inside-work-tree",
                         timeout=timeout).strip()
    except GitCommandError as exc:
        if exc.returncode is None:
            raise
        inside = "false"
    if inside != "true":
        raise CleanupError("not a git work tree")
    try:
        run_git(path, "rev-parse", "--verify", "HEAD^{commit}",
                timeout=timeout)
    except GitCommandError as exc:
        if exc.returncode is None:
            raise
        raise CleanupError("worktree has no commits") from exc
    report = preflight(path, timeout=timeout)
    branch: str | None = None
    try:
        branch = run_git(path, "symbolic-ref", "--quiet", "--short", "HEAD",
                         timeout=timeout).strip()
    except GitCommandError:
        branch = None
    return report, branch


def _peer_worktree(path: Path, timeout: float) -> str:
    """同仓库另一 worktree 路径（作 git worktree remove 的 cwd）。"""
    out = run_git(path, "worktree", "list", "--porcelain", timeout=timeout)
    wt_norm = os.path.normcase(os.path.normpath(str(path)))
    for line in out.splitlines():
        if line.startswith("worktree "):
            peer = os.path.normpath(line[len("worktree "):])
            if os.path.normcase(peer) != wt_norm:
                return peer
    raise CleanupError("no peer worktree available for removal")


def archive_worktree(
    worktree: str | Path,
    *,
    archive_root: str | Path,
    at: datetime | None = None,
    timeout: float = 30.0,
) -> ArchiveInfo:
    """把任务 worktree 的证据与唯一变更归档到 archive_root/<name>/。

    dirty（未 checkpoint）亦可归档；只写 archive_root；对 worktree/base
    只读。输入全先校验（at/nesting/manifest），落盘失败回滚已建目录。
    """
    path = Path(worktree)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"worktree path not found or not a directory: {worktree}")
    report, branch = _prepare(path, timeout)

    archived_at = _as_utc(at)  # naive at 在落盘前拒绝
    root_dir = Path(archive_root)
    if not root_dir.is_absolute():
        raise CleanupError("archive root must be absolute")
    for other in (report.root, str(path)):
        if _nested(str(root_dir), other) or _nested(other, str(root_dir)):
            raise CleanupError(
                "archive root must not be nested with the worktree or base repo"
            )

    manifest_src = Path(str(path) + ".baseline-manifest.json")
    base_commit = _load_base_commit(manifest_src)
    dirty = _dirty(report, path, timeout)

    dest = root_dir / path.name
    if dest.exists():
        raise CleanupError("archive already exists")
    try:
        dest.mkdir(parents=True)
        _fill_archive(dest, path, manifest_src, base_commit, branch, dirty,
                      archived_at, timeout)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # 失败原子性：回滚半成品
        raise

    manifest_bytes = (dest / "baseline-manifest.json").read_bytes()
    patch_bytes = (dest / "delta.patch").read_bytes()
    ut_dir = dest / "untracked"
    return ArchiveInfo(
        archive_dir=str(dest),
        manifest_path=str(dest / "baseline-manifest.json"),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        patch_path=str(dest / "delta.patch"),
        patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
        untracked_dir=str(ut_dir),
        dirty=dirty,
        branch=branch,
        base_commit=base_commit,
        archived_at=archived_at,
    )


def _fill_archive(dest: Path, path: Path, manifest_src: Path,
                  base_commit: str, branch: str | None, dirty: bool,
                  archived_at: str, timeout: float) -> None:
    manifest_bytes = manifest_src.read_bytes()
    (dest / "baseline-manifest.json").write_bytes(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    patch_text = run_git(path, "diff", "--binary", "--no-ext-diff",
                         "--no-color", "--no-renames", base_commit, "--",
                         timeout=timeout)
    # run_git 剥单个尾 \n；git diff 恒以单 \n 结尾 → 还原即原始输出 bytes。
    patch_bytes = (patch_text + "\n" if patch_text else "").encode(
        "utf-8", "surrogateescape"
    )
    (dest / "delta.patch").write_bytes(patch_bytes)
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()

    raw_untracked = run_git(path, "ls-files", "--others", "--exclude-standard",
                            "-z", timeout=timeout)
    untracked = sorted(r for r in raw_untracked.split("\0") if r)
    ut_dir = dest / "untracked"
    ut_dir.mkdir(parents=True)
    for rel in untracked:
        src = path / rel
        dst = ut_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
        except OSError as exc:
            raise CleanupError(f"cannot archive untracked file {rel}: {exc}") \
                from exc

    (dest / "archive.json").write_bytes(_json_bytes({
        "worktree_path": str(path),
        "branch": branch,
        "base_commit": base_commit,
        "dirty": dirty,
        "manifest_sha256": manifest_sha256,
        "patch_sha256": patch_sha256,
        "untracked": untracked,
        "archived_at": archived_at,
    }))


def remove_worktree(
    worktree: str | Path,
    *,
    archive_root: str | Path,
    delete_branch: bool = False,
    base_repo: str | Path | None = None,
    force: bool = False,
    at: datetime | None = None,
    timeout: float = 30.0,
) -> CleanupInfo:
    """先归档再清理 worktree（fail-closed）。dirty 须 force=True。

    dirty 无 force → 直接拒绝（零副作用）；force/clean 才归档并 remove；
    删除经 peer worktree cwd（Windows 不能删自身 cwd）。main 零改动。
    """
    path = Path(worktree)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"worktree path not found or not a directory: {worktree}")
    report, branch = _prepare(path, timeout)
    if delete_branch:
        if base_repo is None:
            raise CleanupError("base_repo required to delete branch")
        if branch is None:
            raise CleanupError("cannot delete detached branch")
    peer = _peer_worktree(path, timeout)

    if _dirty(report, path, timeout) and not force:
        raise CleanupError(
            "worktree not clean; refuse remove without force "
            "(changes archived)"
        )
    archive = archive_worktree(path, archive_root=archive_root, at=at,
                               timeout=timeout)
    if _dirty(report, path, timeout):
        run_git(peer, "worktree", "remove", "--force", str(path),
                timeout=timeout)
    else:
        run_git(peer, "worktree", "remove", str(path), timeout=timeout)
    if delete_branch:
        run_git(base_repo, "branch", "-D", branch, timeout=timeout)

    return CleanupInfo(
        worktree=str(path),
        archive=archive,
        branch=branch,
        removed=True,
        delete_branch=delete_branch,
    )
