"""M0-A1b2a1/A1b2a2/A1b2b1/A1b2b2a/A1b2b2b/A1b2c/A1b2d/A1b2d1/A1b2d2 枚举、payload、事件与 Cycle/Task/Approval/Artifact/AgentRun/Outbox records 及 A3a1 LeaseRecord 测试。

所有 golden 期望值写死；时间全用显式固定值；不访问网络/文件/时钟。
"""

from __future__ import annotations

import aotf.models
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aotf.errors import AotfError, ErrorCode
from aotf.models import (
    AggregateType,
    AgentRunRecord,
    ApprovalRecord,
    ArtifactRecord,
    CanonicalPayload,
    CycleRecord,
    EventRecord,
    LeaseRecord,
    NewEvent,
    OutboxRecord,
    TaskRecord,
    TERMINAL_TASK_PHASES,
    TaskPhase,
)

T0 = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)

PHASES = [
    "CREATED", "BASELINING", "PLAN_READY", "AWAITING_PLAN_APPROVAL",
    "EDIT_AUTHORIZED", "IMPLEMENTING", "DELTA_CAPTURED", "EVIDENCE_RUNNING",
    "REVIEWING", "POLICY_EVALUATING", "CHECKPOINT_READY", "RELEASE_CANDIDATE",
    "COMPLETED", "FAILED", "CANCELLED", "SAFE_HALT",
]

GOLDEN_VALUE = {
    "z": "é",
    "a": [None, True, False, 0, -7, Decimal("1.2300"),
          datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))],
}
GOLDEN_TEXT = (
    '{"a":[null,true,false,0,-7,'
    '{"$aotf":"decimal","value":"1.23"},'
    '{"$aotf":"datetime","value":"2026-08-31T16:00:00.123456Z"}],'
    '"z":"é"}'
)
GOLDEN_SHA = "46b947bf919c1c8ec481db30762cd4296cef0fa714d6baafe17a71ee3b321b7e"


def _payload(obj=None) -> CanonicalPayload:
    return CanonicalPayload.from_value(obj if obj is not None else {"phase": "CREATED"})


def _new(**kw):
    base = dict(event_id="evt-1", aggregate_type=AggregateType.TASK, aggregate_id="task-1",
                expected_version=0, event_type="TASK_CREATED", producer="controller",
                payload=_payload(), created_at=T0)
    base.update(kw)
    return NewEvent(**base)


def _record(**kw):
    base = dict(event_id="evt-9", aggregate_type=AggregateType.TASK, aggregate_id="task-9",
                aggregate_version=1, event_type="TASK_CREATED", producer="controller",
                payload=_payload(), created_at=T0)
    base.update(kw)
    return EventRecord(**base)


def _cycle(**kw):
    base = dict(cycle_id="cycle-1", project_id="project-1", status="ACTIVE",
                version=0, created_at=T0, updated_at=T0)
    base.update(kw)
    return CycleRecord(**base)


def _task(**kw):
    base = dict(task_id="task-1", cycle_id="cycle-1", supersedes_task_id=None,
                phase=TaskPhase.CREATED, version=0, baseline_commit=None,
                baseline_tree=None, proposal_sha256=None, authorization_id=None,
                worktree_path=None, worktree_branch=None, actual_tree=None,
                delta_sha256=None, terminal_reason=None, created_at=T0, updated_at=T0)
    base.update(kw)
    return TaskRecord(**base)


def _approval(**kw):
    base = dict(approval_id="approval-1", task_id="task-1", decision="APPROVED",
                scope_sha256="a" * 64, proposal_sha256="b" * 64,
                baseline_tree="c" * 40, source="human", expires_at=None,
                created_at=T0)
    base.update(kw)
    return ApprovalRecord(**base)


def _artifact(**kw):
    base = dict(artifact_id="artifact-1", task_id="task-1", kind="test-evidence",
                relative_path="evidence/task-1/unit.json", producer="controller",
                sha256="a" * 64, size_bytes=1234, created_at=T0)
    base.update(kw)
    return ArtifactRecord(**base)


