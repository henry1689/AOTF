"""M0-A1c2a-1 cycles/tasks typed store 测试。

DB 一律使用 pytest tmp_path；不访问网络/时钟/环境秘密。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from aotf import db, store
from aotf.models import (
    AggregateType,
    AgentRunRecord,
    ApprovalRecord,
    ArtifactRecord,
    CanonicalPayload,
    CycleRecord,
    EventRecord,
    LeaseRecord,
    OutboxRecord,
    TaskPhase,
    TaskRecord,
)

T0 = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 1, 1, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    store.insert_cycle(conn, CycleRecord(
        cycle_id="cycle-1", project_id="project-1", status="ACTIVE",
        version=0, created_at=T0, updated_at=T0,
    ))
    conn.commit()
    return conn


def _task_full():
    return TaskRecord(
        task_id="task-1", cycle_id="cycle-1", supersedes_task_id="task-0",
        phase=TaskPhase.COMPLETED, version=3, baseline_commit="a" * 40,
        baseline_tree="a" * 40, proposal_sha256="c" * 64,
        authorization_id="auth-1", worktree_path="D:/tmp/wt-task-1",
        worktree_branch="feature/task-1", actual_tree="b" * 64,
        delta_sha256="d" * 64, terminal_reason="all checks passed",
        created_at=T0, updated_at=T0,
    )


def _task_min(task_id="task-min"):
    return TaskRecord(
        task_id=task_id, cycle_id="cycle-1", supersedes_task_id=None,
        phase=TaskPhase.CREATED, version=0, baseline_commit=None,
        baseline_tree=None, proposal_sha256=None, authorization_id=None,
        worktree_path=None, worktree_branch=None, actual_tree=None,
        delta_sha256=None, terminal_reason=None, created_at=T0, updated_at=T0,
    )


def _event():
    payload = CanonicalPayload.from_value({"phase": "CREATED"})
    return EventRecord(
        event_id="evt-1", aggregate_type=AggregateType.TASK, aggregate_id="task-1",
        aggregate_version=1, event_type="TASK_CREATED", producer="controller",
        payload=payload, created_at=T0,
    )


def _db_task(tmp_path):
    conn = _db(tmp_path)
    store.insert_task(conn, _task_min())
    conn.commit()
    return conn


def _approval():
    return ApprovalRecord(
        approval_id="approval-1", task_id="task-min", decision="APPROVED",
        scope_sha256="a" * 64, proposal_sha256="b" * 64, baseline_tree="c" * 40,
        source="human", expires_at=None, created_at=T0,
    )


def _artifact():
    return ArtifactRecord(
        artifact_id="artifact-1", task_id="task-min", kind="test-evidence",
        relative_path="evidence/task-min/unit.json", producer="controller",
        sha256="e" * 64, size_bytes=1234, created_at=T0,
    )


def _agent_run():
    return AgentRunRecord(
        run_id="run-1", task_id="task-min", role="planner", session_id="sess-1",
        requested_model="opus", resolved_model="claude-opus-5", status="completed",
        input_digest="a" * 64, output_artifact_id="artifact-1", input_tokens=1250,
        output_tokens=80, estimated_cost_usd=Decimal("0.012345"),
        started_at=T0, ended_at=T1,
    )


def _outbox():
    return OutboxRecord(
        message_id="msg-1", topic="task.completed",
        payload_json='{"task_id":"task-min","phase":"COMPLETED"}',
        status="pending", attempts=0, next_attempt_at=T1, created_at=T0,
    )


def _lease(**kw):
    base = dict(resource_id="res-task-1", controller_instance_id="controller-a",
                fencing_token=7, acquired_at=T0, heartbeat_at=T1,
                expires_at=datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc),
                version=3)
    base.update(kw)
    return LeaseRecord(**base)


def test_cycle_roundtrip_preserves_all_fields(tmp_path) -> None:
    conn = _db(tmp_path)
    c = store.get_cycle(conn, "cycle-1")
    assert c.cycle_id == "cycle-1"
    assert c.project_id == "project-1"
    assert c.status == "ACTIVE"
    assert c.version == 0
    assert c.created_at == T0
    assert c.updated_at == T0
    conn.close()


def test_task_roundtrip_preserves_all_fields(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_task(conn, _task_min(task_id="task-0"))
    store.insert_task(conn, _task_full())
    conn.commit()
    t = store.get_task(conn, "task-1")
    assert t.task_id == "task-1"
    assert t.cycle_id == "cycle-1"
    assert t.supersedes_task_id == "task-0"
    assert t.phase is TaskPhase.COMPLETED
    assert t.version == 3
    assert t.baseline_commit == "a" * 40
    assert t.baseline_tree == "a" * 40
    assert t.proposal_sha256 == "c" * 64
    assert t.authorization_id == "auth-1"
    assert t.worktree_path == "D:/tmp/wt-task-1"
    assert t.worktree_branch == "feature/task-1"
    assert t.actual_tree == "b" * 64
    assert t.delta_sha256 == "d" * 64
    assert t.terminal_reason == "all checks passed"
    assert t.created_at == T0
    assert t.updated_at == T0
    conn.close()


def test_task_minimal_roundtrip_with_nulls(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_task(conn, _task_min())
    conn.commit()
    t = store.get_task(conn, "task-min")
    assert t.supersedes_task_id is None
    assert t.baseline_commit is None
    assert t.baseline_tree is None
    assert t.proposal_sha256 is None
    assert t.authorization_id is None
    assert t.worktree_path is None
    assert t.worktree_branch is None
    assert t.actual_tree is None
    assert t.delta_sha256 is None
    assert t.terminal_reason is None
    conn.close()


def test_get_cycle_returns_none_for_missing(tmp_path) -> None:
    conn = _db(tmp_path)
    assert store.get_cycle(conn, "cycle-nope") is None
    conn.close()


def test_get_task_returns_none_for_missing(tmp_path) -> None:
    conn = _db(tmp_path)
    assert store.get_task(conn, "task-nope") is None
    conn.close()


def test_insert_task_missing_cycle_raises(tmp_path) -> None:
    conn = _db(tmp_path)
    bad = TaskRecord(
        task_id="task-x", cycle_id="cycle-missing", supersedes_task_id=None,
        phase=TaskPhase.CREATED, version=0, baseline_commit=None,
        baseline_tree=None, proposal_sha256=None, authorization_id=None,
        worktree_path=None, worktree_branch=None, actual_tree=None,
        delta_sha256=None, terminal_reason=None, created_at=T0, updated_at=T0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_task(conn, bad)
    conn.close()


def test_event_roundtrip_preserves_payload_and_fields(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_event(conn, _event())
    conn.commit()
    e = store.get_event(conn, "evt-1")
    assert e.event_id == "evt-1"
    assert e.aggregate_type is AggregateType.TASK
    assert e.aggregate_id == "task-1"
    assert e.aggregate_version == 1
    assert e.event_type == "TASK_CREATED"
    assert e.producer == "controller"
    assert e.payload.json_text == '{"phase":"CREATED"}'
    assert e.payload.sha256 == _event().payload.sha256
    assert e.created_at == T0
    conn.close()


def test_get_event_returns_none_for_missing(tmp_path) -> None:
    conn = _db(tmp_path)
    assert store.get_event(conn, "evt-nope") is None
    conn.close()


def test_duplicate_event_aggregate_version_raises(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_event(conn, _event())
    conn.commit()
    dup = EventRecord(
        event_id="evt-2", aggregate_type=AggregateType.TASK, aggregate_id="task-1",
        aggregate_version=1, event_type="TASK_CREATED", producer="controller",
        payload=_event().payload, created_at=T0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_event(conn, dup)
    conn.close()


def test_cas_task_phase_success_bumps_version(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_task(conn, _task_min())
    conn.commit()
    ok = store.cas_task_phase(
        conn, task_id="task-min", expected_phase=TaskPhase.CREATED,
        expected_version=0, new_phase=TaskPhase.BASELINING,
        terminal_reason=None, updated_at=T1,
    )
    conn.commit()
    assert ok is True
    t = store.get_task(conn, "task-min")
    assert t.phase is TaskPhase.BASELINING
    assert t.version == 1
    assert t.updated_at == T1
    assert t.terminal_reason is None
    conn.close()


@pytest.mark.parametrize("expected_phase,expected_version", [
    (TaskPhase.CREATED, 1),
    (TaskPhase.BASELINING, 0),
])
def test_cas_task_phase_false_when_expected_mismatch(
    tmp_path, expected_phase, expected_version,
) -> None:
    conn = _db(tmp_path)
    store.insert_task(conn, _task_min())
    conn.commit()
    ok = store.cas_task_phase(
        conn, task_id="task-min", expected_phase=expected_phase,
        expected_version=expected_version, new_phase=TaskPhase.BASELINING,
        terminal_reason=None, updated_at=T1,
    )
    conn.commit()
    assert ok is False
    t = store.get_task(conn, "task-min")
    assert t.phase is TaskPhase.CREATED
    assert t.version == 0
    assert t.updated_at == T0
    conn.close()


@pytest.mark.parametrize("new_phase,reason", [
    (TaskPhase.COMPLETED, "done"),
    (TaskPhase.FAILED, "blocked"),
])
def test_cas_task_phase_terminal_sets_reason(
    tmp_path, new_phase, reason,
) -> None:
    conn = _db(tmp_path)
    store.insert_task(conn, _task_min())
    conn.commit()
    ok = store.cas_task_phase(
        conn, task_id="task-min", expected_phase=TaskPhase.CREATED,
        expected_version=0, new_phase=new_phase,
        terminal_reason=reason, updated_at=T1,
    )
    conn.commit()
    assert ok is True
    t = store.get_task(conn, "task-min")
    assert t.phase is new_phase
    assert t.version == 1
    assert t.terminal_reason == reason
    conn.close()


def test_approval_roundtrip_preserves_all_fields(tmp_path) -> None:
    conn = _db_task(tmp_path)
    store.insert_approval(conn, _approval())
    conn.commit()
    a = store.get_approval(conn, "approval-1")
    assert a.approval_id == "approval-1"
    assert a.task_id == "task-min"
    assert a.decision == "APPROVED"
    assert a.scope_sha256 == "a" * 64
    assert a.proposal_sha256 == "b" * 64
    assert a.baseline_tree == "c" * 40
    assert a.source == "human"
    assert a.expires_at is None
    assert a.created_at == T0
    conn.close()


def test_artifact_roundtrip_preserves_all_fields(tmp_path) -> None:
    conn = _db_task(tmp_path)
    store.insert_artifact(conn, _artifact())
    conn.commit()
    a = store.get_artifact(conn, "artifact-1")
    assert a.artifact_id == "artifact-1"
    assert a.task_id == "task-min"
    assert a.kind == "test-evidence"
    assert a.relative_path == "evidence/task-min/unit.json"
    assert a.producer == "controller"
    assert a.sha256 == "e" * 64
    assert a.size_bytes == 1234
    assert a.created_at == T0
    conn.close()


def test_duplicate_artifact_unique_raises(tmp_path) -> None:
    conn = _db_task(tmp_path)
    store.insert_artifact(conn, _artifact())
    conn.commit()
    dup = ArtifactRecord(
        artifact_id="artifact-2", task_id="task-min", kind="test-evidence",
        relative_path="evidence/task-min/unit.json", producer="controller",
        sha256="e" * 64, size_bytes=1234, created_at=T0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_artifact(conn, dup)
    conn.close()


def test_agent_run_roundtrip_preserves_all_fields(tmp_path) -> None:
    conn = _db_task(tmp_path)
    store.insert_agent_run(conn, _agent_run())
    conn.commit()
    r = store.get_agent_run(conn, "run-1")
    assert r.run_id == "run-1"
    assert r.task_id == "task-min"
    assert r.role == "planner"
    assert r.session_id == "sess-1"
    assert r.requested_model == "opus"
    assert r.resolved_model == "claude-opus-5"
    assert r.status == "completed"
    assert r.input_digest == "a" * 64
    assert r.output_artifact_id == "artifact-1"
    assert r.input_tokens == 1250
    assert r.output_tokens == 80
    assert r.estimated_cost_usd == Decimal("0.012345")
    assert r.started_at == T0
    assert r.ended_at == T1
    conn.close()


def test_outbox_roundtrip_preserves_all_fields(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_outbox(conn, _outbox())
    conn.commit()
    m = store.get_outbox(conn, "msg-1")
    assert m.message_id == "msg-1"
    assert m.topic == "task.completed"
    assert m.payload_json == '{"task_id":"task-min","phase":"COMPLETED"}'
    assert m.status == "pending"
    assert m.attempts == 0
    assert m.next_attempt_at == T1
    assert m.created_at == T0
    conn.close()


def test_get_approval_returns_none_for_missing(tmp_path) -> None:
    conn = _db(tmp_path)
    assert store.get_approval(conn, "approval-nope") is None
    conn.close()


def test_get_artifact_returns_none_for_missing(tmp_path) -> None:
    conn = _db(tmp_path)
    assert store.get_artifact(conn, "artifact-nope") is None
    conn.close()


def test_get_agent_run_returns_none_for_missing(tmp_path) -> None:
    conn = _db(tmp_path)
    assert store.get_agent_run(conn, "run-nope") is None
    conn.close()


def test_get_outbox_returns_none_for_missing(tmp_path) -> None:
    conn = _db(tmp_path)
    assert store.get_outbox(conn, "msg-nope") is None
    conn.close()


def test_lease_roundtrip_preserves_all_fields(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_lease(conn, _lease())
    conn.commit()
    l = store.get_lease(conn, "res-task-1")
    assert l.resource_id == "res-task-1"
    assert l.controller_instance_id == "controller-a"
    assert l.fencing_token == 7
    assert l.acquired_at == T0
    assert l.heartbeat_at == T1
    assert l.expires_at == datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc)
    assert l.version == 3
    conn.close()


def test_get_lease_returns_none_for_missing(tmp_path) -> None:
    conn = _db(tmp_path)
    assert store.get_lease(conn, "res-nope") is None
    conn.close()


def test_insert_lease_duplicate_resource_raises(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_lease(conn, _lease())
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_lease(conn, _lease(controller_instance_id="controller-b"))
    conn.close()


def test_lease_row_requires_no_parent_cycle(tmp_path) -> None:
    conn = db.connect(tmp_path / "noparent.db")
    db.init_db(conn)
    store.insert_lease(conn, _lease())
    conn.commit()
    assert store.get_lease(conn, "res-task-1").controller_instance_id == "controller-a"
    conn.close()


def test_cas_lease_heartbeat_success_bumps_version(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_lease(conn, _lease())
    conn.commit()
    ok = store.cas_lease_heartbeat(
        conn, resource_id="res-task-1", controller_instance_id="controller-a",
        fencing_token=7, heartbeat_at=T1,
        expires_at=datetime(2026, 9, 1, 3, 0, 0, tzinfo=timezone.utc),
    )
    conn.commit()
    assert ok is True
    l = store.get_lease(conn, "res-task-1")
    assert l.fencing_token == 7
    assert l.heartbeat_at == T1
    assert l.expires_at == datetime(2026, 9, 1, 3, 0, 0, tzinfo=timezone.utc)
    assert l.version == 4
    conn.close()


def test_cas_lease_heartbeat_false_when_expired(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_lease(conn, _lease(heartbeat_at=T0, expires_at=T0))
    conn.commit()
    ok = store.cas_lease_heartbeat(
        conn, resource_id="res-task-1", controller_instance_id="controller-a",
        fencing_token=7, heartbeat_at=T1,
        expires_at=datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc),
    )
    conn.commit()
    assert ok is False
    assert store.get_lease(conn, "res-task-1").version == 3
    conn.close()


def test_cas_lease_heartbeat_false_when_token_mismatch(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_lease(conn, _lease())
    conn.commit()
    ok = store.cas_lease_heartbeat(
        conn, resource_id="res-task-1", controller_instance_id="controller-a",
        fencing_token=999, heartbeat_at=T1,
        expires_at=datetime(2026, 9, 1, 3, 0, 0, tzinfo=timezone.utc),
    )
    conn.commit()
    assert ok is False
    assert store.get_lease(conn, "res-task-1").version == 3
    conn.close()


def test_cas_lease_takeover_success_bumps_token_and_version(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_lease(conn, _lease(heartbeat_at=T0, expires_at=T0))
    conn.commit()
    ok = store.cas_lease_takeover(
        conn, resource_id="res-task-1", controller_instance_id="controller-b",
        next_fencing_token=8, acquired_at=T1, heartbeat_at=T1,
        expires_at=datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc),
        expected_version=3,
    )
    conn.commit()
    assert ok is True
    l = store.get_lease(conn, "res-task-1")
    assert l.controller_instance_id == "controller-b"
    assert l.fencing_token == 8
    assert l.acquired_at == T1
    assert l.heartbeat_at == T1
    assert l.expires_at == datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc)
    assert l.version == 4
    conn.close()


def test_cas_lease_takeover_false_when_active(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_lease(conn, _lease())
    conn.commit()
    ok = store.cas_lease_takeover(
        conn, resource_id="res-task-1", controller_instance_id="controller-b",
        next_fencing_token=8, acquired_at=T1, heartbeat_at=T1,
        expires_at=datetime(2026, 9, 1, 3, 0, 0, tzinfo=timezone.utc),
        expected_version=3,
    )
    conn.commit()
    assert ok is False
    assert store.get_lease(conn, "res-task-1").controller_instance_id == "controller-a"
    conn.close()


def test_cas_lease_takeover_false_when_version_mismatch(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_lease(conn, _lease(heartbeat_at=T0, expires_at=T0))
    conn.commit()
    ok = store.cas_lease_takeover(
        conn, resource_id="res-task-1", controller_instance_id="controller-b",
        next_fencing_token=8, acquired_at=T1, heartbeat_at=T1,
        expires_at=datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc),
        expected_version=999,
    )
    conn.commit()
    assert ok is False
    assert store.get_lease(conn, "res-task-1").fencing_token == 7
    conn.close()


def test_delete_lease_matches_holder_and_token(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_lease(conn, _lease())
    conn.commit()
    assert store.delete_lease(
        conn, resource_id="res-task-1", controller_instance_id="controller-a",
        fencing_token=999,
    ) is False
    assert store.delete_lease(
        conn, resource_id="res-task-1", controller_instance_id="controller-b",
        fencing_token=7,
    ) is False
    assert store.get_lease(conn, "res-task-1") is not None
    assert store.delete_lease(
        conn, resource_id="res-task-1", controller_instance_id="controller-a",
        fencing_token=7,
    ) is True
    conn.commit()
    assert store.get_lease(conn, "res-task-1") is None
    assert store.delete_lease(
        conn, resource_id="res-task-1", controller_instance_id="controller-a",
        fencing_token=7,
    ) is False
    conn.close()


def test_mark_outbox_delivered_success_and_terminal(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_outbox(conn, _outbox())
    conn.commit()
    assert store.mark_outbox_delivered(conn, message_id="msg-1", attempts=2) is True
    conn.commit()
    m = store.get_outbox(conn, "msg-1")
    assert m.status == "delivered"
    assert m.attempts == 2
    assert m.next_attempt_at is None
    assert store.mark_outbox_delivered(conn, message_id="msg-1", attempts=3) is False
    assert store.mark_outbox_failed(conn, message_id="msg-1", attempts=3) is False
    assert store.get_outbox(conn, "msg-1").status == "delivered"
    conn.close()


def test_mark_outbox_retry_schedules_next_attempt(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_outbox(conn, _outbox())
    conn.commit()
    nxt = datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc)
    assert store.mark_outbox_retry(
        conn, message_id="msg-1", attempts=1, next_attempt_at=nxt,
    ) is True
    conn.commit()
    m = store.get_outbox(conn, "msg-1")
    assert m.status == "pending"
    assert m.attempts == 1
    assert m.next_attempt_at == nxt
    conn.close()


def test_mark_outbox_failed_sets_status(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_outbox(conn, _outbox())
    conn.commit()
    assert store.mark_outbox_failed(conn, message_id="msg-1", attempts=3) is True
    conn.commit()
    m = store.get_outbox(conn, "msg-1")
    assert m.status == "failed"
    assert m.attempts == 3
    assert m.next_attempt_at is None
    conn.close()


def test_mark_outbox_guard_rejects_nonpending(tmp_path) -> None:
    conn = _db(tmp_path)
    store.insert_outbox(conn, _outbox())
    conn.commit()
    store.mark_outbox_delivered(conn, message_id="msg-1", attempts=1)
    conn.commit()
    assert store.mark_outbox_retry(
        conn, message_id="msg-1", attempts=2,
        next_attempt_at=datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc),
    ) is False
    assert store.mark_outbox_failed(conn, message_id="msg-1", attempts=2) is False
    assert store.get_outbox(conn, "msg-1").status == "delivered"
    conn.close()


def test_mark_outbox_guard_rejects_missing(tmp_path) -> None:
    conn = _db(tmp_path)
    assert store.mark_outbox_delivered(conn, message_id="msg-nope", attempts=1) is False
    assert store.mark_outbox_retry(
        conn, message_id="msg-nope", attempts=1,
        next_attempt_at=datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc),
    ) is False
    assert store.mark_outbox_failed(conn, message_id="msg-nope", attempts=1) is False
    conn.close()
