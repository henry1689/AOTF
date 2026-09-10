# AOTF M0-C 终验收对照（Evidence 链与 Policy Engine）

- 日期：2026-09-06
- 状态：M0-C 全部包闭合（C1 typed contract → C5 文档收口）
- 权威架构：`D:\tools\AOTF-Pre-MVP-architecture-and-implementation-spec-2026-08-31.md`
- 权威架构 SHA-256：`86fb98ed094fe750c8b4d7c2e54112d99370a1049e8e8f783deaeb03b4bca2f3`
- 基准：`541 passed` / 26 测试文件 / 63 文件（含本 doc）/ 仓库零 DB 生成物 / Git `main`·unborn·remote 空

## 1. M0-C 包完成清单

| 包 | 内容 | 证据（src / tests） |
|---|---|---|
| C1 | TestEvidenceRecord / EvidenceCheck typed contract + 确定性序列化（§9.2） | `evidence/schema.py` / `test_evidence_schema.py` |
| C2 | EvidenceRunner：命令注册表 + subprocess 隔离执行（真实 stdout/stderr bytes） | `evidence/runner.py` / `test_evidence_runner.py` |
| C3 | evidence bundle：tree/delta 绑定 + §9.3 顺序编排 + B6 ingest 锁定 | `evidence/bundle.py` / `test_evidence_bundle.py` |
| C4 | Policy Engine 机械裁决（PASS/FAIL/INCOMPLETE/ERROR + next-state 建议） | `policy/rules.py` `policy/engine.py` / `test_policy_rules.py` `test_policy_engine.py` |
| C5 | 本 doc + README 收口 | `docs/M0-C-ACCEPTANCE.md` |

## 2. spec §19 M0-C 内容对照

| spec 内容 | 覆盖 | 证据 |
|---|---|---|
| 命令注册表 + subprocess 隔离 | 已覆盖 | `CommandSpec`/`build_registry`（MappingProxyType 只读、唯一 command_id）/`run_command`（shell=False、list argv、registry allowlist、CREATE_NO_WINDOW）；`test_build_registry_unique`/`test_build_registry_readonly`/`test_shell_false_no_injection`/`test_run_command_missing_cwd_valueerror` |
| stdout/stderr artifact | 已覆盖 | 运行期哨兵错误事实（TIMEOUT_EXIT=-1/NOT_FOUND_EXIT=-2，`test_timeout_error_sentinel`/`test_argv0_missing_error_sentinel`）；C3 经 B6 ingest stdout/stderr 并 sha 交叉核对（`test_produce_success_bundle`/`test_evidence_artifact_bytes_match`） |
| tree/delta 绑定 | 已覆盖 | C3 前置冻结 `HEAD^{tree}`==入参 worktree_tree（不符→EvidenceError「evidence tree mismatch」，#19）+ post-run HEAD/tree 未变校验（#21）；`test_post_run_tree_unchanged`/`test_evidence_tree_mismatch_rejected`/`test_runner_changed_tracked_file_invalid` |
| mechanical policy | 已覆盖 | C4 决策表 R1–R9（证据+门+控制面事实→verdict）；#22/#23/#31/#32 见 §3/§4；`test_policy_engine.py` 25 条 |
| evidence tamper/failure tests | 已覆盖 | 测试改 tracked 文件→无效（#21，ingest 零发生）；evidence bundle 不自洽→ERROR/SAFE_HALT（`test_evidence_tamper_error`/`test_unbundled_record_error`）；缺 required→INCOMPLETE（#22） |

**验收（spec §19 M0-C 整体）**：evidence 自 C2 真实进程事实产生 → C3 绑定唯一 tree/delta 并 append-only 锁定 → C4 纯函数裁决；全程无 LLM 参与裁决、无自动 RELEASED。全部测试沙箱验证（GitSandbox tmp 一次性仓或纯构造），零真实仓触碰。

## 3. §20.4 Evidence 反例对照

