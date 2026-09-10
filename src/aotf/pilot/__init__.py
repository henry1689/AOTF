"""AOTF M0-E2 pilot 试点资产（真实试点 + fake 演示，零依赖回环）。

- sample.py —— 样例仓生成器（samplelib demo 库；独立仓 D:\\tools\\aotf-e2e-sample，
  幂等 clean 复用；tmp 内可生成供测试）；
- tasks.py —— SAMPLE_TASKS（样例仓连续三任务：加功能/修 bug/重构）+
  FAULT_TASKS（越界/失败/熔断注入）；
- adapter.py —— RoleRunner 真实 adapter（D2 ClaudeSdkRunner + D3 roles +
  D4 decide_tool 唯一组装点）；with_fake() demo 零 token；
- run.py —— run_one/run_all + PilotReport + §1.2 prove_criteria 七项。

live 真实运行（--live）为**门控步骤**：需 ANTHROPIC_API_KEY（env 提供、
不入文件）+ owner 在场 + 预算上限；默认路径（fake）零真实 token。E2 不改
E1 引擎与各里程碑模块；样例仓独立、仅其可 commit；aotf 仓零写。

__all__ 引用子模块名（不 import 运行时符号，避免环）。
"""

__all__ = ["adapter", "run", "sample", "tasks"]
