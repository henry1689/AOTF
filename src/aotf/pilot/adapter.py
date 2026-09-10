"""AOTF M0-E2 pilot RoleRunner 真实 adapter（唯一 import SDK/agents 组装点）。

把 E1 编排引擎的 RoleRunner 抽象（orchestrate.RoleRunner / RoleOutcome）落
到 D1–D4 机器。2026-09-09 控制权加固后，所有真实角色调用固定为单 turn、
tools=[]、无 settings/MCP；Implementer 只接收 AOTF 生成的批准文件快照并返回
typed mutation intents，Reviewer 只接收 AOTF 生成的机械 patch。模型不再直接
拥有 Edit/Write/Bash，也不负责重试、状态推进或任务终止。

- RealRoleRunner.run(role, inputs)：真实组装；inputs 来自 engine（implementer
  含 proposal_text/allowed_files/worktree_path/baseline_tree；reviewer 含
  actual_tree/changes_summary），adapter 校验并构造 D3 typed inputs，畸形抛
  ValueError（engine fail-closed → SAFE_HALT，#26）。
- with_fake(scenario)：零 token demo RoleRunner（供 fake 测试/演示）。fake 也只
  返回 mutation intents，由 controller 执行；out_of_scope 在写前拒绝，failing
  由 controller 应用后交 EvidenceRunner/Policy 判定，budget 直接 SAFE_HALT。
  reviewer 直接 PASS（等价 E1 FakeRoles review="PASS"）。

2026-09-09 加固：真实 ResultMessage 的 result/structured_output/resolved_model
已进入 SDKRunOutcome；Reviewer 完成态必须机械解析为 PASS/CONCERNS/BLOCK，
缺失或畸形均抛 ValueError 并由 engine SAFE_HALT。三角色使用显式最小工具集，
空工具集不再扩成 SDK 默认全集。本模块是 pilot 内唯一允许 import
aotf.agents.* 的模块。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path, PureWindowsPath

from aotf.agents.claude_sdk import ClaudeSdkRunner
from aotf.agents.reports import (parse_implementer_report,
                                 parse_reviewer_report)
from aotf.agents.roles import (
    ROLE_IDS,
    ImplementerInputs,
    ReviewerInputs,
    render_role_inputs,
    role_system_prompt,
)
from aotf.agents.schema import (
    SDKReason,
    SDKRunOutcome,
    SdkRunContext,
)
from aotf.agents.tools import filter_visible_tools
from aotf.controller import FileMutation, snapshot_files
from aotf.orchestrate import RoleOutcome

__all__ = ["RealRoleRunner", "review_verdict", "role_outcome", "sdk_context"]

_DEFAULT_MAX_TURNS = {"implementer": 1, "reviewer": 1}
_DEMO_SCENARIOS = ("ok", "out_of_scope", "failing", "budget")
_ROLE_VISIBLE_TOOLS = {
    "planner": ("Read", "Glob", "Grep"),
    # Implementer 只接收 AOTF 生成的批准文件快照，零工具、单 turn。模型只
    # 提 mutation intents；AOTF controller 才能落盘。
    "implementer": (),
    "reviewer": (),
}
_SIDE_EFFECT_TOOLS = ("Edit", "Write", "Bash", "WebFetch", "WebSearch",
                      "Agent", "Task")

_STATUS = {
    SDKReason.COMPLETED: "completed",
    SDKReason.TIMEOUT: "timeout",
    SDKReason.BUDGET_EXCEEDED: "budget_exceeded",
    SDKReason.CANCELLED: "cancelled",
    SDKReason.ERROR: "failed",
}


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def _abs_no_parent(value: str, name: str) -> None:
    _require(isinstance(value, str) and bool(value), f"{name} must be str")
    p = Path(value)
    win = PureWindowsPath(value)
    absolute = p.is_absolute() or bool(win.drive and win.root)
    _require(absolute and ".." not in p.parts and ".." not in win.parts,
             f"{name} must be absolute without '..'")


def sdk_context(
    role: str,
    *,
    cwd: str,
    model: str,
    allowed: tuple[str, ...],
    max_turns: int,
    max_budget_usd: Decimal,
) -> SdkRunContext:
    """组装一次 SDK 运行的执行参数（D4 §7.3）。"""
    _require(role in ROLE_IDS, f"unknown role: {role}")
    _abs_no_parent(cwd, "cwd")
    _require(isinstance(allowed, tuple), "allowed must be a tuple")
    return SdkRunContext(
        cwd=cwd,
        requested_model=model,
        visible_tools=filter_visible_tools(
            role, visible=_ROLE_VISIBLE_TOOLS[role]),
        denied_rules=_SIDE_EFFECT_TOOLS,
        max_turns=1,
        max_budget_usd=max_budget_usd,
        permission_mode="dontAsk",
    )


def role_outcome(outcome: SDKRunOutcome, *, role: str) -> RoleOutcome:
    """SDKRunOutcome → RoleOutcome（status 映射 + tokens/cost/error 透传）。"""
    if not isinstance(outcome, SDKRunOutcome):
        raise ValueError("expected SDKRunOutcome")
    _require(role in ROLE_IDS, f"unknown role: {role}")
    verdict = None
    mutations: tuple[FileMutation, ...] = ()
    status = _STATUS[outcome.reason]
    error_code = (outcome.error_code if outcome.reason == SDKReason.ERROR
                  else None)
    if role in ("implementer", "reviewer") \
            and outcome.reason == SDKReason.COMPLETED:
        if outcome.structured_output is not None:
            result_text = (outcome.structured_output
                           if isinstance(outcome.structured_output, str)
                           else json.dumps(outcome.structured_output,
                                           ensure_ascii=False))
        else:
            result_text = outcome.result_text
        if result_text is None:
            raise ValueError(f"{role} output missing")
        if role == "reviewer":
            verdict = review_verdict(result_text)
        else:
            try:
                obj = json.loads(result_text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"implementer output not JSON: {exc}") from None
            try:
                report = parse_implementer_report(obj)
            except ValueError as exc:
                raise ValueError(f"invalid implementer report: {exc}") from None
            if report.outcome == "escalated":
                status, error_code = "cancelled", "implementer_escalated"
            else:
                mutations = tuple(FileMutation(
                    path=item.path, operation=item.operation,
                    expected_sha256=item.expected_sha256,
                    content=item.content) for item in report.mutations)
    return RoleOutcome(
        status=status,
        verdict=verdict,
        mutations=mutations,
        resolved_model=outcome.resolved_model,
        input_tokens=outcome.usage.input_tokens,
        output_tokens=outcome.usage.output_tokens,
        estimated_cost_usd=outcome.estimated_cost_usd,
        error_code=error_code,
    )


def review_verdict(result_text: str) -> str:
    """reviewer 结果 JSON 文本 → verdict（PASS/CONCERNS/BLOCK）。畸形 ValueError。

    live reviewer 文本回传的解析入口（#26 fail-closed：畸形不推进）。
    """
    _require(isinstance(result_text, str) and bool(result_text.strip()),
             "reviewer output empty")
    try:
        obj = json.loads(result_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"reviewer output not JSON: {exc}") from None
    try:
        report = parse_reviewer_report(obj)
    except ValueError as exc:
        raise ValueError(f"invalid reviewer report: {exc}") from None
    return report.verdict


class RealRoleRunner:
    """实现 orchestrate.RoleRunner：真实 Claude SDK 组装（E1 引擎注入）。"""

    def __init__(
        self,
        task_id: str,
        *,
        cwd: str,
        model: str = "deepseek",
        max_budget_usd: Decimal = Decimal("5"),
        max_turns: dict[str, int] | None = None,
        query_fn=None,
    ) -> None:
        _require(isinstance(task_id, str) and bool(task_id), "task_id required")
        _abs_no_parent(cwd, "cwd")
        _require(isinstance(model, str) and bool(model), "model required")
        _require(isinstance(max_budget_usd, Decimal) and max_budget_usd >= 0,
                 "max_budget_usd must be non-negative Decimal")
        self._task_id = task_id
        self._cwd = cwd
        self._model = model
        self._budget = max_budget_usd
        self._max_turns = {**_DEFAULT_MAX_TURNS, **(max_turns or {})}
        _require(all(self._max_turns.get(role) == 1
                     for role in ("implementer", "reviewer")),
                 "model roles must be a single controller-owned turn")
        self._query_fn = query_fn

    def _typed_inputs(self, role: str, inputs: dict):
        try:
            if role == "implementer":
                return ImplementerInputs(
                    task_id=self._task_id,
                    proposal_text=inputs["proposal_text"],
                    allowed_files=tuple(inputs["allowed_files"]),
                    worktree_path=inputs["worktree_path"],
                    baseline_tree=inputs["baseline_tree"],
                )
            return ReviewerInputs(
                task_id=self._task_id,
                proposal_text=inputs["proposal_text"],
                allowed_files=tuple(inputs["allowed_files"]),
                baseline_tree=inputs["baseline_tree"],
                actual_tree=inputs["actual_tree"],
                changes_summary=inputs["changes_summary"],
                patch_text=inputs["patch_text"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"missing/malformed {role} input: {exc}") from None

    async def run(self, role: str, *, inputs: dict) -> RoleOutcome:
        if role not in ("implementer", "reviewer"):
            raise ValueError(
                f"pilot runner supports implementer/reviewer only: {role}")
        typed = self._typed_inputs(role, inputs)
        prompt = (role_system_prompt(role) + "\n\n"
                  + render_role_inputs(role, typed))
        allowed = tuple(inputs["allowed_files"])
        run_cwd = inputs["worktree_path"] if role == "implementer" else self._cwd
        if role == "implementer":
            snapshots = snapshot_files(run_cwd, allowed)
            prompt += ("\n\nAOTF 批准文件快照（唯一可用源码事实；JSON）：\n"
                       + json.dumps([asdict(item) for item in snapshots],
                                    ensure_ascii=False, separators=(",", ":")))
        ctx = sdk_context(
            role, cwd=run_cwd, model=self._model, allowed=allowed,
            max_turns=self._max_turns.get(role, 30),
            max_budget_usd=self._budget)
        runner = ClaudeSdkRunner(
            prompt=prompt, query_fn=self._query_fn, role=role,
            allowed_files=allowed, tool_decider=None)
        outcome = await runner.run(ctx)
        return role_outcome(outcome, role=role)

    def with_fake(self, *, scenario: str = "ok") -> "_DemoRoleRunner":
        """零 token demo RoleRunner（供 fake 演示/测试）。见模块 docstring。"""
        _require(scenario in _DEMO_SCENARIOS, f"unknown scenario: {scenario}")
        return _DemoRoleRunner(self, scenario)


class _DemoRoleRunner:
    """确定性 mutation-intent RoleRunner（with_fake 返回，零模型调用）。"""

    def __init__(self, owner: RealRoleRunner, scenario: str) -> None:
        self._owner = owner
        self._scenario = scenario

    async def run(self, role: str, *, inputs: dict) -> RoleOutcome:
        if role == "reviewer":
            # demo 使用确定性 PASS，不模拟真实模型文本。
            return RoleOutcome(status="completed", verdict="PASS")
        if role != "implementer":
            raise ValueError(f"unexpected role {role}")
        allowed = tuple(inputs["allowed_files"])
        wt = inputs["worktree_path"]
        if self._scenario == "budget":
            return RoleOutcome(status="budget_exceeded",
                               estimated_cost_usd=Decimal("1.00"))
        if self._scenario == "out_of_scope":
            mutation = FileMutation(
                path="demo-evil.py", operation="create",
                expected_sha256=None, content="x\n")
        else:
            target = Path(wt) / allowed[0]
            before = target.read_bytes() if target.exists() else None
            suffix = ("\nraise RuntimeError('demo injected failing change')\n"
                      if self._scenario == "failing" else "# demo edit\n")
            mutation = FileMutation(
                path=allowed[0],
                operation="replace" if before is not None else "create",
                expected_sha256=(hashlib.sha256(before).hexdigest()
                                 if before is not None else None),
                content=(before or b"").decode("utf-8") + suffix)
        return RoleOutcome(
            status="completed", mutations=(mutation,),
            input_tokens=12, output_tokens=6,
            estimated_cost_usd=Decimal("0.01"))
