"""AOTF Execution Snapshot（M1）。

中间阶段崩溃恢复机制：每次 agent run 完成后持久化完整上下文，
支持重入时跳过已完成步骤、避免重复副作用或空 verdict 推进。

设计裁决：
- 快照粒度：每次 agent run 一次（implementer/reviewer 独立）；
- 快照内容：phase、worktree、mutations、evidence、verdict；
- 恢复语义：只读 snapshot 不自动恢复，由 engine 显式调用；
- 存储：SQLite snapshots 表（扩展 SCHEMA_SQL）；
- 幂等：INSERT OR REPLACE，同一 task_id+role 只保留最新；
- 清理：任务终态后自动删除（防止垃圾堆积）。

边界：本模块不做状态机逻辑，只提供 get/save/delete；
engine 负责何时调用了。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from typing import Any, Literal

__all__ = [
    "SnapshotRecord",
    "SnapshotError",
    "delete_snapshots_for_task",
    "load_snapshot",
    "save_snapshot",
]


class SnapshotError(Exception):
    """snapshot 操作失败（调用方 bug 或 DB 错误）。"""


# ─── typed record ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """单次 agent run 的完整执行上下文。"""

    snapshot_id: str
    task_id: str
    role: Literal["implementer", "reviewer"]
    phase_before: str  # 进入该 run 前的 task phase
    phase_after: str  # 离开该 run 后的 task phase
    worktree_path: str
    baseline_tree: str
    actual_tree: str | None
    mutations_applied: tuple[str, ...] | None
    delta_sha256: str | None
    evidence_json: str | None  # EvidenceBundleResult 序列化
    review_verdict: str | None  # "PASS" | "CONCERNS" | "BLOCK"
    run_inputs_json: str  # 原始 inputs 序列化
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id:
            raise SnapshotError("snapshot_id required")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise SnapshotError("task_id required")
        if self.role not in ("implementer", "reviewer"):
            raise SnapshotError(f"invalid role: {self.role}")
        if not isinstance(self.phase_before, str) or not self.phase_before:
            raise SnapshotError("phase_before required")
        if not isinstance(self.phase_after, str) or not self.phase_after:
            raise SnapshotError("phase_after required")
        if not isinstance(self.worktree_path, str) or not self.worktree_path:
            raise SnapshotError("worktree_path required")
        if self.worktree_path != self.worktree_path.strip():
            raise SnapshotError("worktree_path must not have whitespace")
        if not isinstance(self.baseline_tree, str) or not self.baseline_tree:
            raise SnapshotError("baseline_tree required")
        if self.actual_tree is not None and (
            not isinstance(self.actual_tree, str) or len(self.actual_tree) != 40
        ):
            raise SnapshotError("actual_tree must be 40-hex git oid or None")
        if self.mutations_applied is not None:
            if not isinstance(self.mutations_applied, tuple):
                raise SnapshotError("mutations_applied must be tuple")
            for m in self.mutations_applied:
                if not isinstance(m, str) or not m:
                    raise SnapshotError("mutation path must be non-empty str")
        if self.delta_sha256 is not None:
            if not isinstance(self.delta_sha256, str) or len(self.delta_sha256) != 64:
                raise SnapshotError("delta_sha256 must be 64-hex")
        if self.evidence_json is not None and not isinstance(self.evidence_json, str):
            raise SnapshotError("evidence_json must be str or None")
        if self.review_verdict is not None and self.review_verdict not in (
            "PASS", "CONCERNS", "BLOCK"
        ):
            raise SnapshotError("review_verdict must be PASS/CONCERNS/BLOCK or None")
        if not isinstance(self.run_inputs_json, str) or not self.run_inputs_json:
            raise SnapshotError("run_inputs_json required")
        try:
            json.loads(self.run_inputs_json)
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"run_inputs_json not valid JSON: {exc}") from exc
        # 时间链
        if self.updated_at < self.created_at:
            raise SnapshotError("updated_at < created_at")


# ─── DB ops ─────────────────────────────────────────────────────────────────

SNAPSHOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('implementer', 'reviewer')),
  phase_before TEXT NOT NULL,
  phase_after TEXT NOT NULL,
  worktree_path TEXT NOT NULL,
  baseline_tree TEXT NOT NULL,
  actual_tree TEXT,
  mutations_applied TEXT,
  delta_sha256 TEXT,
  evidence_json TEXT,
  review_verdict TEXT CHECK(review_verdict IN ('PASS', 'CONCERNS', 'BLOCK')),
  run_inputs_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _encode_dt(value: datetime) -> str:
    return value.isoformat()


def _decode_dt(text: str) -> datetime:
    from datetime import timezone
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _encode_tuple(value: tuple[str, ...] | None) -> str | None:
    if value is None:
        return None
    return "|".join(value)


def _decode_tuple(text: str | None) -> tuple[str, ...] | None:
    if text is None:
        return None
    return tuple(p for p in text.split("|") if p)


def _to_row(record: SnapshotRecord) -> tuple:
    return (
        record.snapshot_id,
        record.task_id,
        record.role,
        record.phase_before,
        record.phase_after,
        record.worktree_path,
        record.baseline_tree,
        record.actual_tree,
        _encode_tuple(record.mutations_applied),
        record.delta_sha256,
        record.evidence_json,
        record.review_verdict,
        record.run_inputs_json,
        _encode_dt(record.created_at),
        _encode_dt(record.updated_at),
    )


def _from_row(row: sqlite3.Row) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=row["snapshot_id"],
        task_id=row["task_id"],
        role=row["role"],
        phase_before=row["phase_before"],
        phase_after=row["phase_after"],
        worktree_path=row["worktree_path"],
        baseline_tree=row["baseline_tree"],
        actual_tree=row["actual_tree"],
        mutations_applied=_decode_tuple(row["mutations_applied"]),
        delta_sha256=row["delta_sha256"],
        evidence_json=row["evidence_json"],
        review_verdict=row["review_verdict"],
        run_inputs_json=row["run_inputs_json"],
        created_at=_decode_dt(row["created_at"]),
        updated_at=_decode_dt(row["updated_at"]),
    )


def save_snapshot(conn: sqlite3.Connection, record: SnapshotRecord) -> None:
    """保存或更新 snapshot（幂等）。"""
    conn.execute("""
        INSERT INTO snapshots
        (snapshot_id, task_id, role, phase_before, phase_after,
         worktree_path, baseline_tree, actual_tree, mutations_applied,
         delta_sha256, evidence_json, review_verdict, run_inputs_json,
         created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_id) DO UPDATE SET
          phase_after=excluded.phase_after,
          actual_tree=excluded.actual_tree,
          mutations_applied=excluded.mutations_applied,
          delta_sha256=excluded.delta_sha256,
          evidence_json=excluded.evidence_json,
          review_verdict=excluded.review_verdict,
          updated_at=excluded.updated_at
    """, _to_row(record))


def load_snapshot(
    conn: sqlite3.Connection,
    task_id: str,
    role: str,
) -> SnapshotRecord | None:
    """加载最近一次 snapshot（用于重入恢复）。"""
    row = conn.execute("""
        SELECT * FROM snapshots
        WHERE task_id = ? AND role = ?
        ORDER BY updated_at DESC
        LIMIT 1
    """, (task_id, role)).fetchone()
    return _from_row(row) if row else None


def delete_snapshots_for_task(conn: sqlite3.Connection, task_id: str) -> int:
    """删除任务所有 snapshot（终态后清理）。"""
    cursor = conn.execute("DELETE FROM snapshots WHERE task_id = ?", (task_id,))
    conn.commit()
    return cursor.rowcount
