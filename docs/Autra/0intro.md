---
title: "Autra 设计笔记（引言）：一个个人 Agent Runtime 要解决什么"
sidebar_label: "引言：一个个人 Agent Runtime 要解决什么"
sidebar_position: 1
slug: autra-intro
description: "Autra 是一个用 Rust 编写的个人 Agent Runtime。这一页说明这个系列记录什么、按什么顺序读，以及它和普通 Agent Loop 的差别在哪里。"
tags: [Autra, Agent Runtime, Rust, 设计笔记]
---

Autra 是一个用 Rust 编写的个人 Agent Runtime，目标是管理**长任务**：任务可以暂停、恢复、跨进程存活，而模型调用、工具执行和 Sandbox 只是它调度出来的临时资源。

这个系列不写 API 手册，也不逐个介绍模块。它记录的是设计推导过程：**一个普通 Agent Loop 在什么条件下不够用，于是需要什么机制，这个机制又付出了什么代价。**

## 它和普通 Agent Loop 的差别

大多数 Agent 框架的生命周期和执行资源是绑定的：进程活着，任务才活着；进程退出，未完成的执行就消失了。

Autra 把两者分开：

| 概念 | 性质 |
| --- | --- |
| Task / Run | 持久化的逻辑实体，有明确的状态迁移 |
| 进程、Worker、Sandbox | 承载执行的临时资源，可以随时创建和销毁 |

由此得到三个核心信念：

1. **Correctness by Construction**：用状态机和类型约束非法行为，例如已完成的任务不能重新执行、结果未确认的外部操作不能当作安全重试。
2. **Composition over Coupling**：模型、工具、Sandbox、存储和 Agent Engine 可以独立变化，但插件不能绕过状态一致性和权限检查。
3. **Task over Process**：任务的生命周期长于进程的生命周期。

用一句话概括：

> 一个小而严格的 Rust Runtime Kernel，定义可靠的执行语义；通过可组合的 Harness 和基础设施后端，构建安全、可恢复、可扩展的 Agent 系统。

## 本系列的阅读顺序

| 章节 | 回答的问题 |
| --- | --- |
| [一、从 async/await 到消息驱动](/docs/Autra/autra-async-to-message-driven) | 协程调度和消息驱动分别解决什么？为什么要把 Coordinator 与 Executor 拆开？ |

后续章节会继续沿同一条线索展开：状态机如何表达 Run 的迁移、Effect 如何被记录和重放、任务所有权如何跨进程转移，以及 Sandbox 和进程为什么可以随时被回收。

## 阅读约定

本系列的代码分三类，和文档区其他源码分析保持一致：

| 标注 | 含义 |
| --- | --- |
| 真实源码节选 | 与文中标注的 `mvp-v1` 分支对应，可对照仓库原文 |
| 教学化简 | 能运行，但不是框架原貌 |
| 架构伪代码 | 只表达结构与控制流，不保证能编译 |

文中提到的 `mvp-v1` 是 Autra 的早期架构分支。设计笔记描述的是该版本讨论中的控制流和一种可能的演进方向，不承诺仓库当前每个执行路径都已经实现。涉及尚未实现的部分，正文会明确标注。

源码入口：[stack-call/autra](https://github.com/stack-call/autra/tree/mvp-v1)。
