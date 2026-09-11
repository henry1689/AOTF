"""AOTF Snapshot 单元测试（M1）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from aotf import db
from aotf.snapshot import (
    SnapshotRecord,
    SnapshotError,
    delete_snapshots_for_task,
    load_snapshot,
    save_snapshot,
)


def _make_record(
    task_id: str = "task-1",
    role: str = "implementer",
    **overrides,
) -> SnapshotRecord:
    """构建 SnapshotRecord，支持通过 overrides 覆盖字段。"""
    now = datetime.now(timezone.utc)
    kwargs = dict(
        snapshot_id=f"snap-{task_id}-{role}",
        task_id=task_id,
        role=role,
        phase_before="EDIT_AUTHORIZED",
        phase_after="IMPLEMENTING",
        worktree_path="/tmp/worktree-test",
        baseline_tree="abc123" + "0" * 34,
        actual_tree=None,
        mutations_applied=None,
        delta_sha256=None,
        evidence_json=None,
        review_verdict=None,
        run_inputs_json=json.dumps({"proposal": "test"}),
        created_at=now,
        updated_at=now,
    )
    kwargs.update(overrides)
    return SnapshotRecord(**kwargs)


class TestSnapshotValidation:
    """SnapshotRecord 校验规则。"""

    def test_valid_record(self):
        r = _make_record()
        assert r.snapshot_id == "snap-task-1-implementer"
        assert r.role == "implementer"

    def test_empty_snapshot_id_raises(self):
        with pytest.raises(SnapshotError, match="snapshot_id required"):
            _make_record(snapshot_id="")

    def test_invalid_role_raises(self):
        with pytest.raises(SnapshotError, match="invalid role"):
            _make_record(role="unknown")

    def test_empty_phase_raises(self):
        with pytest.raises(SnapshotError, match="phase_before required"):
            _make_record(phase_before="")

    def test_empty_worktree_raises(self):
        with pytest.raises(SnapshotError, match="worktree_path required"):
            _make_record(worktree_path="")

    def test_whitespace_worktree_raises(self):
        with pytest.raises(SnapshotError, match="whitespace"):
            _make_record(worktree_path=" /tmp/foo ")

    def test_invalid_actual_tree_raises(self):
        with pytest.raises(SnapshotError, match="actual_tree"):
            _make_record(actual_tree="not-a-hash")

    def test_none_actual_tree_ok(self):
        r = _make_record(actual_tree=None)
        assert r.actual_tree is None

    def test_invalid_delta_sha_raises(self):
        with pytest.raises(SnapshotError, match="delta_sha256"):
            _make_record(delta_sha256="short")

    def test_invalid_review_verdict_raises(self):
        with pytest.raises(SnapshotError, match="review_verdict"):
            _make_record(review_verdict="UNKNOWN")

    def test_valid_review_verdicts(self):
        for v in ("PASS", "CONCERNS", "BLOCK", None):
            r = _make_record(review_verdict=v)
            assert r.review_verdict == v

    def test_empty_run_inputs_raises(self):
        with pytest.raises(SnapshotError, match="run_inputs_json"):
            _make_record(run_inputs_json="")

    def test_invalid_json_in_run_inputs_raises(self):
        with pytest.raises(SnapshotError, match="run_inputs_json"):
            _make_record(run_inputs_json="not-json")

    def test_updated_before_created_raises(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(SnapshotError, match="created_at"):
            SnapshotRecord(
                snapshot_id="snap-x", task_id="t", role="implementer",
                phase_before="p1", phase_after="p2",
                worktree_path="/tmp/wt", baseline_tree="a" * 40,
                actual_tree=None, mutations_applied=None,
                delta_sha256=None, evidence_json=None, review_verdict=None,
                run_inputs_json="{}",
                created_at=now + timedelta(hours=1),
                updated_at=now,
            )

    def test_mutations_applied_tuple_required(self):
        with pytest.raises(SnapshotError, match="mutations_applied"):
            _make_record(mutations_applied="not-a-tuple")

    def test_empty_mutations_path_raises(self):
        with pytest.raises(SnapshotError, match="mutation path"):
            _make_record(mutations_applied=("", "valid"))


class TestSnapshotCRUD:
    """持久化 CRUD。"""

    @pytest.fixture
    def conn(self, tmp_path):
        db_path = tmp_path / "test.db"
        c = db.connect(db_path)
        db.init_db(c)
        yield c
        c.close()

    def test_save_and_load(self, conn):
        r = _make_record()
        save_snapshot(conn, r)
        loaded = load_snapshot(conn, "task-1", "implementer")
        assert loaded is not None
        assert loaded.snapshot_id == r.snapshot_id
        assert loaded.task_id == r.task_id
        assert loaded.role == r.role
        assert loaded.phase_before == r.phase_before
        assert loaded.worktree_path == r.worktree_path

    def test_upsert_updates_existing(self, conn):
        r1 = _make_record()
        save_snapshot(conn, r1)
        r2 = _make_record(phase_after="DELTA_CAPTURED", actual_tree="def456" + "0" * 34)
        save_snapshot(conn, r2)
        loaded = load_snapshot(conn, "task-1", "implementer")
        assert loaded.phase_after == "DELTA_CAPTURED"
        assert loaded.actual_tree == "def456" + "0" * 34

    def test_load_nonexistent(self, conn):
        assert load_snapshot(conn, "nonexistent", "implementer") is None

    def test_multiple_roles(self, conn):
        snap_impl = _make_record(role="implementer", snapshot_id="snap-impl")
        snap_rev = _make_record(role="reviewer", snapshot_id="snap-rev")
        save_snapshot(conn, snap_impl)
        save_snapshot(conn, snap_rev)
        impl = load_snapshot(conn, "task-1", "implementer")
        rev = load_snapshot(conn, "task-1", "reviewer")
        assert impl is not None
        assert rev is not None
        assert impl.snapshot_id != rev.snapshot_id

    def test_delete_snapshots(self, conn):
        r = _make_record()
        save_snapshot(conn, r)
        count = delete_snapshots_for_task(conn, "task-1")
        assert count == 1
        assert load_snapshot(conn, "task-1", "implementer") is None

    def test_delete_nonexistent(self, conn):
        count = delete_snapshots_for_task(conn, "nonexistent")
        assert count == 0


class TestSchema:
    """DB schema 正确性。"""

    def test_snapshots_table_exists(self, tmp_path):
        db_path = tmp_path / "test.db"
        c = db.connect(db_path)
        db.init_db(c)
        cur = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='snapshots'")
        assert cur.fetchone() is not None

    def test_approval_signatures_table_exists(self, tmp_path):
        db_path = tmp_path / "test.db"
        c = db.connect(db_path)
        db.init_db(c)
        cur = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='approval_signatures'")
        assert cur.fetchone() is not None
