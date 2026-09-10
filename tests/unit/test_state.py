"""M0-A2a StateService 合法转换与事务化 transition 测试。

DB 一律使用 pytest tmp_path；不访问网络/时钟/环境秘密。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aotf import db, state, store
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

_MAIN = [
    TaskPhase.CREATED, TaskPhase.BASELINING, TaskPhase.PLAN_READY,
    TaskPhase.AWAITING_PLAN_APPROVAL, TaskPhase.EDIT_AUTHORIZED,
    TaskPhase.IMPLEMENTING, TaskPhase.DELTA_CAPTURED, TaskPhase.EVIDENCE_RUNNING,
    TaskPhase.REVIEWING, TaskPhase.POLICY_EVALUATING, TaskPhase.CHECKPOINT_READY,
]


def _task_min():
    return TaskRecord(
        task_id="task-min", cycle_id="cycle-1", supersedes_task_id=None,
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


def _advance_to(conn, target, tag):
    if target == TaskPhase.RELEASE_CANDIDATE:
        _advance_to(conn, TaskPhase.POLICY_EVALUATING, tag)
        state.transition(conn, task_id="task-min", to_phase=TaskPhase.RELEASE_CANDIDATE,
                         terminal_reason=None, event_id=f"{tag}-release", at=T1)
        return
    cur = store.get_task(conn, "task-min").phase
    idx = _MAIN.index(cur)
    while _MAIN[idx] != target:
        idx += 1
        state.transition(conn, task_id="task-min", to_phase=_MAIN[idx],
                         terminal_reason=None, event_id=f"{tag}-{idx}", at=T1)


@pytest.mark.parametrize("frm,to", [
    (TaskPhase.CREATED, TaskPhase.BASELINING),
    (TaskPhase.BASELINING, TaskPhase.PLAN_READY),
    (TaskPhase.PLAN_READY, TaskPhase.AWAITING_PLAN_APPROVAL),
    (TaskPhase.AWAITING_PLAN_APPROVAL, TaskPhase.EDIT_AUTHORIZED),
    (TaskPhase.EDIT_AUTHORIZED, TaskPhase.IMPLEMENTING),
    (TaskPhase.IMPLEMENTING, TaskPhase.DELTA_CAPTURED),
    (TaskPhase.DELTA_CAPTURED, TaskPhase.EVIDENCE_RUNNING),
    (TaskPhase.EVIDENCE_RUNNING, TaskPhase.REVIEWING),
    (TaskPhase.REVIEWING, TaskPhase.POLICY_EVALUATING),
    (TaskPhase.POLICY_EVALUATING, TaskPhase.CHECKPOINT_READY),
])
def test_can_transition_allows_mainline_forward(frm, to) -> None:
    assert state.can_transition(frm, to) is True


@pytest.mark.parametrize("frm,to", [
    (TaskPhase.POLICY_EVALUATING, TaskPhase.RELEASE_CANDIDATE),
    (TaskPhase.POLICY_EVALUATING, TaskPhase.FAILED),
    (TaskPhase.CHECKPOINT_READY, TaskPhase.COMPLETED),
    (TaskPhase.RELEASE_CANDIDATE, TaskPhase.COMPLETED),
])
def test_can_transition_allows_terminal_and_release_paths(frm, to) -> None:
    assert state.can_transition(frm, to) is True


@pytest.mark.parametrize("frm,to", [
    (TaskPhase.PLAN_READY, TaskPhase.CANCELLED),
    (TaskPhase.IMPLEMENTING, TaskPhase.SAFE_HALT),
])
def test_can_transition_allows_cancel_and_halt_from_nonterminal(frm, to) -> None:
    assert state.can_transition(frm, to) is True


@pytest.mark.parametrize("frm,to", [
    (TaskPhase.REVIEWING, TaskPhase.BASELINING),
    (TaskPhase.PLAN_READY, TaskPhase.COMPLETED),
    (TaskPhase.CREATED, TaskPhase.CREATED),
    (TaskPhase.COMPLETED, TaskPhase.CREATED),
    (TaskPhase.POLICY_EVALUATING, TaskPhase.PLAN_READY),
])
def test_can_transition_rejects_illegal(frm, to) -> None:
    assert state.can_transition(frm, to) is False


def test_transition_advances_phase_and_appends_event(tmp_path) -> None:
    conn = _db_task(tmp_path)
    t = state.transition(conn, task_id="task-min", to_phase=TaskPhase.BASELINING,
                         terminal_reason=None, event_id="evt-t1", at=T1)
    assert t.phase is TaskPhase.BASELINING
    assert t.version == 1
    assert t.updated_at == T1
    assert t.terminal_reason is None
    e = store.get_event(conn, "evt-t1")
    assert e.event_type == "TASK_PHASE_CHANGED"
    assert e.aggregate_version == 1
    assert e.payload_json == '{"from_phase":"CREATED","reason":null,"to_phase":"BASELINING"}'
    conn.close()


@pytest.mark.parametrize("frm,to,reason,version", [
    (TaskPhase.POLICY_EVALUATING, TaskPhase.FAILED, "blocked", 10),
    (TaskPhase.CHECKPOINT_READY, TaskPhase.COMPLETED, "done", 11),
])
def test_transition_to_terminal_sets_reason_and_event(
    tmp_path, frm, to, reason, version,
) -> None:
    conn = _db_task(tmp_path)
    _advance_to(conn, frm, "chain")
    t = state.transition(conn, task_id="task-min", to_phase=to,
                         terminal_reason=reason, event_id="evt-term", at=T1)
    assert t.phase is to
    assert t.version == version
    assert t.terminal_reason == reason
    e = store.get_event(conn, "evt-term")
    assert e.payload_json == '{"from_phase":"' + frm.value + '","reason":"' + reason + '","to_phase":"' + to.value + '"}'
    conn.close()


def test_transition_cancel_from_nonterminal(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance_to(conn, TaskPhase.AWAITING_PLAN_APPROVAL, "chain")
    t = state.transition(conn, task_id="task-min", to_phase=TaskPhase.CANCELLED,
                         terminal_reason="cancelled by user", event_id="evt-cancel", at=T1)
    assert t.phase is TaskPhase.CANCELLED
    assert t.terminal_reason == "cancelled by user"
    conn.close()


def test_transition_rejects_illegal_with_no_side_effect(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance_to(conn, TaskPhase.REVIEWING, "chain")
    before = store.get_task(conn, "task-min")
    with pytest.raises(AotfError) as exc:
        state.transition(conn, task_id="task-min", to_phase=TaskPhase.BASELINING,
                         terminal_reason=None, event_id="evt-bad", at=T1)
    assert exc.value.code is ErrorCode.INVALID_STATE_TRANSITION
    after = store.get_task(conn, "task-min")
    assert after.phase is before.phase
    assert after.version == before.version
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 8
    conn.close()
    # 独立场景：手工预插同 event_id 事件后 transition 应回滚且抛 IDEMPOTENCY_CONFLICT
    conn2 = _db_task(tmp_path, "t2.db")
    payload = CanonicalPayload.from_value({"dup": True})
    store.insert_event(conn2, EventRecord(
        event_id="evt-dup", aggregate_type=AggregateType.TASK, aggregate_id="task-min",
        aggregate_version=1, event_type="MANUAL", producer="test",
        payload=payload, created_at=T0,
    ))
    conn2.commit()
    with pytest.raises(AotfError) as exc2:
        state.transition(conn2, task_id="task-min", to_phase=TaskPhase.BASELINING,
                         terminal_reason=None, event_id="evt-dup", at=T1)
    assert exc2.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    t2 = store.get_task(conn2, "task-min")
    assert t2.phase is TaskPhase.CREATED
    assert t2.version == 0
    conn2.close()


def test_transition_rejects_when_task_missing(tmp_path) -> None:
    conn = _db_task(tmp_path)
    with pytest.raises(AotfError) as exc:
        state.transition(conn, task_id="task-nope", to_phase=TaskPhase.BASELINING,
                         terminal_reason=None, event_id="evt-x", at=T1)
    assert exc.value.code is ErrorCode.VERSION_CONFLICT
    conn.close()


@pytest.mark.parametrize("wait", [
    TaskPhase.CHECKPOINT_READY,
    TaskPhase.RELEASE_CANDIDATE,
])
def test_transition_terminal_blocks_further(wait, tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance_to(conn, wait, "chain")
    state.transition(conn, task_id="task-min", to_phase=TaskPhase.COMPLETED,
                     terminal_reason="accepted", event_id="evt-done", at=T1)
    with pytest.raises(AotfError) as exc:
        state.transition(conn, task_id="task-min", to_phase=TaskPhase.CANCELLED,
                         terminal_reason="late", event_id="evt-late", at=T1)
    assert exc.value.code is ErrorCode.TERMINAL_STATE
    conn.close()
