---
title: 第十四章：Planning——为什么需要显式任务计划？
sidebar_label: 第十四章：Planning
description: 从复杂修复任务反复调查推导计划、任务状态、依赖、重规划与适用边界
sidebar_position: 15
tags: [Agent, Harness, Planning, Codex, DeepAgents]
---

# 第十四章：Planning——Agent 为什么会一边修 Bug 一边忘记目标？

## 事故：修复一半，Agent 开始重构整个订单系统

我们的 Agent 已能安全执行工具。用户最初只要求修复优惠券退款 Bug，可执行到第 70 轮时，模型又开始全面重构订单模块。它明明有完整 Context，为何仍然偏离目标？

小李第一次尝试在 System Prompt 里重复要求“不要跑题”。但任务包含定位错误、调整业务计算、回归测试、审核影响范围，单条指令不足以持续表达**当前已完成哪些子目标、下一步依赖什么**。

于是他引入一份明确的任务状态：

```text
1. 找到退款计算入口           completed
2. 确认优惠券分摊规则         completed
3. 修改退款计算实现           in_progress
4. 跑全部回归测试             pending
5. 汇总改动与风险             pending
```

这就是计划的最初形态：**给一个长期任务提供持续可更新的进度约束。**

## 新故障：计划不是现实本身

Agent 标记“单元测试完成”，却其实只运行了一个失败的子集。计划状态由模型自报，可能与执行事实不符。

因此要区分：

- **计划目标**：预期做什么；
- **执行证据**：真正运行了什么工具、退出码如何、产物在哪里；
- **状态变更**：根据执行证据将哪一步更新为完成。

计划不能替代 Event History、Checkpoint 或真实测试结果。理想情况下，关键步骤的完成状态需要可验证的证据，尤其是部署、退款等高风险动作。

## 第二个故障：任务依赖发生变化

Agent 原计划先修 `refund.py` 再测 `coupon.py`。但调查发现退款模块依赖一个已废弃的折扣 API，必须先更新契约。

如果计划被当成不可变清单，Agent 会死板执行错误顺序。于是计划还需要支持**重规划（Replanning）**：在有新证据时修改步骤、更新依赖、记录理由。

一个复杂任务可以用 DAG 描述步骤依赖；较小任务只需带状态的列表。不要在所有简单问答中强制运行规划步骤：计划本身也会消耗 Token、工具调用和用户注意力。

## 真实源码：Codex 的 `update_plan`

[Codex `tools/handlers/plan_spec.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/plan_spec.rs) 定义了 `create_update_plan_tool()`。其工具 Schema 要求计划条目包含 `step`、`status`，状态可选：

```text
pending | in_progress | completed
```

工具说明还约束：同一时间最多一项 `in_progress`。这一设计重在**让 Agent 明确报告和更新进度**，不等于系统自动证明所有完成的步骤已经真实执行。

对照 [LangChain `todo.py`](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/todo.py)，其中 `Todo` 结构包含 `content` 与 `status`，体现另一种计划状态实现。

一个有意思的版本差异：DeepAgents 在 2026-07 的 0.7.0 版本起不再默认内置 `TodoListMiddleware`；需要时再显式添加。阅读 [DeepAgents CHANGELOG](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/CHANGELOG.md)，可以看到“规划是否默认启用”也是产品权衡。

## 最终设计：轻量计划 + 证据驱动 + 可选重规划

```text
Goal
 ├─ Task State (todo / doing / done)
 ├─ Evidence refs (files/tests/changes)
 ├─ Dependencies
 └─ Replan reason
```

计划是“我们打算做什么”的动态模型，而执行历史是“实际发生了什么”的事实记录。两者可以互相校验，不能互相取代。

| 五个问题 | 回答 |
|---|---|
| 是什么 | Planning 是把复杂目标分解为可跟踪子任务的机制。 |
| 为什么需要 | 防止长任务偏离目标、重复劳动或错过必要验收。 |
| 没有它 | 进度只能散落在自然语言历史中，协作和恢复困难。 |
| 实际怎么做 | 任务列表/依赖、状态更新、证据引用、重规划。 |
| 处于哪里 | Agent Loop 上层的任务状态控制，与 Context/Checkpoint 共享任务信息。 |

### 实践验证

给 Agent 一个新增优惠券类型并保持旧接口兼容的任务。中途注入一条“原接口已废弃”的新证据；要求合理重规划，且不能因“计划已完成”就跳过测试。

**下一章的问题**：当 Agent 必须停下来等待用户决定时，计划和执行状态如何共同保持一致？
