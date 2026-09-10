"""M0-C3 evidence bundle（§9.3 编排）测试。

全用 GitSandbox tmp 一次性仓 + create_worktree（checkpoint 后 clean 的
post-edit snapshot）；artifacts/scratch 全在 tmp；#19/#21 反例断言 ingest
零发生。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aotf.evidence.bundle import EvidenceBundleResult, produce_evidence
from aotf.evidence.runner import CommandSpec, build_registry
from aotf.evidence.schema import EvidenceError
from aotf.git.worktree import create_worktree

H40 = "a" * 40
H64 = "d" * 64


def _spec(command_id: str, code: str) -> CommandSpec:
    return CommandSpec(command_id=command_id,
                       argv=(sys.executable, "-c", code))


def _registry():
    return build_registry((
        _spec("py-a", "print('a')"),
        _spec("py-err", "import sys;sys.stderr.write('e!')"),
        _spec("py-fail", "import sys;sys.exit(1)"),
        _spec("py-dirty", "open('init.txt','w').write('x')"),
    ))


def _wt(sandbox) -> Path:
    base = sandbox.init()
    return Path(create_worktree(
        base, worktree_path=str(sandbox.base / "aotf" / "task-1"),
        branch="task-1",
    ).worktree_path)


def _git(wt: Path, *args) -> str:
    proc = subprocess.run(["git", *args], cwd=str(wt),
                          capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _tree(wt: Path) -> str:
    return _git(wt, "rev-parse", "--verify", "HEAD^{tree}")


def _dirs(tmp_path):
    return (str(tmp_path / "art"), str(tmp_path / "scratch"))


def test_produce_success_bundle(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    res = produce_evidence(
        wt, task_id="task-1", evidence_id="ev-1", worktree_tree=_tree(wt),
        delta_sha256=H64, plan=(("unit", "py-a"),), registry=_registry(),
        artifacts_root=art, scratch_root=scratch,
    )
    assert isinstance(res, EvidenceBundleResult)
    assert res.record.evidence_id == "ev-1"
    assert len(res.record.checks) == 1
    assert res.record.checks[0].status == "passed"
    assert res.record.bundle_sha256
    kinds = {a.kind for a in res.artifacts}
    assert kinds == {"unit-stdout", "unit-stderr", "test-evidence"}
    out_art = next(a for a in res.artifacts if a.kind == "unit-stdout")
    assert out_art.sha256 == res.record.checks[0].stdout_sha256


def test_produce_multiple_checks(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    res = produce_evidence(
        wt, task_id="task-1", evidence_id="ev-2", worktree_tree=_tree(wt),
        delta_sha256=H64,
        plan=(("c1", "py-a"), ("c2", "py-err")), registry=_registry(),
        artifacts_root=art, scratch_root=scratch,
    )
    assert [c.check_id for c in res.record.checks] == ["c1", "c2"]
    assert len(res.artifacts) == 5
    assert res.results[1].stderr == b"e!"


def test_post_run_tree_unchanged(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    tree = _tree(wt)
    art, scratch = _dirs(tmp_path)
    produce_evidence(wt, task_id="task-1", evidence_id="ev-3",
                     worktree_tree=tree, delta_sha256=H64,
                     plan=(("unit", "py-a"),), registry=_registry(),
                     artifacts_root=art, scratch_root=scratch)
    assert _tree(wt) == tree


def test_evidence_tree_mismatch_rejected(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    wrong = "b" + H40[1:]
    with pytest.raises(EvidenceError, match="tree mismatch"):
        produce_evidence(wt, task_id="task-1", evidence_id="ev-4",
                         worktree_tree=wrong, delta_sha256=H64,
                         plan=(("unit", "py-a"),), registry=_registry(),
                         artifacts_root=art, scratch_root=scratch)
    assert not Path(art).exists()  # ingest 零发生


def test_runner_changed_tracked_file_invalid(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    with pytest.raises(EvidenceError, match="changed tracked files"):
        produce_evidence(wt, task_id="task-1", evidence_id="ev-5",
                         worktree_tree=_tree(wt), delta_sha256=H64,
                         plan=(("unit", "py-dirty"),), registry=_registry(),
                         artifacts_root=art, scratch_root=scratch)
    assert not Path(art).exists()  # ingest 零发生


def test_unknown_command_id_rejected(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    with pytest.raises(EvidenceError, match="unknown command_id"):
        produce_evidence(wt, task_id="task-1", evidence_id="ev-6",
                         worktree_tree=_tree(wt), delta_sha256=H64,
                         plan=(("unit", "nope"),), registry=_registry(),
                         artifacts_root=art, scratch_root=scratch)
    assert not Path(art).exists()


def test_duplicate_check_id_rejected(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    with pytest.raises(EvidenceError, match="duplicate check_id"):
        produce_evidence(wt, task_id="task-1", evidence_id="ev-7",
                         worktree_tree=_tree(wt), delta_sha256=H64,
                         plan=(("unit", "py-a"), ("unit", "py-err")),
                         registry=_registry(),
                         artifacts_root=str(tmp_path / "art"),
                         scratch_root=str(tmp_path / "scratch"))


def test_not_clean_worktree_rejected(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    (wt / "init.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(EvidenceError, match="not clean"):
        produce_evidence(wt, task_id="task-1", evidence_id="ev-8",
                         worktree_tree=_tree(wt), delta_sha256=H64,
                         plan=(("unit", "py-a"),), registry=_registry(),
                         artifacts_root=str(tmp_path / "art"),
                         scratch_root=str(tmp_path / "scratch"))


def test_evidence_artifact_id_is_evidence_id(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    res = produce_evidence(
        wt, task_id="task-1", evidence_id="ev-9", worktree_tree=_tree(wt),
        delta_sha256=H64, plan=(("unit", "py-a"),), registry=_registry(),
        artifacts_root=art, scratch_root=scratch,
    )
    ev = next(a for a in res.artifacts if a.kind == "test-evidence")
    assert ev.artifact_id == "ev-9"


def test_evidence_artifact_bytes_match(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    res = produce_evidence(
        wt, task_id="task-1", evidence_id="ev-10", worktree_tree=_tree(wt),
        delta_sha256=H64, plan=(("unit", "py-a"),), registry=_registry(),
        artifacts_root=art, scratch_root=scratch,
    )
    ev = next(a for a in res.artifacts if a.kind == "test-evidence")
    stored = Path(art) / "task-1" / "test-evidence" / f"{ev.artifact_id}.data"
    assert stored.read_bytes() == res.evidence_bytes


def test_scratch_and_artifacts_separated(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    produce_evidence(wt, task_id="task-1", evidence_id="ev-11",
                     worktree_tree=_tree(wt), delta_sha256=H64,
                     plan=(("unit", "py-a"),), registry=_registry(),
                     artifacts_root=art, scratch_root=scratch)
    assert (Path(scratch) / "unit-stdout").exists()
    assert not (Path(scratch) / "task-1").exists()
    assert (Path(art) / "task-1").exists()


def test_evidence_bytes_parse_and_bundle_match(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    res = produce_evidence(
        wt, task_id="task-1", evidence_id="ev-12", worktree_tree=_tree(wt),
        delta_sha256=H64, plan=(("unit", "py-a"),), registry=_registry(),
        artifacts_root=art, scratch_root=scratch,
    )
    parsed = json.loads(res.evidence_bytes.decode("utf-8"))
    assert parsed["bundle_sha256"] == res.record.bundle_sha256
    assert parsed["worktree_tree"] == _tree(wt)


def test_runner_identity_recorded(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    res = produce_evidence(
        wt, task_id="task-1", evidence_id="ev-13", worktree_tree=_tree(wt),
        delta_sha256=H64, plan=(("unit", "py-a"),), registry=_registry(),
        artifacts_root=art, scratch_root=scratch,
        runner_identity="aotf-custom-v1", host_fingerprint=H64,
    )
    assert res.record.runner.identity == "aotf-custom-v1"
    assert res.record.runner.host_fingerprint == H64


def test_delta_sha_hex_required(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    with pytest.raises(EvidenceError, match="sha256"):
        produce_evidence(wt, task_id="task-1", evidence_id="ev-14",
                         worktree_tree=_tree(wt), delta_sha256="z" * 64,
                         plan=(("unit", "py-a"),), registry=_registry(),
                         artifacts_root=str(tmp_path / "art"),
                         scratch_root=str(tmp_path / "scratch"))


def test_missing_worktree_valueerror(sandbox, tmp_path) -> None:
    with pytest.raises(ValueError):
        produce_evidence(sandbox.base / "nope", task_id="task-1",
                         evidence_id="ev-15", worktree_tree=H40,
                         delta_sha256=H64, plan=(("unit", "py-a"),),
                         registry=_registry(),
                         artifacts_root=str(tmp_path / "art"),
                         scratch_root=str(tmp_path / "scratch"))


def test_failed_check_retained(sandbox, tmp_path) -> None:
    wt = _wt(sandbox)
    art, scratch = _dirs(tmp_path)
    res = produce_evidence(
        wt, task_id="task-1", evidence_id="ev-16", worktree_tree=_tree(wt),
        delta_sha256=H64, plan=(("fail", "py-fail"), ("ok", "py-a")),
        registry=_registry(), artifacts_root=art, scratch_root=scratch,
    )
    by_id = {c.check_id: c for c in res.record.checks}
    assert by_id["fail"].status == "failed"
    assert by_id["ok"].status == "passed"
