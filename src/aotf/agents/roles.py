"""AOTF 三角色 adapter（M0-D3）：角色职责落为可执行 spec。

把 spec §4.1 三角色职责编码为「中文 system prompt + 输入清单 dataclass」，
纯函数零 SDK/零 git/零 DB，直接喂 D2 ClaudeSdkRunner(prompt=…) 或
M0-E orchestrator。spec §15 的 prompts/*.md 是生产参考布局，Pre-MVP 内嵌
Python 常量（可校验、可测）；M0-E 后可拆文件。

关键约束落位：
- §4.1 Planner：全库只读产 typed task proposal；P0/P1 缺陷须立即止血与
  结构修复，不得"等重构自然解决"。
- §4.1 Implementer：仅任务 worktree / 只改批准文件集 / 不碰 AOTF 控制面
  证据与规则 / 范围扩大·基线漂移·设计矛盾 → escalation 自停；模型仅提出
  typed mutation intents，文件写入与正式证据均由 AOTF controller 执行。
- §4.1 Reviewer：正确性·结构·测试·兼容性·风险五维；无阻塞允许 PASS、
  不制造假阳性；只审阅 AOTF 提供的机械 patch，不调用工具、不修码/不推进
  状态/不覆盖机械门。
- #28 双保险：ReviewerInputs 类型**无** implementer summary 字段（结构
  保证），reviewer prompt 再明示「不读主观总结」（文本保证）。

校验失败抛 ValueError（运行期契约惯例，非领域错误）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

__all__ = [
    "ROLE_IDS",
    "ROLE_SYSTEM_PROMPTS",
    "PlannerInputs",
    "ImplementerInputs",
    "ReviewerInputs",
    "render_role_inputs",
    "role_system_prompt",
]

ROLE_IDS: tuple[str, ...] = ("planner", "implementer", "reviewer")

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def _token_like(name: str, value: object) -> None:
    _require(isinstance(value, str) and value not in (".", "..")
             and _TOKEN_RE.fullmatch(value), f"invalid {name} token")


def _nonempty(value: object, name: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()),
             f"{name} must be non-empty str")


def _hex40(value: object, name: str) -> None:
    _require(isinstance(value, str) and _HEX40_RE.fullmatch(value),
             f"{name} must be 40-hex")


def _abs_no_parent(value: object, name: str) -> None:
    _require(isinstance(value, str) and bool(value), f"{name} must be str")
    p = Path(value)
    win = PureWindowsPath(value)
    absolute = p.is_absolute() or bool(win.drive and win.root)
    _require(absolute and ".." not in p.parts and ".." not in win.parts,
             f"{name} must be absolute without '..'")


def _tokens(coll: object, name: str, *, allow_dots: bool) -> None:
    _require(isinstance(coll, tuple), f"{name} must be a tuple")
    for entry in coll:
        _nonempty(entry, f"{name} entry")
        if not allow_dots and ".." in Path(str(entry)).parts:
            raise ValueError(f"{name} entry must not contain '..'")


ROLE_SYSTEM_PROMPTS: dict[str, str] = {
    "planner": (
        "你是 AOTF 系统的 Planner 角色（全库只读，不拥有任何源码写权限）。\n"
        "任务：根据给定的项目上下文与基线，输出一份 typed task proposal。\n"
        "职责约束：\n"
        "- 全库只读；不修改源码、不签发授权、不推进任何任务状态；\n"
        "- 依据提供的架构上下文、发布标准、当前模块能力与已批准决策判断改进点；\n"
        "- 对 P0/P1 缺陷必须提出立即止血与结构修复方案，不得以“等待重构自然解决”敷衍；\n"
        "- 只输出一个 JSON 对象 {\"proposal_text\": \"...\", \"note\": \"...\"}，"
        "proposal_text 必须非空，禁止输出 JSON 之外的任何文本。"
    ),
    "implementer": (
        "你是 AOTF 系统的 Implementer 角色。\n"
        "任务：依据 AOTF 提供的批准文件快照，为已批准 proposal 提出精确文件变更。\n"
        "职责约束：\n"
        "- 你不拥有文件写入、命令执行、状态推进、重试或任务结束权；不得调用或假定 Edit/Write/Bash；\n"
        "- 只可为 allowed_files 提出 mutation；真正写入由 AOTF controller 校验并执行；\n"
        "- 不修改 AOTF 控制面、证据目录、任务授权与发布规则文件；\n"
        "- 遇到范围扩大、基线漂移或设计矛盾 → 输出 outcome=escalated 并立即停止，"
        "不得继续扩大改动；\n"
        "- 每项 mutation 必须含 path、operation(create|replace|delete)、expected_sha256 和 content；"
        "create 的 hash 为 null，delete 的 content 为 null，replace/delete 必须绑定快照 hash；\n"
        "- 正式证据由 AOTF EvidenceRunner 重跑，你的自述不构成正式证据；\n"
        "- 只输出一个 JSON 对象 {\"outcome\": \"completed\"|\"escalated\", "
        "\"summary\": \"...\", \"mutations\": [...]}，禁止额外文本。"
    ),
    "reviewer": (
        "你是 AOTF 系统的 Reviewer 角色（不拥有源码写权限）。\n"
        "任务：审查 AOTF 提供的已批准任务机械 patch。\n"
        "职责约束：\n"
        "- 你不拥有工具调用、重试、状态推进或任务结束权；只作一次 typed 判定；\n"
        "- 只读取批准任务、baseline manifest、actual delta 与源码等**事实**输入；\n"
        "- 不得读取 Implementer 的主观总结——你的审查仅依据输入中的事实 manifest；\n"
        "- 从五维审查：correctness / structure / tests / compatibility / risk；\n"
        "- 无阻塞问题时允许 verdict=PASS，不得为“必须发现问题”制造假阳性；\n"
        "- 不修改代码、不推进状态、不覆盖任何机械门；\n"
        "- 只输出一个 JSON 对象 {\"verdict\": \"PASS\"|\"CONCERNS\"|\"BLOCK\", "
        "\"findings\": [{\"dimension\": ..., \"severity\": \"info\"|\"concern\"|"
        "\"blocker\", \"message\": \"...\"}]}，禁止额外文本。"
    ),
}


def role_system_prompt(role: str) -> str:
    try:
        return ROLE_SYSTEM_PROMPTS[role]
    except KeyError:
        raise ValueError(f"unknown role: {role}") from None


@dataclass(frozen=True, slots=True)
class PlannerInputs:
    """Planner 输入：proposal 所需上下文（§4.1 只读面）。"""

    project_context: str
    baseline_commit: str
    baseline_tree: str
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.project_context, "project_context")
        _hex40(self.baseline_commit, "baseline_commit")
        _hex40(self.baseline_tree, "baseline_tree")
        _tokens(self.constraints, "constraints", allow_dots=True)


@dataclass(frozen=True, slots=True)
class ImplementerInputs:
    """Implementer 输入：worktree + 批准文件集 + 已批准 proposal。"""

    task_id: str
    proposal_text: str
    allowed_files: tuple[str, ...]
    worktree_path: str
    baseline_tree: str

    def __post_init__(self) -> None:
        _token_like("task_id", self.task_id)
        _nonempty(self.proposal_text, "proposal_text")
        _tokens(self.allowed_files, "allowed_files", allow_dots=False)
        _abs_no_parent(self.worktree_path, "worktree_path")
        _hex40(self.baseline_tree, "baseline_tree")


@dataclass(frozen=True, slots=True)
class ReviewerInputs:
    """Reviewer 输入：事实 manifest。无 implementer summary 字段（#28）。"""

    task_id: str
    proposal_text: str
    allowed_files: tuple[str, ...]
    baseline_tree: str
    actual_tree: str
    changes_summary: str  # 机械 delta 摘要（B4 事实生成，非主观总结）
    patch_text: str  # AOTF 从 baseline..checkpoint 机械生成

    def __post_init__(self) -> None:
        _token_like("task_id", self.task_id)
        _nonempty(self.proposal_text, "proposal_text")
        _tokens(self.allowed_files, "allowed_files", allow_dots=False)
        _hex40(self.baseline_tree, "baseline_tree")
        _hex40(self.actual_tree, "actual_tree")
        _nonempty(self.changes_summary, "changes_summary")
        _nonempty(self.patch_text, "patch_text")


