"""AOTF SQLite schema and connection (A1c1).

实现权威架构 §6.1/6.2 的 SQLite 基础：SCHEMA_SQL 定义八张表（§6.2 七表
cycles/tasks/events/approvals/artifacts/agent_runs/outbox + A3a1
controller_leases，CREATE IF NOT EXISTS）；
connect() 建立连接并启用 foreign_keys / WAL / busy_timeout / Row；
init_db() 幂等建表。不含 typed record 读写、事件账本或状态机（A1c2/A2）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cycles (
  cycle_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  status TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES cycles(cycle_id),
  supersedes_task_id TEXT REFERENCES tasks(task_id),
  phase TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0,
  baseline_commit TEXT,
  baseline_tree TEXT,
  proposal_sha256 TEXT,
  authorization_id TEXT,
  worktree_path TEXT,
  worktree_branch TEXT,
  actual_tree TEXT,
  delta_sha256 TEXT,
  terminal_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  aggregate_version INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  producer TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(aggregate_type, aggregate_id, aggregate_version)
);

CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  decision TEXT NOT NULL,
  scope_sha256 TEXT NOT NULL,
  proposal_sha256 TEXT NOT NULL,
  baseline_tree TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source = 'human'),
  expires_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  kind TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  producer TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(task_id, kind, sha256)
);

CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  role TEXT NOT NULL,
  session_id TEXT,
  requested_model TEXT NOT NULL,
  resolved_model TEXT,
  status TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  output_artifact_id TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  estimated_cost_usd REAL,
  started_at TEXT NOT NULL,
  ended_at TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
  message_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS controller_leases (
  resource_id TEXT PRIMARY KEY,
  controller_instance_id TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0
);

-- M1: Execution Snapshot
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('implementer', 'reviewer')),
  phase_before TEXT NOT NULL,
  phase_after TEXT NOT NULL,
  worktree_path TEXT NOT NULL,
  baseline_tree TEXT NOT NULL,
  actual_tree TEXT,
  mutations_applied TEXT,
  delta_sha256 TEXT,
  evidence_json TEXT,
  review_verdict TEXT CHECK(review_verdict IN ('PASS', 'CONCERNS', 'BLOCK')),
  run_inputs_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- M1: Approval signature storage
CREATE TABLE IF NOT EXISTS approval_signatures (
  approval_id TEXT PRIMARY KEY REFERENCES approvals(approval_id),
  signature TEXT NOT NULL,
  public_key TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

BUSY_TIMEOUT_MS = 5000


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with schema-required pragmas applied."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Idempotently create all tables from SCHEMA_SQL."""
    conn.executescript(SCHEMA_SQL)
