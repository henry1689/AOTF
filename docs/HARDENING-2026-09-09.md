# AOTF 2026-09-09 安全收口加固

- 更新时间：2026-09-09
- 适用范围：AOTF Pre-MVP M0-A～M0-E
- 定位：受监督的调优治理与证据框架原型；不是生产级自主调优系统

## 本轮目标

修正源码审阅中发现的高风险假通过、安全暴露和验收失真问题，不扩张 M1
角色体系，不发起真实模型调用，不修改外部项目。

## 结构性整改原则

AOTF 不把测试暴露出的单点症状直接转化为局部特判。每个问题必须先回答：

1. 哪条系统不变量失效，真正的责任层属于 schema、adapter、runner、
   orchestrator、policy 还是 evidence；
2. 同一语义经过哪些入口、状态和失败路径，是否还存在旁路；
3. 能否在唯一归属层建立单一事实来源，使所有调用者共同受约束；
4. 契约测试、边界测试和闭环测试能否共同证明结构已闭合，而不只是复现样例通过。

原则上禁止通过调用点特判、重复校验、隐藏 fallback 或只针对某个测试输入的分支
长期修复问题。确需紧急止血时，必须明确标记为临时措施，记录移除条件和后续结构
整改任务，不能把止血补丁计作最终完成。

本轮 Reviewer、工具权限、Evidence 环境和 Pilot 证明的修正均落在各自事实拥有层，
并覆盖正常、缺失、畸形与拒绝路径；不是在单个试点任务上增加例外。

## 已完成修正

1. SDK 结果闭环
   - `SDKRunOutcome` 承载 `result_text`、`structured_output`、
     `resolved_model` 和 `SDKToolAttempt`。
   - `ClaudeSdkRunner` 从终止 `ResultMessage` 映射上述事实。
   - Reviewer 完成态必须得到合法 PASS/CONCERNS/BLOCK；缺失或畸形结果
     fail-closed。编排层只有明确 PASS 才进入 Policy，CONCERNS/BLOCK 均
     SAFE_HALT。

2. 工具权限闭合
   - 首轮先把 Planner/Reviewer 收敛为只读、Implementer 限为批准路径写入；
     后续控制权加固进一步把所有生产模型角色收敛为零工具。
   - 空 `visible_tools` 显式映射为 SDK `tools=[]`，不再扩成默认全集。
   - `can_use_tool` 拒绝不可见工具，并在 outcome 中记录每次 allow/deny。

3. EvidenceRunner 环境最小化
   - 子进程只继承 PATH、系统根、临时目录、locale 和 Python 编码等启动所需
     环境。
   - 不下传 API key、token、cookie 与代理配置；设置
     `PYTHONNOUSERSITE=1`、`GIT_TERMINAL_PROMPT=0`。
   - 本修正减少凭据暴露，但不等同于 OS 级网络沙箱。

4. Pilot 证明去空泛真
   - 无 READY 任务时，范围与 evidence-policy 链证明返回 False。
   - 没有实际注入 budget fault 时，`budget_halt_injected` 返回 False。
   - 历史 live 三任务全部 SAFE_HALT 时“七项全 True”的口径不再成立。

5. 编排可靠性
   - Reviewer 缺判定和 CONCERNS 不再放行。
   - 每次状态转换和 Agent run 使用各自真实时间戳。
   - 终态写入失败不再静默吞掉；返回前重新读取 DB 事实。
   - execution snapshot 尚未持久化前，中间阶段新进程重入明确 SAFE_HALT，
     防止重复 Agent、副作用或空 verdict 推进。

6. 可移植性与仓库卫生
   - Windows/当前平台绝对路径使用一致的跨平台识别规则。
   - `.gitignore` 的运行目录改为根锚定 `/artifacts/`、`/scratch/`、
     `/worktrees/`，不再误隐藏 `src/aotf/artifacts` 源码包。

7. AOTF controller 唯一控制权
   - 修复原 RealRoleRunner 将 Edit/Write 和最多 50 个 agent turns 交给
     Claude Code 的结构缺陷；原实现中 AOTF 只能在 agent loop 前后设门，
     不能控制中间步骤、重试和结束条件。
   - 所有生产模型角色现在固定单 turn、`tools=[]`，同时禁用用户/项目
     settings 与外部 MCP；Claude Code 只作为模型传输层，不再作为任务
     controller。
   - Implementer 只接收 AOTF 生成的批准文件 UTF-8 快照，返回带 preimage
     SHA-256 的 typed create/replace/delete intents。AOTF 在唯一 controller
     层统一验证范围、规范路径、`.git`、symlink、重复路径、hash、文件数和
     字节上限，再原子写入。
   - Reviewer 只接收 AOTF 从 baseline..checkpoint 机械生成的 patch，返回
     typed verdict；不再自行调用 Claude Code Read/Glob/Grep 循环。
   - 任意 Agent/transport 在 controller 应用前直接污染 worktree，立即
     SAFE_HALT，不能通过事后 checkpoint 合法化。

## 验证方法

隔离环境：Linux aarch64，Python 3.11.15，`claude-agent-sdk==0.2.152`。

```text
678 passed, 1 skipped in 49.02s
```

skip 为需要真实 API 凭据的 live controlled example。本轮未设置凭据、未发起
模型请求、未产生模型费用。新增覆盖包括 Reviewer 缺失/畸形/CONCERNS、
structured result、resolved model、空工具集、不可见工具、工具审计、环境秘密
不下传、中间阶段重入、无 READY 空泛证明，以及 controller 直接写入接管、
越批准范围、preimage 漂移、重复路径、控制目录和容量上限反例。

## 仍然存在的边界

- controller-owned 单轮 mutation 协议尚未做真实模型 live 校准；历史 live
  三任务走的是已废止的 Claude Code 多轮直写路径，且均为零落盘。
- Planner 与人工 approval 的完整 operator 流程尚未接入闭环。
- lease/fencing、outbox、recovery 是组件能力，尚未接入
  `run_task_closed_loop`。
- worktree/delta/evidence/Reviewer execution snapshot 尚未持久化，所以本轮
  采用中间阶段 SAFE_HALT，而不是承诺自动续跑。
- 仍无 OS 级进程和网络沙箱；生产模型现为零工具，但 transport 进程隔离仍不足。
- 原样例仓约 240 LOC，未完成 5k～20k LOC 真实中型仓试点。
- 项目在 DEREK 上仍需 owner 决定何时建立首个 Git 版本基线；本轮不自动
  commit 或 push。

## 恢复方式

本轮修改同步前应在 `D:\tools` 仓库外保存同路径备份。若出现兼容问题，按
备份清单逐文件恢复；不要使用 broad reset，也不要覆盖其他现场改动。
