"""AOTF typed store for cycles/tasks/events and CAS (A1c2a-1 + A1c2a-2).

实现 cycles/tasks/events 三表的 typed insert/get 与 TaskRecord 版本 CAS
（权威架构 §6.2/§6.3）。序列化：datetime -> ISO-8601 文本、enum -> .value、
None -> NULL、CanonicalPayload -> json_text+sha256 两列；读回经 typed 校验。
approvals/artifacts/agent_runs/outbox store 已含（A1c2b）、controller_leases store
已含（A3a2）；状态机/事件账本属后续包。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

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
from aotf.snapshot import SnapshotRecord, SnapshotError


def _integrity(message: str) -> None:
    raise RuntimeError(f"store integrity: {message}")


def _encode_dt(value: datetime) -> str:
    return value.isoformat()


def _decode_dt(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        _integrity(f"non-utc datetime: {text}")
    return dt


def _parse_phase(text: str) -> TaskPhase:
    try:
        return TaskPhase(text)
    except ValueError:
        _integrity(f"unknown task phase: {text}")


def _parse_aggregate(text: str) -> AggregateType:
    try:
        return AggregateType(text)
    except ValueError:
        _integrity(f"unknown aggregate type: {text}")


def _encode_money(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _decode_money(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def insert_cycle(conn: sqlite3.Connection, cycle: CycleRecord) -> None:
    conn.execute(
        "INSERT INTO cycles (cycle_id, project_id, status, version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cycle.cycle_id, cycle.project_id, cycle.status, cycle.version,
         _encode_dt(cycle.created_at), _encode_dt(cycle.updated_at)),
    )


def get_cycle(conn: sqlite3.Connection, cycle_id: str) -> CycleRecord | None:
    row = conn.execute(
        "SELECT cycle_id, project_id, status, version, created_at, updated_at "
        "FROM cycles WHERE cycle_id = ?",
        (cycle_id,),
    ).fetchone()
    if row is None:
        return None
    return CycleRecord(
        cycle_id=row["cycle_id"],
        project_id=row["project_id"],
        status=row["status"],
        version=row["version"],
        created_at=_decode_dt(row["created_at"]),
        updated_at=_decode_dt(row["updated_at"]),
    )


def insert_task(conn: sqlite3.Connection, task: TaskRecord) -> None:
    conn.execute(
        "INSERT INTO tasks (task_id, cycle_id, supersedes_task_id, phase, version, "
        "baseline_commit, baseline_tree, proposal_sha256, authorization_id, "
        "worktree_path, worktree_branch, actual_tree, delta_sha256, terminal_reason, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task.task_id, task.cycle_id, task.supersedes_task_id, task.phase.value,
         task.version, task.baseline_commit, task.baseline_tree, task.proposal_sha256,
         task.authorization_id, task.worktree_path, task.worktree_branch, task.actual_tree,
         task.delta_sha256, task.terminal_reason, _encode_dt(task.created_at),
         _encode_dt(task.updated_at)),
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> TaskRecord | None:
    row = conn.execute(
        "SELECT task_id, cycle_id, supersedes_task_id, phase, version, "
        "baseline_commit, baseline_tree, proposal_sha256, authorization_id, "
        "worktree_path, worktree_branch, actual_tree, delta_sha256, terminal_reason, "
        "created_at, updated_at FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return TaskRecord(
        task_id=row["task_id"],
        cycle_id=row["cycle_id"],
        supersedes_task_id=row["supersedes_task_id"],
        phase=_parse_phase(row["phase"]),
        version=row["version"],
        baseline_commit=row["baseline_commit"],
        baseline_tree=row["baseline_tree"],
        proposal_sha256=row["proposal_sha256"],
        authorization_id=row["authorization_id"],
        worktree_path=row["worktree_path"],
        worktree_branch=row["worktree_branch"],
        actual_tree=row["actual_tree"],
        delta_sha256=row["delta_sha256"],
        terminal_reason=row["terminal_reason"],
        created_at=_decode_dt(row["created_at"]),
        updated_at=_decode_dt(row["updated_at"]),
    )


def insert_event(conn: sqlite3.Connection, event: EventRecord) -> None:
    conn.execute(
        "INSERT INTO events (event_id, aggregate_type, aggregate_id, aggregate_version, "
        "event_type, producer, payload_json, payload_sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event.event_id, event.aggregate_type.value, event.aggregate_id,
         event.aggregate_version, event.event_type, event.producer,
         event.payload_json, event.payload_sha256, _encode_dt(event.created_at)),
    )


def get_event(conn: sqlite3.Connection, event_id: str) -> EventRecord | None:
    row = conn.execute(
        "SELECT event_id, aggregate_type, aggregate_id, aggregate_version, "
        "event_type, producer, payload_json, payload_sha256, created_at "
        "FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    payload = CanonicalPayload.from_value(json.loads(row["payload_json"]))
    if payload.sha256 != row["payload_sha256"]:
        _integrity("payload sha256 mismatch")
    return EventRecord(
        event_id=row["event_id"],
        aggregate_type=_parse_aggregate(row["aggregate_type"]),
        aggregate_id=row["aggregate_id"],
        aggregate_version=row["aggregate_version"],
        event_type=row["event_type"],
        producer=row["producer"],
        payload=payload,
        created_at=_decode_dt(row["created_at"]),
    )


def cas_task_phase(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    expected_phase: TaskPhase,
    expected_version: int,
    new_phase: TaskPhase,
    terminal_reason: str | None,
    updated_at: datetime,
) -> bool:
    cur = conn.execute(
        "UPDATE tasks SET phase = ?, version = version + 1, updated_at = ?, "
        "terminal_reason = ? WHERE task_id = ? AND phase = ? AND version = ?",
        (new_phase.value, _encode_dt(updated_at), terminal_reason,
         task_id, expected_phase.value, expected_version),
    )
    return cur.rowcount == 1


def insert_approval(conn: sqlite3.Connection, approval: ApprovalRecord) -> None:
    conn.execute(
        "INSERT INTO approvals (approval_id, task_id, decision, scope_sha256, "
        "proposal_sha256, baseline_tree, source, expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (approval.approval_id, approval.task_id, approval.decision,
         approval.scope_sha256, approval.proposal_sha256, approval.baseline_tree,
         approval.source,
         _encode_dt(approval.expires_at) if approval.expires_at is not None else None,
         _encode_dt(approval.created_at)),
    )


def get_approval(conn: sqlite3.Connection, approval_id: str) -> ApprovalRecord | None:
    row = conn.execute(
        "SELECT approval_id, task_id, decision, scope_sha256, proposal_sha256, "
        "baseline_tree, source, expires_at, created_at FROM approvals "
        "WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    if row is None:
        return None
    return ApprovalRecord(
        approval_id=row["approval_id"],
        task_id=row["task_id"],
        decision=row["decision"],
        scope_sha256=row["scope_sha256"],
        proposal_sha256=row["proposal_sha256"],
        baseline_tree=row["baseline_tree"],
        source=row["source"],
        expires_at=_decode_dt(row["expires_at"]) if row["expires_at"] is not None else None,
        created_at=_decode_dt(row["created_at"]),
    )


def insert_artifact(conn: sqlite3.Connection, artifact: ArtifactRecord) -> None:
    conn.execute(
        "INSERT INTO artifacts (artifact_id, task_id, kind, relative_path, producer, "
        "sha256, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (artifact.artifact_id, artifact.task_id, artifact.kind, artifact.relative_path,
         artifact.producer, artifact.sha256, artifact.size_bytes,
         _encode_dt(artifact.created_at)),
    )


def get_artifact(conn: sqlite3.Connection, artifact_id: str) -> ArtifactRecord | None:
    row = conn.execute(
        "SELECT artifact_id, task_id, kind, relative_path, producer, sha256, "
        "size_bytes, created_at FROM artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        return None
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        task_id=row["task_id"],
        kind=row["kind"],
        relative_path=row["relative_path"],
        producer=row["producer"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        created_at=_decode_dt(row["created_at"]),
    )


def insert_agent_run(conn: sqlite3.Connection, run: AgentRunRecord) -> None:
    conn.execute(
        "INSERT INTO agent_runs (run_id, task_id, role, session_id, requested_model, "
        "resolved_model, status, input_digest, output_artifact_id, input_tokens, "
        "output_tokens, estimated_cost_usd, started_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run.run_id, run.task_id, run.role, run.session_id, run.requested_model,
         run.resolved_model, run.status, run.input_digest, run.output_artifact_id,
         run.input_tokens, run.output_tokens, _encode_money(run.estimated_cost_usd),
         _encode_dt(run.started_at),
         _encode_dt(run.ended_at) if run.ended_at is not None else None),
    )


def get_agent_run(conn: sqlite3.Connection, run_id: str) -> AgentRunRecord | None:
    row = conn.execute(
        "SELECT run_id, task_id, role, session_id, requested_model, resolved_model, "
        "status, input_digest, output_artifact_id, input_tokens, output_tokens, "
        "estimated_cost_usd, started_at, ended_at FROM agent_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return AgentRunRecord(
        run_id=row["run_id"],
        task_id=row["task_id"],
        role=row["role"],
        session_id=row["session_id"],
        requested_model=row["requested_model"],
        resolved_model=row["resolved_model"],
        status=row["status"],
        input_digest=row["input_digest"],
        output_artifact_id=row["output_artifact_id"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        estimated_cost_usd=_decode_money(row["estimated_cost_usd"]),
        started_at=_decode_dt(row["started_at"]),
        ended_at=_decode_dt(row["ended_at"]) if row["ended_at"] is not None else None,
    )


def insert_outbox(conn: sqlite3.Connection, outbox: OutboxRecord) -> None:
    conn.execute(
        "INSERT INTO outbox (message_id, topic, payload_json, status, attempts, "
        "next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (outbox.message_id, outbox.topic, outbox.payload_json, outbox.status,
         outbox.attempts,
         _encode_dt(outbox.next_attempt_at) if outbox.next_attempt_at is not None else None,
         _encode_dt(outbox.created_at)),
    )


def get_outbox(conn: sqlite3.Connection, message_id: str) -> OutboxRecord | None:
    row = conn.execute(
        "SELECT message_id, topic, payload_json, status, attempts, next_attempt_at, "
        "created_at FROM outbox WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    return OutboxRecord(
        message_id=row["message_id"],
        topic=row["topic"],
        payload_json=row["payload_json"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=_decode_dt(row["next_attempt_at"]) if row["next_attempt_at"] is not None else None,
        created_at=_decode_dt(row["created_at"]),
    )


def insert_lease(conn: sqlite3.Connection, lease: LeaseRecord) -> None:
    conn.execute(
        "INSERT INTO controller_leases (resource_id, controller_instance_id, fencing_token, "
        "acquired_at, heartbeat_at, expires_at, version) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lease.resource_id, lease.controller_instance_id, lease.fencing_token,
         _encode_dt(lease.acquired_at), _encode_dt(lease.heartbeat_at),
         _encode_dt(lease.expires_at), lease.version),
    )


def get_lease(conn: sqlite3.Connection, resource_id: str) -> LeaseRecord | None:
    row = conn.execute(
        "SELECT resource_id, controller_instance_id, fencing_token, acquired_at, "
        "heartbeat_at, expires_at, version FROM controller_leases WHERE resource_id = ?",
        (resource_id,),
    ).fetchone()
    if row is None:
        return None
    return LeaseRecord(
        resource_id=row["resource_id"],
        controller_instance_id=row["controller_instance_id"],
        fencing_token=row["fencing_token"],
        acquired_at=_decode_dt(row["acquired_at"]),
        heartbeat_at=_decode_dt(row["heartbeat_at"]),
        expires_at=_decode_dt(row["expires_at"]),
        version=row["version"],
    )


def cas_lease_heartbeat(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    controller_instance_id: str,
    fencing_token: int,
    heartbeat_at: datetime,
    expires_at: datetime,
) -> bool:
    cur = conn.execute(
        "UPDATE controller_leases SET heartbeat_at = ?, expires_at = ?, "
        "version = version + 1 "
        "WHERE resource_id = ? AND controller_instance_id = ? AND fencing_token = ? "
        "AND expires_at > ?",
        (_encode_dt(heartbeat_at), _encode_dt(expires_at), resource_id,
         controller_instance_id, fencing_token, _encode_dt(heartbeat_at)),
    )
    return cur.rowcount == 1


def cas_lease_takeover(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    controller_instance_id: str,
    next_fencing_token: int,
    acquired_at: datetime,
    heartbeat_at: datetime,
    expires_at: datetime,
    expected_version: int,
) -> bool:
    cur = conn.execute(
        "UPDATE controller_leases SET controller_instance_id = ?, "
        "fencing_token = ?, acquired_at = ?, heartbeat_at = ?, expires_at = ?, "
        "version = version + 1 "
        "WHERE resource_id = ? AND expires_at <= ? AND version = ?",
        (controller_instance_id, next_fencing_token, _encode_dt(acquired_at),
         _encode_dt(heartbeat_at), _encode_dt(expires_at), resource_id,
         _encode_dt(heartbeat_at), expected_version),
    )
    return cur.rowcount == 1


def delete_lease(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    controller_instance_id: str,
    fencing_token: int,
) -> bool:
    cur = conn.execute(
        "DELETE FROM controller_leases WHERE resource_id = ? "
        "AND controller_instance_id = ? AND fencing_token = ?",
        (resource_id, controller_instance_id, fencing_token),
    )
    return cur.rowcount == 1


def mark_outbox_delivered(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    attempts: int,
) -> bool:
    cur = conn.execute(
        "UPDATE outbox SET status = 'delivered', attempts = ?, next_attempt_at = NULL "
        "WHERE message_id = ? AND status = 'pending'",
        (attempts, message_id),
    )
    return cur.rowcount == 1


def mark_outbox_retry(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    attempts: int,
    next_attempt_at: datetime,
) -> bool:
    cur = conn.execute(
        "UPDATE outbox SET attempts = ?, next_attempt_at = ? "
        "WHERE message_id = ? AND status = 'pending'",
        (attempts, _encode_dt(next_attempt_at), message_id),
    )
    return cur.rowcount == 1


def mark_outbox_failed(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    attempts: int,
) -> bool:
    cur = conn.execute(
        "UPDATE outbox SET status = 'failed', attempts = ?, next_attempt_at = NULL "
        "WHERE message_id = ? AND status = 'pending'",
        (attempts, message_id),
    )
    return cur.rowcount == 1


# ─── M1: Approval Signature ──────────────────────────────────────────────────


def insert_approval_signature(
    conn: sqlite3.Connection,
    *,
    approval_id: str,
    signature: str,  # hex string
    public_key: str,  # hex string
    created_at: datetime,
) -> None:
    conn.execute("""
        INSERT INTO approval_signatures (approval_id, signature, public_key, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(approval_id) DO UPDATE SET
            signature=excluded.signature,
            public_key=excluded.public_key,
            created_at=excluded.created_at
    """, (approval_id, signature, public_key, _encode_dt(created_at)))


def get_approval_signature(
    conn: sqlite3.Connection,
    approval_id: str,
) -> tuple[str, str, str] | None:
    """返回 (signature_hex, public_key_hex, created_at) 或 None。"""
    row = conn.execute(
        "SELECT signature, public_key, created_at FROM approval_signatures WHERE approval_id=?",
        (approval_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["signature"], row["public_key"], row["created_at"])
