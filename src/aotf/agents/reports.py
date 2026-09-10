"""AOTF 三角色输出 typed schema + parse（M0-D3）。

LLM 层结构化输出（D2 SDK structured_output / output_format 反序列化后）→
typed dataclass，fail-closed 校验：#26（Agent schema 输出非法不推进阶段）
——缺字段/非法枚举/空消息 → ValueError，调用方不据此推进。

verdict 对齐 spec §11.1 LLMReviewResult = PASS | CONCERNS | BLOCK（≠ 机械
MechanicalResult；C4 #31 已保证机械不可覆盖，本模块不触碰 policy）。五维
dimension 对齐 §4.1 Reviewer。纯函数零依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

__all__ = [
    "ImplementerReport",
    "ProposedFileMutation",
    "PlannerReport",
    "ReviewFinding",
    "ReviewerReport",
    "parse_implementer_report",
    "parse_planner_report",
    "parse_reviewer_report",
]

_DIMENSIONS = ("correctness", "structure", "tests", "compatibility", "risk")
_SEVERITIES = ("info", "concern", "blocker")
_VERDICTS = ("PASS", "CONCERNS", "BLOCK")
_OPERATIONS = ("create", "replace", "delete")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def _require_dict(obj: object, name: str) -> dict:
    _require(isinstance(obj, dict), f"{name} must be a dict")
    return obj


def _require_str_field(d: dict, key: str, *, nonempty: bool) -> str:
    value = d.get(key)
    _require(isinstance(value, str), f"{key} must be a str")
    if nonempty and not value.strip():
        raise ValueError(f"{key} must be non-empty")
    return value


def _require_in(value: object, allowed, name: str) -> str:
    _require(value in allowed, f"invalid {name}")
    return value  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class PlannerReport:
    """Planner 输出：typed task proposal（§4.1）。"""

    proposal_text: str
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_text, str) or not self.proposal_text.strip():
            raise ValueError("proposal_text must be non-empty")
        if not isinstance(self.note, str):
            raise ValueError("note must be str")


@dataclass(frozen=True, slots=True)
class ProposedFileMutation:
    """Implementer 只能提议变更；真正写入由 AOTF controller 执行。"""

    path: str
    operation: Literal["create", "replace", "delete"]
    expected_sha256: str | None
    content: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("mutation path must be non-empty")
        if self.operation not in _OPERATIONS:
            raise ValueError("invalid mutation operation")
        if self.expected_sha256 is not None and (
                not isinstance(self.expected_sha256, str)
                or not _SHA256_RE.fullmatch(self.expected_sha256)):
            raise ValueError("expected_sha256 must be lowercase sha256")
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("mutation content must be str or null")
        if self.operation == "create":
            _require(self.expected_sha256 is None and self.content is not None,
                     "create mutation shape invalid")
        elif self.operation == "replace":
            _require(self.expected_sha256 is not None and self.content is not None,
                     "replace mutation shape invalid")
        else:
            _require(self.expected_sha256 is not None and self.content is None,
                     "delete mutation shape invalid")


@dataclass(frozen=True, slots=True)
class ImplementerReport:
    """Implementer 输出 mutation intents；summary 不进入 Reviewer。"""

    outcome: Literal["completed", "escalated"]
    summary: str
    mutations: tuple[ProposedFileMutation, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in ("completed", "escalated"):
            raise ValueError("invalid outcome")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("summary must be non-empty")
        if not isinstance(self.mutations, tuple) or not all(
                isinstance(item, ProposedFileMutation)
                for item in self.mutations):
            raise ValueError("mutations must be tuple of ProposedFileMutation")
        if self.outcome == "completed" and not self.mutations:
            raise ValueError("completed implementer requires mutations")
        if self.outcome == "escalated" and self.mutations:
            raise ValueError("escalated implementer cannot propose mutations")


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """单条审查发现（五维 × 严重度）。"""

    dimension: Literal["correctness", "structure", "tests", "compatibility", "risk"]
    severity: Literal["info", "concern", "blocker"]
    message: str

    def __post_init__(self) -> None:
        if self.dimension not in _DIMENSIONS:
            raise ValueError("invalid dimension")
        if self.severity not in _SEVERITIES:
            raise ValueError("invalid severity")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be non-empty")


@dataclass(frozen=True, slots=True)
class ReviewerReport:
    """Reviewer 输出（§11.1 LLMReviewResult 对齐）。"""

    verdict: Literal["PASS", "CONCERNS", "BLOCK"]
    findings: tuple[ReviewFinding, ...] = ()

    def __post_init__(self) -> None:
        if self.verdict not in _VERDICTS:
            raise ValueError("invalid verdict")
        if not isinstance(self.findings, tuple) \
                or not all(isinstance(f, ReviewFinding) for f in self.findings):
            raise ValueError("findings must be tuple of ReviewFinding")


def _parse_finding(obj: object) -> ReviewFinding:
    d = _require_dict(obj, "finding")
    return ReviewFinding(
        dimension=_require_in(_require_str_field(d, "dimension", nonempty=True),
                              _DIMENSIONS, "dimension"),
        severity=_require_in(_require_str_field(d, "severity", nonempty=True),
                             _SEVERITIES, "severity"),
        message=_require_str_field(d, "message", nonempty=True),
    )


def parse_planner_report(obj: object) -> PlannerReport:
    """LLM planner structured_output → PlannerReport；畸形 → ValueError（#26）。"""
    d = _require_dict(obj, "planner report")
    note = d.get("note", "")
    if not isinstance(note, str):
        raise ValueError("note must be str")
    return PlannerReport(
        proposal_text=_require_str_field(d, "proposal_text", nonempty=True),
        note=note,
    )


def parse_implementer_report(obj: object) -> ImplementerReport:
    """LLM implementer structured_output → ImplementerReport；畸形 → ValueError。"""
    d = _require_dict(obj, "implementer report")
    raw_mutations = d.get("mutations", ())
    _require(isinstance(raw_mutations, (list, tuple)),
             "mutations must be a list")
    return ImplementerReport(
        outcome=_require_in(_require_str_field(d, "outcome", nonempty=True),
                            ("completed", "escalated"), "outcome"),
        summary=_require_str_field(d, "summary", nonempty=True),
        mutations=tuple(_parse_mutation(item) for item in raw_mutations),
    )


def _parse_mutation(obj: object) -> ProposedFileMutation:
    d = _require_dict(obj, "mutation")
    expected = d.get("expected_sha256")
    content = d.get("content")
    if expected is not None and not isinstance(expected, str):
        raise ValueError("expected_sha256 must be str or null")
    if content is not None and not isinstance(content, str):
        raise ValueError("content must be str or null")
    return ProposedFileMutation(
        path=_require_str_field(d, "path", nonempty=True),
        operation=_require_in(
            _require_str_field(d, "operation", nonempty=True),
            _OPERATIONS, "mutation operation"),
        expected_sha256=expected,
        content=content,
    )


def parse_reviewer_report(obj: object) -> ReviewerReport:
    """LLM reviewer structured_output → ReviewerReport；畸形 → ValueError（#26）。"""
    d = _require_dict(obj, "reviewer report")
    verdict = _require_in(_require_str_field(d, "verdict", nonempty=True),
                          _VERDICTS, "verdict")
    raw_findings = d.get("findings", ())
    # null 非合法容器 → 显式拒（#26 fail-closed）
    _require(raw_findings is not None
             and isinstance(raw_findings, (list, tuple)),
             "findings must be a list")
    return ReviewerReport(
        verdict=verdict,  # type: ignore[arg-type]
        findings=tuple(_parse_finding(f) for f in raw_findings),
    )