def render_role_inputs(role: str, inputs: object) -> str:
    """按角色渲染「任务输入」文本（逐行 字段: 值），供 runner user prompt。"""
    lines: list[str] = []
    if role == "planner":
        if not isinstance(inputs, PlannerInputs):
            raise ValueError("planner requires PlannerInputs")
        lines.append(f"- project_context: {inputs.project_context}")
        lines.append(f"- baseline_commit: {inputs.baseline_commit}")
        lines.append(f"- baseline_tree: {inputs.baseline_tree}")
        if inputs.constraints:
            lines.append("- constraints:")
            lines.extend(f"  - {c}" for c in inputs.constraints)
    elif role == "implementer":
        if not isinstance(inputs, ImplementerInputs):
            raise ValueError("implementer requires ImplementerInputs")
        lines.append(f"- task_id: {inputs.task_id}")
        lines.append(f"- baseline_tree: {inputs.baseline_tree}")
        lines.append(f"- worktree_path: {inputs.worktree_path}")
        lines.append("- allowed_files:")
        lines.extend(f"  - {f}" for f in inputs.allowed_files)
        lines.append("- proposal_text:")
        lines.append(f"  {inputs.proposal_text}")
    elif role == "reviewer":
        if not isinstance(inputs, ReviewerInputs):
            raise ValueError("reviewer requires ReviewerInputs")
        lines.append(f"- task_id: {inputs.task_id}")
        lines.append(f"- baseline_tree: {inputs.baseline_tree}")
        lines.append(f"- actual_tree: {inputs.actual_tree}")
        lines.append("- allowed_files:")
        lines.extend(f"  - {f}" for f in inputs.allowed_files)
        lines.append("- proposal_text:")
        lines.append(f"  {inputs.proposal_text}")
        lines.append("- changes_summary:")
        lines.append(f"  {inputs.changes_summary}")
        lines.append("- patch_text:")
        lines.append(inputs.patch_text)
    else:
        raise ValueError(f"unknown role: {role}")
    return "任务输入：\n" + "\n".join(lines)
