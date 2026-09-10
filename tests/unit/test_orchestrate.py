"""M0-E1 任务闭环编排引擎测试。

GitSandbox 真实仓 + tmp DB + FakeRoleRunner（脚本化 implementer/reviewer）
全程 fake 零真实 token。探针校订：#19 evidence tree mismatch 与 #22 policy
INCOMPLETE 在引擎自洽下不可达（engine 恒传 delta.actual_tree、produce 全跑
plan）——由 C3/C4 单测覆盖；本文件以 engine 层可达反例覆盖（unknown
evidence command → SAFE_HALT；failing evidence → policy FAIL → FAILED）。
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

from aotf import db, state, store
from aotf.controller import FileMutation
from aotf.evidence.runner import CommandSpec
from aotf.git.preflight import preflight
from aotf.models import ApprovalRecord, CycleRecord, TaskPhase, TaskRecord
from aotf.orchestrate import (
    EngineConfig,
    RoleOutcome,
    TaskPlan,
    run_task_closed_loop,
)

CYCLE = "cycle-1"
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
H64 = "d" * 64
TASK = "task-1"


def _scope(allowed, proposal) -> str:
    return hashlib.sha256(
        ("\n".join(sorted(allowed)) + "\x00" + proposal).encode()).hexdigest()


class FakeRoles:
    """确定性 RoleRunner：模型只提 mutation；可直接写模拟控制权接管。"""

    def __init__(self, *, write: bool = True, out_of_scope: bool = False,
                 direct_write: bool = False, review: str = "PASS") -> None:
        self.write = write
        self.out_of_scope = out_of_scope
        self.direct_write = direct_write
        self.review = review

    async def run(self, role: str, *, inputs: dict) -> RoleOutcome:
        if role == "implementer":
            wt = inputs["worktree_path"]
            target = wt + "/" + inputs["allowed_files"][0]
            try:
                with open(target, "rb") as fh:
                    before = fh.read()
            except FileNotFoundError:
                before = None
            if self.direct_write:
                with open(target, "ab") as fh:
                    fh.write(b"# agent bypass\n")
            if self.out_of_scope:
                mutation = FileMutation(
                    "evil.py", "create", None, "x\n")
            elif self.write:
                mutation = FileMutation(
                    inputs["allowed_files"][0],
                    "replace" if before is not None else "create",
                    (hashlib.sha256(before).hexdigest()
                     if before is not None else None),
                    ((before or b"").decode("utf-8") + "# edited\n"))
            else:
                return RoleOutcome(status="completed")
            return RoleOutcome(status="completed", mutations=(mutation,))
        if role == "reviewer":
            if self.review == "raise":
                raise ValueError("malformed review output")
            return RoleOutcome(status="completed",
                               verdict=self.review)  # type: ignore[arg-type]
        raise ValueError(f"unexpected role {role}")


def _spec(command_id: str, code: str) -> CommandSpec:
    return CommandSpec(command_id=command_id,
                       argv=(sys.executable, "-c", code))


def _cfg(repo, tmp_path, *, registry=None) -> EngineConfig:
    return EngineConfig(
        base_repo=str(repo),
        worktrees_root=str(tmp_path / "wt"),
        artifacts_root=str(tmp_path / "art"),
        scratch_root=str(tmp_path / "scratch"),
        registry=registry or {"unit": _spec("unit", "print('ok')")},
    )


def _engine_db(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "e.db")
    db.init_db(conn)
    store.insert_cycle(conn, CycleRecord(
        cycle_id=CYCLE, project_id="project-1", status="ACTIVE",
        version=0, created_at=T0, updated_at=T0,
    ))
    conn.commit()
    return conn


def _setup(conn: sqlite3.Connection, repo: str, *,
           allowed=("a.py",), proposal: str = "edit a.py",
           approval_id: str | None = None,
           created_at: datetime | None = None,
           expires_at: datetime | None = None,
           scope_override: str | None = None) -> None:
    rep = preflight(repo)
    assert rep.head_commit and rep.head_tree
    aid = approval_id or f"ap-{TASK}"
    store.insert_task(conn, TaskRecord(
        task_id=TASK, cycle_id=CYCLE, supersedes_task_id=None,
        phase=TaskPhase.CREATED, version=0,
        baseline_commit=rep.head_commit, baseline_tree=rep.head_tree,
        proposal_sha256=H64, authorization_id=aid,
        worktree_path=None, worktree_branch=None,
        actual_tree=None, delta_sha256=None, terminal_reason=None,
        created_at=T0, updated_at=T0,
    ))
    if approval_id != "MISSING":
        store.insert_approval(conn, ApprovalRecord(
            approval_id=aid, task_id=TASK, decision="approved",
            scope_sha256=scope_override or _scope(allowed, proposal),
            proposal_sha256=H64, baseline_tree=rep.head_tree,
            source="human", expires_at=expires_at,
            created_at=created_at or T0,
        ))
    conn.commit()
    for to_phase in (TaskPhase.BASELINING, TaskPhase.PLAN_READY,
                     TaskPhase.AWAITING_PLAN_APPROVAL,
                     TaskPhase.EDIT_AUTHORIZED):
        state.transition(conn, task_id=TASK, to_phase=to_phase,
                         terminal_reason=None,
                         event_id=to_phase.value + "-" + TASK, at=T0)


def _bootstrap(sandbox, tmp_path, *, registry=None, **setup_over):
    """init 仓 + db + authorized task → (conn, repo, cfg, plan)。"""
    repo = sandbox.init()
    conn = _engine_db(tmp_path)
    _setup(conn, repo, **setup_over)
    cfg = _cfg(repo, tmp_path, registry=registry)
    plan = TaskPlan(task_id=TASK, proposal_text="edit a.py",
                    allowed_files=("a.py",))
    return conn, repo, cfg, plan


def test_closed_loop_success_checkpoint_ready(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles()))
    assert phase == TaskPhase.CHECKPOINT_READY


def test_approval_missing_halt(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path,
                                    approval_id="MISSING")
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles()))
    assert phase == TaskPhase.SAFE_HALT


def test_approval_expired_halt(sandbox, tmp_path) -> None:
    created = T0 - timedelta(days=40)  # 2026-07-23
    expires = T0 - timedelta(days=20)  # 2026-08-12，早于 engine now（09-06）
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path,
                                    created_at=created, expires_at=expires)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles()))
    assert phase == TaskPhase.SAFE_HALT


def test_approval_scope_mismatch_halt(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path, scope_override=H64)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles()))
    assert phase == TaskPhase.SAFE_HALT


def test_implementer_delta_empty_halt(sandbox, tmp_path) -> None:
    # 探针校订：FAILED 仅 POLICY_EVALUATING 可达 → 中途 delta 空走 SAFE_HALT
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles(write=False)))
    assert phase == TaskPhase.SAFE_HALT


def test_implementer_out_of_scope_halt(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles(out_of_scope=True)))
    assert phase == TaskPhase.SAFE_HALT


def test_agent_direct_write_is_takeover_and_halts(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg,
        role_runner=FakeRoles(direct_write=True)))
    assert phase == TaskPhase.SAFE_HALT
    rec = store.get_task(conn, TASK)
    assert rec is not None
    assert rec.terminal_reason == \
        "controller ownership violated: agent mutated worktree"


def test_unknown_evidence_command_halt(sandbox, tmp_path) -> None:
    conn, _, cfg, _ = _bootstrap(sandbox, tmp_path)
    plan = TaskPlan(task_id=TASK, proposal_text="edit a.py",
                    allowed_files=("a.py",),
                    test_plan=(("x", "nope"),))
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles()))
    assert phase == TaskPhase.SAFE_HALT


def test_evidence_runner_changed_tracked_halt(sandbox, tmp_path) -> None:
    dirty = {"unit": _spec("unit", "open('a.py','a').write('x')")}
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path, registry=dirty)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles()))
    assert phase == TaskPhase.SAFE_HALT  # #21


def test_reviewer_block_safe_halt(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles(review="BLOCK")))
    assert phase == TaskPhase.SAFE_HALT


def test_reviewer_invalid_output_halt(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles(review="raise")))
    assert phase == TaskPhase.SAFE_HALT  # #26


@pytest.mark.parametrize("verdict", [None, "CONCERNS"])
def test_reviewer_missing_or_concerns_halt(
        sandbox, tmp_path, verdict) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg,
        role_runner=FakeRoles(review=verdict)))  # type: ignore[arg-type]
    assert phase == TaskPhase.SAFE_HALT


def test_midphase_reentry_halts_without_rerunning_agent(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    state.transition(conn, task_id=TASK, to_phase=TaskPhase.IMPLEMENTING,
                     terminal_reason=None, event_id="simulate-crash", at=T0)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles()))
    assert phase == TaskPhase.SAFE_HALT
    assert conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE task_id=?", (TASK,)
    ).fetchone()[0] == 0


def test_policy_failed_via_failing_evidence(sandbox, tmp_path) -> None:
    failing = {"unit": _spec("unit", "import sys; sys.exit(3)")}
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path, registry=failing)
    phase = asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles()))
    assert phase == TaskPhase.FAILED


def test_already_ready_noop(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    assert asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles())) \
        == TaskPhase.CHECKPOINT_READY
    before = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE task_id=?", (TASK,)
    ).fetchone()[0]
    assert asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles())) \
        == TaskPhase.CHECKPOINT_READY
    after = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE task_id=?", (TASK,)
    ).fetchone()[0]
    assert after == before  # 幂等不重跑


def test_run_records_agent_runs(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles()))
    n = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE task_id=?", (TASK,)
    ).fetchone()[0]
    assert n == 2  # implementer + reviewer


def test_evidence_ingested(sandbox, tmp_path) -> None:
    conn, _, cfg, plan = _bootstrap(sandbox, tmp_path)
    assert asyncio.run(run_task_closed_loop(
        conn, plan=plan, cfg=cfg, role_runner=FakeRoles())) \
        == TaskPhase.CHECKPOINT_READY
    assert list((tmp_path / "art").rglob("*.data"))
