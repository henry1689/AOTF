"""AOTF task state machine core (A2a).

冻结权威架构 §5.1/§5.2 的合法转换矩阵；transition 在单个事务内执行
CAS 状态更新与 TASK_PHASE_CHANGED 事件追加，保证原子性（§6.1）。
EventLedger（stream/聚合重建/重放幂等）属 A2b。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from aotf import store
from aotf.errors import AotfError, ErrorCode
from aotf.models import (
    AggregateType,
    CanonicalPayload,
    EventRecord,
    TERMINAL_TASK_PHASES,
    TaskPhase,
    TaskRecord,
)

LEGAL_TRANSITIONS = {
    TaskPhase.CREATED: frozenset({TaskPhase.BASELINING}),
    TaskPhase.BASELINING: frozenset({TaskPhase.PLAN_READY}),
    TaskPhase.PLAN_READY: frozenset({TaskPhase.AWAITING_PLAN_APPROVAL}),
    TaskPhase.AWAITING_PLAN_APPROVAL: frozenset({TaskPhase.EDIT_AUTHORIZED}),
    TaskPhase.EDIT_AUTHORIZED: frozenset({TaskPhase.IMPLEMENTING}),
    TaskPhase.IMPLEMENTING: frozenset({TaskPhase.DELTA_CAPTURED}),
    TaskPhase.DELTA_CAPTURED: frozenset({TaskPhase.EVIDENCE_RUNNING}),
    TaskPhase.EVIDENCE_RUNNING: frozenset({TaskPhase.REVIEWING}),
    TaskPhase.REVIEWING: frozenset({TaskPhase.POLICY_EVALUATING}),
    TaskPhase.POLICY_EVALUATING: frozenset({
        TaskPhase.CHECKPOINT_READY,
        TaskPhase.RELEASE_CANDIDATE,
        TaskPhase.FAILED,
    }),
    TaskPhase.CHECKPOINT_READY: frozenset({TaskPhase.COMPLETED}),
    TaskPhase.RELEASE_CANDIDATE: frozenset({TaskPhase.COMPLETED}),
}


def can_transition(from_phase: TaskPhase, to_phase: TaskPhase) -> bool:
    if from_phase in TERMINAL_TASK_PHASES or from_phase == to_phase:
        return False
    if to_phase in (TaskPhase.CANCELLED, TaskPhase.SAFE_HALT):
        return True
    return to_phase in LEGAL_TRANSITIONS.get(from_phase, frozenset())


def transition(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    to_phase: TaskPhase,
    terminal_reason: str | None,
    event_id: str,
    at: datetime,
) -> TaskRecord:
    task = store.get_task(conn, task_id)
    if task is None:
        raise AotfError(
            ErrorCode.VERSION_CONFLICT, "task not found", {"task_id": task_id}
        )
    from_phase = task.phase
    if from_phase in TERMINAL_TASK_PHASES:
        raise AotfError(
            ErrorCode.TERMINAL_STATE, "task already terminal",
            {"task_id": task_id, "phase": from_phase.value},
        )
    if not can_transition(from_phase, to_phase):
        raise AotfError(
            ErrorCode.INVALID_STATE_TRANSITION, "illegal transition",
            {"task_id": task_id, "from": from_phase.value, "to": to_phase.value},
        )
    if to_phase in TERMINAL_TASK_PHASES:
        if terminal_reason is None or not terminal_reason.strip():
            raise AotfError(
                ErrorCode.INVALID_INPUT, "terminal reason required",
                {"field": "terminal_reason"},
            )
        reason = terminal_reason
    else:
        reason = None
    ok = store.cas_task_phase(
        conn, task_id=task_id, expected_phase=from_phase,
        expected_version=task.version, new_phase=to_phase,
        terminal_reason=reason, updated_at=at,
    )
    if not ok:
        raise AotfError(
            ErrorCode.VERSION_CONFLICT, "cas conflict", {"task_id": task_id}
        )
    payload = CanonicalPayload.from_value({
        "from_phase": from_phase.value,
        "to_phase": to_phase.value,
        "reason": reason,
    })
    event = EventRecord(
        event_id=event_id,
        aggregate_type=AggregateType.TASK,
        aggregate_id=task_id,
        aggregate_version=task.version + 1,
        event_type="TASK_PHASE_CHANGED",
        producer="state-service",
        payload=payload,
        created_at=at,
    )
    try:
        store.insert_event(conn, event)
    except sqlite3.IntegrityError:
        conn.rollback()
        raise AotfError(
            ErrorCode.IDEMPOTENCY_CONFLICT, "duplicate event",
            {"event_id": event_id},
        ) from None
    conn.commit()
    return store.get_task(conn, task_id)
