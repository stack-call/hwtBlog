---
title: Agent 系列导读
sidebar_label: 导读
description: AI Agent（智能体）相关的学习与实践记录
tags: [Agent]
sidebar_position: 1
---

# Agent 系列导读

本专题记录 AI Agent（智能体）相关的学习与实践。和汇编、操作系统那些专题相比，这里的知识更新得快得多：很多结论只在某个模型版本、某个框架版本下成立，隔几个月可能就变了。因此这里更像是**持续修订的笔记**，而不是结论性的教材。

写作原则有两条：

1. 尽量写清"这个结论是在什么前提下得出的"，而不是只给结论；
2. 能跑通的代码和可复现的实验，优先于抽象描述。

## 内容组织

全部章节围绕**同一个工程故事**展开：小李要做一个修复优惠券退款 Bug 的 Coding Agent，它一路上不断出故障，每一章从一个真实故障出发，推导出对应的机制，再回到开源 Harness 的真实源码。

分两个部分：

**第一部分：上下文管理（第 1–9 章）**——Agent 记得住什么、记得起什么

1. [什么是上下文管理](./1context)——上下文溢出、工具输出卸载、摘要压缩
2. [上下文信息利用质量](./2context)——Context Rot 与信息选择
3. [长期记忆](./agent-context-memory-chapter-3)——Memory 的生命周期与存储隔离
4. [Subagent 的上下文隔离](./agent-context-subagents-chapter-4)——isolated 与 fork 的取舍
5. [从 Subagent 到 A2A](./agent-context-a2a-chapter-5)——跨 Harness 的上下文交接
6. [Checkpoint 与中断恢复](./checkpoint-resume)——历史还在，为什么不能继续
7. [Context Assembly 与 Token Budget](./context-assembly)——下一次调用要带什么
8. [Context Evaluation](./context-evaluation)——压缩率提高了，成功率为什么下降
9. [从零实现 Context Manager](./build-context-manager)——把前八章连起来

**第二部分：Harness 运行时（第 10–19 章）**——Agent 怎么真正动起来

10. [Agent Loop](./agent-loop)——从 while 循环到可控运行时
11. [Tool System](./tool-system)——模型说要运行工具之后发生了什么
12. [Execution Environment](./execution-environment)——工作区与 Sandbox
13. [Permission 与 Approval](./permission-approval)——什么操作需要人批准
14. [Planning](./planning-task-management)——为什么需要显式任务计划
15. [Human-in-the-Loop](./human-in-the-loop)——Agent 如何暂停和恢复
16. [Model Adapter](./model-adapter)——为什么不能把模型 SDK 直接塞进 Loop
17. [Hooks、MCP 与 Skills](./hooks-mcp-skills)——Harness 如何扩展
18. [Observability](./observability-tracing)——如何看见 Agent 实际做了什么
19. [从零实现一个最小 Agent Harness](./build-agent-harness)——把运行时连起来

## Agent Harness 的模块地图

上面的章节顺序是**叙事顺序**，不是架构顺序。换一个视角来看：上下文管理只是 Agent Harness 的一个核心模块，真正要回答的问题还有——谁决定下一步行动？工具怎么执行？错误怎么恢复？危险操作如何控制？任务怎样规划？系统如何判断 Agent 做得好不好？

这些问题归并起来，一套 Agent Harness 大致可以拆成 12 个能力模块。这是便于学习和讨论的逻辑划分，不代表开源项目真的按这个目录组织代码：

```text
Application / CLI / IDE / API
  用户交互、任务入口、事件展示
        ↓
Agent Loop / Runtime
  驱动整个执行流程
        ↓
Model Adapter · Context Manager · Planning · Tool Registry
Tool Runtime · Policy Engine · Subagent · Session Manager
Execution Environment · Extension Layer
        ↓
Persistence · Observability · Evaluation · Configuration
```

