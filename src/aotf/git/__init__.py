"""AOTF git 隔离子包（M0-B）。

- base.py       —— git 子进程运行器（-c core.quotepath=false、GIT_OPTIONAL_LOCKS=0
  只读、GitCommandError）。
- preflight.py  —— 只读仓库探测（PreflightReport：clean/dirty/unborn/not_a_repo）。
- worktree.py   —— 任务 worktree 建立 + baseline manifest（工作树外兄弟写入）。
- checkpoint.py —— 控制器 checkpoint commit（范围验证 actual⊆allowed / 固定身份 /
  提交后 clean）。
- delta.py      —— baseline↔checkpoint 树对树差量事实（binary-safe patch sha /
  semantic / 逐文件 sha / actual_tree）。
- cleanup.py    —— 先归档后清理（dirty 须 force；经 peer worktree cwd remove）。

artifact store（filesystem append-only ingest/verify）在独立子包 aotf.artifacts
（M0-B6）。顶层 aotf/__init__ 不引用任何子包（__all__ 断言不变）。
"""

__all__ = ["base", "preflight", "worktree", "checkpoint", "delta", "cleanup"]
