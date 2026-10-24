---
title: 第六章：Checkpoint 与中断恢复
description: 从优惠券 Agent 崩溃开始，推导状态快照、恢复语义、重试与幂等
sidebar_position: 7
tags: [Agent, Context, Checkpoint, LangGraph]
---

# 第六章：Checkpoint 与中断恢复——历史还在，为什么 Agent 仍然不能继续？

> 本章延续同一个项目：修复电商优惠券退款 Bug 的 Coding Agent。我们继续遵循「遇到故障 → 最小改进 → 新故障 → 源码验证」。
>
> **先记住一个区别**：History 记录发生过什么；Checkpoint 记录可继续执行的状态；Context 是下一次 LLM 调用真正接收的信息。它们相关，但不等价。

## 事故发生在第 104 轮

小李的 Coding Agent 已经把工具结果卸载到文件，把旧消息压缩成摘要，还让子 Agent 完成了退款规则审计。就在他以为长任务终于稳定时，进程崩溃了。

```text
101 修改 coupon.py          ✓
102 修改 refund.py          ✓
103 执行订单数据迁移        ✓
104 准备运行回归测试        × 进程退出
```

重新启动后，历史文件还在、代码也已经改了。小李很自然地把所有 History 重新发给 LLM，并说：「接着做」。

但模型不知道第 103 轮迁移到底是否提交成功。有的日志写着“开始迁移”，却没有最后的执行回执。如果它再次执行迁移，可能让数据重复变更。

**问题不在于模型忘了什么，而在于系统不知道自己完成到了哪一步。**

## 第一次改进：把 Agent 当前状态存起来

小李不再只存消息，还保存一份运行快照：

```text
thread_id: coupon-fix-001
step: 104
phase: test
pending_action: run_refund_regression_tests
modified_files: [coupon.py, refund.py]
summary_ref: /summaries/coupon-fix-001.md
```

现在，恢复流程不是「把所有对话再跑一遍」，而是：

```text
启动 → 读取 thread 的最后一个已提交快照
     → 恢复状态与待执行节点
     → 构建下一次 Context
     → 继续执行
```

这就是 **Checkpoint（检查点）** 在此场景中的职责。它不只是摘要文本，而是 *运行时状态的持久化表示*：当前变量、待执行步骤、图的调度信息等。不同框架存储内容不同，不能把示例 YAML 当作 LangGraph 的真实存储格式。

### 到源码里验证：LangGraph 的 Checkpointer

