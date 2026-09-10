"""M0-D3 三角色输出 reports/parse 测试（#26 fail-closed、§11.1 verdict 对齐）。"""

from __future__ import annotations

import pytest

from aotf.agents.reports import (
    ImplementerReport,
    PlannerReport,
    ReviewerReport,
    parse_implementer_report,
    parse_planner_report,
    parse_reviewer_report,
)


def test_planner_report_ok() -> None:
    r = parse_planner_report({"proposal_text": "P"})
    assert isinstance(r, PlannerReport)
    assert r.proposal_text == "P"
    assert r.note == ""  # 缺省


def test_planner_empty_proposal_rejected() -> None:
    with pytest.raises(ValueError, match="proposal_text"):
        parse_planner_report({"proposal_text": ""})
    with pytest.raises(ValueError, match="proposal_text"):
        parse_planner_report({})


def test_implementer_report_ok() -> None:
    c = parse_implementer_report({
        "outcome": "completed", "summary": "done",
        "mutations": [{"path": "a.py", "operation": "replace",
                       "expected_sha256": "a" * 64, "content": "x = 2\n"}],
    })
    assert isinstance(c, ImplementerReport) and c.outcome == "completed"
    assert c.mutations[0].path == "a.py"
    e = parse_implementer_report({"outcome": "escalated",
                                  "summary": "scope grew", "mutations": []})
    assert e.outcome == "escalated"


def test_implementer_bad_outcome_rejected() -> None:
    # #26：非法 schema 输出不推进
    with pytest.raises(ValueError, match="outcome"):
        parse_implementer_report({"outcome": "done", "summary": "x"})


def test_implementer_mutation_contract_fails_closed() -> None:
    with pytest.raises(ValueError, match="requires mutations"):
        parse_implementer_report({"outcome": "completed", "summary": "x",
                                  "mutations": []})
    with pytest.raises(ValueError, match="cannot propose"):
        parse_implementer_report({
            "outcome": "escalated", "summary": "x",
            "mutations": [{"path": "a.py", "operation": "create",
                           "expected_sha256": None, "content": "x"}],
        })
    with pytest.raises(ValueError, match="replace mutation shape"):
        parse_implementer_report({
            "outcome": "completed", "summary": "x",
            "mutations": [{"path": "a.py", "operation": "replace",
                           "expected_sha256": None, "content": "x"}],
        })


def test_reviewer_report_ok() -> None:
    p = parse_reviewer_report({"verdict": "PASS"})
    assert isinstance(p, ReviewerReport) and p.verdict == "PASS"
    assert p.findings == ()
    b = parse_reviewer_report({
        "verdict": "BLOCK",
        "findings": [{"dimension": "correctness", "severity": "blocker",
                      "message": "logic bug"}],
    })
    assert b.findings[0].dimension == "correctness"


def test_reviewer_finding_validated() -> None:
    base = {"dimension": "risk", "severity": "concern", "message": "m"}
    with pytest.raises(ValueError, match="dimension"):
        parse_reviewer_report({"verdict": "CONCERNS",
                               "findings": [{**base, "dimension": "style"}]})
    with pytest.raises(ValueError, match="severity"):
        parse_reviewer_report({"verdict": "CONCERNS",
                               "findings": [{**base, "severity": "fatal"}]})
    with pytest.raises(ValueError, match="message"):
        parse_reviewer_report({"verdict": "CONCERNS",
                               "findings": [{**base, "message": ""}]})


def test_reviewer_bad_verdict_rejected() -> None:
    # #26 + §11.1：LLM 层无 FAIL
    with pytest.raises(ValueError, match="verdict"):
        parse_reviewer_report({"verdict": "FAIL"})
    with pytest.raises(ValueError, match="verdict"):
        parse_reviewer_report({})


def test_parse_non_dict_rejected() -> None:
    for bad in ("x", None, [], 42):
        with pytest.raises(ValueError, match="must be a dict"):
            parse_planner_report(bad)
        with pytest.raises(ValueError, match="must be a dict"):
            parse_implementer_report(bad)
        with pytest.raises(ValueError, match="must be a dict"):
            parse_reviewer_report(bad)
    # findings 显式 null → 拒（#26 fail-closed）
    with pytest.raises(ValueError, match="findings"):
        parse_reviewer_report({"verdict": "PASS", "findings": None})


def test_planner_note_passthrough() -> None:
    r = parse_planner_report({"proposal_text": "P", "note": "ctx"})
    assert r.note == "ctx"


def test_reviewer_verdict_matches_spec_llm_results() -> None:
    # §11.1 LLMReviewResult 值域 = {PASS, CONCERNS, BLOCK}（无 FAIL）
    for v in ("PASS", "CONCERNS", "BLOCK"):
        assert parse_reviewer_report({"verdict": v}).verdict == v
