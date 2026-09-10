"""M0-A1a 错误契约测试。"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from aotf.errors import AotfError, ErrorCode

# 任务书 §7.5 精确 16 项必需集合（值 = 名称，一一对应）。
REQUIRED_CODES = [
    "INVALID_STATE_TRANSITION",
    "VERSION_CONFLICT",
    "TERMINAL_STATE",
    "DUPLICATE_EVENT",
    "IDEMPOTENCY_CONFLICT",
    "INTEGRITY_FAILURE",
    "LEASE_CONFLICT",
    "STALE_FENCING_TOKEN",
    "NOT_AUTHORIZED",
    "INVALID_INPUT",
    "MIGRATION_FAILED",
    "BUSY_TIMEOUT",
    "OUTBOX_DELIVERY_FAILED",
    "BUDGET_EXCEEDED",
    "DEADLINE_EXCEEDED",
    "SCHEMA_VALIDATION_FAILED",
]


def test_error_code_values_are_unique_and_stable() -> None:
    values = [c.value for c in ErrorCode]
    assert len(values) == len(set(values))
    assert all(c.value == c.name for c in ErrorCode)


def test_error_code_contains_required_contract() -> None:
    names = {c.name for c in ErrorCode}
    assert names == set(REQUIRED_CODES)


def test_aotf_error_exposes_typed_code_and_stable_message() -> None:
    err = AotfError(ErrorCode.INVALID_INPUT, "bad input")
    assert err.code is ErrorCode.INVALID_INPUT
    assert isinstance(err.code, ErrorCode)
    assert err.message == "bad input"


def test_aotf_error_rejects_blank_message() -> None:
    with pytest.raises(ValueError):
        AotfError(ErrorCode.INVALID_INPUT, "   ")
    with pytest.raises(ValueError):
        AotfError(ErrorCode.INVALID_INPUT, "")


def test_aotf_error_defensively_copies_details() -> None:
    src = {"k": [1, 2]}
    err = AotfError(ErrorCode.INVALID_INPUT, "m", details=src)
    src["k"].append(3)
    src["extra"] = "x"
    assert err.details["k"] == [1, 2]
    assert "extra" not in err.details


def test_aotf_error_details_are_read_only() -> None:
    err = AotfError(ErrorCode.INVALID_INPUT, "m", details={"k": "v"})
    with pytest.raises(TypeError):
        err.details["k"] = "changed"  # type: ignore[index]
    assert isinstance(err.details, MappingProxyType)


def test_aotf_error_string_does_not_expand_details() -> None:
    err = AotfError(ErrorCode.INVALID_INPUT, "m", details={"secret": "s3cr3t"})
    text = str(err)
    assert text == "INVALID_INPUT: m"
    assert "s3cr3t" not in text
