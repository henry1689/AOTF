"""AOTF Policy Engine 机械裁决（M0-C4）。

spec §11.1 MechanicalPolicyResult = PASS | FAIL | INCOMPLETE | ERROR 的
确定性纯函数实现：TestEvidenceRecord + 门配置（required / release_gates /
deferred）+ 控制面事实（ControlFacts）→ PolicyVerdict（result +
next-state 建议 + findings）。

决策表（确定性次序，先 ERROR 后 FAIL/INCOMPLETE 再 PASS）：
  R1  任一控制面事实异常（越界/hash 漂移/BLOCK/生产者不符/批准失效）
      → ERROR / SAFE_HALT
  R2  evidence bundle 不自洽（compute_bundle_sha256 != bundle_sha256，
      含占位全零未 with_bundle）→ ERROR / SAFE_HALT
  R3  record 为 None（证据整体缺失）→ INCOMPLETE / None
  R4  任一 required 有记录但 status != passed（failed/error）→ FAIL / FAILED
  R5  required 缺记录 或 required ∩ deferred → INCOMPLETE / None
  R6  全部 required passed 且 wants_release=False → PASS / CHECKPOINT_READY
  R7  R6 + wants_release=True + release_gates 空 → PASS / RELEASE_CANDIDATE
  R8  R6 + wants_release=True + 每个 release_gate 在 evidence 有记录且
      passed 且不在 deferred → PASS / RELEASE_CANDIDATE
  R9  R6 + wants_release=True + 任一 release_gate ∈ deferred / 缺记录 /
      非 passed → PASS / CHECKPOINT_READY（降级）

spec 映射：#22 缺 required check → INCOMPLETE（required_missing）；#23
deferred_out_of_scope 不计 passed（required 侧 → required_deferred →
INCOMPLETE；release 侧 → release_gate_deferred → 降级 CHECKPOINT_READY，
不产 RELEASE_CANDIDATE）；#31 LLM PASS 不能覆盖机械 FAIL —— 本接口不收
任何 LLM/review 入参，机械结果由证据+门+事实唯一决定，无覆盖路径；#32 全
部机械门通过也只能 RELEASE_CANDIDATE —— 无 RELEASED 枚举值，上限由类型
保证（spec §11.1 无自动 RELEASED）。

边界：纯裁决原语层，不接 DB/状态机/编排（TaskPhase 名仅作 PhaseHint 字面
量对照，不 import aotf.state/models）；INCOMPLETE 的 next_phase=None =
「不可自行推进，需补证据/重授权」，不是任何 phase；RELEASE_CANDIDATE 仍
需人工 decision（#33），本包不产生任何发布动作。异常域：PolicyError =
门配置/入参畸形（调用方 bug）；ERROR verdict = 运行事实异常（控制面/证据
→ SAFE_HALT 建议）。无副作用纯函数。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aotf.evidence.schema import (
    EvidenceCheck,
    TestEvidenceRecord,
    compute_bundle_sha256,
)
from aotf.policy.rules import PolicyError, validate_gates

__all__ = [
    "ControlFacts",
    "MechanicalResult",
    "PhaseHint",
    "PolicyFinding",
    "PolicyVerdict",
    "evaluate",
]


class MechanicalResult(StrEnum):
    """机械裁决结果（spec §11.1 字面量；无 RELEASED = #32 类型上限）。"""

    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"
    ERROR = "ERROR"


class PhaseHint(StrEnum):
    """next-state 建议（spec §5.1 字面量对照；policy 自治枚举）。"""

    CHECKPOINT_READY = "CHECKPOINT_READY"
    RELEASE_CANDIDATE = "RELEASE_CANDIDATE"
    FAILED = "FAILED"
    SAFE_HALT = "SAFE_HALT"


@dataclass(frozen=True, slots=True)
class ControlFacts:
    """控制面事实（全字段必填无默认：忘传即 TypeError，不静默放行）。

    异常侧：boundary_violation / hash_drift / blocked / producer_mismatch
    为 True；approval_valid 为 False。任一异常侧 → R1 ERROR。
    """

    boundary_violation: bool
    hash_drift: bool
    blocked: bool
    approval_valid: bool
    producer_mismatch: bool


@dataclass(frozen=True, slots=True)
class PolicyFinding:
    """单条裁决依据。code 机器码（见引擎各分支），check_id 关联门/check。"""

    code: str
    check_id: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """一次裁决结果。PASS 两态（R6/R7/R8）findings=() 干净。"""

    result: MechanicalResult
    next_phase: PhaseHint | None
    findings: tuple[PolicyFinding, ...]


