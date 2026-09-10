"""AOTF EventLedger — 账本流 / 聚合重建 / 重放幂等 (A2b).

读侧实现权威架构「SQLite 事件账本是唯一状态真相」：event_stream 按
aggregate_version 升序读回聚合事件流；fold_task_stream 为纯函数，从
CREATED 起点沿 TASK_PHASE_CHANGED 链重建任务状态投影
（phase/version/terminal_reason）；verify_task 以账本为真对账物化行。
本模块不写事件、不推进状态；重建范围为状态投影，完整行
materialization（含元数据/快照）与 lease/outbox/recovery 属 A3。
当前任务事件流严格仅 TASK_PHASE_CHANGED 一种事件类。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from aotf import state, store
from aotf.errors import AotfError, ErrorCode
from aotf.models import (
    AggregateType,
    EventRecord,
    TERMINAL_TASK_PHASES,
    TaskPhase,
)

_PHASE_EVENT = "TASK_PHASE_CHANGED"


@dataclass(frozen=True, slots=True)
class TaskLedgerState:
    """任务状态投影：由事件账本折叠得到的最小可验证状态。"""

    phase: TaskPhase
    version: int
    terminal_reason: str | None


def _bad(detail: str) -> None:
    raise AotfError(
        ErrorCode.INTEGRITY_FAILURE,
        "ledger integrity",
        {"detail": detail},
    )


def event_stream(
    conn: sqlite3.Connection,
    *,
    aggregate_type: AggregateType,
    aggregate_id: str,
) -> list[EventRecord]:
    """按 aggregate_version 升序返回某聚合的全部事件（账本流）。"""
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT event_id FROM events WHERE aggregate_type = ? AND aggregate_id = ? "
            "ORDER BY aggregate_version ASC",
            (aggregate_type.value, aggregate_id),
        )
    ]
    result: list[EventRecord] = []
    for event_id in ids:
        event = store.get_event(conn, event_id)
        if event is not None:
            result.append(event)
    return result


def fold_task_stream(events: Sequence[EventRecord]) -> TaskLedgerState:
    """纯函数：从 CREATED 折叠 TASK_PHASE_CHANGED 链，重建状态投影。"""
    phase = TaskPhase.CREATED
    reason: str | None = None
    for index, event in enumerate(events, start=1):
        if event.aggregate_version != index:
            _bad(f"version gap at {index}")
        if event.event_type != _PHASE_EVENT:
            _bad(f"unexpected event type: {event.event_type}")
        try:
            body = json.loads(event.payload_json)
            from_phase = TaskPhase(body["from_phase"])
            to_phase = TaskPhase(body["to_phase"])
        except (KeyError, ValueError, TypeError):
            _bad("malformed phase payload")
        if from_phase != phase:
            _bad("broken chain")
        if not state.can_transition(from_phase, to_phase):
            _bad("illegal phase event")
        next_reason = body.get("reason")
        if (to_phase in TERMINAL_TASK_PHASES) != (next_reason is not None):
            _bad("reason mismatch")
        phase = to_phase
        reason = next_reason
    return TaskLedgerState(phase=phase, version=len(events), terminal_reason=reason)


def verify_task(conn: sqlite3.Connection, *, task_id: str) -> TaskLedgerState:
    """以账本为真对账 tasks 行；漂移即 INTEGRITY_FAILURE。"""
    task = store.get_task(conn, task_id)
    if task is None:
        _bad(f"task row missing: {task_id}")
    events = event_stream(conn, aggregate_type=AggregateType.TASK, aggregate_id=task_id)
    projection = fold_task_stream(events)
    if (projection.phase, projection.version, projection.terminal_reason) != (
        task.phase,
        task.version,
        task.terminal_reason,
    ):
        _bad("task projection drifted from ledger")
    if projection.version > 0 and events[-1].created_at != task.updated_at:
        _bad("updated_at drifted from ledger")
    return projection
