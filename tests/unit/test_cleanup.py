"""M0-B5 worktree archive/清理与保留 测试。

全部仓库来自 sandbox fixture（tmp 一次性 git 仓）；归档只写 archive_root
（对 worktree/base 只读）；remove 后 main 零改动有硬断言；dirty 无 force
拒绝、force 前 untracked 已归档可恢复。
"""

from __future__ import annotations

import hashlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aotf.git.base import GitCommandError
from aotf.git.checkpoint import checkpoint_commit
from aotf.git.cleanup import (
    ArchiveInfo,
    CleanupError,
    CleanupInfo,
    archive_worktree,
    remove_worktree,
)
from aotf.git.preflight import preflight
from aotf.git.worktree import create_worktree

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)

# ── 环境层容错（仅测试侧；产品实现保持严格 fail-closed）────────────────────
#
# 现象：Windows 下 worktree 目录在刚被写入后可能被瞬时占用（典型为 Defender
#       实时扫描或文件系统收尾），使 `git worktree remove --force` 报
#       `error: failed to delete '<path>': Permission denied`。
# 证据：该失败在全量长跑中观测率约 40%（5 次 2 次），隔离与文件级均不复现；
#       失败后目录仍存在，但可被**立即**删除 ⇒ 占用是瞬时的；
#       archive_worktree 明示"只写 archive_root、对 worktree 只读" ⇒ 非本系统
#       写入所致。
# 处置：仅对「错误含 Permission denied 且目标目录仍存在」做有上限的等待重试。
#       其他错误、以及超出上限的占用，一律原样上抛——不掩盖真实缺陷。
#       产品侧 cleanup.remove_worktree 不做重试，维持 AOTF
#       「机械异常统一 fail-closed、不猜测恢复」的原则。
_TRANSIENT_LOCK_ATTEMPTS = 5
_TRANSIENT_LOCK_DELAY_SECONDS = 0.5


def _remove_worktree(worktree, /, **kwargs):
    """remove_worktree 的环境层容错包装（见上方说明）。"""
    for attempt in range(1, _TRANSIENT_LOCK_ATTEMPTS + 1):
        try:
            return remove_worktree(worktree, **kwargs)
        except GitCommandError as exc:
            transient = (
                "Permission denied" in str(exc)
                and Path(worktree).exists()
                and attempt < _TRANSIENT_LOCK_ATTEMPTS
            )
            if not transient:
                raise
            time.sleep(_TRANSIENT_LOCK_DELAY_SECONDS * attempt)
    raise AssertionError("unreachable: 重试循环应已在内部返回或上抛")


def test_transient_lock_wrapper_does_not_retry_other_errors(
        monkeypatch, tmp_path) -> None:
    """非「瞬时占用」特征的错误必须立即上抛，不得重试。"""
    calls: list = []

    def _boom(worktree, **kwargs):
        calls.append(worktree)
        raise GitCommandError(("git",), 1, b"", b"some real failure")

    monkeypatch.setattr(sys.modules[__name__], "remove_worktree", _boom)
    with pytest.raises(GitCommandError):
        _remove_worktree(str(tmp_path), archive_root=str(tmp_path))
    assert len(calls) == 1, "非瞬时错误不得重试"


def test_transient_lock_wrapper_retries_bounded_then_raises(
        monkeypatch, tmp_path) -> None:
    """即使命中瞬时特征，重试也必须有上限并最终上抛。"""
    calls: list = []

    def _locked(worktree, **kwargs):
        calls.append(worktree)
        raise GitCommandError(
            ("git",), 255, b"",
            b"error: failed to delete 'x': Permission denied")

    monkeypatch.setattr(sys.modules[__name__], "remove_worktree", _locked)
    with pytest.raises(GitCommandError):
        _remove_worktree(str(tmp_path), archive_root=str(tmp_path))
    assert len(calls) == _TRANSIENT_LOCK_ATTEMPTS, (
        "重试次数必须等于设定上限，防止无限重试"
    )


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


def _aroot(sandbox) -> str:
    return str(sandbox.base / "archive")


def _manifest_path(wt: Path) -> str:
    return str(wt) + ".baseline-manifest.json"


