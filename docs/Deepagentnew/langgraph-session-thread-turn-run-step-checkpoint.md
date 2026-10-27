---
title: LangGraph 中的 Session、Thread、Turn、Run、Step 与 Checkpoint
description: 用退款 Coding Agent 案例理解 LangGraph 的交互层级、图执行步骤与状态持久化边界。
tags: [LangGraph, DeepAgents, Agent, Checkpointer, State, Runtime]
sidebar_position: 17
slug: /langgraph/session-thread-turn-run-step-checkpoint
---

# LangGraph 中的 Session、Thread、Turn、Run、Step 与 Checkpoint

在阅读 LangGraph 的 Checkpointer 和 Runtime 源码时，我们经常遇到 `Session`、`Thread`、`Turn`、`Run`、`Step` 和 `Checkpoint`。它们看起来都在描述“一次执行”，但实际上关注的是**不同层次的边界**。

先记住最重要的一句话：

> **Turn 描述一次用户交互；Thread 关联持续保存的 Graph 状态；Step（更准确地说，Superstep）描述 Graph 内部的一轮调度；Checkpoint 保存某个执行边界的可恢复状态。**

## 一、从一个 Coding Agent 任务开始

继续使用贯穿本系列的任务：

> 用户要求 Agent 修复 `refund.py` 中的优惠券部分退款精度 Bug，并运行 `pytest`。

用户只提出了一次请求，但 Agent 可能需要执行多轮模型和工具调用：

```text
用户：修复 refund.py 并运行测试
         |
         v
       Model
         |
         v
   Tool: read_file
         |
         v
       Model
         |
         v
   Tool: edit_file
         |
         v
       Model
         |
         v
    Tool: pytest
         |
         v
       Model
         |
         v
   回复用户：修复结果
```

这条流程说明：**一次用户交互并不等于一次模型调用，也不等于一个 Graph Step。**

## 二、六个概念分别指什么？

| 概念 | 一句话理解 | 典型标识或边界 |
| --- | --- | --- |
| **Session** | 应用层面的一段持续交互 | 应用自定义的会话 ID |
| **Thread** | LangGraph 持久化状态的逻辑分组 | `thread_id` |
| **Turn** | 一次用户请求及对应的完整响应过程 | 用户发起一次新请求 |
| **Run** | 一次执行调用；在追踪系统中也可以是一次子操作 | 执行 ID / `run_id`（语境相关） |
| **Step / Superstep** | Graph 内部的一轮调度推进 | 运行时步骤编号 |
| **Checkpoint** | 在执行边界保存的可恢复状态 | `checkpoint_id` |

### 2.1 Session：一次持续交互

例如用户先要求修复问题，之后又要求补充边界测试。这些交互可以属于同一个 Session。

但要注意：**Session 不是 LangGraph Checkpointer 必须配置的标准字段**。它一般由业务应用自行定义，一个 Session 可以对应一个 Thread，也可以管理多个 Thread。

### 2.2 Thread：让多次执行接续同一份状态

LangGraph 使用 `thread_id` 定位同一条执行线程的持久化历史：

```python
config = {
    "configurable": {
        "thread_id": "refund-001"
    }
}
```

例如：

```python
# 第一次调用：修复问题
graph.invoke(first_input, config)

# 第二次调用：继续检查测试结果
graph.invoke(second_input, config)
```

前提是 Graph 已配置 Checkpointer，并且输入与 State 定义符合要求。这样第二次调用可以利用同一 Thread 中已经保存的状态。

```text
Thread: refund-001
    |
    +-- 第一次执行
    |     +-- Checkpoint A
    |     +-- Checkpoint B
    |     +-- Checkpoint C
    |
    +-- 第二次执行
          +-- Checkpoint D
          +-- Checkpoint E
```

**Thread 不是一次 Run。一个 Thread 可以包含多次 Run 和多个 Checkpoint。**

### 2.3 Turn：用户的一轮交互

如果用户说：“修复退款 Bug 并运行测试”，Agent 内部即便调用了四次模型、三次工具，通常仍然是**一个 Turn**。

等用户继续问：“测试真的通过了吗？”才进入下一个 Turn。

Turn 是交互层概念；LangGraph 并不会天然为每一条用户消息生成一个专门叫作 `turn_id` 的 Checkpointer 字段。

### 2.4 Run：一次执行，但要看使用语境

在 Graph 调用语境中，可以将一次 `graph.invoke(...)` 理解为一次 Graph 执行。

不过在 LangSmith Tracing 中，`Run` 还可能表示一次模型调用、工具调用或其他子操作：

```text
Agent Run
   |
   +-- Model Run
   +-- Tool Run: read_file
   +-- Model Run
   +-- Tool Run: edit_file
```

因此，看到 `run_id` 时，必须确认它指的是**整次 Graph 执行**还是**追踪树里的某个子操作**。

