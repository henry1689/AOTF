"""AOTF M0-E2 pilot 执行器与指标（run_one/run_all/prove_criteria）。

把样例仓 + PilotTask 驱动成 E1 引擎闭环（bootstrap 建 cycle/task/approval →
EDIT_AUTHORIZED → run_task_closed_loop）→ PilotReport；run_all 以「共享推进
仓」连续语义：READY 任务 checkpoint 分支 ff 合回样例主仓为下一 baseline
（aotf 仓零写，样例仓独立可 commit）。interactive 每任务向 owner 请求
continue/cancel（replan/adjust Pre-MVP 记录不重排）。live 需 owner 在场 +
ANTHROPIC_API_KEY(env) + 预算上限；默认 fake 路径零 token。

prove_criteria 输出 §1.2 七项机械证明 dict[str, bool|str]（fake 全 true）：
phase_chain_legal / changes_within_allowed / evidence_policy_chain /
no_subjective_summary / final_phase_consistent / budget_halt_injected /
sample_main_untouched。
"""

from __future__ import annotations

import hashlib
import json
import time
import os
import sys
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from aotf import db, state, store
from aotf.agents.roles import ROLE_SYSTEM_PROMPTS, ReviewerInputs
from aotf.artifacts.store import list_artifacts
from aotf.evidence.runner import CommandSpec, build_registry
from aotf.git.base import GitCommandError, run_git
from aotf.git.preflight import preflight
from aotf.models import (ApprovalRecord, CycleRecord, TERMINAL_TASK_PHASES,
                         TaskPhase, TaskRecord)
from aotf.orchestrate import EngineConfig, TaskPlan, run_task_closed_loop
from aotf.pilot.adapter import RealRoleRunner
from aotf.pilot.tasks import PilotTask, SAMPLE_TASKS

__all__ = ["PilotReport", "prove_criteria", "run_all", "run_one"]

_READY_PHASES = frozenset({TaskPhase.CHECKPOINT_READY,
                           TaskPhase.RELEASE_CANDIDATE})
_READY_VALUES = frozenset({p.value for p in _READY_PHASES})
_CANCEL_WORDS = frozenset({"c", "cancel", "x", "取消"})


@dataclass(frozen=True, slots=True)
class PilotReport:
    """单任务试点报告（真实/fake 同构）。"""

    task_id: str
    final_phase: str
    terminal_reason: str | None
    agent_runs: int
    evidence_ok: bool
    tokens: dict
    cost_usd: Decimal
    duration_s: float
    human_interventions: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scope_sha(allowed: tuple[str, ...], proposal: str) -> str:
    return hashlib.sha256(("\n".join(sorted(allowed)) + "\x00" + proposal)
                          .encode("utf-8")).hexdigest()


def _proposal_sha(proposal: str) -> str:
    return hashlib.sha256(proposal.encode("utf-8")).hexdigest()



    candidates = [sys.executable]
    # Windows 常见路径
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        for ver in ("313", "312", "311"):
            py = os.path.join(local, "Programs", "Python", f"Python{ver}", "python.exe")
            if os.path.isfile(py) and py not in candidates:
                candidates.append(py)

    import subprocess
    for py in candidates:
        result = subprocess.run([py, "-m", "pytest", "--version"],
                              capture_output=True)
        if result.returncode == 0:
            return py

    # 回退到当前 python（可能失败）
    return sys.executable


    python = _find_python_with_pytest()
    spec = CommandSpec(command_id="pytest",
                       argv=(python, "-B", "-m",
                             "pytest", "-p", "no:cacheprovider", "-q"),
                       cwd=".", timeout=180.0)
    return build_registry((spec,))




def _pytest_registry():
    python = _find_python_with_pytest()
    spec = CommandSpec(command_id="pytest",
                       argv=(python, "-B", "-m",
                             "pytest", "-p", "no:cacheprovider", "-q"),
                       cwd=".", timeout=180.0)
    return build_registry((spec,))
