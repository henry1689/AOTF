"""M0-A4c MockAgentRunner 驱动 fault-injection drill。

组合既有服务（state/ledger/lease/outbox/recovery/runner/orchestrator）为
场景模拟：成功/失败/重复/并发/崩溃（§19 M0-A、§20.1 #1/#2/#5）。
无真实 Agent/网络/时钟；DB 一律 pytest tmp_path。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aotf import db, lease, ledger, outbox, orchestrator, recovery, state, store
from aotf.errors import AotfError, ErrorCode
from aotf.models import (
    AggregateType,
    CanonicalPayload,
    CycleRecord,
    EventRecord,
    OutboxRecord,
    TaskPhase,
    TaskRecord,
)
from aotf.runner import AgentRunRequest, AgentRunResult, MockAgentRunner

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 1, 1, 0, 0, tzinfo=timezone.utc)
TASK = "task-min"
CYCLE = "cycle-1"
DIGEST = "a" * 64

MAIN = [
    TaskPhase.CREATED, TaskPhase.BASELINING, TaskPhase.PLAN_READY,
    TaskPhase.AWAITING_PLAN_APPROVAL, TaskPhase.EDIT_AUTHORIZED,
    TaskPhase.IMPLEMENTING, TaskPhase.DELTA_CAPTURED, TaskPhase.EVIDENCE_RUNNING,
    TaskPhase.REVIEWING, TaskPhase.POLICY_EVALUATING, TaskPhase.CHECKPOINT_READY,
]


class _OkSender:
    def __init__(self):
        self.sent = []

    def send(self, message_id, topic, payload):
        self.sent.append(message_id)


class _DedupReceiver:
    def __init__(self):
        self.applied = []

    def apply(self, message_id):
        if message_id not in self.applied:
            self.applied.append(message_id)


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


def _advance(conn, to_phase, tag):
    cur = store.get_task(conn, TASK).phase
    idx = MAIN.index(cur)
    while MAIN[idx] != to_phase:
        idx += 1
        state.transition(conn, task_id=TASK, to_phase=MAIN[idx],
                         terminal_reason=None, event_id=f"{tag}-{idx}", at=T1)


def _req(run_id, role):
    return AgentRunRequest(
        run_id=run_id, task_id=TASK, role=role, requested_model="opus",
        input_digest=DIGEST, max_turns=20, max_budget_usd=Decimal("20.00"),
        deadline_at=T0 + timedelta(hours=2),
    )


def _run_mock(role, run_id, status="completed"):
    req = _req(run_id, role)
    mock = MockAgentRunner(
        lambda r: AgentRunResult(
            run_id=r.run_id, status=status, resolved_model="claude-opus-5",
            input_tokens=100, output_tokens=50,
            estimated_cost_usd=Decimal("0.001000"),
            output_artifact_id=None, error_code=None,
        ),
    )
    return req, asyncio.run(mock.run(req))


def _record_run(conn, run_id, role, status="completed"):
    req, res = _run_mock(role, run_id, status)
    orchestrator.record_run(conn, request=req, result=res,
                            started_at=T0, ended_at=T0 + timedelta(minutes=1))
    conn.commit()


def test_drill_success_flow_to_checkpoint(tmp_path) -> None:
    conn = _db_task(tmp_path)
    lease.acquire(conn, resource_id=TASK, holder="controller-a",
                  lease_seconds=300, at=T0)
    conn.commit()
    targets = [
        ("planner", TaskPhase.PLAN_READY),
        ("implementer", TaskPhase.DELTA_CAPTURED),
        ("reviewer", TaskPhase.POLICY_EVALUATING),
    ]
    for i, (role, target) in enumerate(targets):
        _record_run(conn, f"run-{i}", role)
        _advance(conn, target, f"t{i}")
        conn.commit()
    _advance(conn, TaskPhase.CHECKPOINT_READY, "tail")
    conn.commit()
    store.insert_outbox(conn, OutboxRecord(
        message_id="msg-1", topic="task.completed",
        payload_json='{"task_id":"task-min"}', status="pending",
        attempts=0, next_attempt_at=None, created_at=T1,
    ))
    conn.commit()
    sender = _OkSender()
    report = outbox.process_due(conn, sender=sender, at=T1,
                                max_attempts=3, base_delay_seconds=60)
    conn.commit()
    row = store.get_task(conn, TASK)
    assert row.phase is TaskPhase.CHECKPOINT_READY
    assert report.delivered == 1
    assert store.get_outbox(conn, "msg-1").status == "delivered"
    assert conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 3
    assert ledger.verify_task(conn, task_id=TASK).version == row.version
    d = recovery.diagnose_task(conn, task_id=TASK)
    assert (d.verdict, d.phase) == ("CONSISTENT", TaskPhase.CHECKPOINT_READY)
    conn.close()


def test_drill_failure_leads_successor(tmp_path) -> None:
    conn = _db_task(tmp_path)
    _advance(conn, TaskPhase.POLICY_EVALUATING, "c")
    conn.commit()
    _record_run(conn, "run-1", "reviewer", status="failed")
    state.transition(conn, task_id=TASK, to_phase=TaskPhase.FAILED,
                     terminal_reason="review failed", event_id="fail-1", at=T1)
    conn.commit()
    rec = recovery.diagnose_task(conn, task_id=TASK)
    assert rec.verdict == "CONSISTENT"
    assert rec.recoverable_terminal is True
    succ = recovery.propose_successor(rec, successor_task_id="task-2",
                                      cycle_id=CYCLE, created_at=T1)
    store.insert_task(conn, succ)
    conn.commit()
    assert store.get_task(conn, "task-2").supersedes_task_id == TASK
    assert ledger.verify_task(conn, task_id=TASK).phase is TaskPhase.FAILED
    conn.close()


def test_drill_duplicate_event_replay_no_double(tmp_path) -> None:
    conn = _db_task(tmp_path)
    state.transition(conn, task_id=TASK, to_phase=TaskPhase.BASELINING,
                     terminal_reason=None, event_id="evt-1", at=T1)
    conn.commit()
    with pytest.raises(AotfError) as exc:
        state.transition(conn, task_id=TASK, to_phase=TaskPhase.BASELINING,
                         terminal_reason=None, event_id="evt-1", at=T1)
    assert exc.value.code is ErrorCode.INVALID_STATE_TRANSITION
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_event(conn, store.get_event(conn, "evt-1"))
    conn.rollback()
    _advance(conn, TaskPhase.PLAN_READY, "c")
    conn.commit()
    assert ledger.verify_task(conn, task_id=TASK).version == 2
    conn.close()


def test_drill_outbox_crash_redelivery_single_effect(tmp_path) -> None:
    conn = _db_task(tmp_path)
    store.insert_outbox(conn, OutboxRecord(
        message_id="msg-1", topic="task.completed",
        payload_json='{"task_id":"task-min"}', status="pending",
        attempts=0, next_attempt_at=None, created_at=T1,
    ))
    conn.commit()

    class _Sender:
        def __init__(self, receiver):
            self.sent = []
            self._r = receiver

        def send(self, message_id, topic, payload):
            self.sent.append(message_id)
            self._r.apply(message_id)

    receiver = _DedupReceiver()
    sender = _Sender(receiver)
    outbox.process_due(conn, sender=sender, at=T1, max_attempts=3,
                       base_delay_seconds=60)
    conn.rollback()
    assert store.get_outbox(conn, "msg-1").status == "pending"
    outbox.process_due(conn, sender=sender, at=T1, max_attempts=3,
                       base_delay_seconds=60)
    conn.commit()
    assert sender.sent == ["msg-1", "msg-1"]
    assert receiver.applied == ["msg-1"]
    assert store.get_outbox(conn, "msg-1").status == "delivered"
    conn.close()


def test_drill_dual_controller_single_lease_fencing(tmp_path) -> None:
    path = tmp_path / "dual.db"
    conn_a = db.connect(path)
    db.init_db(conn_a)
    conn_b = db.connect(path)
    db.init_db(conn_b)
    a = lease.acquire(conn_a, resource_id=TASK, holder="controller-a",
                      lease_seconds=60, at=T0)
    conn_a.commit()
    assert a.fencing_token == 1
    with pytest.raises(AotfError) as exc:
        lease.acquire(conn_b, resource_id=TASK, holder="controller-b",
                      lease_seconds=60, at=T0)
    assert exc.value.code is ErrorCode.LEASE_CONFLICT
    later = T0 + timedelta(seconds=61)
    b = lease.acquire(conn_b, resource_id=TASK, holder="controller-b",
                      lease_seconds=60, at=later)
    conn_b.commit()
    assert b.fencing_token == 2
    with pytest.raises(AotfError) as exc2:
        lease.check_held(conn_a, resource_id=TASK, holder="controller-a",
                         fencing_token=1, at=later)
    assert exc2.value.code is ErrorCode.STALE_FENCING_TOKEN
    conn_a.close()
    conn_b.close()


def test_drill_crash_mid_transaction_deterministic(tmp_path) -> None:
    conn = _db_task(tmp_path)
    conn.execute("BEGIN")
    lease.acquire(conn, resource_id=TASK, holder="controller-a",
                  lease_seconds=60, at=T0)
    store.cas_task_phase(conn, task_id=TASK, expected_phase=TaskPhase.CREATED,
                         expected_version=0, new_phase=TaskPhase.BASELINING,
                         terminal_reason=None, updated_at=T1)
    store.insert_event(conn, EventRecord(
        event_id="crash-1", aggregate_type=AggregateType.TASK,
        aggregate_id=TASK, aggregate_version=1,
        event_type="TASK_PHASE_CHANGED", producer="drill",
        payload=CanonicalPayload.from_value(
            {"from_phase": "CREATED", "to_phase": "BASELINING", "reason": None}),
        created_at=T1,
    ))
    conn.rollback()
    assert store.get_task(conn, TASK).phase is TaskPhase.CREATED
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    d = recovery.diagnose_task(conn, task_id=TASK)
    assert (d.verdict, d.phase) == ("CONSISTENT", TaskPhase.CREATED)
    state.transition(conn, task_id=TASK, to_phase=TaskPhase.BASELINING,
                     terminal_reason=None, event_id="ok-1", at=T1)
    conn.commit()
    d2 = recovery.diagnose_task(conn, task_id=TASK)
    assert (d2.verdict, d2.phase) == ("CONSISTENT", TaskPhase.BASELINING)
    conn.close()
