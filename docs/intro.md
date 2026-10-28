---
sidebar_position: 1
---

# 文档介绍

这里放的是**需要成体系展开**的内容：一个主题往往要连着好几篇才讲得完，拆成单篇博客反而不好按顺序读，所以整理成文档。一次性的总结、观点和随笔放在[博客](https://stack-call.github.io/hwtBlog/blog)。

## 文档里有什么

内容大致分三块。

**计算机基础。** 汇编语言、编译原理、操作系统、x86-64 体系结构、Rust、Leetcode，以及计算机相关的发展历史。这部分偏概念与机制，按知识脉络组织，目标是能把一个东西「从指令到程序」串起来。

**中间件源码。** MySQL、Redis、RocketMQ。对着固定的版本和提交读关键路径：一条 SQL 怎么被解析和执行、一次 `SET` 在事件循环里怎么走、一条消息从生产到投递经过了哪些结构。

**模型与 Agent。** Transformer 的模型结构；LangChain / LangGraph 与 DeepAgents 的运行时和源码；以及 AI Agent 在成本、权限、沙箱、可观测性这些工程问题上的实践。

## 源码分析怎么读

源码类章节会区分三种代码，正文里都有标注：

| 标注 | 含义 |
| --- | --- |
| 真实源码节选 | 与文中固定的版本、Commit 对应的代码，可对照仓库原文 |
| 教学化简 | 为讲清机制而删减或改写的示例，能运行但不是框架原貌 |
| 概念伪代码 | 只表达结构关系，不保证能运行 |

这样读的时候能分清：哪些是框架本来的行为，哪些是我为了讲明白补的解释。文中的运行结果也会注明是实测、语法检查，还是按源码推断的预期。

## 关于完整度

文档是个人学习整理。覆盖的大类不少，但深度并不一致：有的专题已经能顺着读完一条完整链路，有的还只是骨架，章节会陆续补。

所以更合适的用法是把它当作一个框架——先从目录找到入口，再顺着章节往下读，缺的部分以后填。

> [!NOTE] 关于配图
> 早期章节（汇编语言、操作系统、计算机历史）的图片仍托管在 CSDN 上，加载可能偏慢；后续新增内容优先使用仓库内的图片。

## 文档分类

### 计算机基础

- [汇编语言](./category/assembly)
- [编译原理](./category/compiler)
- [操作系统](./category/os)
- [x86-64 体系结构](./category/architecture)
- [计算机相关历史发展](./category/history)
- [Rust](./category/rust)
- [Leetcode](./category/leetcode)

### 中间件源码分析

- [MySQL 源码分析](./category/mysql)
- [Redis 源码分析](./category/redis)
- [RocketMQ 源码分析](./category/rocketmq)

### 模型与 Agent

- [Transformer](./category/transformer)
- [LangChain 与 LangGraph 源码分析](./category/langchain)
- [DeepAgents 源码分析](./category/deepagents)
- [DeepAgents 源码分析（新）](./category/deepagents-new)
- [AI Agent](./category/agent)
