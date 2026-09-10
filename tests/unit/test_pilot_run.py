"""M0-E2 pilot 执行器测试（live=False 全 fake 零 token，tmp 样例仓）。

覆盖 taskbook §5.6 #12–#18：run_one 成功到 CHECKPOINT_READY 且 PilotReport
全字段；越界/失败/熔断三故障路径收束；run_all 依序三报告并推进样例主仓；
prove_criteria §1.2 七项全 true；单任务样例主仓零改动。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aotf import db
from aotf.git.preflight import preflight
from aotf.pilot.run import prove_criteria, run_all, run_one
from aotf.pilot.sample import generate_sample
from aotf.pilot.tasks import FAULT_TASKS, SAMPLE_TASKS


def _sample(tmp_path):
    return generate_sample(str(tmp_path / "sample"))


def _fresh(tmp_path):
    d = tmp_path / "db"
    d.mkdir(parents=True, exist_ok=True)
    conn = db.connect(d / "pilot.db")
    db.init_db(conn)
    return conn, str(d)


def _reopen(db_root):
    return db.connect(Path(db_root) / "pilot.db")


def test_run_one_fake_success_checkpoint_ready(tmp_path) -> None:
    sample = _sample(tmp_path)
    conn, d = _fresh(tmp_path)
    report = asyncio.run(run_one(
        conn, sample_root=sample.root, task=SAMPLE_TASKS[0], db_root=d))
    assert report.final_phase == "CHECKPOINT_READY"
    assert report.terminal_reason is None
    assert report.evidence_ok is True
    assert report.agent_runs == 2  # implementer + reviewer
    assert "implementer" in report.tokens and "reviewer" in report.tokens
    assert report.tokens["implementer"]["input"] > 0
    assert report.cost_usd > 0
    assert isinstance(report.duration_s, float) and report.duration_s > 0
    assert report.human_interventions == 0


def test_run_one_out_of_scope_safe_halt(tmp_path) -> None:
    sample = _sample(tmp_path)
    conn, d = _fresh(tmp_path)
    report = asyncio.run(run_one(
        conn, sample_root=sample.root, task=FAULT_TASKS["out_of_scope"],
        db_root=d))
    assert report.final_phase == "SAFE_HALT"
    assert report.evidence_ok is False


def test_run_one_failing_evidence_failed(tmp_path) -> None:
    sample = _sample(tmp_path)
    conn, d = _fresh(tmp_path)
    report = asyncio.run(run_one(
        conn, sample_root=sample.root, task=FAULT_TASKS["failing"],
        db_root=d))
    assert report.final_phase in ("FAILED", "SAFE_HALT")
    assert report.evidence_ok is False


def test_run_all_sequential_three_ready(tmp_path) -> None:
    sample = _sample(tmp_path)
    reports = asyncio.run(run_all(
        sample_root=sample.root, db_root=str(tmp_path / "db")))
    assert len(reports) == 3
    assert all(r.final_phase == "CHECKPOINT_READY" for r in reports)
    # 共享推进仓：样例主仓 HEAD 前移（三任务连续 baseline）
    assert preflight(sample.root).head_commit != sample.head_commit


def test_prove_criteria_success_requires_fault_coverage(tmp_path) -> None:
    sample = _sample(tmp_path)
    reports = asyncio.run(run_all(
        sample_root=sample.root, db_root=str(tmp_path / "db")))
    conn = _reopen(str(tmp_path / "db"))
    try:
        proof = prove_criteria(conn, sample.root, reports,
                               tasks=SAMPLE_TASKS)
    finally:
        conn.close()
    assert proof["budget_halt_injected"] is False
    for key, value in proof.items():
        if key != "budget_halt_injected":
            assert value is True, f"{key}: {value!r}"


def test_prove_criteria_no_ready_is_not_vacuously_true(tmp_path) -> None:
    sample = _sample(tmp_path)
    conn, d = _fresh(tmp_path)
    report = asyncio.run(run_one(
        conn, sample_root=sample.root, task=FAULT_TASKS["out_of_scope"],
        db_root=d))
    proof = prove_criteria(
        conn, sample.root, (report,), tasks=(FAULT_TASKS["out_of_scope"],))
    assert proof["changes_within_allowed"] is False
    assert proof["evidence_policy_chain"] is False
    assert proof["budget_halt_injected"] is False


def test_run_one_sample_main_untouched(tmp_path) -> None:
    sample = _sample(tmp_path)
    before = preflight(sample.root)
    conn, d = _fresh(tmp_path)
    asyncio.run(run_one(
        conn, sample_root=sample.root, task=SAMPLE_TASKS[0], db_root=d))
    after = preflight(sample.root)
    assert after.verdict == "clean"
    assert after.head_commit == before.head_commit
    assert after.head_tree == before.head_tree


def test_run_one_budget_fault_halt(tmp_path) -> None:
    sample = _sample(tmp_path)
    conn, d = _fresh(tmp_path)
    report = asyncio.run(run_one(
        conn, sample_root=sample.root, task=FAULT_TASKS["budget"], db_root=d))
    assert report.final_phase == "SAFE_HALT"
    assert report.evidence_ok is False