| 项 | 状态 | 证据 |
|---|---|---|
| #18 baseline 测试不能冒充 post-edit | 机制覆盖 | evidence 只对 checkpoint 后 clean 的 post-edit snapshot（create_worktree + produce 前置 clean 门禁）产生且绑定 actual_tree；baseline 无该绑定入口；`test_not_clean_worktree_rejected`/`test_evidence_tree_mismatch_rejected` |
| #19 evidence tree 与 actual tree 不同则无效 | 已覆盖 | `test_evidence_bundle.py::test_evidence_tree_mismatch_rejected`（入参 tree 改一位→EvidenceError，ingest 零发生） |
| #20 test stdout 伪造不影响 runner 事实 | 已覆盖 | C2 `run_command` 只从 subprocess 真实捕获的 stdout bytes 派生 `stdout_sha256`（无外部注入路径）；`test_check_sha_matches_captured`/`test_run_unicode_stdout_bytes`/`test_shell_false_no_injection`；C3 sha 交叉核对 `test_evidence_artifact_bytes_match` |
| #21 测试命令改变 tracked 文件则无效 | 已覆盖 | `test_evidence_bundle.py::test_runner_changed_tracked_file_invalid`（post-run HEAD/tree 未变 + clean 校验；任何 ingest 前拒绝） |
| #22 缺 required check 时 Policy 为 INCOMPLETE | 已覆盖 | `test_policy_engine.py::test_missing_required_incomplete`（required_missing，next=None）/`test_record_none_incomplete`（evidence_missing） |
| #23 deferred_out_of_scope 不被计为 passed | 已覆盖 | required 侧压制→INCOMPLETE `test_deferred_required_incomplete`；release 侧压制→降级 CHECKPOINT_READY `test_release_gate_deferred_downgrade` |
| #24 artifact 任一字节变化都能被 verify 检出 | 已覆盖（B6 + M0-C 锁定） | `test_artifact_store.py::test_verify_detects_byte_tamper`/`test_verify_detects_size_change`/`test_verify_missing_data_raises`；bundle ingest 后证据 bytes 保真 `test_evidence_artifact_bytes_match` |

## 4. §20.6 发布语义反例对照

| 项 | 状态 | 证据 |
|---|---|---|
| #31 LLM PASS 不能覆盖机械 FAIL | 已覆盖 | `test_policy_engine.py::test_llm_cannot_override_mechanical_fail`（evaluate 签名无 llm/review/override 入参 + 机械 FAIL 恒定） |
| #32 全部机械门通过也只能产生 RELEASE_CANDIDATE | 已覆盖 | `test_no_released_concept`（MechanicalResult/PhaseHint 无 RELEASED 值，输出上限 RELEASE_CANDIDATE）/`test_release_candidate_all_gates_met` |
| #33 RELEASE_CANDIDATE 无人工 decision 不产生发布动作 | 机制覆盖 + 编排边界 | C4 输出上限 `PhaseHint.RELEASE_CANDIDATE`，无任何「发布/RELEASED」输出类型（类型保证，spec §11.1 无自动 RELEASED）；人工 decision 受理与状态推进归编排层（M0-D/E） |
| #34 任何生产部署/数据库/外部发送均需独立授权流程 | 边界声明 | Pre-MVP 全链零真实外发/零生产写入；独立授权流程属 M0-D/E + 生产部署治理（不在 Pre-MVP 机器内） |

## 5. §9.2 / §9.3 对照

| spec | 落点 |
|---|---|
| §9.2 TestEvidence 最小结构 | C1 typed contract 冻结全部字段（schema_version/evidence_id/task_id/worktree_tree/delta_sha256/runner{identity,host_fingerprint,started_at,ended_at}/checks[{check_id,command_id,cwd,exit_code,stdout_sha256,stderr_sha256,duration_ms,status}]/bundle_sha256）；fail-closed 校验 + 确定性序列化（sorted-json ensure_ascii、UTC `%f`）；checks 空合法、required 判定归 C4 |
| §9.3 证据生产顺序 1–7 | C3 `produce_evidence` 步骤映射：1 冻结 actual tree/delta → 2 runner 逐 check 执行 → 3/6 stdout/stderr 落 scratch 源文件 + B6 ingest 锁定（sha 交叉核对）→ 5 post-run tree/delta 未漂移验证 **先于** 4 组装与 ingest（原子拒绝，§9.3 #7 语义）→ evidence JSON（kind=test-evidence、artifact_id=evidence_id）ingest；post-run 校验拒绝即 #21 |

