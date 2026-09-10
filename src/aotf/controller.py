"""AOTF controller-owned 文件快照与变更执行。

模型只能提出 typed mutation intents；不得直接持有 Edit/Write/Bash 等副作用
工具。控制器在这里统一校验批准范围、路径、symlink、preimage hash、文件数和
总字节数，随后原子替换。这样 Claude Code 只提供推理结果，AOTF 始终拥有
项目写入和状态推进权。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

__all__ = [
    "ControllerMutationError",
    "FileMutation",
    "FileSnapshot",
    "apply_mutations",
    "snapshot_files",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_MAX_FILES = 20
_DEFAULT_MAX_TOTAL_BYTES = 1_000_000


class ControllerMutationError(Exception):
    """控制器拒绝或无法完整应用 mutation intents。"""


@dataclass(frozen=True, slots=True)
class FileMutation:
    """模型提出、由 AOTF 控制器执行的一项文件变更。"""

    path: str
    operation: Literal["create", "replace", "delete"]
    expected_sha256: str | None
    content: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("mutation path required")
        if self.operation not in ("create", "replace", "delete"):
            raise ValueError("invalid mutation operation")
        if self.expected_sha256 is not None and (
                not isinstance(self.expected_sha256, str)
                or not _SHA256_RE.fullmatch(self.expected_sha256)):
            raise ValueError("expected_sha256 must be lowercase sha256")
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("mutation content must be str or None")
        if self.operation == "create":
            if self.expected_sha256 is not None or self.content is None:
                raise ValueError("create requires null hash and string content")
        elif self.operation == "replace":
            if self.expected_sha256 is None or self.content is None:
                raise ValueError("replace requires hash and string content")
        elif self.expected_sha256 is None or self.content is not None:
            raise ValueError("delete requires hash and null content")


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """AOTF 提供给一次性模型调用的批准文件事实。"""

    path: str
    exists: bool
    sha256: str | None
    content: str | None


def _relative(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ControllerMutationError("path must be non-empty POSIX relative path")
    posix = PurePosixPath(raw)
    win = PureWindowsPath(raw)
    if posix.is_absolute() or win.drive or any(
            part in ("", ".", "..") for part in posix.parts):
        raise ControllerMutationError("path must be normalized relative path")
    if posix.parts[0].casefold() == ".git":
        raise ControllerMutationError("git control path is protected")
    return posix.as_posix()


def _safe_target(root: Path, rel: str) -> Path:
    target = root
    for part in PurePosixPath(rel).parts:
        target = target / part
        if target.is_symlink():
            raise ControllerMutationError(f"symlink path rejected: {rel}")
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError:
        raise ControllerMutationError(f"path escapes worktree: {rel}") from None
    return target


def _allowed_set(allowed_files: tuple[str, ...]) -> set[str]:
    if not isinstance(allowed_files, tuple):
        raise ControllerMutationError("allowed_files must be tuple")
    normalized = [_relative(path) for path in allowed_files]
    if len(normalized) != len(set(normalized)):
        raise ControllerMutationError("duplicate allowed file")
    return {os.path.normcase(path) for path in normalized}


def _root(worktree: str) -> Path:
    root = Path(worktree)
    if not root.is_dir() or root.is_symlink():
        raise ControllerMutationError("worktree must be a real directory")
    return root.resolve(strict=True)


def snapshot_files(
    worktree: str,
    allowed_files: tuple[str, ...],
    *,
    max_files: int = _DEFAULT_MAX_FILES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
) -> tuple[FileSnapshot, ...]:
    """读取批准文件供模型一次性推理；二进制/超限/路径歧义均拒绝。"""
    root = _root(worktree)
    allowed = _allowed_set(allowed_files)
    if len(allowed) > max_files:
        raise ControllerMutationError("snapshot file limit exceeded")
    total = 0
    snapshots: list[FileSnapshot] = []
    for raw in allowed_files:
        rel = _relative(raw)
        target = _safe_target(root, rel)
        if not target.exists():
            snapshots.append(FileSnapshot(rel, False, None, None))
            continue
        if not target.is_file():
            raise ControllerMutationError(f"approved path is not a file: {rel}")
        data = target.read_bytes()
        total += len(data)
        if total > max_total_bytes:
            raise ControllerMutationError("snapshot byte limit exceeded")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ControllerMutationError(
                f"non-UTF-8 approved file unsupported: {rel}") from None
        snapshots.append(FileSnapshot(
            rel, True, hashlib.sha256(data).hexdigest(), content))
    return tuple(snapshots)


def _atomic_write(target: Path, data: bytes, mode: int) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".aotf-controller-",
                                     dir=str(target.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def apply_mutations(
    worktree: str,
    allowed_files: tuple[str, ...],
    mutations: tuple[FileMutation, ...],
    *,
    max_files: int = _DEFAULT_MAX_FILES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
) -> tuple[str, ...]:
    """校验后由控制器应用完整 mutation batch；失败时尽力恢复 preimage。"""
    root = _root(worktree)
    allowed = _allowed_set(allowed_files)
    if not isinstance(mutations, tuple) or not mutations:
        raise ControllerMutationError("non-empty mutation tuple required")
    if len(mutations) > max_files:
        raise ControllerMutationError("mutation file limit exceeded")
    if not all(isinstance(item, FileMutation) for item in mutations):
        raise ControllerMutationError("invalid mutation item")

    prepared: list[tuple[FileMutation, Path, bytes | None, int, bytes | None]] = []
    seen: set[str] = set()
    total = 0
    for item in mutations:
        rel = _relative(item.path)
        key = os.path.normcase(rel)
        if key not in allowed:
            raise ControllerMutationError(f"mutation outside approval: {rel}")
        if key in seen:
            raise ControllerMutationError(f"duplicate mutation path: {rel}")
        seen.add(key)
        target = _safe_target(root, rel)
        if not target.parent.is_dir() or target.parent.is_symlink():
            raise ControllerMutationError(f"mutation parent missing: {rel}")
        before: bytes | None
        mode = 0o644
        if target.exists():
            if not target.is_file():
                raise ControllerMutationError(f"mutation target not file: {rel}")
            before = target.read_bytes()
            mode = target.stat().st_mode & 0o777
        else:
            before = None
        actual_sha = hashlib.sha256(before).hexdigest() \
            if before is not None else None
        if item.operation == "create" and before is not None:
            raise ControllerMutationError(f"create target exists: {rel}")
        if item.operation in ("replace", "delete") and before is None:
            raise ControllerMutationError(f"mutation target missing: {rel}")
        if item.expected_sha256 != actual_sha:
            raise ControllerMutationError(f"preimage hash mismatch: {rel}")
        after = item.content.encode("utf-8") \
            if item.content is not None else None
        total += len(after or b"")
        if total > max_total_bytes:
            raise ControllerMutationError("mutation byte limit exceeded")
        prepared.append((item, target, before, mode, after))

    applied: list[tuple[Path, bytes | None, int]] = []
    try:
        for item, target, before, mode, after in prepared:
            # 写入前再次核对，避免模型调用后到执行间的 preimage 漂移。
            current = target.read_bytes() if target.exists() else None
            current_sha = hashlib.sha256(current).hexdigest() \
                if current is not None else None
            if current_sha != item.expected_sha256:
                raise ControllerMutationError(
                    f"preimage changed during apply: {item.path}")
            if after is None:
                target.unlink()
            else:
                _atomic_write(target, after, mode)
            applied.append((target, before, mode))
    except Exception as exc:
        rollback_errors: list[str] = []
        for target, before, mode in reversed(applied):
            try:
                if before is None:
                    if target.exists():
                        target.unlink()
                else:
                    _atomic_write(target, before, mode)
            except OSError as rollback_exc:
                rollback_errors.append(type(rollback_exc).__name__)
        if rollback_errors:
            raise ControllerMutationError(
                "mutation apply failed and rollback incomplete: "
                + ",".join(rollback_errors)) from exc
        if isinstance(exc, ControllerMutationError):
            raise
        raise ControllerMutationError(
            f"mutation apply failed: {type(exc).__name__}") from exc
    return tuple(item.path for item, *_ in prepared)