def _find_python_with_pytest() -> str:
    """查找有 pytest 的 python 可执行文件。

    注意：必须用 subprocess 检查（不用 import），因为 WindowsApps python
    stub 在 import 时可访问用户 site-packages，但 subprocess 继承的
    最小化环境看不到（PYTHONNOUSERSITE=1 + 用户 site 路径不同）。
    
    优先选择系统级 python（Program Files/Local/Programs），因为这些 python
    的 site-packages 在系统路径下，不受 PYTHONNOUSERSITE 影响。
    """
    import subprocess

    # Windows 系统级 python 优先
    if sys.platform == "win32":
        candidates = []
        # 系统级路径
        for prefix in [os.environ.get("LOCALAPPDATA", ""), r"C:\Program Files", r"C:\Program Files (x86)"]:
            if not prefix:
                continue
            for ver in ("313", "312", "311"):
                py = os.path.join(prefix, "Programs", "Python", f"Python{ver}", "python.exe")
                if os.path.isfile(py):
                    candidates.append(py)
        # 添加当前 python 作为最后选项
        if sys.executable not in candidates:
            candidates.append(sys.executable)
    else:
        candidates = [sys.executable]

    for py in candidates:
        result = subprocess.run([py, "-m", "pytest", "--version"],
                              capture_output=True)
        if result.returncode == 0:
            # 验证最小化环境下也能运行
            env = dict(os.environ)
            env["PYTHONNOUSERSITE"] = "1"
            result2 = subprocess.run([py, "-m", "pytest", "--version"],
                                   capture_output=True, env=env)
            if result2.returncode == 0:
                return py

    # 回退到当前 python（可能失败）
    return sys.executable


def _pytest_registry():
    python = _find_python_with_pytest()
    spec = CommandSpec(command_id="pytest",
                       argv=(python, "-B", "-m",
                             "pytest", "-p", "no:cacheprovider", "-q"),
                       cwd=".", timeout=180.0)
    return build_registry((spec,))

async def run_one(conn, *, sample_root: str, task: PilotTask, db_root: str,
                  live: bool = False, model: str = "deepseek",
                  budget: Decimal | None = None, interactive: bool = False,
                  role_runner=None, registry=None) -> PilotReport:
    """单任务闭环：bootstrap + engine + 指标。live=False 全 fake 零 token。"""
    rep = preflight(sample_root)
    if rep.verdict != "clean" or not rep.head_commit:
        raise ValueError(f"sample root not clean: {rep.verdict}")
    budget_val = task.budget_usd if budget is None else budget
    if not isinstance(budget_val, Decimal) or budget_val < 0:
        raise ValueError("budget must be non-negative Decimal")
    base = Path(db_root)
    wt_root = base / "worktrees"
    task_id = f"{task.id}-{uuid.uuid4().hex[:8]}"
    at = _now()
    proposal_sha = _proposal_sha(task.proposal)
    cycle_id, approval_id = f"cycle-{task_id}", f"ap-{task_id}"
    store.insert_cycle(conn, CycleRecord(
        cycle_id=cycle_id, project_id="samplelib-pilot", status="ACTIVE",
        version=0, created_at=at, updated_at=at))
    store.insert_task(conn, TaskRecord(
        task_id=task_id, cycle_id=cycle_id, supersedes_task_id=None,
        phase=TaskPhase.CREATED, version=0,
        baseline_commit=rep.head_commit, baseline_tree=rep.head_tree,
        proposal_sha256=proposal_sha, authorization_id=approval_id,
        worktree_path=None, worktree_branch=None, actual_tree=None,
        delta_sha256=None, terminal_reason=None, created_at=at,
        updated_at=at))
    store.insert_approval(conn, ApprovalRecord(
        approval_id=approval_id, task_id=task_id, decision="approved",
        scope_sha256=_scope_sha(task.allowed_files, task.proposal),
        proposal_sha256=proposal_sha, baseline_tree=rep.head_tree,
        source="human", expires_at=None, created_at=at))
    conn.commit()
    for phase in (TaskPhase.BASELINING, TaskPhase.PLAN_READY,
                  TaskPhase.AWAITING_PLAN_APPROVAL,
                  TaskPhase.EDIT_AUTHORIZED):
        state.transition(conn, task_id=task_id, to_phase=phase,
                         terminal_reason=None,
                         event_id=f"bootstrap-{task_id}-{phase.value}", at=at)
    cfg = EngineConfig(
        base_repo=str(sample_root), worktrees_root=str(wt_root),
        artifacts_root=str(base / "artifacts"),
        scratch_root=str(base / "scratch"),
        registry=task.registry if task.registry else _pytest_registry(), model_alias=model,
        agent_max_budget_usd=budget_val)
    plan = TaskPlan(task_id=task_id, proposal_text=task.proposal,
                    allowed_files=task.allowed_files,
                    test_plan=task.test_plan)
    if role_runner is None:
        real = RealRoleRunner(task_id, cwd=str(wt_root / task_id),
                              model=model, max_budget_usd=budget_val)
        runner = real if live else real.with_fake(scenario=task.fault or "ok")
    else:
        runner = role_runner
    started = time.perf_counter()
    final = await run_task_closed_loop(conn, plan=plan, cfg=cfg,
                                       role_runner=runner)
    duration_s = time.perf_counter() - started

    interventions = 0
    if interactive:
        choice = input(f"[pilot] {task_id} final={final.value}; "
                       "continue/replan/adjust/cancel> ").strip().lower()
        interventions = 1
        if choice in _CANCEL_WORDS:
            rec = store.get_task(conn, task_id)
            if rec is not None and rec.phase not in TERMINAL_TASK_PHASES:
                state.transition(conn, task_id=task_id,
                                 to_phase=TaskPhase.CANCELLED,
                                 terminal_reason="owner cancel",
                                 event_id=f"cancel-{task_id}", at=_now())
    rec = store.get_task(conn, task_id)
    if rec is None:
        raise RuntimeError(f"task {task_id} missing after run")
    rows = conn.execute(
        "SELECT role, input_tokens, output_tokens, estimated_cost_usd "
        "FROM agent_runs WHERE task_id=?", (task_id,)).fetchall()
    tokens: dict[str, dict[str, int]] = {}
    cost = Decimal("0")
    for role, it, ot, est in rows:
        bucket = tokens.setdefault(role, {"input": 0, "output": 0})
        bucket["input"] += int(it or 0)
        bucket["output"] += int(ot or 0)
        if est is not None:
            cost += Decimal(str(est))
    return PilotReport(
        task_id=task_id, final_phase=rec.phase.value,
        terminal_reason=rec.terminal_reason, agent_runs=len(rows),
        evidence_ok=rec.phase in _READY_PHASES, tokens=tokens,
        cost_usd=cost, duration_s=round(duration_s, 3),
        human_interventions=interventions)