def test_archive_clean_after_checkpoint(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    _cp(wt)
    info = archive_worktree(wt, archive_root=_aroot(sandbox), at=T0)
    assert isinstance(info, ArchiveInfo)
    assert info.dirty is False
    assert info.branch == "task-1"
    assert info.base_commit == json_load(_manifest_path(wt))["base_commit"]
    assert Path(info.manifest_path).exists()
    assert Path(info.patch_path).exists()
    assert Path(info.patch_path).stat().st_size > 0
    assert info.manifest_sha256 == _sha(_manifest_path(wt))


def test_archive_dirty_preserves_patch(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")  # dirty（未 checkpoint）
    info = archive_worktree(wt, archive_root=_aroot(sandbox), at=T0)
    assert info.dirty is True
    patch = Path(info.patch_path).read_bytes()
    assert b"a.txt" in patch


def test_archive_untracked_copied(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.untracked(wt, "u.py", content="x = 1\n")
    sandbox.untracked(wt, "z/u2.txt", content="inner\n")
    info = archive_worktree(wt, archive_root=_aroot(sandbox), at=T0)
    assert (Path(info.untracked_dir) / "u.py").read_text(
        encoding="utf-8") == "x = 1\n"
    assert (Path(info.untracked_dir) / "z" / "u2.txt").read_text(
        encoding="utf-8") == "inner\n"


def test_archive_manifest_sha_matches_source_bytes(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    src = _manifest_path(wt)
    src_sha = _sha(src)
    info = archive_worktree(wt, archive_root=_aroot(sandbox), at=T0)
    assert info.manifest_sha256 == src_sha
    assert _sha(info.manifest_path) == src_sha


def test_archive_json_records_fields(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    info = archive_worktree(wt, archive_root=_aroot(sandbox), at=T0)
    payload = json_load(Path(info.archive_dir) / "archive.json")
    assert payload["worktree_path"] == str(wt)
    assert payload["branch"] == "task-1"
    assert payload["base_commit"] == info.base_commit
    assert payload["dirty"] is False
    assert payload["manifest_sha256"] == info.manifest_sha256
    assert payload["patch_sha256"] == info.patch_sha256
    assert payload["untracked"] == []
    assert payload["archived_at"] == "2026-09-01T00:00:00Z"


def test_archive_manifest_missing_raises(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    Path(_manifest_path(wt)).unlink()
    with pytest.raises(CleanupError, match="manifest not found"):
        archive_worktree(wt, archive_root=_aroot(sandbox))


def test_archive_refuse_existing_archive_dir(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    archive_worktree(wt, archive_root=_aroot(sandbox))
    with pytest.raises(CleanupError, match="archive already exists"):
        archive_worktree(wt, archive_root=_aroot(sandbox))


def test_archive_refuse_nested_archive_root(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    with pytest.raises(CleanupError, match="not be nested"):
        archive_worktree(wt, archive_root=str(wt))


def test_remove_clean_worktree(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    _cp(wt)
    main_head = preflight(base).head_commit
    info = _remove_worktree(wt, archive_root=_aroot(sandbox), at=T0)
    assert isinstance(info, CleanupInfo)
    assert info.removed is True
    assert not Path(info.worktree).exists()
    assert Path(info.archive.patch_path).exists()
    assert Path(info.archive.manifest_path).exists()
    # 分支默认保留
    sandbox.git(base, "show-ref", "--verify", "--quiet",
                "refs/heads/task-1")
    report = preflight(base)
    assert report.verdict == "clean"
    assert report.head_commit == main_head


def test_remove_clean_with_delete_branch(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    _cp(wt)
    _remove_worktree(wt, archive_root=_aroot(sandbox), delete_branch=True,
                    base_repo=base, at=T0)
    assert not Path(wt).exists()
    with pytest.raises(AssertionError):
        sandbox.git(base, "show-ref", "--verify", "--quiet",
                    "refs/heads/task-1")


def test_remove_dirty_refused_without_force(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    with pytest.raises(CleanupError, match="without force"):
        _remove_worktree(wt, archive_root=_aroot(sandbox))
    assert Path(wt).exists()
    assert "a.txt" in preflight(wt).dirty_paths
    assert not (Path(_aroot(sandbox)) / wt.name).exists()


def test_remove_dirty_with_force_removes(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    info = _remove_worktree(wt, archive_root=_aroot(sandbox), force=True,
                           at=T0)
    assert not Path(wt).exists()
    assert Path(info.archive.patch_path).stat().st_size > 0


def test_remove_dirty_force_preserves_untracked(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    sandbox.untracked(wt, "u.py", content="keep\n")
    info = _remove_worktree(wt, archive_root=_aroot(sandbox), force=True,
                           at=T0)
    assert not Path(wt).exists()
    assert (Path(info.archive.untracked_dir) / "u.py").read_text(
        encoding="utf-8") == "keep\n"


def test_remove_base_main_untouched(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    sandbox.write(wt, "a.txt", content="v2\n")
    _cp(wt)
    main_head = preflight(base).head_commit
    main_branch = sandbox.git(base, "symbolic-ref", "--short", "HEAD")
    files_before = {rel: _sha(str(base / rel)) for rel in ("a.txt", "init.txt")}
    _remove_worktree(wt, archive_root=_aroot(sandbox), delete_branch=True,
                    base_repo=base)
    report = preflight(base)
    assert report.verdict == "clean"
    assert report.head_commit == main_head
    assert sandbox.git(base, "symbolic-ref", "--short", "HEAD") == main_branch
    for rel, h in files_before.items():
        assert _sha(str(base / rel)) == h


def test_remove_delete_branch_requires_base_repo(sandbox) -> None:
    base = _base_with(sandbox)
    wt = _wt(sandbox, base)
    with pytest.raises(CleanupError, match="base_repo required"):
        _remove_worktree(wt, archive_root=_aroot(sandbox), delete_branch=True)
    assert Path(wt).exists()
    assert not (Path(_aroot(sandbox)) / wt.name).exists()


def test_remove_non_repo_raises(sandbox) -> None:
    d = sandbox.base / "plain"
    d.mkdir()
    with pytest.raises(CleanupError, match="not a git work tree"):
        _remove_worktree(d, archive_root=_aroot(sandbox))
    with pytest.raises(ValueError):
        _remove_worktree(sandbox.base / "nope", archive_root=_aroot(sandbox))


def json_load(path: str | Path) -> dict:
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))