[LangGraph Persistence 官方文档](https://docs.langchain.com/oss/python/langgraph/persistence) 展示了 `thread_id` 和 Checkpointer 的配合。关键用法如下（来自官方 API，省略了图构建过程）：

```python
from langgraph.checkpoint.memory import InMemorySaver

# builder 是已构建的 StateGraph
app = builder.compile(checkpointer=InMemorySaver())
config = {"configurable": {"thread_id": "coupon-fix-001"}}
app.invoke(initial_state, config)
state = app.get_state(config)
```

为什么要传 `thread_id`？因为同一套 Agent 图可以同时运行多个用户任务，系统需要明确知道当前属于哪一条执行链。否则，恢复时可能拿到另一个任务的状态。

这里的 `InMemorySaver` **只用于演示**：它存于内存，进程退出就没有了。真的需要抗进程重启，要换成持久化 Checkpointer，例如数据库支持的实现。`create_deep_agent(..., checkpointer=...)` 也是将这一能力传入底层执行图，而不是让摘要文件直接充当 Checkpoint。

源码入口：

- [DeepAgents `create_deep_agent`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)：`checkpointer` 参数向 `create_agent` 传递。
- [LangGraph `StateSnapshot` 与 Checkpoint 文档](https://langchain-ai.github.io/langgraph/reference/checkpoints/)：快照、Thread、Pending Writes 的定义。

## 第二次故障：有 Checkpoint，为什么还会重复执行？

小李认为「每一步执行完存一次」应该足够了。可问题恰好发生在两个动作之间：

```text
发起迁移操作
   ↓
数据库已经提交      ← 外部世界的事实
   ↓
进程崩溃            ← 还没来得及写本地 Checkpoint
   ↓
恢复时只看到“迁移未完成”
```

这就是典型的 **提交结果与状态记录之间的原子性缺口**。

无论 Checkpoint 多么完整，只要它与外部数据库不是同一个原子事务，系统就可能处于「外部成功、本地未知」的状态。

小李因此引入第二个机制：**幂等操作与结果核对**。

```python
# 教学代码：表示设计原则，不是任何 Harness 的原样实现
operation_id = f"{thread_id}:migration:v2"
result = migration_service.apply_once(operation_id, migration_payload)
```

`operation_id` 必须是可重试的稳定标识，而不能每次重试都生成随机 ID。外部迁移服务要么识别重复请求并返回第一次的结果，要么支持通过该 ID 查询最终状态。如果服务完全不支持幂等或结果查询，仅靠 Checkpoint **不能保证 exactly-once**；只能结合人工核对、补偿事务或接受某些操作最多一次的限制。

> **小结**：Checkpoint 解决「恢复到哪里」；幂等性或结果核对解决「这一步能否安全重试」。

## 第三次故障：恢复和重放，真的是同一件事吗？

恢复有两种不同意图：

- **Resume**：从当前已保存的位置继续完成同一个任务。
- **Replay / Time Travel**：从过去某个检查点重新走后续步骤，通常用于调试或分叉方案。

假设第 120 轮已完成了新的退款修复，如果研发人员选择从第 80 轮检查点重放，之后的模型调用、工具副作用可能再次触发。这并不等同于「恢复最新进度」。

[LangGraph `Command` 与 `interrupt` 实现](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py) 展示了另一个容易忽略的事实：`interrupt()` 恢复时，节点逻辑可能从节点开头重新执行。因此，**中断前的副作用不能想当然地视为只执行一次**。

```python
# LangGraph API 用法：人为审批后继续
from langgraph.types import Command

app.invoke(Command(resume={"approved": True}), config)
```

这一代码适用于事先通过 `interrupt()` 暂停的图；普通崩溃是否可以直接 `invoke(None, config)` 继续，取决于图当前状态、节点与恢复入口，不能把它当作适用于所有 Harness 的通用调用。

### 再看 OpenHands 与 Codex：两个不同实现角度

OpenHands 的 [`ConversationState.create(...)`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/state.py) 明确处理 **create-or-resume**：既可能新建对话，也可能从持久化数据恢复。这说明 Event History 必须与配置校验、工作空间状态结合，而不是简单地把历史文本塞回模型。

Codex 的 [`rollout/src/recorder.rs`](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/recorder.rs) 将会话 rollout 保存到 JSONL；`RolloutRecorderParams` 中区分 `Create` 和 `Resume`。它提供了记录与重新加载会话的基础，但**回放文件不自动撤销 Git 修改、数据库事务或 shell 命令副作用**。

它们与 LangGraph 的快照运行时不属于同一套接口；我们学习的是共同的边界，而不是强行统一成一份状态结构。

## 第四次改进：恢复时还要重新构建 Context

小李终于发现，持久化运行状态与给 LLM 看什么仍然分属两层。

```text
Event History ───────┐
                     │
Checkpoint ──────────┼─→ 恢复 Agent Runtime
                     │          │
Artifact/Memory ─────┘          ↓
                           Context Builder
                                ↓
                        下一次模型输入
```

例如恢复后，LLM 并不需要看到 104 轮全部原文；它需要已确认的业务约束、已修改文件、当前测试状态和待执行操作。构建 Context 的过程将在下一章细讲。

## 工程验证：恢复策略要经过故障注入

只测试「正常暂停后恢复」远远不够，至少需要四次故障演练：

1. **在 LLM 响应前崩溃**：能否找到正确的待执行步骤？
2. **外部工具成功但 Checkpoint 未写入**：能否发现操作已经完成，避免重复迁移？
3. **多个并行步骤中某一步失败**：成功步骤的输出是否保留，失败步骤是否按预期重试？LangGraph 有 pending writes 的相关机制，但依然要测试真实图。
4. **使用旧快照回放**：是否明确限制危险副作用，避免把调试 Replay 当作生产恢复？

### 本章归位

| 概念 | 是什么 / 为什么需要 | 没有它会怎样 | 与上下游关系 |
|---|---|---|---|
| History | 过去事件的记录 | 找不到详细证据 | 供审计与 Context Builder 使用 |
| Checkpoint | 可恢复的状态快照 | 无法可靠继续执行 | Runtime 的持久化层 |
| Idempotency | 让重试不重复产生效果 | 崩溃后可能重复副作用 | Tool / 外部服务边界 |
| Context Rehydration | 从持久状态构造模型输入 | 恢复后模型仍不知道下一步 | Checkpoint 下游，LLM 上游 |

**下一章的问题**：现在运行状态能恢复了，但模型窗口只有 32k tokens。System Prompt、工具定义、记忆、摘要、最近消息、当前代码证据都想占用预算。到底该怎样有依据地分配它们？
