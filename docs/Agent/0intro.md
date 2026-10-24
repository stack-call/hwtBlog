---
title: Agent 简介
description: AI Agent（智能体）相关的学习与实践记录
tags: [Agent]
sidebar_position: 1
---

# 简介

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
