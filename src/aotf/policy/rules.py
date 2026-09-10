"""AOTF policy 门配置（M0-C4 rules）：门集合一致性校验。

required（本任务闭环必需）/ release_gates（发布标准门）/ deferred
（deferred_out_of_scope，超出当前授权范围未验证，spec §11.2/#23）三集合
的令牌合法性与一致性检验：

- 元素令牌域对齐 C1 evidence 命名 `[A-Za-z0-9._@-]{1,64}`（允许 @）——
  门只引用 evidence 的 check_id/command_id，不经 B6 artifact ingest，故
  无需收敛到无 @ 的 artifact 域；
- 集合内重复 → 拒绝（判定歧义）；required 非空（evaluate 无门可判）；
- deferred 必须指已知门（required ∪ release_gates）——把未知门标记为
  「授权外延迟」是配置错，fail-fast PolicyError。

PolicyError 独立异常（字段 reason），不扩冻结 16 ErrorCode（evidence 先例）。
"""

from __future__ import annotations

import re

__all__ = ["PolicyError", "check_token", "validate_gates"]

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


class PolicyError(Exception):
    """policy 门配置/入参畸形拒绝（调用方 bug，非运行事实异常）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def check_token(name: str, value: object) -> None:
    if not isinstance(value, str) or value in (".", ".."):
        raise PolicyError(f"invalid {name} token")
    if not _TOKEN_RE.fullmatch(value):
        raise PolicyError(f"invalid {name} token")


def _require_tuple(name: str, value: object) -> None:
    if not isinstance(value, tuple):
        raise PolicyError(f"{name} must be a tuple")


def validate_gates(
    *,
    required: tuple[str, ...],
    release_gates: tuple[str, ...] = (),
    deferred: tuple[str, ...] = (),
) -> None:
    """三集合令牌/重复/必填/known 一致性校验（任一畸形 → PolicyError）。"""
    for label, coll in (
        ("required", required),
        ("release_gates", release_gates),
        ("deferred", deferred),
    ):
        _require_tuple(label, coll)
        seen: set[str] = set()
        for entry in coll:
            check_token(label, entry)
            if entry in seen:
                raise PolicyError(f"duplicate gate in {label}: {entry}")
            seen.add(entry)
    if not required:
        raise PolicyError("required must be non-empty")
    known = set(required) | set(release_gates)
    for entry in deferred:
        if entry not in known:
            raise PolicyError(f"deferred gate unknown: {entry}")
