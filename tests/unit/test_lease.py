"""M0-A3a4 lease/fencing 服务测试。

DB 一律使用 pytest tmp_path；不访问网络/时钟/环境秘密；时间全用显式 at。
服务函数不 commit——单连接内自见其写；跨连接场景显式 commit 模拟真实编排。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aotf import db, lease, store
from aotf.errors import AotfError, ErrorCode
from aotf.models import LeaseRecord

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
DUR = 60
RES = "res-task-1"
A = "controller-a"
B = "controller-b"


def _db(tmp_path, name="t.db"):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    return conn


def _expect(code, fn):
    with pytest.raises(AotfError) as exc:
        fn()
    assert exc.value.code is code


def test_acquire_creates_lease_with_token_one(tmp_path) -> None:
    conn = _db(tmp_path)
    got = lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    assert got == LeaseRecord(
        resource_id=RES, controller_instance_id=A, fencing_token=1,
        acquired_at=T0, heartbeat_at=T0, expires_at=T0 + timedelta(seconds=DUR),
        version=0,
    )
    conn.close()


def test_acquire_conflict_when_active(tmp_path) -> None:
    conn = _db(tmp_path)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    _expect(ErrorCode.LEASE_CONFLICT, lambda: lease.acquire(
        conn, resource_id=RES, holder=B, lease_seconds=DUR, at=T0))
    assert store.get_lease(conn, RES).controller_instance_id == A
    conn.close()


def test_acquire_takeover_when_expired(tmp_path) -> None:
    conn = _db(tmp_path)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    later = T0 + timedelta(seconds=DUR + 1)
    got = lease.acquire(conn, resource_id=RES, holder=B,
                        lease_seconds=DUR, at=later)
    assert got.fencing_token == 2
    assert got.version == 1
    assert got.controller_instance_id == B
    conn.close()


@pytest.mark.parametrize("seconds", [0, -1])
def test_acquire_rejects_invalid_lease_seconds(tmp_path, seconds) -> None:
    conn = _db(tmp_path)
    _expect(ErrorCode.INVALID_INPUT, lambda: lease.acquire(
        conn, resource_id=RES, holder=A, lease_seconds=seconds, at=T0))
    conn.close()


def test_heartbeat_renews_within_lease(tmp_path) -> None:
    conn = _db(tmp_path)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    at = T0 + timedelta(seconds=30)
    got = lease.heartbeat(conn, resource_id=RES, holder=A, fencing_token=1,
                          lease_seconds=DUR, at=at)
    assert got.version == 1
    assert got.fencing_token == 1
    assert got.heartbeat_at == at
    assert got.expires_at == at + timedelta(seconds=DUR)
    conn.close()


def test_heartbeat_stale_after_takeover(tmp_path) -> None:
    conn = _db(tmp_path)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    later = T0 + timedelta(seconds=DUR + 1)
    lease.acquire(conn, resource_id=RES, holder=B, lease_seconds=DUR, at=later)
    _expect(ErrorCode.STALE_FENCING_TOKEN, lambda: lease.heartbeat(
        conn, resource_id=RES, holder=A, fencing_token=1,
        lease_seconds=DUR, at=later))
    conn.close()


def test_heartbeat_conflict_when_expired_untaken(tmp_path) -> None:
    conn = _db(tmp_path)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    later = T0 + timedelta(seconds=DUR + 1)
    _expect(ErrorCode.LEASE_CONFLICT, lambda: lease.heartbeat(
        conn, resource_id=RES, holder=A, fencing_token=1,
        lease_seconds=DUR, at=later))
    conn.close()


def test_heartbeat_missing_raises_lease_conflict(tmp_path) -> None:
    conn = _db(tmp_path)
    _expect(ErrorCode.LEASE_CONFLICT, lambda: lease.heartbeat(
        conn, resource_id=RES, holder=A, fencing_token=1,
        lease_seconds=DUR, at=T0))
    conn.close()


def test_release_idempotent_and_defensive(tmp_path) -> None:
    conn = _db(tmp_path)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    lease.release(conn, resource_id=RES, holder=A, fencing_token=1)
    assert store.get_lease(conn, RES) is None
    lease.release(conn, resource_id=RES, holder=A, fencing_token=1)
    later = T0 + timedelta(seconds=DUR + 1)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    lease.acquire(conn, resource_id=RES, holder=B, lease_seconds=DUR, at=later)
    _expect(ErrorCode.STALE_FENCING_TOKEN, lambda: lease.release(
        conn, resource_id=RES, holder=A, fencing_token=1))
    assert store.get_lease(conn, RES).controller_instance_id == B
    conn.close()


def test_check_held_passes_while_held(tmp_path) -> None:
    conn = _db(tmp_path)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    lease.check_held(conn, resource_id=RES, holder=A, fencing_token=1,
                     at=T0 + timedelta(seconds=30))
    conn.close()


def test_check_held_stale_after_takeover(tmp_path) -> None:
    conn = _db(tmp_path)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    later = T0 + timedelta(seconds=DUR + 1)
    lease.acquire(conn, resource_id=RES, holder=B, lease_seconds=DUR, at=later)
    _expect(ErrorCode.STALE_FENCING_TOKEN, lambda: lease.check_held(
        conn, resource_id=RES, holder=A, fencing_token=1, at=later))
    conn.close()


def test_check_held_conflict_expired_or_unleased(tmp_path) -> None:
    conn = _db(tmp_path)
    lease.acquire(conn, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    later = T0 + timedelta(seconds=DUR + 1)
    _expect(ErrorCode.LEASE_CONFLICT, lambda: lease.check_held(
        conn, resource_id=RES, holder=A, fencing_token=1, at=later))
    fresh = _db(tmp_path, "fresh.db")
    _expect(ErrorCode.LEASE_CONFLICT, lambda: lease.check_held(
        fresh, resource_id=RES, holder=A, fencing_token=1, at=T0))
    fresh.close()
    conn.close()


def test_dual_controllers_single_lease_and_fencing(tmp_path) -> None:
    path = tmp_path / "dual.db"
    ca = _db(tmp_path, "dual.db")
    cb = db.connect(path)
    db.init_db(cb)
    a = lease.acquire(ca, resource_id=RES, holder=A, lease_seconds=DUR, at=T0)
    ca.commit()
    assert a.fencing_token == 1
    _expect(ErrorCode.LEASE_CONFLICT, lambda: lease.acquire(
        cb, resource_id=RES, holder=B, lease_seconds=DUR, at=T0))
    later = T0 + timedelta(seconds=DUR + 1)
    b = lease.acquire(cb, resource_id=RES, holder=B, lease_seconds=DUR, at=later)
    cb.commit()
    assert b.fencing_token == 2
    _expect(ErrorCode.STALE_FENCING_TOKEN, lambda: lease.check_held(
        ca, resource_id=RES, holder=A, fencing_token=1, at=later))
    ca.close()
    cb.close()
