"""M0-C4 policy rules（门配置校验）测试。

check_token / validate_gates 纯函数；畸形一律 PolicyError（fail-fast，
不产 verdict）。无 git/DB/时间依赖。
"""

from __future__ import annotations

import pytest

from aotf.policy.rules import PolicyError, check_token, validate_gates


def test_token_valid() -> None:
    check_token("gate", "a")
    check_token("gate", "a@b.c-d")
    check_token("gate", "cmd_unit")
    check_token("gate", "_x-1.2@3")


def test_token_invalid() -> None:
    bads: list = [None, 123, "", ".", "..", "a b", "a" * 65, "a/b"]
    for bad in bads:
        with pytest.raises(PolicyError, match="invalid gate token"):
            check_token("gate", bad)


def test_validate_gates_ok() -> None:
    validate_gates(required=("a", "b"), release_gates=("c",), deferred=("c",))
    validate_gates(required=("unit",), release_gates=(), deferred=())


def test_required_empty_rejected() -> None:
    with pytest.raises(PolicyError, match="required must be non-empty"):
        validate_gates(required=())


def test_duplicate_gate_rejected() -> None:
    with pytest.raises(PolicyError, match="duplicate gate in required"):
        validate_gates(required=("a", "a"))
    with pytest.raises(PolicyError, match="duplicate gate in release_gates"):
        validate_gates(required=("a",), release_gates=("b", "b"))
    with pytest.raises(PolicyError, match="duplicate gate in deferred"):
        validate_gates(required=("a",), deferred=("a", "a"))


def test_deferred_unknown_rejected() -> None:
    with pytest.raises(PolicyError, match="deferred gate unknown"):
        validate_gates(required=("a",), release_gates=("b",), deferred=("x",))


def test_deferred_subset_of_required_ok() -> None:
    validate_gates(required=("a", "b"), release_gates=("r",), deferred=("b",))
    validate_gates(required=("a",), deferred=("a",))


def test_invalid_type_rejected() -> None:
    with pytest.raises(PolicyError, match="required must be a tuple"):
        validate_gates(required=["a"])
    with pytest.raises(PolicyError, match="required must be a tuple"):
        validate_gates(required=None)  # type: ignore[arg-type]
    with pytest.raises(PolicyError, match="deferred must be a tuple"):
        validate_gates(required=("a",), deferred=["a"])
