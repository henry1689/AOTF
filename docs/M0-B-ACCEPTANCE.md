# AOTF M0-B 终验收对照（Git 隔离与 artifact store）

- 日期：2026-09-06
- 状态：M0-B 全部包闭合（B1 preflight → B7 文档收口）
- 权威架构：`D:\tools\AOTF-Pre-MVP-architecture-and-implementation-spec-2026-08-31.md`
- 权威架构 SHA-256：`86fb98ed094fe750c8b4d7c2e54112d99370a1049e8e8f783deaeb03b4bca2f3`
- 基准：`457 passed` / 21 测试文件 / 50 文件（含本 doc）/ 仓库零 DB 生成物 / Git `main`·unborn·remote 空

## 1. M0-B 包完成清单

| 包 | 内容 | 证据（src / tests） |
|---|---|---|
| B1 | git 沙箱夹具 + repo preflight 只读探测 | `git/base.py` `git/preflight.py` `conftest.py` / `test_preflight.py` |
| B2 | worktree create + baseline manifest（工作树外兄弟） | `git/worktree.py` / `test_worktree.py` |
| B3 | 控制器 checkpoint commit（范围验证/固定身份/提交后 clean） | `git/checkpoint.py` / `test_checkpoint.py` |
| B4 | actual delta（baseline↔checkpoint 树对树） | `git/delta.py` / `test_delta.py` |
| B5 | worktree archive/清理与保留（先归档后清理） | `git/cleanup.py` / `test_cleanup.py` |
| B6 | artifact store（filesystem append-only ingest/verify） | `artifacts/store.py` / `test_artifact_store.py` |
| B7 | 本 doc + README/git/__init__ 收口 | `docs/M0-B-ACCEPTANCE.md` |

## 2. spec §19 M0-B 内容对照

| spec 内容 | 覆盖 | 证据 |
|---|---|---|
| repo preflight | 已覆盖 | `preflight` 四 verdict（clean/dirty/unborn/not_a_repo）；dirty 主仓如实报告，决策归编排层 |
| worktree create / manifest | 已覆盖 | `create_worktree` clean 门禁 + 双向隔离守卫 + 专用分支；manifest 记 commit/tree/逐文件 sha/allowed/tools |
| delta / checkpoint / archive | 已覆盖 | B3 checkpoint 范围验证 actual⊆allowed；B4 树对树差量；B5 先归档后清理 |
| canonical JSON 与 hash | 已覆盖 | manifest/archive/artifact sidecar 统一确定性 sorted-json ensure_ascii + sha（自由文本字段不经 canonical 守卫，B2 定案） |
| dirty 主仓反例 | 已覆盖 | `test_worktree.py::test_refuse_dirty_base`、`test_cleanup.py::test_remove_dirty_refused_without_force` |
| rename/binary/delete/new 反例 | 已覆盖 | `test_checkpoint.py::test_rename_both_paths_checked_out_of_scope`/`test_prestaged_deletion_commits`/`test_checkpoint_adds_approved_untracked_file`、`test_delta.py::test_delta_rename_reported_and_split`/`test_delta_binary_file`/`test_capture_delta_delete`/`test_capture_delta_add_new_file` |
| submodule 反例 | 已覆盖 | `test_preflight.py::test_submodule_gitlink_listed_clean`、`test_worktree.py::test_submodules_recorded_and_gitlink_skipped` |
| symlink 越界写 | **边界声明** | 拦截属 Implementer 工具边界（M0-D/hooks），M0-B 机器模块只读/路径令牌，不声明覆盖（不填凑数测试） |

**验收（spec §19 M0-B：「所有测试均不改变主工作树」）**：全部测试仓库来自 `tests/unit/conftest.py` `GitSandbox`（pytest tmp_path 一次性仓）；零真实仓触碰；main 零改动有硬断言（见 §3 #14/#17）。

## 3. §20.3 Worktree / §20.4 Evidence 对照

