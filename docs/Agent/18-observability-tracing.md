---
title: 第十八章：Observability——如何看见 Agent 实际做了什么？
sidebar_label: 第十八章：Observability
description: 从线上退款修复失败推导事件、追踪、指标、回放及隐私控制
sidebar_position: 19
tags: [Agent, Harness, Observability, Tracing, Codex]
---

# 第十八章：Observability——Agent 明明回答成功，为什么退款 Bug 还在？

## 事故：用户看到“修复完成”，生产测试却仍失败

某次用户报告：Agent 花了十五分钟，最后说“修复完成”，但代码中退款金额仍然不对。

小李最初只能看到最后的聊天文本，完全不知道 Agent 中途进行了哪些工具调用。没有轨迹，就无法区分：模型没找到关键文件、工具执行失败、权限拒绝、上下文压缩丢掉事实，还是最后的验证步骤被跳过。

他决定记录每一个关键事件。最开始事件只有：

```text
turn_started
model_requested
model_responded
tool_started
tool_finished
turn_completed
```

这就是可观测性的第一步：**把不可见的控制流变成可检查的事件流。**

## 第二次故障：事件太多，但无法串起来

一个 Task 里有多个 Turn，每个 Turn 又有若干 Model Request 和 Tool Call。仅有时间戳不足以回答“哪次模型决策触发了这个危险命令”。

小李引入层次化的追踪 ID：

```text
trace_id (任务)
 ├─ span_id (turn)
 │   ├─ span_id (model_request)
 │   ├─ span_id (tool_call: grep)
 │   └─ span_id (tool_call: pytest)
 └─ span_id (turn)
     └─ span_id (tool_call: apply_patch)
```

每个 Span 记录开始/结束、状态、错误类型、耗时，以及对上游 Parent 的关联。完整 Tool Output 可以放在 Artifact Store，只在 Trace 中保留引用。

**Event History 不等于 Trace**：前者倾向于任务事实与恢复；后者倾向于跨组件诊断与性能分析。可以复用事件来源，但保存策略不一定一样。

## 第三次故障：记录了全部内容，反而泄露数据

工具日志里可能包含支付令牌、顾客邮箱和订单信息。若默认把所有 Prompt、Tool 参数和结果写进分析平台，就会引入新的隐私与权限风险。

因此追踪系统至少需要：字段级脱敏、访问控制、数据保留期、Trace 采样策略、敏感 Artifact 的独立授权，以及“记录元数据但不记录原文”的模式。

### 对照真实源码：Codex 的事件与 UI 协议

在 [Codex `codex-rs/docs/protocol_v1.md`](https://github.com/openai/codex/blob/main/codex-rs/docs/protocol_v1.md) 中，UI 与核心通过 Submission Queue / Event Queue 交互；`EventMsg` 用于呈现执行过程。具体事件类型可从 [protocol.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs) 追踪。

[Codex `tools/events.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/events.rs) 展示工具执行事件与文件变更等结果如何转成可消费的协议事件。注意：**给 UI 的事件流**与**专门的 OpenTelemetry Tracing**不是同一个东西；两者可以关联，但不能等同。

OpenCode 的 [session/llm.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm.ts) 还提供一个有意思的阅读切口：LLM 流路径中涉及追踪配置、会话 ID 与流事件归一化，可以沿调用链区分 Runtime 事件和底层模型调用事件。

## 第四次故障：有 Trace 了，但不知道要优化什么

小李建立一张仪表板，分开观察：

- **正确性**：实际测试成功率、任务完成率、错误回滚率；
- **效率**：每任务 Token、总耗时、模型请求次数、工具调用次数；
- **稳定性**：工具失败率、超时率、重试率、恢复成功率；
- **策略与安全**：拒绝/审批比例、越界拦截事件、重复副作用；
- **上下文质量**：压缩后的目标偏移、证据找回成功率、重复搜索率。

如果 Agent 将工具调用次数减少一半，却遗漏关键测试，不能说优化成功。可观测性负责看见事实，评测负责判断结果是否满足目标。两者应该关联，却各有边界。

| 五个问题 | 回答 |
|---|---|
| 是什么 | Observability 通过事件、Trace、指标、日志解释系统实际行为。 |
| 为什么需要 | 无法只凭最终回答定位多轮执行中的故障。 |
| 没有它 | 失败归因靠猜，性能和成本无法系统优化。 |
| 实际怎么做 | Trace/Span、事件流、结构化指标、脱敏与查询。 |
| 处于哪里 | 横切 Agent Loop、Model Adapter、Tools、Context、Session 和 UI。 |

### 实践验证

在模拟 Agent 中注入一次“模型返回有效调用、但工具实际超时”的故障。仅通过 Trace 输出定位：哪一轮、哪个工具、为何失败、有没有重试、最终回答是否过早宣称成功。

**下一章的问题**：所有模块学过了，能否自己写一个真正运行的最小 Agent Harness，把这些抽象连成一条线？
