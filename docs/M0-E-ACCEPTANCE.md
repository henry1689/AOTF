# AOTF M0-E 终验收对照（Pre-MVP 完结候选）

> 历史验收快照（2026-09-06）。其中 Reviewer 结果缺口、空工具集语义和
> `prove_criteria` 空泛真已于 2026-09-09 修正；Claude Code 多轮直写路径亦
> 已由 controller-owned 单轮 mutation 协议替代。当前边界与 678 项通过证据
> 见 `HARDENING-2026-09-09.md`。下文历史 live 数据保留用于追溯，不再作为
> 当前实现能力声明。

- 日期：2026-09-06
- 阶段：M0-E / E3 收尾（Pre-MVP 最后一包）
- 权威架构：`D:\tools\AOTF-Pre-MVP-architecture-and-implementation-spec-2026-08-31.md`（SHA-256 `86fb98ed…`）
- 完成态：89 文件 / 652 passed + 1 skipped / 37 测试文件 / 0 warnings / git main·unborn·remote 空

## 1 包清单

| 包 | 内容 | taskbook SHA-256 |
|---|---|---|
| M0-E1 | 任务闭环编排引擎 `src/aotf/orchestrate.py`（授权→implementer→checkpoint→delta→evidence→reviewer→policy，fake 全测） | `60503c96…` |
| M0-E2 | 真实试点资产 `src/aotf/pilot/`：sample 样例仓生成器（samplelib demo 库）/ tasks SAMPLE_TASKS×3 + FAULT_TASKS / adapter RealRoleRunner（D2+D3+D4 唯一组装）+ with_fake 零 token demo / run run_one+run_all+PilotReport+prove_criteria | `b105aabd886a6f304a22218912ca855a989c18011aad3a6854f34828ccab5495` |
| M0-E2-amend | pilot 默认模型 opus→deepseek；`ALIAS_MODELS["deepseek"]="deepseek-v4-flash"`（live 网关校准点） | `10ed29d5b9b15176158b4c1a84611d0c89d7516c9cb8b1dd6acac8a8376d20d0` |
| M0-E3 | 本文档（M0-E 终验收 + §19 汇总 + Pre-MVP 完结） | `249a61c9226e3be70dfbde459a75747f65bf838679496680b82e6bf9b01e2dd1` |

## 2 §19 M0-E 验收逐条映射

spec §19 M0-E 要求六项 → 真实落点（fake 演示零 token 全测；live 真跑为 owner 门控附件，见 §5/§6）：

| §19 要求 | 真实落点 |
|---|---|
| 3 个真实小任务 | `pilot/tasks.py` SAMPLE_TASKS：t1 为 slugify 加 max_length 截断（src/samplelib/slugify.py + test）、t2 修 normalize 裸 CR 换行缺陷、t3 抽取 normalize/slugify 重复的 _strip_control 到 cleaning（行为不变重构）。live 时由真实 Claude 在样例仓 worktree 依 proposal 实施；fake 演示以「写批准文件 → checkpoint → pytest 绿 → READY」闭环等价覆盖 |
| 至少一次越界拒绝 | `pilot/tasks.py` FAULT_TASKS["out_of_scope"]（proposal 诱导改批准集外的 stats.py）+ decide_tool 越界/out-of-scope 拒写 + checkpoint OutOfScopeError → SAFE_HALT。测试：`test_pilot_run.py::test_run_one_out_of_scope_safe_halt`、`test_pilot_adapter.py::test_tool_matrix_denies_out_of_scope_and_traversal` |
| 至少一次测试失败 | FAULT_TASKS["failing"]（demo 注入顶层 raise → pytest 收集失败 → evidence FAIL → policy FAIL → FAILED）。测试：`test_pilot_run.py::test_run_one_failing_evidence_failed`；E1 反例 `test_orchestrate.py::test_policy_failed_via_failing_evidence` |
| 至少一次熔断 | FAULT_TASKS["budget"]（budget_usd=0.001，cost 超 → FakeClaudeSDK 预算门 BUDGET_EXCEEDED → engine SAFE_HALT）。测试：`test_pilot_run.py::test_run_one_budget_fault_halt`；engine 熔断路径 `orchestrate.py` `_run_agent` norm≠completed → SAFE_HALT |
| 至少一次人工战略调整 | `run.py` run_one/run_all `interactive=True`：每任务终态向 owner 请求 continue/replan/adjust/cancel（Pre-MVP：replan/adjust 记录不重排、cancel 即 CANCELLED 并终止 run_all）；run_all「共享推进仓」在 READY 后向 owner 展示并推进样例主仓（promote = `merge --ff-only` + worktree remove），人工检查点即调整点。human_interventions 计入 PilotReport |
| 汇总 token/成本/耗时/失败率/人工介入 | PilotReport（task_id/final_phase/agent_runs/evidence_ok/tokens{role:{input,output}}/cost_usd/duration_s/human_interventions）+ run_all 报告列表；prove_criteria 输出机械证明。fake 汇总实测见 §5 |

