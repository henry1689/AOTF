"""AOTF recovery — 一致性诊断 + 后继任务工厂 (A3c)。

R1-07 落码：终态任务不在此包被改写；FAILED/SAFE_HALT 恢复业务的唯一机器出口
是产出 superseding 后继任务（COMPLETED 正常闭环 / CANCELLED 人工取消不产自动
后继）。diagnose_task 只读、不推进（§16「只提出方案」）——以 ledger 为真对账，
DRIFT 不得自动推进；propose_successor 纯工厂、零 DB 写。恢复编排/阶段回放归
A4、recover CLI 归 A5；recovery event 事件类不引入（A2b fold/verify 冻结保持）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aotf import ledger, store
from aotf.errors import AotfError, ErrorCode
from aotf.models import TERMINAL_TASK_PHASES, TaskPhase, TaskRecord


@dataclass(frozen=True, slots=True)
class TaskRecovery:
    """任务恢复诊断：verdict ∈ CONSISTENT / DRIFT / MISSING。"""

    task_id: str
    verdict: str
    phase: TaskPhase | None
    version: int | None
    terminal_reason: str | None
    detail: str

    @property
    def is_terminal(self) -> bool:
        return (self.verdict == "CONSISTENT" and self.phase is not None
                and self.phase in TERMINAL_TASK_PHASES)

    @property
    def recoverable_terminal(self) -> bool:
        return (self.is_terminal
                and self.phase in (TaskPhase.FAILED, TaskPhase.SAFE_HALT))


def diagnose_task(conn, *, task_id: str) -> TaskRecovery:
    """只读对账：ledger 为真，输出恢复诊断；不写库不推进。"""
    if store.get_task(conn, task_id) is None:
        return TaskRecovery(task_id=task_id, verdict="MISSING", phase=None,
                            version=None, terminal_reason=None, detail="no task row")
    try:
        projection = ledger.verify_task(conn, task_id=task_id)
    except AotfError as exc:
        return TaskRecovery(task_id=task_id, verdict="DRIFT", phase=None,
                            version=None, terminal_reason=None, detail=str(exc))
    return TaskRecovery(task_id=task_id, verdict="CONSISTENT",
                        phase=projection.phase, version=projection.version,
                        terminal_reason=projection.terminal_reason,
                        detail="ledger consistent")


def propose_successor(
    rec: TaskRecovery,
    *,
    successor_task_id: str,
    cycle_id: str,
    created_at: datetime,
) -> TaskRecord:
    """纯工厂：仅 CONSISTENT 的 FAILED/SAFE_HALT 产出 superseding 后继。"""
    if rec.verdict != "CONSISTENT":
        raise AotfError(
            ErrorCode.INVALID_INPUT, "successor requires consistent diagnosis",
            {"detail": rec.detail},
        )
    if rec.phase not in (TaskPhase.FAILED, TaskPhase.SAFE_HALT):
        raise AotfError(
            ErrorCode.INVALID_INPUT, "successor only from FAILED or SAFE_HALT",
            {"phase": rec.phase.value if rec.phase is not None else None},
        )
    return TaskRecord(
        task_id=successor_task_id, cycle_id=cycle_id,
        supersedes_task_id=rec.task_id, phase=TaskPhase.CREATED, version=0,
        baseline_commit=None, baseline_tree=None, proposal_sha256=None,
        authorization_id=None, worktree_path=None, worktree_branch=None,
        actual_tree=None, delta_sha256=None, terminal_reason=None,
        created_at=created_at, updated_at=created_at,
    )
