"""M0-A2b EventLedger 账本流/聚合重建/重放幂等 测试。

DB 一律使用 pytest tmp_path；不访问网络/时钟/环境秘密。
读侧验证：fold/verify 无任何写副作用。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from aotf import db, ledger, state, store
from aotf.errors import AotfError, ErrorCode
from aotf.models import (
    AggregateType,
    CanonicalPayload,
    CycleRecord,
    EventRecord,
    TaskPhase,
    TaskRecord,
)

T0 = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 1, 1, 0, 0, tzinfo=timezone.utc)
TASK = "task-min"

_MAIN = [
    TaskPhase.CREATED, TaskPhase.BASELINING, TaskPhase.PLAN_READY,
    TaskPhase.AWAITING_PLAN_APPROVAL, TaskPhase.EDIT_AUTHORIZED,
    TaskPhase.IMPLEMENTING, TaskPhase.DELTA_CAPTURED, TaskPhase.EVIDENCE_RUNNING,
    TaskPhase.REVIEWING, TaskPhase.POLICY_EVALUATING, TaskPhase.CHECKPOINT_READY,
]


def _task_min():
    return TaskRecord(
        task_id=TASK, cycle_id="cycle-1", supersedes_task_id=None,
        phase=TaskPhase.CREATED, version=0, baseline_commit=None,
        baseline_tree=None, proposal_sha256=None, authorization_id=None,
        worktree_path=None, worktree_branch=None, actual_tree=None,
        delta_sha256=None, terminal_reason=None, created_at=T0, updated_at=T0,
    )


def _db_task(tmp_path, name="t.db"):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    store.insert_cycle(conn, CycleRecord(
        cycle_id="cycle-1", project_id="project-1", status="ACTIVE",
        version=0, created_at=T0, updated_at=T0,
    ))
    store.insert_task(conn, _task_min())
    conn.commit()
    return conn


def _stream(conn):
    return ledger.event_stream(
        conn, aggregate_type=AggregateType.TASK, aggregate_id=TASK,
    )


def _insert_event(conn, event_id, version, event_type, body,
                  aggregate_type=AggregateType.TASK, aggregate_id=TASK):
    store.insert_event(conn, EventRecord(
        event_id=event_id, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
        aggregate_version=version, event_type=event_type, producer="test",
        payload=CanonicalPayload.from_value(body), created_at=T0,
    ))
    conn.commit()


def _advance(conn, to_phase, tag):
    if to_phase == TaskPhase.FAILED:
        _advance(conn, TaskPhase.POLICY_EVALUATING, tag)
        state.transition(conn, task_id=TASK, to_phase=TaskPhase.FAILED,
                         terminal_reason="blocked", event_id=f"{tag}-failed", at=T1)
        return
    cur = store.get_task(conn, TASK).phase
    idx = _MAIN.index(cur)
    while _MAIN[idx] != to_phase:
        idx += 1
        state.transition(conn, task_id=TASK, to_phase=_MAIN[idx],
                         terminal_reason=None, event_id=f"{tag}-{idx}", at=T1)


def _expect_integrity(fn):
    with pytest.raises(AotfError) as exc:
        fn()
    assert exc.value.code is ErrorCode.INTEGRITY_FAILURE


def test_event_stream_returns_events_in_version_order(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance(conn, TaskPhase.REVIEWING, "chain")
    stream = _stream(conn)
    assert len(stream) == 8
    assert [e.aggregate_version for e in stream] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [e.event_id for e in stream] == [f"chain-{i}" for i in range(1, 9)]
    conn.close()


def test_event_stream_empty_and_excludes_other_aggregate(tmp_path) -> None:
    conn = _db_task(tmp_path)
    assert _stream(conn) == []
    _insert_event(conn, "other-1", 1, "TASK_PHASE_CHANGED",
                  {"from_phase": "CREATED", "to_phase": "BASELINING",
                   "reason": None},
                  aggregate_id="task-other")
    _insert_event(conn, "cycle-1", 1, "CYCLE_OPENED", {"status": "ACTIVE"},
                  aggregate_type=AggregateType.CYCLE)
    assert _stream(conn) == []
    conn.close()


def test_fold_task_stream_empty_is_created_v0() -> None:
    assert ledger.fold_task_stream([]) == ledger.TaskLedgerState(
        TaskPhase.CREATED, 0, None,
    )


def test_fold_task_stream_rebuilds_mainline_state(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance(conn, TaskPhase.REVIEWING, "chain")
    assert ledger.fold_task_stream(_stream(conn)) == ledger.TaskLedgerState(
        TaskPhase.REVIEWING, 8, None,
    )
    conn.close()


def test_fold_task_stream_rebuilds_terminal_state(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance(conn, TaskPhase.FAILED, "chain")
    assert ledger.fold_task_stream(_stream(conn)) == ledger.TaskLedgerState(
        TaskPhase.FAILED, 10, "blocked",
    )
    conn.close()


@pytest.mark.parametrize("event_type", ["MANUAL", "TASK_CREATED"])
def test_fold_task_stream_rejects_unexpected_event_type(tmp_path, event_type) -> None:
    conn = _db_task(tmp_path)
    _insert_event(conn, "synthetic-1", 1, event_type, {"phase": "CREATED"})
    _expect_integrity(lambda: ledger.fold_task_stream(_stream(conn)))
    conn.close()


def test_fold_task_stream_rejects_version_gap(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance(conn, TaskPhase.AWAITING_PLAN_APPROVAL, "chain")
    conn.execute("DELETE FROM events WHERE event_id = ?", ("chain-2",))
    conn.commit()
    _expect_integrity(lambda: ledger.fold_task_stream(_stream(conn)))
    conn.close()


@pytest.mark.parametrize("body", [
    {"from_phase": "REVIEWING", "to_phase": "BASELINING", "reason": None},
    {"from_phase": "CREATED", "to_phase": "COMPLETED", "reason": "x"},
])
def test_fold_task_stream_rejects_broken_chain(tmp_path, body) -> None:
    conn = _db_task(tmp_path)
    _insert_event(conn, "synthetic-1", 1, "TASK_PHASE_CHANGED", body)
    _expect_integrity(lambda: ledger.fold_task_stream(_stream(conn)))
    conn.close()


def test_verify_task_matches_ledger(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance(conn, TaskPhase.POLICY_EVALUATING, "chain")
    projection = ledger.verify_task(conn, task_id=TASK)
    assert projection == ledger.TaskLedgerState(TaskPhase.POLICY_EVALUATING, 9, None)
    assert store.get_task(conn, TASK).updated_at == _stream(conn)[-1].created_at
    conn.close()


@pytest.mark.parametrize("column,value", [
    ("phase", "COMPLETED"),
    ("version", "11"),
    ("terminal_reason", "changed"),
    ("updated_at", "2026-09-01T00:00:00+00:00"),
])
def test_verify_task_detects_projection_drift(tmp_path, column, value) -> None:
    conn = _db_task(tmp_path)
    _advance(conn, TaskPhase.FAILED, "chain")
    conn.execute(f"UPDATE tasks SET {column} = ? WHERE task_id = ?", (value, TASK))
    conn.commit()
    _expect_integrity(lambda: ledger.verify_task(conn, task_id=TASK))
    conn.close()


def test_verify_task_missing_row_raises_integrity(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _expect_integrity(lambda: ledger.verify_task(conn, task_id="task-nope"))
    conn.close()


def test_replay_and_fold_are_idempotent_no_writes(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance(conn, TaskPhase.AWAITING_PLAN_APPROVAL, "idem")
    count_before = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    row_before = store.get_task(conn, TASK)
    last = _stream(conn)[-1]
    dup = EventRecord(
        event_id=last.event_id, aggregate_type=last.aggregate_type,
        aggregate_id=last.aggregate_id, aggregate_version=last.aggregate_version,
        event_type=last.event_type, producer=last.producer,
        payload=CanonicalPayload.from_value({
            "from_phase": "PLAN_READY", "to_phase": "AWAITING_PLAN_APPROVAL",
            "reason": None,
        }),
        created_at=last.created_at,
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_event(conn, dup)
    conn.rollback()
    events = _stream(conn)
    first = ledger.fold_task_stream(events)
    assert first == ledger.fold_task_stream(events)
    assert first == ledger.verify_task(conn, task_id=TASK)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == count_before
    row_after = store.get_task(conn, TASK)
    assert (row_after.phase, row_after.version, row_after.updated_at) == (
        row_before.phase, row_before.version, row_before.updated_at,
    )
    conn.close()


def test_projection_survives_row_deletion(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance(conn, TaskPhase.POLICY_EVALUATING, "chain")
    row = store.get_task(conn, TASK)
    conn.execute("DELETE FROM tasks WHERE task_id = ?", (TASK,))
    conn.commit()
    assert len(_stream(conn)) == 9
    assert ledger.fold_task_stream(_stream(conn)) == ledger.TaskLedgerState(
        row.phase, row.version, row.terminal_reason,
    )
    _expect_integrity(lambda: ledger.verify_task(conn, task_id=TASK))
    conn.close()