def _agent_run(**kw):
    base = dict(run_id="run-1", task_id="task-1", role="planner",
                session_id=None, requested_model="opus", resolved_model=None,
                status="completed", input_digest="a" * 64, output_artifact_id=None,
                input_tokens=None, output_tokens=None, estimated_cost_usd=None,
                started_at=T0, ended_at=None)
    base.update(kw)
    return AgentRunRecord(**base)


def _outbox(**kw):
    base = dict(message_id="msg-1", topic="task.completed",
                payload_json='{"task_id":"task-1","phase":"COMPLETED"}',
                status="pending", attempts=0, next_attempt_at=None, created_at=T0)
    base.update(kw)
    return OutboxRecord(**base)


def _lease(**kw):
    base = dict(resource_id="res-task-1", controller_instance_id="controller-a",
                fencing_token=1, acquired_at=T0, heartbeat_at=T0,
                expires_at=T0, version=0)
    base.update(kw)
    return LeaseRecord(**base)


def test_task_phase_values_are_exact() -> None:
    assert [p.value for p in TaskPhase] == PHASES
    assert all(p.value == p.name for p in TaskPhase)
    assert aotf.models.__all__ == [
        "AggregateType",
        "AgentRunRecord",
        "ApprovalRecord",
        "ArtifactRecord",
        "CanonicalPayload",
        "CycleRecord",
        "EventRecord",
        "LeaseRecord",
        "NewEvent",
        "OutboxRecord",
        "TaskRecord",
        "TERMINAL_TASK_PHASES",
        "TaskPhase",
    ]


def test_terminal_task_phases_are_exact() -> None:
    assert TERMINAL_TASK_PHASES == frozenset(
        {TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.CANCELLED, TaskPhase.SAFE_HALT}
    )
    assert TaskPhase.CHECKPOINT_READY not in TERMINAL_TASK_PHASES
    assert TaskPhase.RELEASE_CANDIDATE not in TERMINAL_TASK_PHASES


def test_aggregate_type_values_are_exact() -> None:
    assert [a.value for a in AggregateType] == ["CYCLE", "TASK"]


def test_canonical_payload_matches_golden_bytes_and_digest() -> None:
    p = CanonicalPayload.from_value(GOLDEN_VALUE)
    assert p.json_text == GOLDEN_TEXT
    assert p.sha256 == GOLDEN_SHA


@pytest.mark.parametrize("alt", [
    {"a": GOLDEN_VALUE["a"], "z": "é"},
    {"z": "é", "a": GOLDEN_VALUE["a"]},
    {"z": "é", "a": [None, True, False, 0, -7, Decimal("1.23"),
                           datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))]},
])
def test_canonical_payload_equivalent_inputs_match(alt) -> None:
    assert CanonicalPayload.from_value(alt).sha256 == GOLDEN_SHA


@pytest.mark.parametrize("bad", [
    {"v": float("nan")},
    {"access_token": "SENSITIVE-VALUE-88ABC"},
])
def test_canonical_payload_propagates_schema_failure(bad) -> None:
    with pytest.raises(AotfError) as exc:
        CanonicalPayload.from_value(bad)
    assert exc.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED
    assert "SENSITIVE-VALUE-88ABC" not in str(exc.value)
    assert "SENSITIVE-VALUE-88ABC" not in str(exc.value.details)
    assert "access_token" not in str(exc.value)
    assert "access_token" not in str(exc.value.details)


def test_canonical_payload_is_frozen_slotted_and_not_directly_injectable() -> None:
    p = CanonicalPayload.from_value({"x": 1})
    with pytest.raises(AttributeError):
        p.json_text = "x"
    with pytest.raises((AttributeError, TypeError)):
        p.extra = 1
    with pytest.raises(TypeError):
        CanonicalPayload(json_text="x", sha256="y")
    assert not hasattr(p, "__dict__")


