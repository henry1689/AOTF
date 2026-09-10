"""AOTF evidence bundle 机器（M0-C3）。

把 spec §9.3 证据生产顺序实现为单一编排动作：对 checkpoint 后 clean 的
post-edit snapshot（task worktree），按 required plan 执行注册命令，验证
tree/delta 未漂移，组装 TestEvidenceRecord，并把 stdout/stderr/evidence
经 B6 artifact store ingest 锁定。

§9.3 顺序与本实现映射：
1 冻结 actual tree/delta（入参 worktree_tree/delta_sha256）；
2 runner 在 worktree 执行确定性命令（C2 run_command，逐 check）；
3 单独保存 stdout/stderr（捕获 bytes → scratch 源文件 → B6 ingest）；
5 验证 evidence 中 tree/delta 未漂移（post-run HEAD/HEAD^{tree} 不变 +
   clean/porcelain 空）——**先于任何 ingest**（本实现将其提前到组装前）；
4 生成 typed evidence（record_with_bundle，C1，验证通过后执行）；
6 ingest 并锁定（stdout/stderr/test-evidence 三类 artifact；stdout
   artifact.sha 与 check.stdout_sha 交叉核对）；
7 测试改变 tracked 文件 → 证据无效（#21，post-run 校验拒绝）。

§20.4 映射：#18 baseline 测试不能冒充 post-edit（evidence 只对 checkpoint
snapshot 产生，#19 树绑定）；#19 evidence tree ≠ actual tree → EvidenceError；
#21 测试改 tracked 文件 → EvidenceError。

设计裁决：
- 先验证后 ingest：tree/delta 漂移或 tracked 改动在**任何 artifact 落盘前**
  拒绝（原子语义；partial ingest 仅可能因 ingest 自身中途失败，append-only
  可审计残留，无半成品 record 返回）；
- 真实墙钟 started/ended（测试不做跨调用字节相等）；evidence_id 必传；
- 依赖单向：→ schema/runner/artifacts.store/git.base（只读）；异常域来源
  清晰不吞（EvidenceError/ArtifactError/GitCommandError）。

与 aotf.runner（AgentRunner）同名异包勿混；本模块为「证据生产编排」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from aotf.artifacts.store import ArtifactInfo, ingest as artifact_ingest
from aotf.evidence.runner import (
    CommandSpec,
    EvidenceRunResult,
    run_command,
)
from aotf.evidence.schema import (
    EvidenceError,
    EvidenceRunnerInfo,
    TestEvidenceRecord,
    evidence_json_bytes,
    record_with_bundle,
)
from aotf.git.base import run_git
from aotf.git.preflight import preflight

__all__ = ["EvidenceBundleResult", "produce_evidence"]

# 统一用 B6 artifact 域令牌（无 @、上限 64）：task_id/evidence_id/check_id/
# runner_identity 都会直传 artifacts.ingest（producer/artifact_id/kind 基座），
# 在入口 fail-fast 收敛，避免重活后/partial ingest 才炸（评审 H1/M1/M2）。
_B6_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_KIND_SUFFIX_STDOUT = "-stdout"
_KIND_SUFFIX_STDERR = "-stderr"
_EVIDENCE_KIND = "test-evidence"
_CHECK_ID_MAX = 64 - len(_KIND_SUFFIX_STDOUT)  # 57：kind 后缀后仍 ≤64


def _check_token(name: str, value: object, *, check_id: bool = False) -> None:
    if not isinstance(value, str) or value in (".", ".."):
        raise EvidenceError(f"invalid {name} token")
    if not _B6_RE.fullmatch(value):
        raise EvidenceError(f"invalid {name} token")
    if check_id and len(value) > _CHECK_ID_MAX:
        raise EvidenceError(f"invalid {name} token")


def _require_hex(name: str, value: object, *, hex64: bool) -> None:
    ok = _HEX64_RE.fullmatch(value) if hex64 else _HEX40_RE.fullmatch(value)
    if not ok:
        raise EvidenceError(f"invalid {name} sha256" if hex64
                            else f"invalid {name} hex")


def _porcelain_empty(worktree: Path) -> bool:
    out = run_git(worktree, "status", "--porcelain")
    return not any(line.strip() for line in out.splitlines())


def _clean(worktree: Path) -> bool:
    return preflight(worktree).verdict == "clean" and _porcelain_empty(worktree)


@dataclass(frozen=True, slots=True)
class EvidenceBundleResult:
    """一次 produce 的结果：record + 运行事实 + 已 ingest artifacts。"""

    record: TestEvidenceRecord
    results: tuple[EvidenceRunResult, ...]
    artifacts: tuple[ArtifactInfo, ...]
    evidence_bytes: bytes


def produce_evidence(
    worktree: str | Path,
    *,
    task_id: str,
    evidence_id: str,
    worktree_tree: str,
    delta_sha256: str,
    plan: tuple[tuple[str, str], ...],
    registry: Mapping[str, CommandSpec],
    artifacts_root: str | Path,
    scratch_root: str | Path,
    runner_identity: str = "aotf-evidence-runner-v1",
    host_fingerprint: str | None = None,
) -> EvidenceBundleResult:
    """按 §9.3 顺序生产并锁定一份 evidence bundle。

    失败抛 EvidenceError（含 #19/#21 与 plan 校验）；git 机械失败抛
    GitCommandError；artifacts.ingest 失败抛 ArtifactError（不吞）。
    """
    path = Path(worktree)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"worktree path not found or not a directory: {worktree}")

    _check_token("evidence_id", evidence_id)
    _check_token("task_id", task_id)
    _check_token("runner_identity", runner_identity)
    _require_hex("worktree_tree", worktree_tree, hex64=False)
    _require_hex("delta_sha256", delta_sha256, hex64=True)
    if not plan:
        raise EvidenceError("plan must be non-empty")
    seen: set[str] = set()
    for check_id, command_id in plan:
        _check_token("check_id", check_id, check_id=True)
        if check_id in seen:
            raise EvidenceError(f"duplicate check_id: {check_id}")
        seen.add(check_id)
        if command_id not in registry:
            raise EvidenceError(f"unknown command_id: {command_id}")

    # §9.3 step 1 前置：snapshot 必须 clean 且 tree 绑定
    if not _clean(path):
        raise EvidenceError("worktree not clean")
    head0 = run_git(path, "rev-parse", "--verify", "HEAD^{commit}").strip()
    tree0 = run_git(path, "rev-parse", "--verify", "HEAD^{tree}").strip()
    if tree0 != worktree_tree:
        raise EvidenceError("evidence tree mismatch")  # §20.4 #19

    # step 2：逐 check 执行（只读命令；改 tracked → step 后校验拒）
    started_at = datetime.now(timezone.utc)
    results: list[EvidenceRunResult] = []
    for check_id, command_id in plan:
        results.append(run_command(path, registry[command_id],
                                   check_id=check_id))
    ended_at = datetime.now(timezone.utc)

    # step 5 post-run 验证（先于任何 ingest）：#21
    if not _clean(path):
        raise EvidenceError("runner changed tracked files")
    if run_git(path, "rev-parse", "--verify", "HEAD^{commit}").strip() != head0 \
            or run_git(path, "rev-parse", "--verify",
                       "HEAD^{tree}").strip() != tree0:
        raise EvidenceError("runner changed tracked files")

    # step 4：组装 typed evidence
    record = record_with_bundle(TestEvidenceRecord(
        schema_version=1,
        evidence_id=evidence_id,
        task_id=task_id,
        worktree_tree=worktree_tree,
        delta_sha256=delta_sha256,
        runner=EvidenceRunnerInfo(
            identity=runner_identity,
            host_fingerprint=host_fingerprint,
            started_at=started_at,
            ended_at=ended_at,
        ),
        checks=tuple(r.check for r in results),
        bundle_sha256="0" * 64,
    ))

    scratch = Path(scratch_root)
    try:
        scratch.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EvidenceError(f"cannot create scratch dir: {exc}") from exc

    # step 3+6：stdout/stderr 落 scratch → ingest（sha 交叉核对）
    artifacts: list[ArtifactInfo] = []
    for res in results:
        check = res.check
        out_file = scratch / f"{check.check_id}-stdout"
        err_file = scratch / f"{check.check_id}-stderr"
        out_file.write_bytes(res.stdout)
        err_file.write_bytes(res.stderr)
        out_art = artifact_ingest(
            out_file, root=artifacts_root, task_id=task_id,
            kind=check.check_id + _KIND_SUFFIX_STDOUT,
            producer=runner_identity,
        )
        err_art = artifact_ingest(
            err_file, root=artifacts_root, task_id=task_id,
            kind=check.check_id + _KIND_SUFFIX_STDERR,
            producer=runner_identity,
        )
        if out_art.sha256 != check.stdout_sha256:
            raise EvidenceError("artifact sha mismatch (stdout)")
        if err_art.sha256 != check.stderr_sha256:
            raise EvidenceError("artifact sha mismatch (stderr)")
        artifacts.extend((out_art, err_art))

    evidence_bytes = evidence_json_bytes(record, include_bundle=True)
    evidence_file = scratch / "evidence.json"
    evidence_file.write_bytes(evidence_bytes)
    ev_art = artifact_ingest(
        evidence_file, root=artifacts_root, task_id=task_id,
        kind=_EVIDENCE_KIND, producer=runner_identity,
        artifact_id=evidence_id,
    )
    artifacts.append(ev_art)

    return EvidenceBundleResult(
        record=record,
        results=tuple(results),
        artifacts=tuple(artifacts),
        evidence_bytes=evidence_bytes,
    )