## 3 §1.2 七项成功标准 → 机械证明

`pilot/run.py::prove_criteria` 输出七键。2026-09-09 起，未实际覆盖的 READY 或
budget fault 条件返回 False，不再用空泛真补齐“七项全 true”；对应测试见
`test_prove_criteria_success_requires_fault_coverage` 与
`test_prove_criteria_no_ready_is_not_vacuously_true`：

| §1.2 | 键 | 证明手段 |
|---|---|---|
| 1 无跳阶段/双终态/重复副作用 | `phase_chain_legal` | events 账本 TASK_PHASE_CHANGED 按 aggregate_version 序逐对 `state.can_transition` 合法 + 末事件=报告终态；E1 `test_orchestrate.py::test_already_ready_noop`（幂等不重跑副作用） |
| 2 仅批准范围变更 | `changes_within_allowed` | 每 READY 任务 checkpoint commit diff（`git diff branch^ branch --name-only`）⊆ 批准文件集（engine checkpoint 白名单同源） |
| 3 hash/delta/evidence/审查一致 | `evidence_policy_chain` | 每 READY 任务事件链含 DELTA_CAPTURED→EVIDENCE_RUNNING→REVIEWING→POLICY_EVALUATING→CHECKPOINT_READY 且 artifact 落盘（list_artifacts 非空）；tree/delta/evidence 一致性由 C3 bundle 绑定（见 `docs/M0-C-ACCEPTANCE.md` §3） |
| 4 Reviewer 不消费自我评价 | `no_subjective_summary` | ReviewerInputs 无 summary 字段（结构，#28）+ reviewer system prompt 无该词（文本）；见 `src/aotf/agents/roles.py`、`reports.py` |
| 5 Policy 正确处理通过/失败/越界/缺失/中止 | `final_phase_consistent` | DB task.phase/terminal_reason 与报告一致；通过/失败/越界/缺失/中止路径由 C4 决策表 R1–R9 + E1/E2 反例覆盖（`test_policy_engine.py`、`test_orchestrate.py`、`test_pilot_run.py`） |
| 6 并发/崩溃/重复/超预算进入预期终态 | `budget_halt_injected` | 只有 reports 中实际存在 budget fault 且以 SAFE_HALT 收束才为 True；未注入即 False。并发/崩溃/重复仍归 M0-A 组件测试，不能由本键代证 |
| 7 主树/远端/服务/生产零改动 | `sample_main_untouched` | 样例主仓 preflight clean 且每 READY 分支为主仓 HEAD 祖先（只追加）；aotf 仓 git 仍 main·unborn·remote 空、零写；测试 `test_pilot_run.py::test_run_one_sample_main_untouched` |

## 4 §20 强制验收矩阵子集落点引用（既有覆盖，非本包重复断言）

本里程碑复用并验证此前已闭合机器的矩阵项；逐条详细映射见 M0-A/B/C/D-ACCEPTANCE.md：

