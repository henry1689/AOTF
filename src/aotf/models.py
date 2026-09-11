"""AOTF M0 事件基础 typed contracts（A1b2a1 + A1b2a2）。

冻结任务阶段枚举、terminal 集合、事件 aggregate 类型、canonical
payload 与事件 typed records。不实现 state machine、event append、
CAS、SQLite、ID/clock 生成；调用方必须显式提供 event_id 与
expected_version。
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from aotf.attest import SIGNATURE_BYTES
from aotf.canonical import canonical_json_bytes
from aotf.errors import AotfError, ErrorCode

__all__ = [
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


class TaskPhase(StrEnum):
    CREATED = "CREATED"
    BASELINING = "BASELINING"
    PLAN_READY = "PLAN_READY"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    EDIT_AUTHORIZED = "EDIT_AUTHORIZED"
    IMPLEMENTING = "IMPLEMENTING"
    DELTA_CAPTURED = "DELTA_CAPTURED"
    EVIDENCE_RUNNING = "EVIDENCE_RUNNING"
    REVIEWING = "REVIEWING"
    POLICY_EVALUATING = "POLICY_EVALUATING"
    CHECKPOINT_READY = "CHECKPOINT_READY"
    RELEASE_CANDIDATE = "RELEASE_CANDIDATE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SAFE_HALT = "SAFE_HALT"


TERMINAL_TASK_PHASES = frozenset(
    {
        TaskPhase.COMPLETED,
        TaskPhase.FAILED,
        TaskPhase.CANCELLED,
        TaskPhase.SAFE_HALT,
    }
)


class AggregateType(StrEnum):
    CYCLE = "CYCLE"
    TASK = "TASK"


@dataclass(frozen=True, slots=True, init=False)
class CanonicalPayload:
    json_text: str
    sha256: str

    @classmethod
    def from_value(cls, value: object) -> "CanonicalPayload":
        raw = canonical_json_bytes(value)
        obj = object.__new__(cls)
        object.__setattr__(obj, "json_text", raw.decode("utf-8"))
        object.__setattr__(obj, "sha256", hashlib.sha256(raw).hexdigest())
        return obj


def _reject(field: str) -> None:
    raise AotfError(ErrorCode.INVALID_INPUT, "invalid field", details={"field": field})


def _token(field: str, value: object) -> str:
    if type(value) is not str:
        _reject(field)
    if value != unicodedata.normalize("NFC", value):
        _reject(field)
    if value != value.strip() or not value:
        _reject(field)
    if len(value) > 255:
        _reject(field)
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        _reject(field)
    return value


def _version(field: str, value: object) -> int:
    if type(value) is not int or value < 0:
        _reject(field)
    return value


def _datetime(field: str, value: object) -> datetime:
    if type(value) is not datetime:
        _reject(field)
    if value.tzinfo is None or value.utcoffset() is None:
        _reject(field)
    if value.utcoffset() != timedelta(0):
        _reject(field)
    return value


def _enum(field: str, value: object) -> AggregateType:
    if not isinstance(value, AggregateType):
        _reject(field)
    return value


def _payload(field: str, value: object) -> CanonicalPayload:
    if not isinstance(value, CanonicalPayload):
        _reject(field)
    return value


def _validate_time_order(created_at: datetime, updated_at: datetime) -> None:
    _datetime("created_at", created_at)
    _datetime("updated_at", updated_at)
    if updated_at < created_at:
        _reject("updated_at")


def _optional_token(field: str, value: object) -> str | None:
    if value is None:
        return None
    return _token(field, value)


def _optional_text(field: str, value: object, max_length: int) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        _reject(field)
    if value != unicodedata.normalize("NFC", value):
        _reject(field)
    if value != value.strip() or not value:
        _reject(field)
    if len(value) > max_length:
        _reject(field)
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        _reject(field)
    return value


def _optional_sha256(field: str, value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) != 64:
        _reject(field)
    if any(ch not in "0123456789abcdef" for ch in value):
        _reject(field)
    return value


def _optional_git_oid(field: str, value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) not in (40, 64):
        _reject(field)
    if any(ch not in "0123456789abcdef" for ch in value):
        _reject(field)
    return value


def _task_phase(field: str, value: object) -> TaskPhase:
    if not isinstance(value, TaskPhase):
        _reject(field)
    return value


def _optional_datetime(field: str, value: object) -> datetime | None:
    if value is None:
        return None
    return _datetime(field, value)


def _optional_version(field: str, value: object) -> int | None:
    if value is None:
        return None
    return _version(field, value)


def _optional_money(field: str, value: object) -> Decimal | None:
    if value is None:
        return None
    if type(value) is not Decimal:
        _reject(field)
    if not value.is_finite():
        _reject(field)
    if value < 0:
        _reject(field)
    return value


@dataclass(frozen=True, slots=True)
class NewEvent:
    event_id: str
    aggregate_type: AggregateType
    aggregate_id: str
    expected_version: int
    event_type: str
    producer: str
    payload: CanonicalPayload
    created_at: datetime

    @classmethod
    def from_payload(
        cls,
        *,
        event_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        expected_version: int,
        event_type: str,
        producer: str,
        payload: object,
        created_at: datetime,
    ) -> "NewEvent":
        return cls(
            event_id=event_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            expected_version=expected_version,
            event_type=event_type,
            producer=producer,
            payload=CanonicalPayload.from_value(payload),
            created_at=created_at,
        )

    @property
    def payload_json(self) -> str:
        return self.payload.json_text

    @property
    def payload_sha256(self) -> str:
        return self.payload.sha256

    def __post_init__(self) -> None:
        _token("event_id", self.event_id)
        _enum("aggregate_type", self.aggregate_type)
        _token("aggregate_id", self.aggregate_id)
        _version("expected_version", self.expected_version)
        _token("event_type", self.event_type)
        _token("producer", self.producer)
        _payload("payload", self.payload)
        _datetime("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    aggregate_type: AggregateType
    aggregate_id: str
    aggregate_version: int
    event_type: str
    producer: str
    payload: CanonicalPayload
    created_at: datetime

    @property
    def payload_json(self) -> str:
        return self.payload.json_text

    @property
    def payload_sha256(self) -> str:
        return self.payload.sha256

    def __post_init__(self) -> None:
        _token("event_id", self.event_id)
        _enum("aggregate_type", self.aggregate_type)
        _token("aggregate_id", self.aggregate_id)
        _version("aggregate_version", self.aggregate_version)
        _token("event_type", self.event_type)
        _token("producer", self.producer)
        _payload("payload", self.payload)
        _datetime("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class CycleRecord:
    cycle_id: str
    project_id: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _token("cycle_id", self.cycle_id)
        _token("project_id", self.project_id)
        _token("status", self.status)
        _version("version", self.version)
        _validate_time_order(self.created_at, self.updated_at)


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    cycle_id: str
    supersedes_task_id: str | None
    phase: TaskPhase
    version: int
    baseline_commit: str | None
    baseline_tree: str | None
    proposal_sha256: str | None
    authorization_id: str | None
    worktree_path: str | None
    worktree_branch: str | None
    actual_tree: str | None
    delta_sha256: str | None
    terminal_reason: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _token("task_id", self.task_id)
        _token("cycle_id", self.cycle_id)
        _optional_token("supersedes_task_id", self.supersedes_task_id)
        _task_phase("phase", self.phase)
        _version("version", self.version)
        _optional_git_oid("baseline_commit", self.baseline_commit)
        _optional_git_oid("baseline_tree", self.baseline_tree)
        _optional_sha256("proposal_sha256", self.proposal_sha256)
        _optional_token("authorization_id", self.authorization_id)
        _optional_text("worktree_path", self.worktree_path, 4096)
        _optional_token("worktree_branch", self.worktree_branch)
        _optional_git_oid("actual_tree", self.actual_tree)
        _optional_sha256("delta_sha256", self.delta_sha256)
        _optional_text("terminal_reason", self.terminal_reason, 1024)
        _validate_time_order(self.created_at, self.updated_at)
        if self.supersedes_task_id is not None and self.supersedes_task_id == self.task_id:
            _reject("supersedes_task_id")
        if (self.baseline_commit is None) != (self.baseline_tree is None):
            _reject("baseline")
        if (self.worktree_path is None) != (self.worktree_branch is None):
            _reject("worktree")
        if (self.actual_tree is None) != (self.delta_sha256 is None):
            _reject("actual_delta")
        if self.proposal_sha256 is not None and self.baseline_commit is None:
            _reject("proposal_sha256")
        if self.authorization_id is not None and self.proposal_sha256 is None:
            _reject("authorization_id")
        if self.worktree_path is not None and self.authorization_id is None:
            _reject("worktree")
        if self.actual_tree is not None and self.worktree_path is None:
            _reject("actual_delta")
        if (self.phase in TERMINAL_TASK_PHASES) != (self.terminal_reason is not None):
            _reject("terminal_reason")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    task_id: str
    decision: str
    scope_sha256: str
    proposal_sha256: str
    baseline_tree: str
    source: str
    expires_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        _token("approval_id", self.approval_id)
        _token("task_id", self.task_id)
        _token("decision", self.decision)
        _optional_sha256("scope_sha256", self.scope_sha256)
        if self.scope_sha256 is None:
            _reject("scope_sha256")
        _optional_sha256("proposal_sha256", self.proposal_sha256)
        if self.proposal_sha256 is None:
            _reject("proposal_sha256")
        _optional_git_oid("baseline_tree", self.baseline_tree)
        if self.baseline_tree is None:
            _reject("baseline_tree")
        _token("source", self.source)
        _optional_datetime("expires_at", self.expires_at)
        _datetime("created_at", self.created_at)
        if self.source != "human":
            _reject("source")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            _reject("expires_at")


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    task_id: str
    kind: str
    relative_path: str
    producer: str
    sha256: str
    size_bytes: int
    created_at: datetime

    def __post_init__(self) -> None:
        _token("artifact_id", self.artifact_id)
        _token("task_id", self.task_id)
        _token("kind", self.kind)
        _optional_text("relative_path", self.relative_path, 4096)
        if self.relative_path is None:
            _reject("relative_path")
        _token("producer", self.producer)
        _optional_sha256("sha256", self.sha256)
        if self.sha256 is None:
            _reject("sha256")
        _version("size_bytes", self.size_bytes)
        _datetime("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class AgentRunRecord:
    run_id: str
    task_id: str
    role: str
    session_id: str | None
    requested_model: str
    resolved_model: str | None
    status: str
    input_digest: str
    output_artifact_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: Decimal | None
    started_at: datetime
    ended_at: datetime | None

    def __post_init__(self) -> None:
        _token("run_id", self.run_id)
        _token("task_id", self.task_id)
        _token("role", self.role)
        _optional_token("session_id", self.session_id)
        _token("requested_model", self.requested_model)
        _optional_token("resolved_model", self.resolved_model)
        _token("status", self.status)
        _optional_sha256("input_digest", self.input_digest)
        if self.input_digest is None:
            _reject("input_digest")
        _optional_token("output_artifact_id", self.output_artifact_id)
        _optional_version("input_tokens", self.input_tokens)
        _optional_version("output_tokens", self.output_tokens)
        _optional_money("estimated_cost_usd", self.estimated_cost_usd)
        _datetime("started_at", self.started_at)
        _optional_datetime("ended_at", self.ended_at)
        if self.ended_at is not None and self.ended_at < self.started_at:
            _reject("ended_at")


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    message_id: str
    topic: str
    payload_json: str
    status: str
    attempts: int
    next_attempt_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        _token("message_id", self.message_id)
        _token("topic", self.topic)
        _optional_text("payload_json", self.payload_json, 65536)
        if self.payload_json is None:
            _reject("payload_json")
        _token("status", self.status)
        _version("attempts", self.attempts)
        _optional_datetime("next_attempt_at", self.next_attempt_at)
        _datetime("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    """controller_leases 行 typed contract：controller 对资源的租约（A3a1）。

    controller_instance_id 为所有权标识（禁止 PID）；fencing_token 单调递增，
    过期 takeover 时 +1 产生新代。时间链要求 acquired <= heartbeat <= expires。
    store 读写归 A3a2、acquire/takeover/fencing 语义归 A3a3。
    """

    resource_id: str
    controller_instance_id: str
    fencing_token: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    version: int

    def __post_init__(self) -> None:
        _token("resource_id", self.resource_id)
        _token("controller_instance_id", self.controller_instance_id)
        _version("fencing_token", self.fencing_token)
        if self.fencing_token < 1:
            _reject("fencing_token")
        _datetime("acquired_at", self.acquired_at)
        _datetime("heartbeat_at", self.heartbeat_at)
        _datetime("expires_at", self.expires_at)
        _version("version", self.version)
        if self.heartbeat_at < self.acquired_at:
            _reject("heartbeat_at")
        if self.expires_at < self.heartbeat_at:
            _reject("expires_at")