## 三、Step 为什么不是 Tool Call？

LangGraph 以 **Superstep** 为单位推进图计算。

最简单的串行图可以这样理解：

```text
Superstep 1: Model
      |
      v
Superstep 2: Tools
      |
      v
Superstep 3: Model
```

但一个 Superstep 可以包含多个能够并行执行的 Graph 节点。另一方面，一个 `ToolNode` 内也可能处理多个工具请求。所以不要机械地认为：

- 一个 Step = 一条 Message；
- 一个 Step = 一个 Tool Call；
- 一个 Step = 一次模型调用。

**Step 描述的是 Graph 调度阶段，而不是业务动作数量。**

`metadata.step` 是 LangGraph 记录的执行步骤信息，不能当成第几个用户 Turn。它的实际数值也不应按上述示意图硬编码推断。

## 四、Checkpoint 与 Step 的关系

在正常的逐步持久化执行中，LangGraph 通常会在 Superstep 边界创建 Checkpoint：

```text
       Model
         |
         v
   Checkpoint A
         |
         v
       Tools
         |
         v
   Checkpoint B
         |
         v
       Model
         |
         v
   Checkpoint C
```

图为概念示意。具体持久化时机受运行配置、执行模式和失败情况影响。

Checkpoint 不只是 `messages` 的副本。底层持久化机制还包括：

- **通道状态**：例如 `messages`、`todos`、`files` 等 State 字段对应的值或可重建数据。
- **通道版本**：例如 `channel_versions`，表示各通道更新进度。
- **节点已见版本**：例如 `versions_seen`，帮助 Runtime 判断调度状态。
- **历史与元数据**：包括 Checkpoint ID、步骤信息、父 Checkpoint 等。
- **待提交写入**：部分情况下保存已经完成节点的 Pending Writes，以支持失败恢复。

因此，**Checkpoint 既保存数据状态，也保存继续调度所需的信息**。

## 五、用一张图串起来

```text
Session：退款 Bug 修复会话（应用层）
|
+-- Thread: refund-001（持久化状态的归属）
    |
    +-- Turn 1：修复退款问题
    |   |
    |   +-- Graph Run A
    |       |
    |       +-- Superstep：Model
    |       +-- Checkpoint A
    |       +-- Superstep：Tools / read_file
    |       +-- Checkpoint B
    |       +-- Superstep：Model
    |       +-- ...
    |
    +-- Turn 2：重新检查测试
        |
        +-- Graph Run B
            |
            +-- Superstep：Model
            +-- Checkpoint C
            +-- Superstep：Tools / pytest
            +-- Checkpoint D
```

这张图只是帮助建立概念的**常见对应关系**，并不是强制的一对一映射：例如一个 Turn 可能因为人工审批而跨越多次 Run；应用也可能将多个 Thread 归入同一个 Session。

## 六、源码阅读时应该重点看什么？

如果你正在分析 Checkpointer，不必先钻入所有业务概念。按下面的顺序阅读更直接：

1. **`thread_id`**：当前状态历史属于哪一条 Thread？
2. **Superstep**：Runtime 完成了哪一轮节点调度？
3. **Checkpoint**：在这个边界保存了哪些 State、版本和调度信息？
4. **恢复执行**：下次调用怎样加载已保存的数据并继续推进？

可以从这些接口入手：

```python
# 查看当前 Thread 的可见状态
snapshot = graph.get_state(config)

# 查看 Checkpoint 历史
history = list(graph.get_state_history(config))

# 查看底层保存记录（以支持该接口的 Saver 为例）
record = checkpointer.get_tuple(config)
```

其中：

- `snapshot.values` 表示当前可见 State；
- `snapshot.next` 表示下一步待执行节点；
- `snapshot.metadata` 提供执行元数据；
- `record.checkpoint`、`record.pending_writes` 可用于进一步研究底层持久化结构。

## 七、最后总结

可以用六个问题记住这些概念：

| 你想问的问题 | 对应概念 |
| --- | --- |
| 这是哪段用户会话？ | Session |
| 这次执行接续哪份状态？ | Thread |
| 用户这是第几次提出请求？ | Turn |
| 现在执行哪一次调用？ | Run |
| Graph 推进到哪轮调度？ | Step / Superstep |
| 哪个执行边界的状态被保存了？ | Checkpoint |

理解 LangGraph Checkpointer 时，最核心的一条线是：

> **Thread 决定状态归属，Superstep 决定执行推进，Checkpoint 记录可恢复的执行边界。**

这三个概念结合起来，就能解释 Agent 为什么可以在同一个 `thread_id` 下连续处理用户任务，以及为什么 Graph 能从中断处恢复，而不只是重新读取一段聊天记录。

## 延伸阅读

- [LangGraph Persistence（官方文档）](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Graph API（官方文档）](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangGraph Checkpoint 源码](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint/langgraph/checkpoint)
