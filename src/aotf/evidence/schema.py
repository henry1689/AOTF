"""AOTF TestEvidence typed contract + 确定性序列化（M0-C1）。

spec §9.2 TestEvidence 最小结构的冻结化：evidence_id/task_id/
worktree_tree/delta_sha256/runner{identity/host_fingerprint/started/ended}/
checks[{check_id/command_id/cwd/exit_code/stdout_sha256/stderr_sha256/
duration_ms/status}]/bundle_sha256。

本包为纯数据契约（C2 runner 产出、C3 bundle、C4 policy 共用基座），零
副作用。校验全 fail-closed（EvidenceError，不扩冻结 16 码）：

- 令牌 `[A-Za-z0-9._@-]{1,64}`（evidence_id/task_id/identity/check_id/
  command_id）；cwd 相对、无 `..`、非绝对；
- worktree_tree = 40-hex（git tree oid）；delta_sha256/stdout_sha256/
  stderr_sha256/bundle_sha256/host_fingerprint = 64-hex（sha256）；
- status ∈ {passed, failed, error}；exit_code int（拒 bool）；duration_ms
  int>=0（拒 bool）；时间 tz-aware 且 started <= ended；schema_version==1；
  空 checks 合法（required 判定归 C4）；cwd 相对、无 `..`、无根化前缀
  （Windows 下 `/x` `\\x` is_absolute()==False 但 root 非空——一并拒绝）。

确定性编码：sorted-json ensure_ascii 紧凑（M0-B 先例，不经 canonical——
自有 schema、自由文本字段）；datetime→UTC `Z`（保留 %f 微秒防同秒碰撞）；
checks 按 (command_id, check_id) sorted。bundle_sha256 为排除自身字段的
canonical bytes sha。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

__all__ = [
    "EvidenceError",
    "EvidenceRunnerInfo",
    "EvidenceCheck",
    "TestEvidenceRecord",
    "evidence_json_bytes",
    "compute_bundle_sha256",
    "record_with_bundle",
]

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 1
_STATUSES = frozenset({"passed", "failed", "error"})


class EvidenceError(Exception):
    """evidence typed contract 校验拒绝（非状态机错误）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _check_token(name: str, value: str) -> None:
    if value in (".", ".."):  # 正则放行但会造成目录穿越/路径歧义
        raise EvidenceError(f"invalid {name} token")
    if not _TOKEN_RE.fullmatch(value):
        raise EvidenceError(f"invalid {name} token")


def _require_sha(name: str, value: str, *, hex64: bool) -> None:
    ok = _HEX64_RE.fullmatch(value) if hex64 else _HEX40_RE.fullmatch(value)
    if not ok:
        raise EvidenceError(f"invalid {name} sha256" if hex64
                            else f"invalid {name} hex")


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise EvidenceError(f"{name} must be timezone-aware")


def _require_relative_cwd(cwd: str) -> None:
    p = Path(cwd)
    # Windows 下 '/x' / '\\x' 无盘符 is_absolute()==False，但 root 非空即根化。
    if not cwd or p.is_absolute() or p.root or ".." in p.parts:
        raise EvidenceError("invalid cwd")


@dataclass(frozen=True, slots=True)
class EvidenceRunnerInfo:
    """证据生产方身份与时段（spec §9.2 runner）。"""

    identity: str
    host_fingerprint: str | None
    started_at: datetime
    ended_at: datetime

    def __post_init__(self) -> None:
        _check_token("identity", self.identity)
        if self.host_fingerprint is not None:
            _require_sha("host_fingerprint", self.host_fingerprint, hex64=True)
        _require_aware(self.started_at, "started_at")
        _require_aware(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise EvidenceError("ended_at before started_at")


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    """单条 check 的运行事实（spec §9.2 checks[]）。"""

    check_id: str
    command_id: str
    cwd: str = "."
    exit_code: int = 0
    stdout_sha256: str = "0" * 64
    stderr_sha256: str = "0" * 64
    duration_ms: int = 0
    status: Literal["passed", "failed", "error"] = "passed"

    def __post_init__(self) -> None:
        _check_token("check_id", self.check_id)
        _check_token("command_id", self.command_id)
        _require_relative_cwd(self.cwd)
        if not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool):
            raise EvidenceError("exit_code must be int")
        if (not isinstance(self.duration_ms, int)
                or isinstance(self.duration_ms, bool)
                or self.duration_ms < 0):
            raise EvidenceError("duration_ms must be non-negative int")
        _require_sha("stdout_sha256", self.stdout_sha256, hex64=True)
        _require_sha("stderr_sha256", self.stderr_sha256, hex64=True)
        if self.status not in _STATUSES:
            raise EvidenceError("invalid status")


@dataclass(frozen=True, slots=True)
class TestEvidenceRecord:
    """TestEvidence 最小结构（spec §9.2）。"""

    schema_version: int
    evidence_id: str
    task_id: str
    worktree_tree: str
    delta_sha256: str
    runner: EvidenceRunnerInfo
    checks: tuple[EvidenceCheck, ...]
    bundle_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise EvidenceError("invalid schema_version")
        _check_token("evidence_id", self.evidence_id)
        _check_token("task_id", self.task_id)
        _require_sha("worktree_tree", self.worktree_tree, hex64=False)
        _require_sha("delta_sha256", self.delta_sha256, hex64=True)
        _require_sha("bundle_sha256", self.bundle_sha256, hex64=True)
        if not isinstance(self.checks, tuple):
            raise EvidenceError("checks must be tuple")


def _as_utc_str(dt: datetime) -> str:
    # 保留 %f：秒级截断会让同秒 run 的序列化/bundle 碰撞，确定性有损。
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _record_dict(record: TestEvidenceRecord, *, include_bundle: bool) -> dict:
    checks = sorted(
        (
            {
                "check_id": c.check_id,
                "command_id": c.command_id,
                "cwd": c.cwd,
                "exit_code": c.exit_code,
                "stdout_sha256": c.stdout_sha256,
                "stderr_sha256": c.stderr_sha256,
                "duration_ms": c.duration_ms,
                "status": c.status,
            }
            for c in record.checks
        ),
        key=lambda c: (c["command_id"], c["check_id"]),
    )
    return {
        "schema_version": record.schema_version,
        "evidence_id": record.evidence_id,
        "task_id": record.task_id,
        "worktree_tree": record.worktree_tree,
        "delta_sha256": record.delta_sha256,
        "runner": {
            "identity": record.runner.identity,
            "host_fingerprint": record.runner.host_fingerprint,
            "started_at": _as_utc_str(record.runner.started_at),
            "ended_at": _as_utc_str(record.runner.ended_at),
        },
        "checks": checks,
        "bundle_sha256": record.bundle_sha256 if include_bundle else "",
    }


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def evidence_json_bytes(record: TestEvidenceRecord, *,
                        include_bundle: bool) -> bytes:
    """确定性序列化。include_bundle=False 时 bundle 键留空（布局稳定）。"""
    return _json_bytes(_record_dict(record, include_bundle=include_bundle))


def compute_bundle_sha256(record: TestEvidenceRecord) -> str:
    """排除 bundle_sha256 自身的 canonical bytes sha（自洽）。"""
    payload = _record_dict(record, include_bundle=False)
    del payload["bundle_sha256"]
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def record_with_bundle(record: TestEvidenceRecord) -> TestEvidenceRecord:
    """返回 bundle_sha256 = compute_bundle_sha256 的副本。"""
    return replace(record, bundle_sha256=compute_bundle_sha256(record))