| 项 | 状态 | 证据 |
|---|---|---|
| #13 clean/dirty 两种现场按 policy | 已覆盖 | `preflight` dirty 报告；`create_worktree` dirty 拒绝；`cleanup.remove_worktree` dirty 无 force 拒绝 |
| #14 任务失败不改变主仓 | 已覆盖 | `test_checkpoint.py::test_base_main_repo_untouched`、`test_cleanup.py::test_remove_base_main_untouched`（HEAD/分支/文件字节/preflight clean 断言链） |
| #15 binary/rename/delete/new 正确进 delta | 已覆盖 | `test_checkpoint.py::test_rename_both_paths_checked_out_of_scope`/`test_checkpoint_adds_approved_untracked_file`、`test_delta.py::test_delta_rename_reported_and_split`/`test_delta_binary_file`/`test_capture_delta_delete`/`test_capture_delta_add_new_file` |
| #16 清理前总能恢复 patch 和 manifest | 已覆盖 | `test_cleanup.py` 先归档断言（remove 后 archive 含 baseline-manifest.json + delta.patch）；manifest 拷贝 sha==源 sha |
| #17 外部修改触发漂移门 | 部分（模块级） | 外部修改 → `preflight` dirty → checkpoint 越界 gate（OutOfScopeError）/ delta 范围复核 / cleanup dirty force 拒绝；SAFE_HALT 终态落位属状态机编排层（M0-A） |
| §20.4 #24 artifact 任一字节变 verify 检出 | 已覆盖 | `test_artifact_store.py::test_verify_detects_byte_tamper`/`test_verify_detects_size_change`/`test_verify_missing_data_raises` |

## 4. 诚实边界声明

下列 **不属于 M0-B**，不得据此声称已覆盖：

- EvidenceRunner（命令注册/subprocess 隔离/stdout-stderr artifact）与 Policy Engine（机械 policy）——M0-C；
- 真实 Claude SDK 三角色（Planner/Implementer/Reviewer adapter、tool visibility/dontAsk）——M0-D；
- 端到端试点（真实小任务/越界拒绝/测试失败/熔断）——M0-E；
- approval 门全流程（§20.2 #7–#12，进入 IMPLEMENTING 人工批准）、materialization、recover CLI 自动推进、真实 webhook；
- symlink 越界写拦截（Implementer 工具边界）、DB 行索引落库（artifacts/actual_tree 等，models/store M0-A 已备，接线归编排层）。

M0-B 交付的是「Git 隔离 + artifact store 机器层」：可审计、可暂停、可恢复的任务 worktree 生命周期 + 不可变证据存储，全部沙箱验证、主仓零改动。

## 5. 事实语义与不变量

- **测试隔离**：GitSandbox 每次独立 tmp_path 一次性仓；任何测试不得触碰 `D:\tools\aotf` 或真实仓（机械断言见 §3）。
- **只读保证**：`run_git` 固定 `GIT_OPTIONAL_LOCKS=0`（不写 index/refs）；preflight/archive/artifact-verify 零写目标；manifest/artifact sidecar 编码确定性。
- **隔离语义**：`create_worktree` 嵌套守卫先于 lexists（祖先路径正确拒绝）；checkpoint 只写任务分支（main 零改动）；cleanup 经 peer worktree cwd 删除（Windows 不能删自身 cwd）。
- **失败原子性**：checkpoint 越界不 stage/不 commit；cleanup 先归档后清理、落盘失败回滚已建目录；artifact 写失败清半截。
- **范围闭环**：create 允许集 → manifest.allowed_files → checkpoint actual⊆allowed → delta changes⊆allowed → cleanup 归档 patch（dirty 可归档）。
- **字节级完整性**：baseline manifest（拷贝 sha==源 sha）、delta/archive patch sha（git 原始输出 bytes）、artifact verify（§20.4 #24）。
- **语义边界**：porcelain v2 -z 工作树状态由 checkpoint clean + staged==actual 覆盖；B4 delta 面向 commit 区间。
