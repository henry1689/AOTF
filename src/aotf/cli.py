"""AOTF CLI (A5a + A5b).

手写最小分发（不依赖 argparse，保持确定性）：doctor/health/task status/
recover/cancel。全部输出机器可解析 JSON（sort_keys=True）。doctor/health/
task status/recover 走 mode=ro immutable 只读（§9.6 #35/#36/#37，不写库、
不重建 sidecar）；cancel 是唯一可写命令（缺失 DB 不创建文件）。recover 只提
方案不落库（§16）。文档收口归 A5c。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from aotf import db, recovery, state, store
from aotf.errors import AotfError
from aotf.models import TaskPhase

_EXPECTED_TABLES = frozenset({
    "agent_runs", "approvals", "approval_signatures", "artifacts",
    "controller_leases", "cycles", "events", "outbox", "snapshots", "tasks",
})


def _file_uri(path: str) -> str:
    raw = str(Path(path).resolve()).replace("\\", "/")
    return "file:///" + quote(raw, safe="/:")


def _open_ro(db_path: str) -> sqlite3.Connection:
    # ro+immutable：只读且不重建 WAL/-shm sidecar（§9.6 #37 不改 DB）。
    # 局限：崩溃残留的未 checkpoint WAL 帧不读——真实恢复面属 A5b/A3c。
    conn = sqlite3.connect(_file_uri(db_path) + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scan(conn: sqlite3.Connection) -> dict:
    names = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'",
        )
    }
    drifts = []
    for row in conn.execute("SELECT task_id FROM tasks"):
        diagnosis = recovery.diagnose_task(conn, task_id=row["task_id"])
        if diagnosis.verdict == "DRIFT":
            drifts.append(row["task_id"])
    failed = conn.execute(
        "SELECT count(*) FROM outbox WHERE status = 'failed'",
    ).fetchone()[0]
    return {"tables": names, "drifts": drifts, "outbox_failed": failed}


def doctor(db_path: str) -> dict:
    conn = _open_ro(db_path)
    try:
        scan = _scan(conn)
    finally:
        conn.close()
    checks = [
        {
            "name": "tables",
            "ok": scan["tables"] == _EXPECTED_TABLES,
            "detail": "8 tables present" if scan["tables"] == _EXPECTED_TABLES
            else f"missing={sorted(_EXPECTED_TABLES - scan['tables'])}",
        },
        {
            "name": "tasks_consistent",
            "ok": not scan["drifts"],
            "detail": f"drifts={scan['drifts']}",
        },
        {
            "name": "outbox_no_failed",
            "ok": scan["outbox_failed"] == 0,
            "detail": f"failed={scan['outbox_failed']}",
        },
    ]
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "db": db_path}


def health(db_path: str) -> dict:
    conn = _open_ro(db_path)
    try:
        halted = conn.execute(
            "SELECT count(*) FROM tasks WHERE phase = ?",
            (TaskPhase.SAFE_HALT.value,),
        ).fetchone()[0]
        scan = _scan(conn) if halted == 0 else None
    finally:
        conn.close()
    if halted > 0:
        return {"status": "safe_halt", "reason": "task in SAFE_HALT"}
    if scan is None:
        scan = {"drifts": [], "outbox_failed": 0}
    if scan["drifts"]:
        return {"status": "degraded", "reason": f"drift tasks={scan['drifts']}"}
    if scan["outbox_failed"]:
        return {"status": "degraded", "reason": "outbox has failed messages"}
    return {"status": "healthy", "reason": "all checks passed"}


def _open_rw(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise ValueError(f"db file not found: {db_path}")
    return db.connect(db_path)


def task_status(db_path: str, task_id: str) -> dict:
    conn = _open_ro(db_path)
    try:
        row = store.get_task(conn, task_id)
        if row is None:
            raise ValueError(f"task not found: {task_id}")
        diagnosis = recovery.diagnose_task(conn, task_id=task_id)
    finally:
        conn.close()
    return {
        "task_id": row.task_id,
        "phase": row.phase.value,
        "version": row.version,
        "terminal_reason": row.terminal_reason,
        "verdict": diagnosis.verdict,
    }


def recover(db_path: str, task_id: str) -> dict:
    conn = _open_ro(db_path)
    try:
        diagnosis = recovery.diagnose_task(conn, task_id=task_id)
    finally:
        conn.close()
    base = {"task_id": task_id}
    if diagnosis.verdict == "MISSING":
        return {**base, "action": "blocked", "reason": "task missing"}
    if diagnosis.verdict == "DRIFT":
        return {**base, "action": "blocked", "reason": "ledger drift"}
    assert diagnosis.phase is not None
    if diagnosis.recoverable_terminal:
        return {
            **base, "action": "propose_successor",
            "successor_task_id": f"{task_id}-retry",
            "supersedes_task_id": task_id,
            "phase": diagnosis.phase.value,
        }
    if diagnosis.is_terminal:
        return {**base, "action": "none",
                "phase": diagnosis.phase.value,
                "reason": "terminal not recoverable"}
    return {**base, "action": "none", "phase": diagnosis.phase.value,
            "reason": "task not terminal"}


def cancel(db_path: str, task_id: str,
           reason: str = "cancelled by operator") -> dict:
    conn = _open_rw(db_path)
    try:
        row = store.get_task(conn, task_id)
        if row is None:
            raise ValueError(f"task not found: {task_id}")
        task = state.transition(
            conn, task_id=task_id, to_phase=TaskPhase.CANCELLED,
            terminal_reason=reason,
            event_id=f"cli-cancel-{task_id}-v{row.version}",
            at=datetime.now(timezone.utc),
        )
    finally:
        conn.close()
    return {"task_id": task.task_id, "phase": task.phase.value,
            "terminal_reason": task.terminal_reason}


def _usage_error() -> int:
    print(json.dumps({"error": "usage: aotf <doctor|health|recover|cancel> <db> "
                               "[args...] | aotf task status <db> <task-id>"},
                     sort_keys=True), file=sys.stderr)
    return 2


def _fail(exc: Exception) -> int:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                     sort_keys=True), file=sys.stderr)
    return 1


def run(argv: list[str]) -> int:
    if not argv:
        return _usage_error()
    cmd = argv[0]
    try:
        if cmd in ("doctor", "health") and len(argv) == 2:
            payload = doctor(argv[1]) if cmd == "doctor" else health(argv[1])
        elif cmd == "task" and len(argv) == 4 and argv[1] == "status":
            payload = task_status(argv[2], argv[3])
        elif cmd == "recover" and len(argv) == 3:
            payload = recover(argv[1], argv[2])
        elif cmd == "cancel" and len(argv) in (3, 4):
            reason = argv[3] if len(argv) == 4 else "cancelled by operator"
            payload = cancel(argv[1], argv[2], reason)
        else:
            return _usage_error()
    except (sqlite3.Error, OSError, ValueError, AotfError) as exc:
        return _fail(exc)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return 0
