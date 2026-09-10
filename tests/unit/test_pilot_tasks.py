"""M0-E2 pilot 任务定义测试（SAMPLE_TASKS / FAULT_TASKS 字段与自洽）。"""

from __future__ import annotations

from decimal import Decimal

from aotf.pilot.tasks import FAULT_KEYS, FAULT_TASKS, SAMPLE_TASKS


def test_sample_tasks_length_and_fields() -> None:
    assert len(SAMPLE_TASKS) >= 3
    ids = [t.id for t in SAMPLE_TASKS]
    assert len(set(ids)) == len(ids)
    for t in SAMPLE_TASKS:
        assert t.title and t.proposal.strip()
        assert t.allowed_files
        assert all(not f.startswith(("/", "\\")) for f in t.allowed_files)
        assert t.test_plan and t.test_plan[0][1] == "pytest"
        assert isinstance(t.budget_usd, Decimal) and t.budget_usd > 0
        assert t.fault is None


def test_sample_tasks_proposal_consistent_with_allowed() -> None:
    for t in SAMPLE_TASKS:
        for f in t.allowed_files:
            assert f in t.proposal, f"{t.id}: allowed {f} not in proposal"
        # 三个样例任务各覆盖 slugify/normalize/cleaning 锚点
        assert "slugify.py" in t.allowed_files[0] \
            or "normalize.py" in t.allowed_files[0] \
            or "cleaning.py" in t.allowed_files[0]


def test_fault_tasks_keys_and_semantics() -> None:
    assert tuple(FAULT_TASKS) == FAULT_KEYS
    for key, t in FAULT_TASKS.items():
        assert t.id and t.proposal.strip() and t.allowed_files
        assert t.fault == key
    # out_of_scope：proposal 诱导改的文件须不在批准集内
    assert "stats.py" in FAULT_TASKS["out_of_scope"].proposal
    assert all("stats.py" not in f
               for f in FAULT_TASKS["out_of_scope"].allowed_files)
    # budget：极小预算触发熔断
    assert FAULT_TASKS["budget"].budget_usd < Decimal("0.01")