## 6. 诚实边界声明

下列 **不属于 M0-C**，不得据此声称已覆盖：

- 真实 Claude SDK 三角色（Planner/Implementer/Reviewer adapter、tool visibility/dontAsk）——M0-D；
- approval 门全流程编排（进入 IMPLEMENTING 的人工批准、EDIT_AUTHORIZED 接线、§20.2 #7–#12）——C4 只收「批准仍有效」事实 bool，不校验 approval 生命周期；
- INCOMPLETE 后的编排策略（回 EVIDENCE_RUNNING 补证据 vs 重规划）——C4 只给 next=None「不可自行推进」信号，编排分支归 M0-D/E；
- orchestrator→policy 接线（POLICY_EVALUATING 推进至 CHECKPOINT_READY/RELEASE_CANDIDATE/FAILED/SAFE_HALT 的调度）；
- materialization / recover CLI 自动推进 / 真实 webhook；
- Gate Explainer（M2，LLM 解释层；不得把 FAIL 改成 PASS）；
- 端到端试点（真实小任务/越界拒绝/测试失败/熔断，§19 M0-E）；
- 主仓写入、真实 Agent、生产仓触碰（Pre-MVP 全程零触碰）。

注记（环境事项，非 M0-C 机器缺陷）：`.gitignore` 的 `artifacts/` 无前导 `/`，误忽略 `src/aotf/artifacts/*` 代码包 2 文件（git untracked=60 vs 文件系统 62）——历史配置，owner 待决（建议改 `/artifacts/`）；另有历史 `__pycache__` 残留（非 M0-C 制造，C4 触碰模块零 pyc）。均不影响本验收声明。

## 7. 事实语义与不变量

- **evidence 链闭环**：freeze tree/delta（C3 前置）→ runner 真实进程事实（C2）→ record bundle 自洽（C1 `compute_bundle_sha256` 排除自身字段）→ B6 append-only 锁定（sha 交叉核对）→ policy 纯裁决（C4，无副作用）；
- **令牌域分层**：C1 evidence 域 `[A-Za-z0-9._@-]{1,64}` 含 `@`；B6 artifact 域无 `@`（bundle 边界收敛为 B6 + check_id ≤57 保 kind 后缀 ≤64，fail-fast）；C4 门沿用 C1 域（不经 ingest，无需收敛）；
- **CRLF 字节事实**：Windows 子进程 `print` 管道输出 CRLF；evidence sha = 捕获原始字节事实（跨 OS 不同）；测试内容断言前需 LF 归一；
- **确定性编码**：sorted-json `ensure_ascii` 紧凑；datetime→UTC `%f`（保留微秒防同秒 bundle 碰撞）；
- **runner 事实性**：shell=False、registry allowlist、哨兵错误事实（TIMEOUT_EXIT=-1/NOT_FOUND_EXIT=-2）不冒充真实退出码；
- **纯函数裁决**：evaluate 无 I/O/git/DB/时钟；ControlFacts 全字段必填无默认（忘传即 TypeError，fail-closed）；畸形入参→PolicyError，运行事实异常→ERROR verdict；
- **失败原子性**：C3 post-run 校验先于任何 ingest（#21/#19 拒绝时 artifacts_root 不建）；partial ingest 仅 ingest 自身中途失败才可能（append-only 可审计残留、无半成品 record 返回）；
- **测试隔离**：GitSandbox 每次独立 tmp_path 一次性仓 + 纯构造测试（policy/schema），零真实仓触碰；
- **只读保证**：`run_git` 固定 `GIT_OPTIONAL_LOCKS=0`（不写 index/refs）；bundle/preflight 零写目标；
- **依赖单向**：`bundle → runner/schema/artifacts.store/git.base`；`policy → evidence.schema + policy.rules`；无环、无 DB/状态机依赖。