def test_new_event_from_payload_builds_canonical_payload() -> None:
    e = NewEvent.from_payload(
        event_id="evt-caller-1",
        aggregate_type=AggregateType.TASK,
        aggregate_id="task-caller-1",
        expected_version=0,
        event_type="TASK_CREATED",
        producer="controller",
        payload={"phase": "CREATED"},
        created_at=T0,
    )
    assert e.event_id == "evt-caller-1"
    assert e.aggregate_type is AggregateType.TASK
    assert e.aggregate_id == "task-caller-1"
    assert e.expected_version == 0
    assert e.event_type == "TASK_CREATED"
    assert e.producer == "controller"
    assert isinstance(e.payload, CanonicalPayload)
    assert e.payload_json == '{"phase":"CREATED"}'
    assert e.payload_sha256 == e.payload.sha256
    assert len(e.payload_sha256) == 64
    assert e.created_at == T0


def test_new_event_is_frozen_and_slotted() -> None:
    e = _new()
    with pytest.raises(AttributeError):
        e.event_id = "changed"
    assert not hasattr(e, "__dict__")


def test_new_event_rejects_raw_payload_in_generated_constructor() -> None:
    with pytest.raises(AotfError) as exc:
        _new(payload={"phase": "CREATED"})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "payload"}
    assert "CREATED" not in str(exc.value)
    assert "CREATED" not in str(exc.value.details)
    assert "phase" not in str(exc.value)
    assert "phase" not in str(exc.value.details)
    with pytest.raises(AotfError) as exc2:
        _new(aggregate_type="TASK")
    assert exc2.value.code is ErrorCode.INVALID_INPUT
    assert exc2.value.details == {"field": "aggregate_type"}
    assert "TASK" not in str(exc2.value)
    assert "TASK" not in str(exc2.value.details)


@pytest.mark.parametrize("field,value", [
    ("event_id", "  "),
    ("aggregate_id", "é"),
    ("event_type", "T\nX"),
    ("producer", "p" * 300),
])
def test_new_event_rejects_invalid_token_field(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _new(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    assert value not in str(exc.value)
    assert value not in str(exc.value.details)


@pytest.mark.parametrize("bad", [True, -1])
def test_new_event_rejects_invalid_expected_version(bad) -> None:
    with pytest.raises(AotfError) as exc:
        _new(expected_version=bad)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "expected_version"}
    raw = str(bad)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


@pytest.mark.parametrize("bad", [
    datetime(2026, 9, 1),
    datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8))),
])
def test_new_event_rejects_invalid_created_at(bad) -> None:
    with pytest.raises(AotfError) as exc:
        _new(created_at=bad)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "created_at"}
    raw = bad.isoformat()
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


def test_event_record_exposes_persisted_payload_fields() -> None:
    p = _payload()
    r = EventRecord(
        event_id="evt-9",
        aggregate_type=AggregateType.TASK,
        aggregate_id="task-9",
        aggregate_version=1,
        event_type="TASK_CREATED",
        producer="controller",
        payload=p,
        created_at=T0,
    )
    assert r.event_id == "evt-9"
    assert r.aggregate_type is AggregateType.TASK
    assert r.aggregate_id == "task-9"
    assert r.aggregate_version == 1
    assert r.event_type == "TASK_CREATED"
    assert r.producer == "controller"
    assert r.payload is p
    assert isinstance(r.payload, CanonicalPayload)
    assert r.payload_json == '{"phase":"CREATED"}'
    assert r.payload_sha256 == r.payload.sha256
    assert r.created_at == T0


@pytest.mark.parametrize("bad", [False, -1])
def test_event_record_rejects_invalid_aggregate_version(bad) -> None:
    with pytest.raises(AotfError) as exc:
        _record(aggregate_version=bad)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "aggregate_version"}
    raw = str(bad)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


