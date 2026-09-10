"""M0-C4 policy engine（机械裁决 R1–R9）测试。

纯构造 TestEvidenceRecord（C1 契约 + record_with_bundle 自洽），无
git/DB/时间依赖；#22/#23/#31/#32 边界各有显式测试名。畸形入参 → PolicyError。
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timezone

import pytest

import aotf.evidence.schema as evschema
from aotf.evidence.schema import (
    EvidenceCheck,
    EvidenceRunnerInfo,
    record_with_bundle,
)
from aotf.policy.engine import (
    ControlFacts,
    MechanicalResult,
    PhaseHint,
    evaluate,
)
from aotf.policy.rules import PolicyError

H40 = "a" * 40
H64 = "d" * 64

_RUNNER = EvidenceRunnerInfo(
    identity="aotf-policy-test-v1",
    host_fingerprint=None,
    started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
)


def _check(check_id: str, *, status: str = "passed") -> EvidenceCheck:
    return EvidenceCheck(
        check_id=check_id,
        command_id=f"cmd-{check_id}",
        status=status,
        exit_code=1 if status != "passed" else 0,
    )


def _record(*checks: EvidenceCheck) -> evschema.TestEvidenceRecord:
    raw = evschema.TestEvidenceRecord(
        schema_version=1, evidence_id="ev-1", task_id="task-1",
        worktree_tree=H40, delta_sha256=H64, runner=_RUNNER,
        checks=checks, bundle_sha256="0" * 64,
    )
    return record_with_bundle(raw)


def _facts(**over: bool) -> ControlFacts:
    base = dict(boundary_violation=False, hash_drift=False, blocked=False,
                approval_valid=True, producer_mismatch=False)
    base.update(over)
    return ControlFacts(**base)


def _codes(v) -> tuple[str, ...]:
    return tuple(f.code for f in v.findings)


def _eval(rec, *, required, release_gates=(), deferred=(),
          wants_release=False, **facts_over):
    return evaluate(
        rec, required=required, release_gates=release_gates,
        deferred=deferred, wants_release=wants_release,
        facts=_facts(**facts_over))


def test_all_passed_checkpoint_ready() -> None:
    rec = _record(_check("unit"))
    v = _eval(rec, required=("unit",), wants_release=False)
    assert v.result == MechanicalResult.PASS
    assert v.next_phase == PhaseHint.CHECKPOINT_READY
    assert v.findings == ()


def test_release_candidate_all_gates_met() -> None:
    rec = _record(_check("unit"), _check("rel"))
    v = _eval(rec, required=("unit",), release_gates=("rel",),
              wants_release=True)
    assert v.result == MechanicalResult.PASS
    assert v.next_phase == PhaseHint.RELEASE_CANDIDATE
    assert v.findings == ()


def test_no_released_concept() -> None:
    # #32：全部机械门通过也只能 RELEASE_CANDIDATE，无 RELEASED 概念
    assert not hasattr(MechanicalResult, "RELEASED")
    assert {m.value for m in MechanicalResult} == \
        {"PASS", "FAIL", "INCOMPLETE", "ERROR"}
    assert {p.value for p in PhaseHint} == {
        "CHECKPOINT_READY", "RELEASE_CANDIDATE", "FAILED", "SAFE_HALT"}


def test_vacuous_release_all_passed() -> None:
    rec = _record(_check("unit"))
    v = _eval(rec, required=("unit",), wants_release=True)
    assert v.result == MechanicalResult.PASS
    assert v.next_phase == PhaseHint.RELEASE_CANDIDATE


def test_wants_release_false_never_release() -> None:
    rec = _record(_check("unit"))
    v = _eval(rec, required=("unit",), release_gates=("unit",),
              wants_release=False)
    assert v.result == MechanicalResult.PASS
    assert v.next_phase == PhaseHint.CHECKPOINT_READY


def test_release_gate_deferred_downgrade() -> None:
    # #23：deferred 不计 passed → 发布标准未满足 → 不 RELEASE_CANDIDATE
    rec = _record(_check("unit"))
    v = _eval(rec, required=("unit",), release_gates=("rel",),
              deferred=("rel",), wants_release=True)
    assert v.result == MechanicalResult.PASS
    assert v.next_phase == PhaseHint.CHECKPOINT_READY
    assert ("release_gate_deferred", "rel") in _zip(v)


def test_release_gate_missing_downgrade() -> None:
    rec = _record(_check("unit"))
    v = _eval(rec, required=("unit",), release_gates=("rel",),
              wants_release=True)
    assert v.result == MechanicalResult.PASS
    assert v.next_phase == PhaseHint.CHECKPOINT_READY
    assert ("release_gate_missing", "rel") in _zip(v)


def test_release_gate_not_passed_downgrade() -> None:
    rec = _record(_check("unit"), _check("rel", status="failed"))
    v = _eval(rec, required=("unit",), release_gates=("rel",),
              wants_release=True)
    assert v.result == MechanicalResult.PASS
    assert v.next_phase == PhaseHint.CHECKPOINT_READY
    assert ("release_gate_not_passed", "rel") in _zip(v)


def test_missing_required_incomplete() -> None:
    # #22：缺 required check → INCOMPLETE
    rec = _record(_check("unit"))
    v = _eval(rec, required=("unit", "lint"))
    assert v.result == MechanicalResult.INCOMPLETE
    assert v.next_phase is None
    assert ("required_missing", "lint") in _zip(v)


def test_record_none_incomplete() -> None:
    v = _eval(None, required=("unit",))
    assert v.result == MechanicalResult.INCOMPLETE
    assert v.next_phase is None
    assert _codes(v) == ("evidence_missing",)


def test_failed_required_fail() -> None:
    rec = _record(_check("unit", status="failed"))
    v = _eval(rec, required=("unit",))
    assert v.result == MechanicalResult.FAIL
    assert v.next_phase == PhaseHint.FAILED
    assert ("required_failed", "unit") in _zip(v)


def test_error_status_required_fail() -> None:
    rec = _record(_check("unit", status="error"))
    v = _eval(rec, required=("unit",))
    assert v.result == MechanicalResult.FAIL
    assert v.next_phase == PhaseHint.FAILED


def test_failed_beats_missing() -> None:
    # R4 优先 R5：有 failed 记录 → FAIL，而非 INCOMPLETE
    rec = _record(_check("unit", status="failed"))
    v = _eval(rec, required=("unit", "lint"))
    assert v.result == MechanicalResult.FAIL
    assert v.next_phase == PhaseHint.FAILED


def test_deferred_required_incomplete() -> None:
    # #23 压制：deferred 不计 passed；required 又要求它 → 无法闭环
    rec = _record(_check("unit"))
    v = _eval(rec, required=("unit",), deferred=("unit",))
    assert v.result == MechanicalResult.INCOMPLETE
    assert v.next_phase is None
    assert ("required_deferred", "unit") in _zip(v)


def test_extra_passed_check_ignored() -> None:
    rec = _record(_check("unit"), _check("extra", status="passed"))
    v = _eval(rec, required=("unit",))
    assert v.result == MechanicalResult.PASS
    assert v.next_phase == PhaseHint.CHECKPOINT_READY


def test_boundary_violation_error() -> None:
    v = _eval(_record(_check("unit")), required=("unit",),
              boundary_violation=True)
    assert v.result == MechanicalResult.ERROR
    assert v.next_phase == PhaseHint.SAFE_HALT
    assert v.findings[0].code == "control_fact"
    assert v.findings[0].detail == "boundary_violation"


def test_hash_drift_error() -> None:
    v = _eval(_record(_check("unit")), required=("unit",), hash_drift=True)
    assert v.result == MechanicalResult.ERROR
    assert v.next_phase == PhaseHint.SAFE_HALT


def test_blocked_error() -> None:
    v = _eval(_record(_check("unit")), required=("unit",), blocked=True)
    assert v.result == MechanicalResult.ERROR
    assert v.next_phase == PhaseHint.SAFE_HALT


def test_approval_invalid_error() -> None:
    v = _eval(_record(_check("unit")), required=("unit",),
              approval_valid=False)
    assert v.result == MechanicalResult.ERROR
    assert v.next_phase == PhaseHint.SAFE_HALT
    assert v.findings[0].detail == "approval_valid"


def test_producer_mismatch_error() -> None:
    v = _eval(_record(_check("unit")), required=("unit",),
              producer_mismatch=True)
    assert v.result == MechanicalResult.ERROR
    assert v.next_phase == PhaseHint.SAFE_HALT


def test_multiple_control_facts_all_findings() -> None:
    v = _eval(_record(_check("unit")), required=("unit",),
              boundary_violation=True, blocked=True, approval_valid=False)
    assert v.result == MechanicalResult.ERROR
    assert v.next_phase == PhaseHint.SAFE_HALT
    assert len(v.findings) == 3
    assert all(f.code == "control_fact" for f in v.findings)
    assert {f.detail for f in v.findings} == {
        "boundary_violation", "blocked", "approval_valid"}


def test_evidence_tamper_error() -> None:
    # R2：篡改 check.status 后 bundle 不再自洽 → ERROR（证据 tamper）
    rec = _record(_check("unit"))
    tampered = replace(
        rec, checks=(replace(rec.checks[0], status="failed"),))
    v = _eval(tampered, required=("unit",))
    assert v.result == MechanicalResult.ERROR
    assert v.next_phase == PhaseHint.SAFE_HALT
    assert _codes(v) == ("evidence_tamper",)


def test_unbundled_record_error() -> None:
    # 占位 bundle_sha256（未 with_bundle）视为不完整 → ERROR
    raw = evschema.TestEvidenceRecord(
        schema_version=1, evidence_id="ev-1", task_id="task-1",
        worktree_tree=H40, delta_sha256=H64, runner=_RUNNER,
        checks=(_check("unit"),), bundle_sha256="0" * 64,
    )
    v = _eval(raw, required=("unit",))
    assert v.result == MechanicalResult.ERROR
    assert v.next_phase == PhaseHint.SAFE_HALT


def test_llm_cannot_override_mechanical_fail() -> None:
    # #31：接口不收任何 LLM/review 入参 → 机械 FAIL 无覆盖路径
    params = set(inspect.signature(evaluate).parameters)
    assert not (params & {"llm", "review", "override", "human", "decision"})
    rec = _record(_check("unit", status="failed"))
    v = _eval(rec, required=("unit",))
    assert v.result == MechanicalResult.FAIL


def test_duplicate_check_id_in_record_rejected() -> None:
    rec = _record(_check("unit"), _check("unit"))
    with pytest.raises(PolicyError, match="duplicate check_id"):
        _eval(rec, required=("unit",))


def _zip(v) -> tuple[tuple[str, str | None], ...]:
    return tuple((f.code, f.check_id) for f in v.findings)
