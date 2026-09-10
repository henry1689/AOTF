"""M0-A3c recovery 一致性诊断 + 后继任务工厂测试。

DB 一律使用 pytest tmp_path；不访问网络/时钟/环境秘密。
diagnose 只读、propose_successor 纯工厂（零 DB 写）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aotf import db, recovery, state, store
from aotf.errors import AotfError, ErrorCode
from aotf.models import CycleRecord, TaskPhase, TaskRecord

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 1, 1, 0, 0, tzinfo=timezone.utc)
TASK = "task-min"
CYCLE = "cycle-1"

_MAIN = [
    TaskPhase.CREATED, TaskPhase.BASELINING, TaskPhase.PLAN_READY,
    TaskPhase.AWAITING_PLAN_APPROVAL, TaskPhase.EDIT_AUTHORIZED,
    TaskPhase.IMPLEMENTING, TaskPhase.DELTA_CAPTURED, TaskPhase.EVIDENCE_RUNNING,
    TaskPhase.REVIEWING, TaskPhase.POLICY_EVALUATING, TaskPhase.CHECKPOINT_READY,
]


def _task_min():
    return TaskRecord(
        task_id=TASK, cycle_id=CYCLE, supersedes_task_id=None,
        phase=TaskPhase.CREATED, version=0, baseline_commit=None,
        baseline_tree=None, proposal_sha256=None, authorization_id=None,
        worktree_path=None, worktree_branch=None, actual_tree=None,
        delta_sha256=None, terminal_reason=None, created_at=T0, updated_at=T0,
    )


def _db_task(tmp_path, name="t.db"):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    store.insert_cycle(conn, CycleRecord(
        cycle_id=CYCLE, project_id="project-1", status="ACTIVE",
        version=0, created_at=T0, updated_at=T0,
    ))
    store.insert_task(conn, _task_min())
    conn.commit()
    return conn


def _to(conn, to_phase, tag):
    cur = store.get_task(conn, TASK).phase
    idx = _MAIN.index(cur)
    while _MAIN[idx] != to_phase:
        idx += 1
        state.transition(conn, task_id=TASK, to_phase=_MAIN[idx],
                         terminal_reason=None, event_id=f"{tag}-{idx}", at=T1)


def _terminal(conn, phase):
    if phase == TaskPhase.FAILED:
        _to(conn, TaskPhase.POLICY_EVALUATING, "c")
        state.transition(conn, task_id=TASK, to_phase=TaskPhase.FAILED,
                         terminal_reason="blocked", event_id="c-f", at=T1)
    elif phase == TaskPhase.SAFE_HALT:
        _to(conn, TaskPhase.POLICY_EVALUATING, "c")
        state.transition(conn, task_id=TASK, to_phase=TaskPhase.SAFE_HALT,
                         terminal_reason="halted", event_id="c-h", at=T1)
    elif phase == TaskPhase.COMPLETED:
        _to(conn, TaskPhase.CHECKPOINT_READY, "c")
        state.transition(conn, task_id=TASK, to_phase=TaskPhase.COMPLETED,
                         terminal_reason="accepted", event_id="c-d", at=T1)
    elif phase == TaskPhase.CANCELLED:
        state.transition(conn, task_id=TASK, to_phase=TaskPhase.CANCELLED,
                         terminal_reason="cancelled by user", event_id="c-x", at=T1)
    conn.commit()
    return recovery.diagnose_task(conn, task_id=TASK)


def test_diagnose_consistent_nonterminal(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _to(conn, TaskPhase.REVIEWING, "c")
    conn.commit()
    d = recovery.diagnose_task(conn, task_id=TASK)
    assert d.verdict == "CONSISTENT"
    assert d.phase is TaskPhase.REVIEWING
    assert d.version == 8
    assert d.is_terminal is False
    assert d.recoverable_terminal is False
    conn.close()


def test_diagnose_terminal_failed_is_recoverable(tmp_path) -> None:
    conn = _db_task(tmp_path)
    d = _terminal(conn, TaskPhase.FAILED)
    assert d.verdict == "CONSISTENT"
    assert d.phase is TaskPhase.FAILED
    assert d.terminal_reason == "blocked"
    assert d.is_terminal is True
    assert d.recoverable_terminal is True
    conn.close()


def test_diagnose_terminal_completed_not_recoverable(tmp_path) -> None:
    conn = _db_task(tmp_path)
    d = _terminal(conn, TaskPhase.COMPLETED)
    assert d.verdict == "CONSISTENT"
    assert d.is_terminal is True
    assert d.recoverable_terminal is False
    conn.close()


def test_diagnose_drift_on_tampered_row(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _terminal(conn, TaskPhase.FAILED)
    conn.execute("UPDATE tasks SET phase = ? WHERE task_id = ?",
                 (TaskPhase.COMPLETED.value, TASK))
    conn.commit()
    d = recovery.diagnose_task(conn, task_id=TASK)
    assert d.verdict == "DRIFT"
    assert d.phase is None
    assert d.is_terminal is False
    conn.close()


def test_diagnose_missing_row(tmp_path) -> None:
    conn = _db_task(tmp_path)
    d = recovery.diagnose_task(conn, task_id="task-nope")
    assert d.verdict == "MISSING"
    assert d.phase is None
    conn.close()


@pytest.mark.parametrize("phase", [TaskPhase.FAILED, TaskPhase.SAFE_HALT])
def test_propose_successor_from_failed_or_safe_halt(tmp_path, phase) -> None:
    conn = _db_task(tmp_path)
    rec = _terminal(conn, phase)
    succ = recovery.propose_successor(
        rec, successor_task_id="task-2", cycle_id=CYCLE, created_at=T1,
    )
    assert succ.task_id == "task-2"
    assert succ.supersedes_task_id == TASK
    assert succ.phase is TaskPhase.CREATED
    assert succ.version == 0
    assert succ.terminal_reason is None
    store.insert_task(conn, succ)
    conn.commit()
    assert store.get_task(conn, "task-2").supersedes_task_id == TASK
    conn.close()


@pytest.mark.parametrize("phase", [TaskPhase.COMPLETED, TaskPhase.CANCELLED])
def test_propose_successor_rejects_other_terminal(tmp_path, phase) -> None:
    conn = _db_task(tmp_path)
    rec = _terminal(conn, phase)
    with pytest.raises(AotfError) as exc:
        recovery.propose_successor(
            rec, successor_task_id="task-2", cycle_id=CYCLE, created_at=T1,
        )
    assert exc.value.code is ErrorCode.INVALID_INPUT
    conn.close()


def test_propose_successor_rejects_nonterminal(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _to(conn, TaskPhase.REVIEWING, "c")
    conn.commit()
    rec = recovery.diagnose_task(conn, task_id=TASK)
    assert rec.verdict == "CONSISTENT"
    with pytest.raises(AotfError) as exc:
        recovery.propose_successor(
            rec, successor_task_id="task-2", cycle_id=CYCLE, created_at=T1,
        )
    assert exc.value.code is ErrorCode.INVALID_INPUT
    conn.close()


def test_propose_successor_rejects_drift(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _terminal(conn, TaskPhase.FAILED)
    conn.execute("UPDATE tasks SET phase = ? WHERE task_id = ?",
                 (TaskPhase.COMPLETED.value, TASK))
    conn.commit()
    rec = recovery.diagnose_task(conn, task_id=TASK)
    assert rec.verdict == "DRIFT"
    with pytest.raises(AotfError) as exc:
        recovery.propose_successor(
            rec, successor_task_id="task-2", cycle_id=CYCLE, created_at=T1,
        )
    assert exc.value.code is ErrorCode.INVALID_INPUT
    conn.close()