def evaluate(
    record: TestEvidenceRecord | None,
    *,
    required: tuple[str, ...],
    release_gates: tuple[str, ...] = (),
    deferred: tuple[str, ...] = (),
    wants_release: bool = False,
    facts: ControlFacts,
) -> PolicyVerdict:
    """机械裁决（决策表 R1–R9，见模块 docstring；步骤序如下）。

    畸形入参（facts 非 ControlFacts / 门配置非法 / record 类型错 /
    record.checks 重复 check_id）→ PolicyError。运行事实异常 → ERROR
    verdict（SAFE_HALT 建议）。
    """
    if not isinstance(facts, ControlFacts):
        raise PolicyError("facts must be ControlFacts")
    validate_gates(required=required, release_gates=release_gates,
                   deferred=deferred)
    checks_by_id: dict[str, EvidenceCheck] = {}
    if record is not None:
        if not isinstance(record, TestEvidenceRecord):
            raise PolicyError("record must be TestEvidenceRecord or None")
        for ch in record.checks:
            if ch.check_id in checks_by_id:
                raise PolicyError(f"duplicate check_id in record: {ch.check_id}")
            checks_by_id[ch.check_id] = ch
    deferred_set = frozenset(deferred)

    # R1：控制面事实异常优先——任一违规即 SAFE_HALT，绝无 READY
    violated: list[str] = []
    for name, bad in (
        ("boundary_violation", facts.boundary_violation),
        ("hash_drift", facts.hash_drift),
        ("blocked", facts.blocked),
        ("producer_mismatch", facts.producer_mismatch),
    ):
        if bad:
            violated.append(name)
    if not facts.approval_valid:
        violated.append("approval_valid")
    if violated:
        return PolicyVerdict(
            result=MechanicalResult.ERROR,
            next_phase=PhaseHint.SAFE_HALT,
            findings=tuple(
                PolicyFinding(code="control_fact", detail=name)
                for name in violated))

    # R2：evidence bundle 自洽（tamper / 未 with_bundle → ERROR）
    if record is not None and \
            compute_bundle_sha256(record) != record.bundle_sha256:
        return PolicyVerdict(
            result=MechanicalResult.ERROR,
            next_phase=PhaseHint.SAFE_HALT,
            findings=(PolicyFinding(code="evidence_tamper"),))

    # R3：证据整体缺失 → INCOMPLETE（缺 required check 的可验证前提）
    if record is None:
        return PolicyVerdict(
            result=MechanicalResult.INCOMPLETE,
            next_phase=None,
            findings=(PolicyFinding(code="evidence_missing"),))

    # R4/R5：扫 required（给定顺序；deferred 压制不计 passed = #23）
    failed: list[tuple[str, str]] = []
    missing: list[str] = []
    deferred_required: list[str] = []
    for cid in required:
        if cid in deferred_set:
            deferred_required.append(cid)
            continue
        ch = checks_by_id.get(cid)
        if ch is None:
            missing.append(cid)
        elif ch.status != "passed":
            failed.append((cid, ch.status))
    if failed:
        # R4 优先 R5：任务失败已明确 → FAIL，而非补证据
        return PolicyVerdict(
            result=MechanicalResult.FAIL,
            next_phase=PhaseHint.FAILED,
            findings=tuple(
                PolicyFinding(code="required_failed", check_id=cid,
                              detail=status)
                for cid, status in failed))
    if missing or deferred_required:
        return PolicyVerdict(
            result=MechanicalResult.INCOMPLETE,
            next_phase=None,
            findings=tuple(
                [PolicyFinding(code="required_missing", check_id=cid)
                 for cid in missing]
                + [PolicyFinding(code="required_deferred", check_id=cid)
                   for cid in deferred_required]))

    # R6..R9：required 全过 → PASS 分支
    if not wants_release:
        return PolicyVerdict(
            result=MechanicalResult.PASS,
            next_phase=PhaseHint.CHECKPOINT_READY,
            findings=())
    unmet: list[tuple[str, str]] = []
    for gid in release_gates:
        if gid in deferred_set:
            unmet.append(("release_gate_deferred", gid))
            continue
        ch = checks_by_id.get(gid)
        if ch is None:
            unmet.append(("release_gate_missing", gid))
        elif ch.status != "passed":
            unmet.append(("release_gate_not_passed", gid))
    if unmet:
        # R9：闭环合格但发布标准未满足（含 deferred 不计 passed）→ 降级
        return PolicyVerdict(
            result=MechanicalResult.PASS,
            next_phase=PhaseHint.CHECKPOINT_READY,
            findings=tuple(
                PolicyFinding(code=code, check_id=gid)
                for code, gid in unmet))
    return PolicyVerdict(
        result=MechanicalResult.PASS,
        next_phase=PhaseHint.RELEASE_CANDIDATE,
        findings=())
