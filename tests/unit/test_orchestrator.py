"""M0-A4b orchestrator 编排基元测试。

DB 一律使用 pytest tmp_path；无网络/时钟；agent_runs FK 需 task 父行。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aotf import db, orchestrator, store
from aotf.models import CycleRecord, TaskPhase, TaskRecord
from aotf.runner import AgentRunRequest, AgentRunResult

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
DIGEST = "a" * 64


def _req(**kw):
    base = dict(run_id="run-1", task_id="task-min", role="planner",
                requested_model="opus", input_digest=DIGEST, max_turns=20,
                max_budget_usd=Decimal("20.00"),
                deadline_at=T0 + timedelta(hours=1))
    base.update(kw)
    return AgentRunRequest(**base)


def _result(**kw):
    base = dict(run_id="run-1", status="completed", resolved_model="claude-opus-5",
                input_tokens=100, output_tokens=50,
                estimated_cost_usd=Decimal("0.001000"),
                output_artifact_id=None, error_code=None)
    base.update(kw)
    return AgentRunResult(**base)


def _task_min():
    return TaskRecord(
        task_id="task-min", cycle_id="cycle-1", supersedes_task_id=None,
        phase=TaskPhase.CREATED, version=0, baseline_commit=None,
        baseline_tree=None, proposal_sha256=None, authorization_id=None,
        worktree_path=None, worktree_branch=None, actual_tree=None,
        delta_sha256=None, terminal_reason=None, created_at=T0, updated_at=T0,
    )


def _db(tmp_path, name="t.db", with_task=True):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    store.insert_cycle(conn, CycleRecord(
        cycle_id="cycle-1", project_id="project-1", status="ACTIVE",
        version=0, created_at=T0, updated_at=T0,
    ))
    if with_task:
        store.insert_task(conn, _task_min())
    conn.commit()
    return conn


def _record(conn, req=None, result=None):
    req = req or _req()
    result = result or _result()
    rec = orchestrator.record_run(conn, request=req, result=result,
                                  started_at=T0,
                                  ended_at=T0 + timedelta(minutes=1))
    conn.commit()
    return rec


def test_record_run_persists_full_mapping(tmp_path) -> None:
    conn = _db(tmp_path)
    rec = _record(conn)
    assert rec.run_id == "run-1"
    assert rec.task_id == "task-min"
    assert rec.role == "planner"
    assert rec.requested_model == "opus"
    assert rec.resolved_model == "claude-opus-5"
    assert rec.status == "completed"
    assert rec.input_digest == DIGEST
    assert rec.input_tokens == 100
    assert rec.output_tokens == 50
    assert rec.estimated_cost_usd == Decimal("0.001000")
    assert rec.started_at == T0
    assert rec.ended_at == T0 + timedelta(minutes=1)
    row = store.get_agent_run(conn, "run-1")
    assert row == rec
    conn.close()


def test_record_run_rejects_mismatched_run_id(tmp_path) -> None:
    conn = _db(tmp_path)
    with pytest.raises(ValueError):
        _record(conn, result=_result(run_id="run-other"))
    conn.close()


def test_record_run_missing_task_parent_raises(tmp_path) -> None:
    conn = _db(tmp_path, with_task=False)
    with pytest.raises(sqlite3.IntegrityError):
        _record(conn)
    conn.close()


def test_normalize_status_completed_within_limits() -> None:
    req = _req()
    res = _result()
    assert orchestrator.normalize_status(req, res, T0 + timedelta(minutes=1)) == "completed"


def test_normalize_status_budget_exceeded() -> None:
    req = _req()
    res = _result(estimated_cost_usd=Decimal("99.00"))
    assert orchestrator.normalize_status(req, res, T0 + timedelta(minutes=1)) == "budget_exceeded"


def test_normalize_status_timeout_after_deadline() -> None:
    req = _req()
    res = _result(estimated_cost_usd=Decimal("99.00"))
    assert orchestrator.normalize_status(req, res, T0 + timedelta(hours=2)) == "timeout"


@pytest.mark.parametrize("status", [
    "failed", "timeout", "budget_exceeded", "cancelled",
])
def test_normalize_status_passthrough_non_completed(status) -> None:
    res = _result(status=status)
    assert orchestrator.normalize_status(_req(), res, T0) == status


@pytest.mark.parametrize("role,target", [
    ("planner", TaskPhase.PLAN_READY),
    ("implementer", TaskPhase.DELTA_CAPTURED),
    ("reviewer", TaskPhase.POLICY_EVALUATING),
])
def test_advance_target_completed_roles(role, target) -> None:
    assert orchestrator.advance_target(role, "completed") is target


@pytest.mark.parametrize("role,status", [
    ("adversary", "completed"),
    ("planner", "failed"),
    ("planner", "timeout"),
    ("scientist", "completed"),
])
def test_advance_target_returns_none_for_other(role, status) -> None:
    assert orchestrator.advance_target(role, status) is None
