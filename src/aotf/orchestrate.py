"""AOTF 任务闭环编排引擎（M0-E1）。

把 M0-A..D 各机器串成「单任务闭环」（spec §5.1 主流程，逐合法转换）：
EDIT_AUTHORIZED →(授权校验)→ IMPLEMENTING →(implementer mutation proposal +
controller apply + checkpoint + delta)→ DELTA_CAPTURED →(evidence 产)→ EVIDENCE_RUNNING →(推进)→
REVIEWING →(reviewer)→ POLICY_EVALUATING →(policy)→ CHECKPOINT_READY /
FAILED / SAFE_HALT。

设计裁决：
- 确定性单任务引擎（非守护循环）；试点/E2 harness 每任务调一次；重入时若
  已到 ready/terminal 直接返回（不重复副作用）。
- 角色执行经 RoleRunner 抽象注入（E1 fake 全测零真实 token；E2 harness 以
  ClaudeSdkRunner + roles render + tools 实现之），本模块**不 import**
  claude_agent_sdk 与 agents.roles/reports 细节（render/parse 在适配层）。
- per-task 上下文（proposal/allowed_files/test_plan）经 TaskPlan 注入，不
  改 DB schema；authorization 绑定 scope 摘要校验（决定 + human + 未过期）。
- 机械异常统一 fail-closed → SAFE_HALT（不猜测恢复）；明确「delta 空 /
  agent 非 completed」→ FAILED（合法终态）。
- evidence 每个连续进程闭环仅产一次（DELTA_CAPTURED 阶段产并存
  self.evidence；POLICY_EVALUATING 复用，不重跑不重复 ingest）。
- Reviewer 只有明确 PASS 才能进入策略阶段；缺失、CONCERNS、BLOCK 全部
  SAFE_HALT。每次状态转换与 Agent run 使用各自实际时间戳。
- AOTF 持有唯一控制权：真实模型调用单 turn、零工具；模型只返回 typed
  mutation/decision。工作树写入、preimage 校验、checkpoint、重试、证据、
  状态转换和终止均由 controller 执行；Agent 直接写 worktree 立即 SAFE_HALT。

已知边界：task.worktree_path/actual_tree/delta_sha256 与 Reviewer/evidence
上下文尚未 materialize。为防重复 Agent、副作用或空 verdict 推进，新进程从
IMPLEMENTING..POLICY_EVALUATING 中间阶段重入时明确 SAFE_HALT；完整自动恢复
须等 execution snapshot 持久化后再开放。
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Mapping, Protocol

from aotf.artifacts.store import ArtifactError
from aotf.controller import (ControllerMutationError, FileMutation,
                             apply_mutations)
from aotf.errors import AotfError
from aotf.evidence.bundle import EvidenceBundleResult, produce_evidence
from aotf.evidence.runner import CommandSpec
from aotf.evidence.schema import EvidenceError
from aotf.git.base import GitCommandError, run_git
from aotf.git.checkpoint import (CheckpointError, OutOfScopeError,
                                 checkpoint_commit)
from aotf.git.delta import DeltaError, DeltaReport, capture_delta
from aotf.git.preflight import preflight
from aotf.git.worktree import WorktreeError, create_worktree
from aotf.models import TERMINAL_TASK_PHASES, TaskPhase
from aotf.orchestrator import normalize_status, record_run
from aotf.policy.engine import ControlFacts, evaluate
from aotf.policy.rules import PolicyError
from aotf.retry import (
    BudgetExceededError,
    RateLimitError,
    RetryableError,
    TemporaryNetworkError,
    run_with_retry,
)
from aotf.runner import AgentRunRequest, AgentRunResult
from aotf.snapshot import (
    SnapshotRecord,
    delete_snapshots_for_task,
    load_snapshot,
    save_snapshot,
)
from aotf.state import transition
from aotf.store import get_approval, get_task

__all__ = ["EngineConfig", "RoleOutcome", "RoleRunner", "TaskPlan",
           "run_task_closed_loop"]

_ROLE_MAX_TURNS_DEFAULT = {"planner": 1, "implementer": 1, "reviewer": 1}
_MAX_MODEL_INPUT_BYTES = 1_000_000
_READY_PHASES = frozenset({TaskPhase.CHECKPOINT_READY,
                           TaskPhase.RELEASE_CANDIDATE})


class _Stop(Exception):
    """内部终止信号（转终态）。"""

    def __init__(self, phase: TaskPhase, reason: str) -> None:
        self.phase = phase
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """per-task 试点输入（外部/E2 harness 提供，不入 DB 新表）。"""

    task_id: str
    proposal_text: str
    allowed_files: tuple[str, ...]
    test_plan: tuple[tuple[str, str], ...] = (("unit", "unit"),)
    release_gates: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """试点运行配置（主仓/管理根/证据命令/agent 限额）。"""

    base_repo: str
    worktrees_root: str
    artifacts_root: str
    scratch_root: str
    registry: Mapping[str, CommandSpec]
    model_alias: str = "opus"
    agent_max_turns: dict[str, int] = field(
        default_factory=lambda: dict(_ROLE_MAX_TURNS_DEFAULT))
    agent_max_budget_usd: Decimal = Decimal("20")
    agent_deadline_minutes: int = 120
    agent_max_retries: int = 3  # M1: 重试次数


@dataclass(frozen=True, slots=True)
class RoleOutcome:
    """RoleRunner 一次运行结果（适配层已做 roles render/parse）。"""

    status: Literal["completed", "failed", "timeout", "budget_exceeded",
                    "cancelled"] = "completed"
    verdict: Literal["PASS", "CONCERNS", "BLOCK"] | None = None  # reviewer
    mutations: tuple[FileMutation, ...] = ()  # implementer proposal; controller applies
    resolved_model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: Decimal = Decimal("0")
    error_code: str | None = None


class RoleRunner(Protocol):
    """角色执行抽象：E1 fake 全测；E2 以 ClaudeSdkRunner + roles/tools 实现。"""

    async def run(self, role: str, *, inputs: dict) -> RoleOutcome: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _scope_sha(allowed: tuple[str, ...], proposal: str) -> str:
    payload = "\n".join(sorted(allowed)) + "\x00" + proposal
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _input_digest(inputs: Mapping[str, object]) -> str:
    """run 输入审计摘要：绑定值（非仅 key 名），确定性。"""
    payload = "\x01".join(f"{k}={v!r}" for k, v in sorted(inputs.items()))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


class _Engine:
    def __init__(self, conn: sqlite3.Connection, plan: TaskPlan,
                 cfg: EngineConfig, role_runner: RoleRunner) -> None:
        self.conn = conn
        self.plan = plan
        self.cfg = cfg
        self.role_runner = role_runner
        self.task = get_task(conn, plan.task_id)
        if self.task is None:
            raise ValueError(f"task not found: {plan.task_id}")
        self.worktree: str | None = None
        self.delta: DeltaReport | None = None
        self.evidence: EvidenceBundleResult | None = None
        self.review_verdict: str | None = None

    def _move(self, to_phase: TaskPhase, reason: str | None = None) -> None:
        self.task = transition(self.conn, task_id=self.plan.task_id,
                               to_phase=to_phase, terminal_reason=reason,
                               event_id=_new_id(), at=_now())

    def _stop(self, phase: TaskPhase, reason: str) -> None:
        raise _Stop(phase, reason)

    def _authorize(self) -> None:
        now = _now()
        approval = get_approval(self.conn, self.task.authorization_id) \
            if self.task.authorization_id else None
        if approval is None or approval.source != "human" \
                or approval.decision != "approved":
            self._stop(TaskPhase.SAFE_HALT, "approval invalid")
        if approval.expires_at is not None and approval.expires_at < now:
            self._stop(TaskPhase.SAFE_HALT, "approval expired")
        if approval.scope_sha256 != _scope_sha(self.plan.allowed_files,
                                               self.plan.proposal_text):
            self._stop(TaskPhase.SAFE_HALT, "approval scope mismatch")
        self._move(TaskPhase.IMPLEMENTING)

    def _baseline_tree(self) -> str:
        if self.task.baseline_tree:
            return self.task.baseline_tree
        return self.delta.base_tree if self.delta else ""

    def _ensure_worktree(self) -> str:
        if self.worktree is not None:
            return self.worktree
        import os

        branch = self.task.worktree_branch or f"task-{self.plan.task_id}"
        path = f"{self.cfg.worktrees_root}/{self.plan.task_id}"
        if os.path.isdir(path):  # 重入复用；不存在则直接建（不跑 git 探活）
            if run_git(path, "rev-parse", "--is-inside-work-tree",
                       timeout=15).strip() == "true":
                self.worktree = path
                return path
            self._stop(TaskPhase.SAFE_HALT, "stale worktree path")
        info = create_worktree(
            self.cfg.base_repo, worktree_path=path, branch=branch,
            allowed_files=self.plan.allowed_files, at=_now())
        self.worktree = info.worktree_path
        return self.worktree

    async def _run_agent(self, role: str, inputs: dict) -> RoleOutcome:
        """带 snapshot + retry 的 agent 执行（M1）。"""
        started_at = _now()
        deadline = started_at + timedelta(minutes=self.cfg.agent_deadline_minutes)
        req = AgentRunRequest(
            run_id=_new_id(), task_id=self.plan.task_id, role=role,
            requested_model=self.cfg.model_alias,
            input_digest=_input_digest(inputs),
            max_turns=1,
            max_budget_usd=self.cfg.agent_max_budget_usd,
            deadline_at=deadline)
        
        try:
            # 带重试执行
            oc = await run_with_retry(
                self.role_runner.run,
                role,
                inputs=inputs,
                max_retries=self.cfg.agent_max_retries,
            )
        except (BudgetExceededError, RetryableError):
            # 预算超限或重试耗尽
            self._stop(TaskPhase.SAFE_HALT, f"agent {role} failed after retries")
        except (RateLimitError, TemporaryNetworkError) as exc:
            # 可重试错误耗尽
            self._stop(TaskPhase.SAFE_HALT, f"agent {role} {exc}")
        
        result = AgentRunResult(
            run_id=req.run_id, status=oc.status,
            resolved_model=oc.resolved_model,
            input_tokens=oc.input_tokens, output_tokens=oc.output_tokens,
            estimated_cost_usd=oc.estimated_cost_usd,
            output_artifact_id=None, error_code=oc.error_code)
        ended_at = _now()
        record_run(self.conn, request=req, result=result,
                   started_at=started_at, ended_at=ended_at)
        norm = normalize_status(req, result, ended_at)
        if norm != "completed":
            self._stop(TaskPhase.SAFE_HALT, f"agent {role} {norm}")
        
        # M1: 保存 snapshot
        self._save_snapshot(role, inputs, oc)
        
        return oc
    
    def _save_snapshot(self, role: str, inputs: dict, outcome: RoleOutcome) -> None:
        """持久化 execution snapshot（M1）。"""
        import json as _json
        from dataclasses import asdict
        from datetime import datetime
        
        def _dt_iso(dt):
            return dt.isoformat() if isinstance(dt, datetime) else dt
        
        snapshot_id = f"snap-{self.plan.task_id}-{role}-{_now().isoformat()}"
        # mutations 可能是 list 或 tuple，统一转为 tuple[str, ...]
        mutations_list = list(outcome.mutations) if outcome.mutations else []
        mutations_paths = tuple(m.path for m in mutations_list)
        evidence_json = None
        if self.evidence is not None:
            # 只序列化 record 和 results，排除 bytes 字段
            from dataclasses import asdict
            ev_dict = {
                'record': asdict(self.evidence.record),
                'results': [asdict(r) if hasattr(r, '__dataclass_fields__') else str(r) for r in self.evidence.results],
                'artifacts': [asdict(a) if hasattr(a, '__dataclass_fields__') else str(a) for a in self.evidence.artifacts],
            }
            # 递归转换 datetime 字段
            def _convert_special(obj):
                from datetime import datetime
                if isinstance(obj, datetime):
                    return obj.isoformat()
                if isinstance(obj, bytes):
                    return obj.hex()
                if isinstance(obj, dict):
                    return {k: _convert_special(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_convert_special(i) for i in obj]
                return obj
            evidence_json = _json.dumps(_convert_special(ev_dict), ensure_ascii=False)
        record = SnapshotRecord(
            snapshot_id=snapshot_id,
            task_id=self.plan.task_id,
            role=role,
            phase_before=self.task.phase.value,
            phase_after=self.task.phase.value,
            worktree_path=self.worktree or "",
            baseline_tree=self._baseline_tree(),
            actual_tree=self.delta.actual_tree if self.delta else None,
            mutations_applied=mutations_paths,
            delta_sha256=self.delta.patch_sha256 if self.delta else None,
            evidence_json=evidence_json,
            review_verdict=outcome.verdict,
            run_inputs_json=_json.dumps(inputs, ensure_ascii=False),
            created_at=_now(),
            updated_at=_now(),
        )
        save_snapshot(self.conn, record)
    
    def _resume_from_snapshot(self, snap: SnapshotRecord) -> None:
        """从 snapshot 恢复上下文（M1，仅填充内存状态，不实际重入执行）。"""
        # 注意：当前仍 SAFE_HALT，等待完整 materialization
        # 这里只作为框架占位，避免直接跳过验证
        import logging
        logging.info(f"Found incomplete snapshot for {snap.task_id}/{snap.role}, will HALT")
        self._stop(TaskPhase.SAFE_HALT, f"snapshot exists but recovery not yet implemented")

    async def _implement(self) -> None:
        wt = self._ensure_worktree()
        outcome = await self._run_agent(
            "implementer",
            {"proposal_text": self.plan.proposal_text,
             "allowed_files": self.plan.allowed_files,
             "worktree_path": wt,
             "baseline_tree": self._baseline_tree()})
        # Any dirty state before controller application means the model/transport
        # acquired an unapproved side-effect path. Never bless it post hoc.
        if preflight(wt).verdict != "clean":
            self._stop(TaskPhase.SAFE_HALT,
                       "controller ownership violated: agent mutated worktree")
        try:
            apply_mutations(wt, self.plan.allowed_files, outcome.mutations)
        except ControllerMutationError as exc:
            self._stop(TaskPhase.SAFE_HALT,
                       f"controller mutation rejected: {exc}")
        try:
            checkpoint_commit(wt, allowed_files=self.plan.allowed_files,
                              message="aotf checkpoint")
        except OutOfScopeError as exc:
            self._stop(TaskPhase.SAFE_HALT, f"out of scope: {exc}")
        except CheckpointError:
            self._stop(TaskPhase.SAFE_HALT, "no changes")
        report = capture_delta(wt, manifest_path=f"{wt}.baseline-manifest.json")
        if not report.changes:
            self._stop(TaskPhase.SAFE_HALT, "no changes")
        self.delta = report
        self._move(TaskPhase.DELTA_CAPTURED)

    def _produce_evidence(self) -> EvidenceBundleResult:
        if self.delta is None or self.worktree is None:
            raise RuntimeError("delta/worktree not ready")
        for _, command_id in self.plan.test_plan:
            if command_id not in self.cfg.registry:
                self._stop(TaskPhase.SAFE_HALT,
                           f"unknown evidence command: {command_id}")
        return produce_evidence(
            self.worktree, task_id=self.plan.task_id, evidence_id=_new_id(),
            worktree_tree=self.delta.actual_tree,
            delta_sha256=self.delta.patch_sha256,
            plan=self.plan.test_plan, registry=self.cfg.registry,
            artifacts_root=self.cfg.artifacts_root,
            scratch_root=self.cfg.scratch_root,
            runner_identity="aotf-evidence-runner-v1")

    async def _evidence(self) -> None:
        # DELTA_CAPTURED：产 evidence（仅一次）并推进 EVIDENCE_RUNNING
        self.evidence = self._produce_evidence()
        self._move(TaskPhase.EVIDENCE_RUNNING)

    async def _evidence_advance(self) -> None:
        if self.evidence is None:  # 重入 EVIDENCE_RUNNING 兜底
            self.evidence = self._produce_evidence()
        self._move(TaskPhase.REVIEWING)

    async def _review(self) -> None:
        if self.delta is None or self.worktree is None:
            raise RuntimeError("delta not ready")
        changes = ", ".join(path for _, path in self.delta.changes)
        patch = run_git(
            self.worktree, "diff", "--no-ext-diff", "--no-color",
            "--no-renames", self.delta.base_commit,
            self.delta.checkpoint_commit)
        if len(patch.encode("utf-8", "surrogateescape")) \
                > _MAX_MODEL_INPUT_BYTES:
            self._stop(TaskPhase.SAFE_HALT, "review patch byte limit exceeded")
        oc = await self._run_agent(
            "reviewer",
            {"proposal_text": self.plan.proposal_text,
             "allowed_files": self.plan.allowed_files,
             "baseline_tree": self._baseline_tree(),
             "actual_tree": self.delta.actual_tree,
             "changes_summary": f"changed files: {changes}",
             "patch_text": patch})
        if oc.verdict != "PASS":
            self._stop(TaskPhase.SAFE_HALT,
                       f"reviewer verdict {oc.verdict or 'missing'}")
        self.review_verdict = oc.verdict
        self._move(TaskPhase.POLICY_EVALUATING)

    def _policy(self) -> None:
        if self.evidence is None:  # 重入 POLICY_EVALUATING 兜底
            self.evidence = self._produce_evidence()
        facts = ControlFacts(
            boundary_violation=False, hash_drift=False,
            blocked=(self.review_verdict != "PASS"),
            approval_valid=True, producer_mismatch=False)
        verdict = evaluate(
            self.evidence.record,
            required=tuple(cid for cid, _ in self.plan.test_plan),
            release_gates=self.plan.release_gates,
            deferred=self.plan.deferred,
            wants_release=False, facts=facts)
        if verdict.result.value == "PASS":
            self._move(TaskPhase.CHECKPOINT_READY)
        elif verdict.result.value == "FAIL":
            self._stop(TaskPhase.FAILED, "policy FAIL")
        elif verdict.result.value == "INCOMPLETE":
            self._stop(TaskPhase.SAFE_HALT, "incomplete evidence")
        else:
            self._stop(TaskPhase.SAFE_HALT, "policy ERROR")


async def run_task_closed_loop(
    conn: sqlite3.Connection, *,
    plan: TaskPlan, cfg: EngineConfig, role_runner: RoleRunner,
) -> TaskPhase:
    """单任务闭环执行 → ready/terminal phase。机械异常 fail-closed。"""
    eng = _Engine(conn, plan, cfg, role_runner)
    try:
        # M1: 检查是否有未完成的 snapshot
        for role in ("implementer", "reviewer"):
            snap = load_snapshot(conn, plan.task_id, role)
            if snap and snap.phase_before == eng.task.phase.value:
                # 有未完成的 snapshot，尝试恢复
                eng._resume_from_snapshot(snap)
                break
        
        # 中间阶段包含尚未持久化的 role/evidence 上下文。
        if eng.task.phase in {
            TaskPhase.IMPLEMENTING,
            TaskPhase.DELTA_CAPTURED,
            TaskPhase.EVIDENCE_RUNNING,
            TaskPhase.REVIEWING,
            TaskPhase.POLICY_EVALUATING,
        }:
            eng._stop(TaskPhase.SAFE_HALT,
                      f"automatic resume unsupported from "
                      f"{eng.task.phase.value}")
        while eng.task.phase not in TERMINAL_TASK_PHASES \
                and eng.task.phase not in _READY_PHASES:
            phase = eng.task.phase
            if phase == TaskPhase.EDIT_AUTHORIZED:
                eng._authorize()
            elif phase == TaskPhase.IMPLEMENTING:
                await eng._implement()
            elif phase == TaskPhase.DELTA_CAPTURED:
                await eng._evidence()
            elif phase == TaskPhase.EVIDENCE_RUNNING:
                await eng._evidence_advance()
            elif phase == TaskPhase.REVIEWING:
                await eng._review()
            elif phase == TaskPhase.POLICY_EVALUATING:
                eng._policy()
            else:
                eng._stop(TaskPhase.SAFE_HALT,
                          f"unsupported phase {phase.value}")
    except _Stop as stop:
        eng._move(stop.phase, reason=stop.reason)
    except (AotfError, GitCommandError, DeltaError, WorktreeError,
            EvidenceError, ArtifactError, PolicyError, ValueError,
            RuntimeError, sqlite3.Error, OSError) as exc:
        try:
            eng._move(TaskPhase.SAFE_HALT, reason=f"engine error: {exc}")
        except Exception as halt_exc:
            latest = get_task(conn, plan.task_id)
            if latest is None or latest.phase not in TERMINAL_TASK_PHASES:
                raise RuntimeError(
                    f"cannot persist SAFE_HALT for {plan.task_id}") \
                    from halt_exc
            eng.task = latest
    # M1: 任务终态后清理 snapshot
    if eng.task.phase in TERMINAL_TASK_PHASES or eng.task.phase in _READY_PHASES:
        delete_snapshots_for_task(conn, plan.task_id)
    latest = get_task(conn, plan.task_id)
    if latest is None:
        raise RuntimeError(f"task disappeared: {plan.task_id}")
    return latest.phase