def test_event_record_is_frozen_and_slotted() -> None:
    r = _record()
    with pytest.raises(AttributeError):
        r.aggregate_version = 2
    assert not hasattr(r, "__dict__")


def test_cycle_record_exposes_fields_and_is_frozen_slotted() -> None:
    c = _cycle()
    assert c.cycle_id == "cycle-1"
    assert c.project_id == "project-1"
    assert c.status == "ACTIVE"
    assert c.version == 0
    assert c.created_at == T0
    assert c.updated_at == T0
    with pytest.raises(AttributeError):
        c.cycle_id = "x"
    assert not hasattr(c, "__dict__")


@pytest.mark.parametrize("field,value", [
    ("cycle_id", "  "),
    ("project_id", "é"),
    ("status", "A\nCTIVE"),
    ("version", True),
    ("created_at", datetime(2026, 9, 1)),
    ("updated_at", datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))),
])
def test_cycle_record_rejects_invalid_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _cycle(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


def test_cycle_record_rejects_time_reversal() -> None:
    with pytest.raises(AotfError) as exc:
        _cycle(created_at=datetime(2026, 9, 2, tzinfo=timezone.utc), updated_at=T0)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "updated_at"}


def test_task_record_minimal_created_exposes_fields_and_is_frozen_slotted() -> None:
    t = _task()
    assert t.task_id == "task-1"
    assert t.cycle_id == "cycle-1"
    assert t.supersedes_task_id is None
    assert t.phase is TaskPhase.CREATED
    assert t.version == 0
    assert t.baseline_commit is None
    assert t.baseline_tree is None
    assert t.proposal_sha256 is None
    assert t.authorization_id is None
    assert t.worktree_path is None
    assert t.worktree_branch is None
    assert t.actual_tree is None
    assert t.delta_sha256 is None
    assert t.terminal_reason is None
    assert t.created_at == T0
    assert t.updated_at == T0
    with pytest.raises(AttributeError):
        t.task_id = "changed"
    assert not hasattr(t, "__dict__")


def test_task_record_full_terminal_exposes_all_fields() -> None:
    t = _task(
        task_id="task-1",
        cycle_id="cycle-1",
        supersedes_task_id="task-0",
        phase=TaskPhase.COMPLETED,
        version=3,
        baseline_commit="a" * 40,
        baseline_tree="a" * 40,
        proposal_sha256="c" * 64,
        authorization_id="auth-1",
        worktree_path="D:/tmp/wt-task-1",
        worktree_branch="feature/task-1",
        actual_tree="b" * 64,
        delta_sha256="d" * 64,
        terminal_reason="all checks passed",
        created_at=T0,
        updated_at=T0,
    )
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


@pytest.mark.parametrize("field,value", [
    ("task_id", "  "),
    ("cycle_id", "e\u0301"),
    ("phase", "CREATED"),
    ("version", True),
    ("created_at", datetime(2026, 9, 1)),
    ("updated_at", datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))),
])
def test_task_record_rejects_invalid_required_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _task(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


@pytest.mark.parametrize("field,value", [
    ("supersedes_task_id", "  "),
    ("baseline_commit", "g" * 40),
    ("baseline_tree", "g" * 40),
    ("proposal_sha256", "C" * 64),
    ("authorization_id", "  "),
    ("worktree_path", "wt\x00path"),
    ("worktree_branch", "  "),
    ("actual_tree", "g" * 64),
    ("delta_sha256", "D" * 64),
    ("terminal_reason", "reason\x1f"),
])
def test_task_record_rejects_invalid_optional_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _task(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


def test_task_record_rejects_time_reversal() -> None:
    with pytest.raises(AotfError) as exc:
        _task(created_at=datetime(2026, 9, 2, tzinfo=timezone.utc), updated_at=T0)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "updated_at"}


def test_task_record_rejects_self_supersedes() -> None:
    with pytest.raises(AotfError) as exc:
        _task(supersedes_task_id="task-1")
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "supersedes_task_id"}
    assert "task-1" not in str(exc.value)
    assert "task-1" not in str(exc.value.details)