- 授权与边界：§20.2 #10（越界检测→C4/tools）、#12（Reviewer/Planner 无写工具→D4 `filter_visible_tools`）、#11（Agent 声称批准无效→approval record 权威）；
- Agent 与预算：§20.5 #26（schema 非法不推进→D3/reports parse fail-closed）、#28（Reviewer 不读主观总结→D3 结构保证 + §3#4）、#29（别名解析入 run record→D2 to_agent_run_result + E2-amend deepseek）、#30（网络工具默认不可见→D4 NETWORK_TOOLS）、#25（max turns/budget/deadline→E1 engine + FakeClaudeSDK 门）；
- Evidence：§20.4 #19（tree 与实际不符无效→C3）、#22/#23（INCOMPLETE/deferred→C4）、#21（测试改 tracked 无效→C3 post-run 验证）、#24（artifact 字节完整性→B6 verify）；
- 发布语义：§20.6 #31/#32（机械 FAIL 不可覆盖 / 全门通过只产 RELEASE_CANDIDATE→C4，#23 降级 CHECKPOINT_READY）、#33/#34（无人工 decision 不发布/外部动作独立授权→Pre-MVP 无自动发布）。

## 5 汇总指标（fake 试点演示 + live 附件声明）

fake 演示实测（`test_pilot_run.py` 全绿、零 token）：run_all(SAMPLE_TASKS) 依序 3 任务全部 CHECKPOINT_READY；每任务 agent_runs=2（implementer+reviewer，demo token input=12/output=6、cost=0.01 名义值）；evidence pytest 在任务 worktree 全绿；样例主仓经 promote 前移、preflight clean。

**live 实测附件（2026-09-06，真实 token/成本，落盘未达成——端点工具能力限制）**：

| 运行 | 结果 |
|---|---|
| 三任务连跑（sonnet 别名→网关 deepseek 后端；run a566c2b2） | 3/3 SAFE_HALT `no changes`：in 112,373 / out 46,570 / **$0.856** / 363s；implementer 真实跑完但 0 落盘（worktree 全 clean）；prove 七项 True（无 READY 属空泛真）；resolved_model=None（网关未回 model_usage） |
| 单任务事件诊断（sonnet 别名） | turns=15、**$0.346**、全程零 `TOOL_USE`（只出文本） |
| 候选探测 deepseek-chat | turns=21 触 $1 预算帽、**$1.026**、零 `TOOL_USE` 零落盘 |
| 候选探测 deepseek-reasoner | 启动即终止（同模式预期，未烧满） |

结论：live 机械链路（认证 ANTHROPIC_AUTH_TOKEN+base_url / 预算帽 / 引擎 SAFE_HALT / 证据 / 机械门）**已证正常**；但该 Anthropic 兼容网关的 deepseek 模型端点**不执行 Claude Code 的 tool_use（Edit/Write），只回文本** → implementer 无法落盘。decide_tool 离线验证允许文件写均放行、SDK/适配层正常，属**端点能力限制非 AOTF 缺陷**。累计 live/探测花费 ≈ $2.23。

**deepseek tool-use 接入调研结论（2026-09-06，追加）**：定位到 live 实际经 **CC-Switch 5.x 本地代理（127.0.0.1:15721）→ DeepSeek 官方 Anthropic 端点** `https://api.deepseek.com/anthropic`，CC-Switch 注入 env `ANTHROPIC_MODEL`/`ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL = DeepSeek-V4-flash`。对 claude-agent-sdk 子会话共 **5 种配置试测全部零 `TOOL_USE` 零落盘**：sonnet 别名 via 代理（三任务 $0.856 + 单任务 $0.346）、deepseek-chat via 代理（$1.026）、deepseek-reasoner（启动即终止）、**直连官方端点 + 精确模型名 `DeepSeek-V4-flash`（$1.022）**。结论：DeepSeek-Anthropic 兼容路径在 claude-agent-sdk **子会话不产出文件工具调用**，需**工具转译中间层**（把 Anthropic tool_use 转 deepseek function calling）或工具原生 provider 方可落盘——列为 **M1 演进目标（deepseek tool-use 接入）**，AOTF live 保持门控。累计 live/调研花费 ≈ **$3.25**。

