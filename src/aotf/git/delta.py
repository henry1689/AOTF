"""AOTF actual delta capture（M0-B4）。

spec §8.2：以 baseline_manifest（base_commit/base_tree/allowed_files）为锚，
对 baseline..checkpoint commit 区间产出机械差量事实：

- changes：--no-renames --name-status（A/D/M，rename 展开为 D+A，防折叠漏检）；
- renames：--name-status -M 中 R 对，单独报告；
- patch_sha256：--binary --no-ext-diff --no-color 补丁原文 sha（decode 后以
  surrogateescape 回编码保真，二进制字面量不丢字节）；
- diff_stat：--stat 文本；semantic_added：补丁新增行中 非空 且 首字符非 #；
- file_sha256：checkpoint 侧存在的 A/M 文件逐文件 sha；
- actual_tree：HEAD^{tree}（供 tasks.actual_tree 语义）。

范围/一致性：changes ⊆ manifest.allowed_files，越界 DeltaError（编排层
SAFE_HALT 语义）。只读：本模块不写任何文件。porcelain v2 -z 工作树状态由
B3 checkpoint（clean 校验 + staged==actual）覆盖；本包入口仍要求 preflight
clean 才开算（fail-closed）。

异常域：完整性/policy 拒绝 → DeltaError；git 机械失败 → GitCommandError；
路径无效 → ValueError。不扩冻结 16 码。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from aotf.git.base import GitCommandError, run_git
from aotf.git.preflight import preflight

__all__ = ["DeltaError", "DeltaReport", "capture_delta"]

_MANIFEST_REQUIRED = ("base_commit", "base_tree", "allowed_files",
                      "file_sha256")


class DeltaError(Exception):
    """actual delta 的完整性/policy 拒绝（非状态机错误）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class DeltaReport:
    """baseline↔checkpoint 区间差量事实（只读）。"""

    base_commit: str
    checkpoint_commit: str
    base_tree: str
    actual_tree: str
    changes: tuple[tuple[str, str], ...]
    renames: tuple[tuple[str, str], ...]
    file_sha256: dict[str, str]
    patch_sha256: str
    diff_stat: str
    semantic_added: int


def _name_status(repo: str, extra: tuple[str, ...], base: str, ckpt: str,
                 timeout: float) -> list[tuple[str, str]]:
    out = run_git(repo, "diff", "--name-status", *extra, base, ckpt,
                  timeout=timeout)
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        code, sep, rel = line.partition("\t")
        if sep:
            rows.append((code, rel))
    return rows


def _semantic_added(patch: str) -> int:
    count = 0
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]
        if content.strip() and not content.lstrip().startswith("#"):
            count += 1
    return count


def _load_manifest(path: Path, wt: str) -> dict:
    mp = path if path is not None else Path(str(wt) + ".baseline-manifest.json")
    if not mp.exists() or not mp.is_file():
        raise DeltaError("baseline manifest not found")
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise DeltaError(f"invalid baseline manifest: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise DeltaError("invalid baseline manifest")
    if any(key not in data for key in _MANIFEST_REQUIRED):
        raise DeltaError("invalid baseline manifest")
    # 类型加固：allowed_files 畸形（如裸字符串）会让 set() 变字符集，
    # 导致假越界或绕过范围门——fail-closed 拒绝。
    if (
        not isinstance(data["base_commit"], str)
        or not isinstance(data["base_tree"], str)
        or not isinstance(data["allowed_files"], list)
        or not all(isinstance(x, str) for x in data["allowed_files"])
        or not isinstance(data["file_sha256"], dict)
    ):
        raise DeltaError("invalid baseline manifest")
    return data


def capture_delta(
    worktree: str | Path,
    *,
    manifest_path: str | Path | None = None,
    timeout: float = 30.0,
) -> DeltaReport:
    """捕获 baseline↔checkpoint 差量事实。只读；不写任何文件。

    worktree 必须 clean（否则 DeltaError）；manifest 缺失/非法/越界 →
    DeltaError；git 机械失败抛 GitCommandError；路径无效抛 ValueError。
    """
    path = Path(worktree)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"worktree path not found or not a directory: {worktree}")
    verdict = preflight(path, timeout=timeout).verdict
    if verdict != "clean":
        if verdict == "dirty":
            raise DeltaError("worktree not clean")
        raise DeltaError("worktree is not a clean git work tree")

    manifest = _load_manifest(
        Path(manifest_path) if manifest_path is not None else None, str(path)
    )
    base_commit = manifest["base_commit"]
    base_tree = manifest["base_tree"]
    allowed = set(manifest["allowed_files"])

    ckpt = run_git(path, "rev-parse", "--verify", "HEAD^{commit}",
                   timeout=timeout).strip()
    actual_tree = run_git(path, "rev-parse", "--verify", "HEAD^{tree}",
                          timeout=timeout).strip()

    changes = tuple(
        sorted(_name_status(path, ("--no-renames",), base_commit, ckpt,
                            timeout), key=lambda row: row[1])
    )
    offending = tuple(sorted(p for _c, p in changes if p not in allowed))
    if offending:
        raise DeltaError(
            f"delta out-of-scope: {len(offending)} file(s) "
            f"({offending[0]!r} first)"
        )
    renames = tuple(
        sorted(
            (old, new)
            for code, rest in _name_status(path, ("-M",), base_commit, ckpt,
                                           timeout)
            if code.startswith("R")
            for old, new in (rest.partition("\t")[::2],)
        )
    )

    file_sha256: dict[str, str] = {}
    for code, rel in changes:
        if code not in ("A", "M"):
            continue
        try:
            file_sha256[rel] = hashlib.sha256(
                (path / rel).read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise DeltaError(
                f"changed file missing at checkpoint: {rel}: {exc}"
            ) from exc

    patch = run_git(path, "diff", "--binary", "--no-ext-diff", "--no-color",
                    "--no-renames", base_commit, ckpt, timeout=timeout)
    patch_sha256 = hashlib.sha256(
        patch.encode("utf-8", "surrogateescape")
    ).hexdigest()
    diff_stat = run_git(path, "diff", "--stat", "--no-renames",
                        base_commit, ckpt, timeout=timeout)

    return DeltaReport(
        base_commit=base_commit,
        checkpoint_commit=ckpt,
        base_tree=base_tree,
        actual_tree=actual_tree,
        changes=changes,
        renames=renames,
        file_sha256=file_sha256,
        patch_sha256=patch_sha256,
        diff_stat=diff_stat,
        semantic_added=_semantic_added(patch),
    )
