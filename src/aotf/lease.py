"""AOTF lease/fencing service (A3a4).

在 controller_leases 原子原语之上编排 controller 租约语义与 fencing 裁决：
acquire / heartbeat / release / check_held。判定顺序为代际先于过期/持有者
——被 takeover 取代的旧代际收 STALE_FENCING_TOKEN（fencing），未租/过期/
他人持有收 LEASE_CONFLICT。本模块不 commit（调用方事务）；无时钟/网络；
不做 transactional outbox / recovery（A3b/A3c）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from aotf import store
from aotf.errors import AotfError, ErrorCode
from aotf.models import LeaseRecord

_R = "resource_id"
_H = "controller_instance_id"


def _err(code, message, details):
    raise AotfError(code, message, details)


def _valid_at(at):
    if type(at) is not datetime or at.utcoffset() != timedelta(0):
        _err(ErrorCode.INVALID_INPUT, "at must be aware UTC datetime", {"field": "at"})


def _valid_duration(lease_seconds):
    if type(lease_seconds) is not int or lease_seconds < 1:
        _err(ErrorCode.INVALID_INPUT, "lease_seconds must be positive int",
             {"field": "lease_seconds"})


def acquire(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    holder: str,
    lease_seconds: int,
    at: datetime,
) -> LeaseRecord:
    _valid_at(at)
    _valid_duration(lease_seconds)
    expires = at + timedelta(seconds=lease_seconds)
    existing = store.get_lease(conn, resource_id)
    if existing is None:
        try:
            store.insert_lease(conn, LeaseRecord(
                resource_id=resource_id, controller_instance_id=holder,
                fencing_token=1, acquired_at=at, heartbeat_at=at,
                expires_at=expires, version=0,
            ))
        except sqlite3.IntegrityError:
            _err(ErrorCode.LEASE_CONFLICT, "lease concurrently acquired",
                 {_R: resource_id})
    elif existing.expires_at > at:
        _err(ErrorCode.LEASE_CONFLICT, "resource already leased",
             {_R: resource_id, _H: existing.controller_instance_id})
    else:
        ok = store.cas_lease_takeover(
            conn, resource_id=resource_id,
            controller_instance_id=holder,
            next_fencing_token=existing.fencing_token + 1,
            acquired_at=at, heartbeat_at=at, expires_at=expires,
            expected_version=existing.version,
        )
        if not ok:
            _err(ErrorCode.LEASE_CONFLICT, "lease takeover raced",
                 {_R: resource_id})
    result = store.get_lease(conn, resource_id)
    if result is None:
        _err(ErrorCode.LEASE_CONFLICT, "lease acquire failed", {_R: resource_id})
    return result


def heartbeat(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    holder: str,
    fencing_token: int,
    lease_seconds: int,
    at: datetime,
) -> LeaseRecord:
    _valid_at(at)
    _valid_duration(lease_seconds)
    existing = store.get_lease(conn, resource_id)
    if existing is None:
        _err(ErrorCode.LEASE_CONFLICT, "lease not held", {_R: resource_id})
    if existing.fencing_token != fencing_token:
        _err(ErrorCode.STALE_FENCING_TOKEN, "stale fencing token",
             {_R: resource_id, _H: existing.controller_instance_id,
              "token": fencing_token, "current": existing.fencing_token})
    if existing.expires_at <= at:
        _err(ErrorCode.LEASE_CONFLICT, "lease expired; re-acquire",
             {_R: resource_id})
    expires = at + timedelta(seconds=lease_seconds)
    ok = store.cas_lease_heartbeat(
        conn, resource_id=resource_id,
        controller_instance_id=existing.controller_instance_id,
        fencing_token=existing.fencing_token,
        heartbeat_at=at, expires_at=expires,
    )
    if not ok:
        _err(ErrorCode.LEASE_CONFLICT, "heartbeat raced", {_R: resource_id})
    result = store.get_lease(conn, resource_id)
    if result is None:
        _err(ErrorCode.LEASE_CONFLICT, "lease lost", {_R: resource_id})
    return result


def release(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    holder: str,
    fencing_token: int,
) -> None:
    existing = store.get_lease(conn, resource_id)
    if existing is None:
        return
    if existing.fencing_token != fencing_token or existing.controller_instance_id != holder:
        _err(ErrorCode.STALE_FENCING_TOKEN, "release not owned",
             {_R: resource_id, _H: existing.controller_instance_id,
              "token": fencing_token, "current": existing.fencing_token})
    store.delete_lease(
        conn, resource_id=resource_id,
        controller_instance_id=existing.controller_instance_id,
        fencing_token=existing.fencing_token,
    )


def check_held(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    holder: str,
    fencing_token: int,
    at: datetime,
) -> None:
    _valid_at(at)
    existing = store.get_lease(conn, resource_id)
    if existing is None:
        _err(ErrorCode.LEASE_CONFLICT, "lease not held", {_R: resource_id})
    if existing.fencing_token != fencing_token:
        _err(ErrorCode.STALE_FENCING_TOKEN, "stale fencing token",
             {_R: resource_id, "token": fencing_token,
              "current": existing.fencing_token})
    if existing.controller_instance_id != holder:
        _err(ErrorCode.LEASE_CONFLICT, "lease held by other",
             {_R: resource_id})
    if existing.expires_at <= at:
        _err(ErrorCode.LEASE_CONFLICT, "lease expired; re-acquire",
             {_R: resource_id})