| 模块 | 它解决的工程问题 | 对应章节与源码入口 |
| --- | --- | --- |
| Agent Loop | 模型什么时候继续调用、什么时候执行工具、什么时候结束？ | [第 10 章](./agent-loop)；Codex `session/turn.rs` |
| Model Adapter | 如何屏蔽不同模型 API、工具调用、流式输出的差异？ | [第 16 章](./model-adapter)；OpenCode 统一流事件、Codex `client.rs` |
| Tool System | 工具如何注册、发现、路由、校验参数并返回结果？ | [第 11 章](./tool-system)；Codex `tools/registry.rs` |
| Execution Environment | Shell、文件、浏览器工具在哪里执行？如何隔离工作区？ | [第 12 章](./execution-environment)；Codex `tools/orchestrator.rs`、OpenHands |
| Permission & Approval | Agent 可以执行什么？什么时候必须经用户批准？ | [第 13 章](./permission-approval)；Codex `tools/orchestrator.rs`、DeepAgents `graph.py` |
| Planning & Task Management | 复杂任务如何拆分、跟踪、更新、终止？ | [第 14 章](./planning-task-management)；Codex `plan_spec.rs` |
| Context & Memory | 哪些信息进入模型？哪些应该长期保存？ | 第 1–9 章 |
| Session & Persistence | 会话、状态、中断和恢复怎么管理？ | [第 6 章](./checkpoint-resume) |
| Multi-Agent Orchestration | 子任务如何委派、并行执行、汇总与取消？ | [第 4 章](./agent-context-subagents-chapter-4)、[第 5 章](./agent-context-a2a-chapter-5) |
| Human-in-the-Loop | 执行中如何暂停、等待用户输入、再继续？ | [第 15 章](./human-in-the-loop)；LangGraph `interrupt` |
| Observability & Tracing | 如何追踪每轮模型调用、工具执行、Token 和失败原因？ | [第 18 章](./observability-tracing)；Codex `tools/events.rs` |
| Evaluation & Reliability | 怎么衡量成功率、成本、安全性和稳定性？ | [第 8 章](./context-evaluation)（只覆盖上下文策略这一层） |

这套划分的中心是 **Agent Loop**：它负责驱动，其余模块要么提供能力，要么施加约束。Agent Loop 不需要知道 Sandbox 怎么实现，也不应该知道某个模型的 JSON 长什么样；它只负责推进状态机，其余边界各自独立。扩展层（Hooks、MCP、Skills）没有单独列进 12 个模块，因为它横跨工具执行与上下文组装两端，见[第 17 章](./hooks-mcp-skills)。

如果按架构而不是按叙事读，建议的顺序是 **Agent Loop → Tool System → Execution Environment → Permission → Planning**：这五个模块构成 Harness 最核心的执行闭环。理解它们之后，再回头看 Context、Memory、Checkpoint 和 Subagent，就更容易读懂 Codex 或 DeepAgents 的完整结构，而不只是记住几个孤立机制。

有两处需要单独说明：

- **Evaluation 尚未单独成章。** 第 8 章讲的是「怎样证明一次上下文改动真的有效」，这套方法论可以迁移，但它只回答了评测的一个切面。完整的 Agent 评测（任务集、打分器、回归基线、线上采样）还缺。
- **Planning 是可插拔的，不是必经步骤。** 一个明确的证据是：DeepAgents 从 0.7.0（2026-07-29）起不再默认装配 `TodoListMiddleware`，`write_todos` 工具、`todos` 状态通道和计划提示词都变成需要显式添加的能力。这本身就是一个值得研究的取舍：什么时候明确的计划管理值得它的 Token 与工具调用开销，什么时候反而是负担。

## 配套代码

第 19 章的教学代码在仓库根目录的 `examples/` 下，只依赖 Python 标准库，不接真实 LLM、不执行任意 Shell、不碰真实支付：

```bash
cd examples
python -m unittest -v test_mini_agent_harness.py
python mini_agent_harness.py
```

代码用来演示边界（工具分发、策略拒绝、步数预算、事件流），不是对开源 Harness 的逐字复刻。文中标注的「真实源码」都附了 GitHub 入口。

## 和已有专题的关系

前面几个专题是自底向上的：从门电路、指令集、汇编，到操作系统和编译器。Agent 处在完全相反的一端——它建立在自然语言和概率模型之上，几乎没有"确定性"可言。

但这个对比本身挺有意思：**操作系统的调度器必须在确定的时间约束内做出确定的选择，而 Agent 的决策过程是概率性的、可以失败、可以重试。** 两者都在解决"如何让一个系统在有限资源下完成复杂任务"，只是约束条件完全不同。

## 说明

内容为个人学习整理，会不断修订。如果某个结论已经过时，我会直接改掉而不是保留历史版本——需要看历史的话，走仓库的 git 记录。