@pytest.mark.parametrize("field,updates", [
    ("baseline", {"baseline_tree": "a" * 40}),
    ("baseline", {"baseline_commit": "a" * 40}),
    ("worktree", {"worktree_branch": "feature/task-1"}),
    ("worktree", {"worktree_path": "D:/tmp/wt-task-1"}),
    ("actual_delta", {"delta_sha256": "d" * 64}),
    ("actual_delta", {"actual_tree": "b" * 64}),
])
def test_task_record_rejects_pair_invariant(field, updates) -> None:
    with pytest.raises(AotfError) as exc:
        _task(**updates)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}


@pytest.mark.parametrize("field,updates", [
    ("proposal_sha256", {"proposal_sha256": "c" * 64}),
    ("authorization_id", {"authorization_id": "auth-1"}),
    ("worktree", {"worktree_path": "D:/tmp/wt-task-1",
                  "worktree_branch": "feature/task-1"}),
    ("actual_delta", {"actual_tree": "b" * 64, "delta_sha256": "d" * 64}),
])
def test_task_record_rejects_dependency_chain(field, updates) -> None:
    with pytest.raises(AotfError) as exc:
        _task(**updates)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}


@pytest.mark.parametrize("phase,reason", [
    (TaskPhase.COMPLETED, None),
    (TaskPhase.CREATED, "early stop"),
])
def test_task_record_enforces_terminal_reason_iff_terminal(phase, reason) -> None:
    with pytest.raises(AotfError) as exc:
        _task(phase=phase, terminal_reason=reason)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "terminal_reason"}
    if reason is not None:
        assert reason not in str(exc.value)
        assert reason not in str(exc.value.details)


def test_approval_record_exposes_all_fields_and_is_frozen_slotted() -> None:
    a = _approval()
    assert a.approval_id == "approval-1"
    assert a.task_id == "task-1"
    assert a.decision == "APPROVED"
    assert a.scope_sha256 == "a" * 64
    assert a.proposal_sha256 == "b" * 64
    assert a.baseline_tree == "c" * 40
    assert a.source == "human"
    assert a.expires_at is None
    assert a.created_at == T0
    with pytest.raises(AttributeError):
        a.approval_id = "changed"
    assert not hasattr(a, "__dict__")


def test_approval_record_accepts_future_expiry_and_sha256_tree() -> None:
    a = _approval(baseline_tree="c" * 64, expires_at=T0 + timedelta(days=1))
    assert a.baseline_tree == "c" * 64
    assert a.expires_at == T0 + timedelta(days=1)
    assert a.created_at == T0


@pytest.mark.parametrize("field,value", [
    ("approval_id", "  "),
    ("task_id", "é"),
    ("decision", "A\x01PPROVED"),
    ("scope_sha256", "A" * 64),
    ("proposal_sha256", "c" * 63),
    ("baseline_tree", "z" * 40),
    ("source", "agent"),
])
def test_approval_record_rejects_invalid_required_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _approval(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


@pytest.mark.parametrize("field,value", [
    ("created_at", datetime(2026, 9, 1)),
    ("created_at", datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))),
    ("expires_at", datetime(2026, 9, 1)),
    ("expires_at", datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))),
])
def test_approval_record_rejects_invalid_time_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _approval(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat()
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


@pytest.mark.parametrize("expires,created", [
    (T0, T0),
    (datetime(2026, 8, 31, tzinfo=timezone.utc), T0),
])
def test_approval_record_requires_expiry_after_creation(expires, created) -> None:
    with pytest.raises(AotfError) as exc:
        _approval(expires_at=expires, created_at=created)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "expires_at"}