def _promote(sample_root: str, task_id: str, worktrees_root: str) -> None:
    """checkpoint 分支 ff 合回样例主仓并移除 worktree（连续 baseline）。"""
    run_git(sample_root, "merge", "--ff-only", f"task-{task_id}")
    run_git(sample_root, "worktree", "remove", "--force",
            str(Path(worktrees_root) / task_id))


async def run_all(*, sample_root: str, db_root: str,
                  tasks: Sequence[PilotTask] = SAMPLE_TASKS,
                  live: bool = False, model: str = "deepseek",
                  budget: Decimal | None = None,
                  interactive: bool = False) -> list[PilotReport]:
    """依序执行任务；READY 任务推进样例主仓为下一 baseline。"""
    base = Path(db_root)
    base.mkdir(parents=True, exist_ok=True)
    conn = db.connect(base / "pilot.db")
    db.init_db(conn)
    reports: list[PilotReport] = []
    try:
        for task in tasks:
            report = await run_one(conn, sample_root=sample_root, task=task,
                                   db_root=str(base), live=live, model=model,
                                   budget=budget, interactive=interactive)
            reports.append(report)
            phase = TaskPhase(report.final_phase)
            if phase in _READY_PHASES:
                _promote(str(sample_root), report.task_id,
                         str(base / "worktrees"))
            if phase == TaskPhase.CANCELLED:
                break
    finally:
        conn.close()
    return reports


def _task_by_prefix(reports: Sequence[PilotReport],
                    tasks: Sequence[PilotTask]) -> dict[str, PilotTask]:
    out: dict[str, PilotTask] = {}
    for rp in reports:
        for t in tasks:
            if rp.task_id.startswith(t.id):
                out[rp.task_id] = t
                break
    return out


def _ready(reports):
    return (rp for rp in reports if rp.final_phase in _READY_VALUES)


