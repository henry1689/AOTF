"""M0-A5a CLI doctor/health 只读命令测试。

DB 一律 pytest tmp_path；无网络/时钟；doctor/health 只读（mtime/内容不变）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aotf import cli, db, state, store
from aotf.models import CycleRecord, OutboxRecord, TaskPhase, TaskRecord

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 1, 1, 0, 0, tzinfo=timezone.utc)

_MAIN = [
    TaskPhase.BASELINING, TaskPhase.PLAN_READY, TaskPhase.AWAITING_PLAN_APPROVAL,
    TaskPhase.EDIT_AUTHORIZED, TaskPhase.IMPLEMENTING, TaskPhase.DELTA_CAPTURED,
    TaskPhase.EVIDENCE_RUNNING, TaskPhase.REVIEWING, TaskPhase.POLICY_EVALUATING,
]


def _drive(conn):
    for i, ph in enumerate(_MAIN, start=1):
        state.transition(conn, task_id="task-min", to_phase=ph,
                         terminal_reason=None, event_id=f"c-{i}", at=T1)


def _task_min():
    return TaskRecord(
        task_id="task-min", cycle_id="cycle-1", supersedes_task_id=None,
        phase=TaskPhase.CREATED, version=0, baseline_commit=None,
        baseline_tree=None, proposal_sha256=None, authorization_id=None,
        worktree_path=None, worktree_branch=None, actual_tree=None,
        delta_sha256=None, terminal_reason=None, created_at=T0, updated_at=T0,
    )


def _write_db(path, kind="ok"):
    conn = db.connect(path)
    db.init_db(conn)
    store.insert_cycle(conn, CycleRecord(
        cycle_id="cycle-1", project_id="project-1", status="ACTIVE",
        version=0, created_at=T0, updated_at=T0,
    ))
    store.insert_task(conn, _task_min())
    conn.commit()
    if kind == "drift":
        state.transition(conn, task_id="task-min", to_phase=TaskPhase.BASELINING,
                         terminal_reason=None, event_id="evt-1", at=T1)
        conn.commit()
        conn.execute("DELETE FROM events WHERE event_id = 'evt-1'")
        conn.commit()
    elif kind == "halt":
        state.transition(conn, task_id="task-min", to_phase=TaskPhase.SAFE_HALT,
                         terminal_reason="halted", event_id="evt-h", at=T1)
        conn.commit()
    elif kind == "outbox_failed":
        store.insert_outbox(conn, OutboxRecord(
            message_id="msg-1", topic="task.completed", payload_json="{}",
            status="pending", attempts=0, next_attempt_at=None, created_at=T0,
        ))
        conn.commit()
        store.mark_outbox_failed(conn, message_id="msg-1", attempts=1)
        conn.commit()
    elif kind == "failed":
        _drive(conn)
        state.transition(conn, task_id="task-min", to_phase=TaskPhase.FAILED,
                         terminal_reason="blocked", event_id="c-f", at=T1)
        conn.commit()
    elif kind == "completed":
        _drive(conn)
        state.transition(conn, task_id="task-min",
                         to_phase=TaskPhase.CHECKPOINT_READY,
                         terminal_reason=None, event_id="c-10", at=T1)
        state.transition(conn, task_id="task-min", to_phase=TaskPhase.COMPLETED,
                         terminal_reason="accepted", event_id="c-d", at=T1)
        conn.commit()
    conn.close()
    return path


def _invoke(capsys, argv):
    code = cli.run(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _db_path(tmp_path, kind="ok", name="t.db"):
    return _write_db(str(tmp_path / name), kind)


def test_doctor_ok_on_consistent_db(tmp_path, capsys) -> None:
    path = _db_path(tmp_path)
    code, out, _ = _invoke(capsys, ["doctor", path])
    payload = json.loads(out)
    assert code == 0
    assert payload["ok"] is True
    assert {c["name"] for c in payload["checks"]} == {
        "tables", "tasks_consistent", "outbox_no_failed",
    }


def test_doctor_reports_drift(tmp_path, capsys) -> None:
    path = _db_path(tmp_path, kind="drift")
    code, out, _ = _invoke(capsys, ["doctor", path])
    payload = json.loads(out)
    assert code == 0
    assert payload["ok"] is False
    by_name = {c["name"]: c for c in payload["checks"]}
    assert by_name["tasks_consistent"]["ok"] is False
    assert by_name["tasks_consistent"]["detail"] == "drifts=['task-min']"


@pytest.mark.parametrize("kind,expected", [
    ("ok", "healthy"),
    ("drift", "degraded"),
    ("outbox_failed", "degraded"),
    ("halt", "safe_halt"),
])
def test_health_healthy_degraded_safe_halt(tmp_path, capsys, kind, expected) -> None:
    path = _db_path(tmp_path, kind=kind)
    code, out, _ = _invoke(capsys, ["health", path])
    payload = json.loads(out)
    assert code == 0
    assert payload["status"] == expected


def test_run_missing_db_file_returns_one(tmp_path, capsys) -> None:
    code, _, err = _invoke(capsys, ["doctor", str(tmp_path / "nope.db")])
    assert code == 1
    assert json.loads(err)["error"]


def test_run_unknown_command_returns_two(tmp_path, capsys) -> None:
    code, _, err = _invoke(capsys, ["frobnicate", str(tmp_path)])
    assert code == 2
    assert json.loads(err)["error"]


def test_doctor_does_not_modify_db(tmp_path, capsys) -> None:
    path = _db_path(tmp_path)

    def snapshot():
        return {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}

    before = snapshot()
    code, out_a, _ = _invoke(capsys, ["doctor", path])
    assert code == 0
    code_b, out_b, _ = _invoke(capsys, ["doctor", path])
    assert code_b == 0
    after = snapshot()
    assert before == after
    assert out_a == out_b
    payload = json.loads(out_a)
    assert payload["ok"] is True


def test_run_json_machine_parseable_and_sorted(tmp_path, capsys) -> None:
    path = _db_path(tmp_path)
    code, out, _ = _invoke(capsys, ["health", path])
    payload = json.loads(out)
    assert code == 0
    assert list(payload) == sorted(payload)


def test_task_status_ok(tmp_path, capsys) -> None:
    path = _db_path(tmp_path)
    code, out, _ = _invoke(capsys, ["task", "status", path, "task-min"])
    payload = json.loads(out)
    assert code == 0
    assert payload["task_id"] == "task-min"
    assert payload["phase"] == "CREATED"
    assert payload["verdict"] == "CONSISTENT"


def test_task_status_missing_error(tmp_path, capsys) -> None:
    path = _db_path(tmp_path)
    code, _, err = _invoke(capsys, ["task", "status", path, "task-nope"])
    assert code == 1
    assert json.loads(err)["error"]


def test_recover_proposes_successor_read_only(tmp_path, capsys) -> None:
    path = _db_path(tmp_path, kind="failed")
    from pathlib import Path as P
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    code, out, _ = _invoke(capsys, ["recover", path, "task-min"])
    payload = json.loads(out)
    assert code == 0
    assert payload["action"] == "propose_successor"
    assert payload["successor_task_id"] == "task-min-retry"
    assert payload["phase"] == "FAILED"
    after = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    assert before == after
    conn = db.connect(path)
    names = [r["task_id"] for r in conn.execute("SELECT task_id FROM tasks")]
    conn.close()
    assert names == ["task-min"]


@pytest.mark.parametrize("kind", ["ok", "completed"])
def test_recover_action_none_param(tmp_path, capsys, kind) -> None:
    path = _db_path(tmp_path, kind=kind)
    code, out, _ = _invoke(capsys, ["recover", path, "task-min"])
    payload = json.loads(out)
    assert code == 0
    assert payload["action"] == "none"
    assert payload["reason"]


def test_recover_action_blocked_on_drift(tmp_path, capsys) -> None:
    path = _db_path(tmp_path, kind="drift")
    code, out, _ = _invoke(capsys, ["recover", path, "task-min"])
    payload = json.loads(out)
    assert code == 0
    assert payload["action"] == "blocked"
    assert payload["reason"] == "ledger drift"


@pytest.mark.parametrize("reason,expected", [
    (None, "cancelled by operator"),
    ("operator requested", "operator requested"),
])
def test_cancel_transitions_and_records_reason(tmp_path, capsys,
                                               reason, expected) -> None:
    path = _db_path(tmp_path)
    argv = ["cancel", path, "task-min"] if reason is None else \
        ["cancel", path, "task-min", reason]
    code, out, _ = _invoke(capsys, argv)
    payload = json.loads(out)
    assert code == 0
    assert payload["phase"] == "CANCELLED"
    assert payload["terminal_reason"] == expected
    conn = db.connect(path)
    t = store.get_task(conn, "task-min")
    conn.close()
    assert t.phase is TaskPhase.CANCELLED


@pytest.mark.parametrize("kind", ["completed", "missing-db"])
def test_cancel_rejects_terminal_or_missing_db(tmp_path, capsys, kind) -> None:
    if kind == "completed":
        path = _db_path(tmp_path, kind="completed")
        code, _, err = _invoke(capsys, ["cancel", path, "task-min"])
        assert code == 1
        assert json.loads(err)["error"]
    else:
        missing = str(tmp_path / "nope.db")
        code, _, err = _invoke(capsys, ["cancel", missing, "task-min"])
        assert code == 1
        assert json.loads(err)["error"]
        assert not (tmp_path / "nope.db").exists()


def test_unicode_db_path_command_ok(tmp_path, capsys) -> None:
    sub = tmp_path / "子 目录"
    sub.mkdir()
    path = str(sub / "测 试.db")
    _write_db(path)
    code, out, _ = _invoke(capsys, ["task", "status", path, "task-min"])
    payload = json.loads(out)
    assert code == 0
    assert payload["phase"] == "CREATED"


def test_doctor_rejects_non_aotf_file(tmp_path, capsys) -> None:
    bad = tmp_path / "not.db"
    bad.write_text("this is not a sqlite database", encoding="utf-8", newline="")
    code, _, err = _invoke(capsys, ["doctor", str(bad)])
    assert code == 1
    assert json.loads(err)["error"]