def test_artifact_record_exposes_all_fields_and_is_frozen_slotted() -> None:
    a = _artifact()
    assert a.artifact_id == "artifact-1"
    assert a.task_id == "task-1"
    assert a.kind == "test-evidence"
    assert a.relative_path == "evidence/task-1/unit.json"
    assert a.producer == "controller"
    assert a.sha256 == "a" * 64
    assert a.size_bytes == 1234
    assert a.created_at == T0
    with pytest.raises(AttributeError):
        a.artifact_id = "changed"
    assert not hasattr(a, "__dict__")


@pytest.mark.parametrize("field,value", [
    ("artifact_id", "  "),
    ("task_id", "é"),
    ("kind", "a\x01b"),
    ("producer", "  "),
    ("sha256", "A" * 64),
    ("sha256", "a" * 63),
    ("size_bytes", -1),
    ("created_at", datetime(2026, 9, 1)),
])
def test_artifact_record_rejects_invalid_required_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _artifact(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


@pytest.mark.parametrize("field,value", [
    ("relative_path", "path\x00x"),
    ("relative_path", "p" * 4097),
    ("size_bytes", True),
    ("created_at", datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))),
])
def test_artifact_record_rejects_invalid_text_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _artifact(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


def test_agent_run_record_exposes_all_fields_and_is_frozen_slotted() -> None:
    a = _agent_run()
    assert a.run_id == "run-1"
    assert a.task_id == "task-1"
    assert a.role == "planner"
    assert a.session_id is None
    assert a.requested_model == "opus"
    assert a.resolved_model is None
    assert a.status == "completed"
    assert a.input_digest == "a" * 64
    assert a.output_artifact_id is None
    assert a.input_tokens is None
    assert a.output_tokens is None
    assert a.estimated_cost_usd is None
    assert a.started_at == T0
    assert a.ended_at is None
    with pytest.raises(AttributeError):
        a.run_id = "changed"
    assert not hasattr(a, "__dict__")


def test_agent_run_record_accepts_full_optional_fields() -> None:
    a = _agent_run(
        role="reviewer",
        session_id="sess-1",
        resolved_model="claude-sonnet-5",
        output_artifact_id="artifact-1",
        input_tokens=1250,
        output_tokens=80,
        estimated_cost_usd=Decimal("0.012345"),
        ended_at=T0 + timedelta(minutes=5),
    )
    assert a.run_id == "run-1"
    assert a.task_id == "task-1"
    assert a.role == "reviewer"
    assert a.session_id == "sess-1"
    assert a.requested_model == "opus"
    assert a.resolved_model == "claude-sonnet-5"
    assert a.status == "completed"
    assert a.input_digest == "a" * 64
    assert a.output_artifact_id == "artifact-1"
    assert a.input_tokens == 1250
    assert a.output_tokens == 80
    assert a.estimated_cost_usd == Decimal("0.012345")
    assert a.started_at == T0
    assert a.ended_at == T0 + timedelta(minutes=5)


@pytest.mark.parametrize("field,value", [
    ("run_id", "  "),
    ("task_id", "é"),
    ("role", "a\x01b"),
    ("requested_model", "  "),
    ("status", "a\x00b"),
    ("input_digest", "A" * 64),
    ("input_digest", "a" * 63),
])
def test_agent_run_record_rejects_invalid_required_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _agent_run(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


@pytest.mark.parametrize("field,value", [
    ("session_id", "  "),
    ("resolved_model", "  "),
    ("output_artifact_id", "  "),
    ("input_tokens", True),
    ("output_tokens", -1),
    ("estimated_cost_usd", Decimal("NaN")),
])
def test_agent_run_record_rejects_invalid_optional_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _agent_run(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


@pytest.mark.parametrize("field,value", [
    ("started_at", datetime(2026, 9, 1)),
    ("started_at", datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))),
    ("ended_at", datetime(2026, 9, 1)),
    ("ended_at", datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))),
])
def test_agent_run_record_rejects_invalid_time_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _agent_run(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat()
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


def test_agent_run_record_rejects_ended_before_started() -> None:
    with pytest.raises(AotfError) as exc:
        _agent_run(started_at=datetime(2026, 9, 2, tzinfo=timezone.utc), ended_at=T0)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "ended_at"}
    _agent_run(ended_at=T0)