**最终定论（2026-09-06）**：父会话 = **VSCode 扩展内 Claude Code**（`CLAUDE_CODE_ENTRYPOINT=claude-vscode`）经 CC-Switch 转译，文件工具正常；`claude-agent-sdk` 子会话 spawn **自带 CLI（不同二进制/启动路径）**，共 **7 项实验**（含复刻父 env 经代理 + opus 别名 $0.448、直连官方端点 `DeepSeek-V4-flash` $1.022）**均零 `TOOL_USE` 零落盘** → 子会话工具调用差异属**启动路径/二进制层，非 AOTF 代码可修**。累计 live/调研花费 ≈ **$3.70**。AOTF live 归 M1：需**子会话 tool-capable 启动路径**（工具转译中间层 / 换工具原生 provider / 或 agent-sdk 子进程走扩展入口）。

## 6 诚实边界（Pre-MVP 已知缺口，非隐藏）

- **live 真实落盘未达成（2026-09-06 实测）**：live 已尝试执行（见 §5 附件），机械链路正常、真实 token/成本发生，但**网关 deepseek 端点不支持 Anthropic tool use** → implementer 0 落盘（3/3 SAFE_HALT no changes）。AOTF 真实试点需**支持 Anthropic tool use 的 deepseek 接入**方可落盘（owner 决策：AOTF 主模型为 deepseek；deepseek-Anthropic 路径 7 实验零落盘已定论（子会话启动路径/二进制层差异，见 §5——接入需子会话 tool-capable 启动路径：工具转译中间层或工具原生 provider），列为 M1 演进目标）——此为 M1 准入前置；live 需 owner 在场 + 预算上限（默认每任务 ≤$5 / 总 ≤$20）；真实越界/失败/熔断「不保证自然发生」，FAULT_TASKS 注入尝试，未触发如实记录并以 fake 注入 + E1 反例补证（诚实标注真实 vs 注入）；
- **Reviewer 结果通道已于 2026-09-09 修正**：SDKRunOutcome 已承载 result/structured_output/resolved_model，Reviewer 缺失、畸形、CONCERNS 或 BLOCK 均不会进入 READY；真实 tool-use/落盘能力仍未闭合；
- **approval 程序化简化**：试点 approval 由 run.py 建（source=human 满足 schema CHECK），人工确认以 interactive/owner 在场替代——operator 审批编排非 M0-E 范围；
- **样例仓规模**：生成器产出 ≈240 物理行（demo 文本/统计工具库），未达 spec 建议 5k–20k 行目标；生成器如实统计、不强凑装饰空行，E2 试点仓功能完整（可 checkout/改/测/commit）足以支撑三任务与故障注入闭环；
- **.gitignore artifacts/ 前缀历史缺陷 + 历史 __pycache__**：非本包制造，owner 待决不修；
- **symlink 越界写拦截**：canonical 判定经 Path.resolve 语义覆盖，真实建链端到端属 live 校准点（M0-E2 后未模拟）。

## 7 §21 停止条件自查 + M1 决策建议

spec §21 八项停止条件逐条自查（2026-09-06，M0-A..E 全链路）：

| §21 停止条件 | 状态 |
|---|---|
| 需要 bypassPermissions 才能走通 | 否——全程 C-mode + 任务书哈希背书 + S1–S7 流水线 |
| 状态真相依赖 Agent 自述/报告文件 | 否——SQLite 唯一真相 + 机械证据链 |
| 无法在不触碰主工作树下实施 | 否——worktree 隔离 + preflight clean 验证 |
| evidence 不能绑定唯一 tree/delta | 否——C3 bundle tree/delta 绑定 + B6 锁定 |
| 需要降低机械门才能得到成功演示 | 否——演示全部经机械门 |
| Agent 能修改 approval/ledger/artifact | 否——append-only + CAS + 只读面 |
| 连续 3 次同根因失败 | 否——E1/E2 无同根因三连 |
| 实测成本/耗时/人工负担超上限 | 历史 live 探测已发生约 $3.70，且 3/3 零落盘 SAFE_HALT；不能再写“live 未跑” |

**M1 决策建议（2026-09-09 修订）**：M1 默认暂停。若 owner 日后明确选择自治产品路线，应先取得真实工具可用模型上的落盘闭环，完成 execution snapshot/materialization、lease 接线、operator approval 编排及 5k 行级真实仓试点；Reviewer 结果通道、`.gitignore` 前缀和非空泛证明已在本轮加固中修正。