def _artifacts_root(conn) -> Path:
    """由 DB 文件路径推导 engine cfg.artifacts_root（兄弟目录 artifacts/）。"""
    row = conn.execute("PRAGMA database_list").fetchone()
    db_file = row["file"] if row else ""
    return (Path(db_file).parent if db_file else Path.cwd()) / "artifacts"


def _events_of(conn, task_id: str) -> list[tuple[TaskPhase, TaskPhase]]:
    rows = conn.execute(
        "SELECT payload_json FROM events "
        "WHERE aggregate_id=? AND event_type='TASK_PHASE_CHANGED' "
        "ORDER BY aggregate_version", (task_id,)).fetchall()
    out = []
    for (payload,) in rows:
        data = json.loads(payload)
        out.append((TaskPhase(data["from_phase"]), TaskPhase(data["to_phase"])))
    return out


def prove_criteria(conn, sample_root: str, reports: Sequence[PilotReport],
                   *, tasks: Sequence[PilotTask] = ()) -> dict[str, bool | str]:
    """§1.2 七项机械证明；任一无法成立返回 False（fail-closed）。"""
    by_task = _task_by_prefix(reports, tasks)
    result: dict[str, bool | str] = {}
    ok = bool(reports)
    for rp in reports:  # 1 事件账本相邻转换合法 + 末事件 = 报告终态
        chain = _events_of(conn, rp.task_id)
        if not chain or not all(state.can_transition(f, t)
                                for f, t in chain):
            ok = False
            break
        if chain[-1][1].value != rp.final_phase:
            ok = False
            break
    result["phase_chain_legal"] = ok
    ready_reports = list(_ready(reports))
    ok = bool(ready_reports)
    for rp in ready_reports:  # 2 READY checkpoint diff ⊆ 批准文件集
        branch = f"task-{rp.task_id}"
        try:
            changed = set(run_git(sample_root, "diff", "--name-only",
                                  f"{branch}^", branch).splitlines())
        except GitCommandError:
            ok = False
            break
        task = by_task.get(rp.task_id)
        allowed = (set(task.allowed_files) if task
                   else {p for p in changed
                         if p.startswith(("src/", "tests/"))
                         or p == "conftest.py"})
        if not changed or not changed.issubset(allowed):
            ok = False
            break
    result["changes_within_allowed"] = ok
    ok = bool(ready_reports)  # 3 READY 链；无 READY 不得空泛证明成功
    root = _artifacts_root(conn)
    for rp in ready_reports:
        chain = _events_of(conn, rp.task_id)
        seen = {to for _, to in chain}
        need = {TaskPhase.EVIDENCE_RUNNING, TaskPhase.REVIEWING,
                TaskPhase.POLICY_EVALUATING, TaskPhase.CHECKPOINT_READY}
        if not need.issubset(seen) or not list_artifacts(root, rp.task_id):
            ok = False
            break
    result["evidence_policy_chain"] = ok
    field_names = {f.name for f in fields(ReviewerInputs)}  # 4 无主观总结
    result["no_subjective_summary"] = (
        "summary" not in field_names and "changes_summary" in field_names
        and "summary" not in ROLE_SYSTEM_PROMPTS["reviewer"])
    ok = True  # 5 DB 终态与报告一致
    for rp in reports:
        rec = store.get_task(conn, rp.task_id)
        ready = rp.final_phase in _READY_VALUES
        if rec is None or rec.phase.value != rp.final_phase \
                or (rec.terminal_reason is None) != ready:
            ok = False
            break
    result["final_phase_consistent"] = ok
    budget_seen = False
    ok = True  # 6 budget fault 须被实际注入并 SAFE_HALT
    for rp in reports:
        task = by_task.get(rp.task_id)
        if task is not None and task.fault == "budget":
            budget_seen = True
            if rp.final_phase != "SAFE_HALT":
                ok = False
                break
    result["budget_halt_injected"] = ok and budget_seen
    ok = bool(reports) and preflight(sample_root).verdict == "clean"
    for rp in ready_reports:
        if ok:
            try:
                run_git(sample_root, "merge-base", "--is-ancestor",
                        f"task-{rp.task_id}", "HEAD")
            except GitCommandError:
                ok = False
                break
    result["sample_main_untouched"] = ok
    return result