def test_outbox_record_exposes_all_fields_and_is_frozen_slotted() -> None:
    m = _outbox()
    assert m.message_id == "msg-1"
    assert m.topic == "task.completed"
    assert m.payload_json == '{"task_id":"task-1","phase":"COMPLETED"}'
    assert m.status == "pending"
    assert m.attempts == 0
    assert m.next_attempt_at is None
    assert m.created_at == T0
    with pytest.raises(AttributeError):
        m.message_id = "changed"
    assert not hasattr(m, "__dict__")


def test_outbox_record_accepts_scheduled_retry() -> None:
    m = _outbox(
        status="pending",
        attempts=5,
        payload_json='{"task_id":"task-1","phase":"COMPLETED","retry":true}',
        next_attempt_at=T0 + timedelta(hours=1),
    )
    assert m.message_id == "msg-1"
    assert m.topic == "task.completed"
    assert m.payload_json == '{"task_id":"task-1","phase":"COMPLETED","retry":true}'
    assert m.status == "pending"
    assert m.attempts == 5
    assert m.next_attempt_at == T0 + timedelta(hours=1)
    assert m.created_at == T0


@pytest.mark.parametrize("field,value", [
    ("message_id", "  "),
    ("topic", "é"),
    ("payload_json", "  "),
    ("payload_json", "x\x00y"),
    pytest.param("payload_json", "p" * 65537, id="payload_json-too-long"),
    ("status", "a\x1fb"),
    ("attempts", -1),
])
def test_outbox_record_rejects_invalid_required_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _outbox(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


@pytest.mark.parametrize("field,value", [
    ("created_at", datetime(2026, 9, 1)),
    ("created_at", datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))),
    ("next_attempt_at", datetime(2026, 9, 1)),
    ("next_attempt_at", datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))),
])
def test_outbox_record_rejects_invalid_time_scalar(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _outbox(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}
    raw = value.isoformat()
    assert raw not in str(exc.value)
    assert raw not in str(exc.value.details)


def test_lease_record_accepts_valid_fields() -> None:
    rec = _lease()
    assert rec.resource_id == "res-task-1"
    assert rec.controller_instance_id == "controller-a"
    assert rec.fencing_token == 1
    assert rec.acquired_at == T0
    assert rec.heartbeat_at == T0
    assert rec.expires_at == T0
    assert rec.version == 0


@pytest.mark.parametrize("field,value", [
    ("heartbeat_at", datetime(2026, 8, 31, tzinfo=timezone.utc)),
    ("expires_at", datetime(2026, 8, 31, tzinfo=timezone.utc)),
])
def test_lease_record_rejects_time_order(field, value) -> None:
    with pytest.raises(AotfError) as exc:
        _lease(**{field: value})
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": field}


@pytest.mark.parametrize("fencing_token", [0, -1])
def test_lease_record_rejects_nonpositive_fencing_token(fencing_token) -> None:
    with pytest.raises(AotfError) as exc:
        _lease(fencing_token=fencing_token)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "fencing_token"}


def test_lease_record_rejects_negative_version() -> None:
    with pytest.raises(AotfError) as exc:
        _lease(version=-1)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "version"}


@pytest.mark.parametrize("resource_id", ["", "a\nb", "x" * 256, None])
def test_lease_record_rejects_invalid_resource_id(resource_id) -> None:
    with pytest.raises(AotfError) as exc:
        _lease(resource_id=resource_id)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "resource_id"}


@pytest.mark.parametrize("acquired_at", [
    datetime(2026, 9, 1),
    datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8))),
])
def test_lease_record_rejects_non_utc_datetime(acquired_at) -> None:
    with pytest.raises(AotfError) as exc:
        _lease(acquired_at=acquired_at)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {"field": "acquired_at"}
