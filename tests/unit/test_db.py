"""M0-A1c1 SQLite schema & connection 测试。

所有 DB 一律使用 pytest tmp_path 临时目录；不访问网络/时钟/环境秘密。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from aotf import db


def _fresh_db(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    return conn


def test_schema_sql_defines_all_tables() -> None:
    names = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db.SCHEMA_SQL)
    assert names == [
        "cycles", "tasks", "events", "approvals",
        "artifacts", "agent_runs", "outbox", "controller_leases",
    ]


def test_init_db_creates_all_tables(tmp_path) -> None:
    conn = _fresh_db(tmp_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    assert [r["name"] for r in rows] == [
        "agent_runs", "approvals", "artifacts", "controller_leases",
        "cycles", "events", "outbox", "tasks",
    ]
    conn.close()


def test_connect_enables_foreign_keys(tmp_path) -> None:
    conn = _fresh_db(tmp_path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_connect_uses_wal_journal_mode(tmp_path) -> None:
    conn = _fresh_db(tmp_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_connect_sets_busy_timeout(tmp_path) -> None:
    conn = _fresh_db(tmp_path)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()


def test_connect_row_factory_is_sqlite_row(tmp_path) -> None:
    conn = _fresh_db(tmp_path)
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_init_db_is_idempotent(tmp_path) -> None:
    conn = _fresh_db(tmp_path)
    db.init_db(conn)
    conn.close()


@pytest.mark.parametrize("parents,child_violate,child_valid", [
    (("INSERT INTO cycles (cycle_id, project_id, status, created_at, updated_at) "
      "VALUES ('c-ok','p-1','ACTIVE','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')",),
     "INSERT INTO tasks (task_id, cycle_id, phase, created_at, updated_at) "
     "VALUES ('t-x','c-missing','CREATED','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')",
     "INSERT INTO tasks (task_id, cycle_id, phase, created_at, updated_at) "
     "VALUES ('t-ok','c-ok','CREATED','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')"),
    (("INSERT INTO cycles (cycle_id, project_id, status, created_at, updated_at) "
      "VALUES ('c-ok','p-1','ACTIVE','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')",
      "INSERT INTO tasks (task_id, cycle_id, phase, created_at, updated_at) "
      "VALUES ('t-ok','c-ok','CREATED','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')"),
     "INSERT INTO approvals (approval_id, task_id, decision, scope_sha256, proposal_sha256, "
     "baseline_tree, source, created_at) VALUES ('a-x','t-missing','APPROVED',"
     "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
     "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',"
     "'cccccccccccccccccccccccccccccccccccccccc',"
     "'human','2026-09-01T00:00:00+00:00')",
     "INSERT INTO approvals (approval_id, task_id, decision, scope_sha256, proposal_sha256, "
     "baseline_tree, source, created_at) VALUES ('a-ok','t-ok','APPROVED',"
     "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
     "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',"
     "'cccccccccccccccccccccccccccccccccccccccc',"
     "'human','2026-09-01T00:00:00+00:00')"),
])
def test_foreign_keys_are_enforced(tmp_path, parents, child_violate, child_valid) -> None:
    conn = _fresh_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(child_violate)
    for sql in parents:
        conn.execute(sql)
    conn.execute(child_valid)
    conn.commit()
    conn.close()


def test_wal_mode_creates_sidecar_in_temp(tmp_path) -> None:
    conn = _fresh_db(tmp_path)
    conn.execute("INSERT INTO cycles (cycle_id, project_id, status, created_at, updated_at) "
                 "VALUES ('c-1','p-1','ACTIVE','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')")
    conn.commit()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert any(f.endswith(".db-wal") for f in files) or "t.db-wal" in files
    assert (tmp_path / "t.db").exists()
    conn.close()
