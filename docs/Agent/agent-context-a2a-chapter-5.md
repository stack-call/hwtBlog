---
title: 第五章：从 Subagent 到 A2A——当 Agent 不在同一个 Harness 中
sidebar_label: 第五章：A2A
description: 通过优惠券退款 Coding Agent 的连续工程故事，理解 A2A 与 Subagent、Context、Task、Artifact 的关系
tags: [Agent, A2A, Context Engineering]
sidebar_position: 6
---

# 第五章：从 Subagent 到 A2A——当 Agent 不在同一个 Harness 中

> 本章接续第四章「Subagent 的上下文隔离与任务交接」。重点不在背协议字段，而在搞清楚：**内部委派为什么不够用？跨系统委派需要交换什么信息？A2A 又为什么不能代替 Context Manager？**
>
> 规范依据：[A2A v1.0.1 官方协议](https://a2a-protocol.org/v1.0.1/specification/)。代码以官方 [a2a-python](https://github.com/a2aproject/a2a-python) SDK 为参照。标注为「教学示意」的代码并非原仓库逐字摘录。

## 1. 一个新的工程要求

优惠券退款 Bug 已经接近修好。主 Coding Agent 可以调用内部 Subagent 检查代码、执行测试，并把结果汇总到自己的上下文中。

现在出现新要求：公司的风控团队已经部署了一套 **Refund Audit Agent**，专门检查退款规则。它运行在远程服务上，使用不同的框架，有自己的工具、记忆和安全权限。

小李希望委派一项工作：

> 检查 `refund.py` 的计算规则是否违反「退款基于实际支付金额」的约束。请返回发现的问题、证据和建议，不要直接修改仓库。

以前的内部委派只是调用 `subagent.invoke(task)`。如今问题不同了：

- 主 Agent 怎样发现远程 Agent 的能力和地址？
- 任务可能运行很久，怎么得到进度并在中途补充信息？
- 双方框架不同，怎样交换结果而不暴露内部完整 History？
- 审计报告和聊天消息应该如何区分？

这时才需要引入 **A2A（Agent2Agent）**：它是一套独立 Agent 系统之间进行发现、通信、任务协作与结果交付的开放协议。它**不是** Agent Harness，也**不负责**管理 LLM 的上下文窗口。

## 2. 从内部 Subagent 到远程 Agent

内部 Subagent 的边界通常是同一个 Harness 提供的 `task()` / `spawn()` / `invoke()`。父 Agent 可以依赖框架约定共享状态、工具或文件系统。

但远程 Agent 并不一定信任调用方，也不一定使用相同的数据类型、Session 或工具实现。此时需要一个稳定的跨系统接口：

```text
Coding Agent
  ├── 自身：History / Context Builder / Tools
  │
  └── A2A Client
        │
        │ 标准协议：发现能力、发送任务、跟踪状态、领取结果
        ▼
      A2A Server
        │
        └── Refund Audit Agent
              └── 自己的 Harness / History / Context Builder / Tools
```

**设计原因：** 跨系统协作最不应该耦合的，恰好是每个 Agent 的内部上下文表示。A2A 允许对方只暴露能力与交互结果，而不公开内部 Prompt、私有记忆或工具执行轨迹。

## 3. 第一次尝试：直接 HTTP 调用，不就够了吗？

小李写了一个接口：`POST /audit {"task": "检查退款"}`，得到 `{"result": "..."}`。

简单任务可以工作。但第一个审计用了八分钟，需要安全团队授权，而且中途发现缺少优惠券分摊规则。普通同步请求开始变得笨重。

于是小李意识到：**这里应该管理的是「任务」，而不只是一次问答。**

A2A 通过几种核心对象解决这件事：

| 对象 | 在这个故事里的含义 | 解决的问题 |
| --- | --- | --- |
| `AgentCard` | 退款审计 Agent 的能力、接口和认证声明 | 我应该把任务发给谁？ |
| `Message` | “请审计退款公式，并遵守这些业务约束” | 怎么交换意图和补充信息？ |
| `Task` | 一次退款审计工作及其状态 | 长任务怎么跟踪、取消或继续？ |
| `Artifact` | 最终的审计报告、证据或结构化数据 | 结果如何交付，而不是混在聊天里？ |
| `contextId` | 一组相关任务与消息的关联标识 | 多轮任务如何保持业务连续性？ |
| `taskId` | 具体一个远程任务的标识 | 如何继续和查询同一工作？ |

一个典型过程是：

```text
主 Agent              A2A Server / Refund Agent
   |                            |
   |---- 读取 AgentCard ------->|
   |<--- 能力和接口声明 ---------|
   |                            |
   |---- SendMessage ---------->|
   |<--- Task(id, contextId) ---|
   |                            | 开始审计
   |<--- 状态 working ----------|
   |<--- 状态 input-required ---| 缺少分摊规则
   |---- 补充规则（同 taskId）--->|
   |<--- Artifact: 审计报告 -----|
   |<--- 状态 completed --------|
```

任务更新可通过轮询、流式事件或推送通知获取，取决于服务端声明与实际支持的能力；A2A 并不要求每个 Agent 都支持所有方式。

## 4. 一个特别容易误解的字段：`contextId`

小李看到 `contextId`，以为它就是 DeepAgents / Codex 的 LLM Context。

**不是。**

A2A 的 `contextId` 是相关任务和消息的**关联标识符**。它不是 Token 列表，也不是历史摘要，更不是 KV Cache。远程 Agent *可以* 用它检索自己的会话状态，*也可以*采用另一种实现。协议并不规定要如何构造 LLM Prompt。

例如：

```text
contextId = refund-audit-session-7
  ├── taskId = audit-001  检查退款规则
  └── taskId = audit-002  复核分摊公式
```

这两个 Task 可以在业务上关联，但不意味着第二个任务自动获得第一个任务的全部模型消息。

而 `taskId` 是某个具体任务的 ID，用于查询状态、取消或继续该任务。新 Task 的 ID 由服务端生成；客户端不应自行编造远程新任务的 ID。

还有一个细节：A2A 的 `Task.history` 并不保证保存所有内部执行事件，甚至部分交互消息也可能不在其中。因此**不能把 A2A Task History 视作远程 Agent 的完整 Event Log**。

## 5. 第二次故障：父 Agent 把 200 轮 History 全发过去

因为担心丢信息，小李把 Coding Agent 的全部 200 轮消息都打包给远程审计 Agent。

结果出现三个问题：消息巨大、泄露了不必要的内部实现和日志、远程 Agent 仍然不清楚这次审计的验收标准。

问题的根源不是 A2A 传输不可靠，而是**交接内容没有被设计**。

回到第四章的任务交接原则，父 Agent 应组装一份有边界的 Handoff：

```json
{
  "objective": "审计退款折扣分摊规则，不修改代码",
  "constraints": ["退款基于实际支付金额", "金额按约定规则舍入"],
  "evidence": ["refund.py:42", "tests/test_refund.py::test_partial_refund"],
  "expected_output": ["问题列表", "证据位置", "修改建议"]
}
```

这段 JSON 是**我们设计的业务交接内容**，不是 A2A 强制规定的顶层数据结构。实际发送时，业务内容需编码进 A2A `Message.parts`，或使用双方约定的扩展字段/数据格式。

主 Agent 不需要把完整 History 发送出去；远程 Agent 也不应自动获得超出授权范围的文件。必要时可以传带权限控制的 Artifact 引用；这些引用必须是远程可访问的，不能假定主机本地路径在远程服务器同样有效。

**A2A 标准化「怎样交接」；Context Engineering 决定「交接什么」。**

## 6. 源码对照：AgentExecutor 是协议与 Harness 之间的桥

官方 Python SDK 的核心接口位于：

[`src/a2a/server/agent_execution/agent_executor.py`](https://github.com/a2aproject/a2a-python/blob/main/src/a2a/server/agent_execution/agent_executor.py)

其关键接口是：

```python
class AgentExecutor(ABC):
    @abstractmethod
    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        ...

    @abstractmethod
    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        ...
```

这段接口本身就揭示了一层架构边界：

1. `RequestContext` 提供远程请求、消息和任务相关信息；
2. `AgentExecutor` 将协议请求映射为具体 Agent 的内部执行逻辑；
3. `EventQueue` 把任务状态与结果发布回 A2A 层。

**真实调用链的核心形态**是：

```text
A2A 请求处理器
    -> AgentExecutor.execute(...)
       -> 内部 Agent / Harness 执行
       -> TaskUpdater.add_artifact(...)
       -> TaskUpdater.update_status(completed)
    -> A2A 客户端获得进度和结果
```

`TaskUpdater` 的调用方式可在官方 [Agent Executor 教程](https://a2a-protocol.org/latest/tutorials/python/4-agent-executor/) 看到。这里没有粘贴完整 Server 代码，因为我们要研究的是 **A2A 到 Agent Runtime 的适配边界**，而不是 SDK 初始化细节。

## 7. 第三次故障：Agent 说“审计完成”，但报告在哪里？

小李把结果当作普通聊天 `Message` 返回，主 Agent 得到“已完成审计”的文字，却没有拿到具体证据。

A2A 将沟通和交付分开：

- `Message` 更适合请求、澄清、互动；
- `Task.status` 表示当前执行状态；
- `Artifact` 承载任务形成的报告、文件引用或结构化输出。

远程审计 Agent 可以交回一个命名 Artifact，例如 `refund-audit-report`，内容包含失败测试、涉及的业务规则和证据引用。

对于长期任务，主 Agent 应当根据 `Task` 状态继续等待、补充输入或处理失败，而不是收到第一条文字消息就认定执行结束。已完成 Task 的后续新工作应按规范创建新 Task，并用 `contextId` 或任务引用维持关联。

## 8. 第四次故障：有了 contextId，就能跨用户共享会话吗？

不能。`contextId` 只是关联标识，不是访问凭证。

如果主 Agent 把内部用户的账单、生产 Token 或绝对文件路径直接发送给一个远程服务，即使 A2A 消息格式合法，也可能产生权限和数据泄露问题。

真正的跨系统协作需要：对远程 Agent 身份进行验证；对要发送的文件和数据做最小授权；验证 Artifact 的来源；将业务用户、租户与任务访问权限分开管理。尤其不能把远程发来的文本直接当作高优先级系统指令。

A2A 的 Agent Card 也应被视为需要验证的外部数据。官方 SDK 提供了 [Agent Card 签名与验证示例](https://a2aproject.github.io/a2a-python/samples/)。

## 9. A2A、Subagent、MCP、Context Manager 怎么放在一张图里？

```text
                   Coding Agent Harness
                ┌────────────────────────┐
                │ History / Memory       │
                │ Context Builder        │
                │ Agent Loop             │
                └───────┬────────┬───────┘
                        │        │
                 内部 Subagent   A2A Client ──网络── A2A Server
                        │                         │
                 同一框架委派                 远程 Agent Harness
                                                  │
                                             MCP / Tools
```

| 技术 | 主要管理的边界 | 是否负责 LLM 上下文压缩 |
| --- | --- | --- |
| 内部 Subagent 机制 | 同一系统里的任务委派与执行状态 | 不必然；由 Harness 决定 |
| A2A | 不同 Agent 系统之间的通信、任务、产物 | **不负责** |
| MCP | Agent 与工具、数据资源之间的标准交互 | **不负责** |
| Context Manager | 模型输入的选择、裁剪、压缩、检索 | **负责** |

A2A 官方明确将自己定位为独立 Agent 系统间的通信标准，而不是取代 MCP 或框架内的 Subagent API。参考：[A2A 官方介绍](https://a2a-protocol.org/latest/) 与 [A2A and MCP](https://a2a-protocol.org/latest/topics/a2a-and-mcp/)。

## 10. 最后用工程验收来检验设计

给主 Agent 一项完整任务：委派远程退款审计，必要时补充业务规则，取回证据报告，并在自己的 Context 中汇总。合格的实现应能做到：

1. 根据可信 Agent Card 找到有能力的远程 Agent。
2. 发送必要的业务约束，不泄漏完整历史和额外敏感数据。
3. 使用远程返回的 `taskId` 跟踪任务，通过 `contextId` 维持相关交互。
4. 遇到 `input-required` 能补充信息，遇到错误能向主 Agent 汇报。
5. 以 Artifact 形式收取实际审计报告，并将**摘要和可回读的证据引用**加入主 Agent 的 Working Context。
6. 验证远程服务与 Artifact 来源，不把对端文字升级为系统指令。

### 本章归位

> **Subagent 是一个执行委派机制；A2A 是跨 Agent 系统的协作协议；Context Manager 才负责决定模型看什么。**
>
> 它们可以组合使用，但不能互相替代。A2A 提供任务通信的协议骨架，而可靠的跨 Agent 上下文交接依然需要你设计 Handoff 内容、权限和恢复策略。

### 源码阅读索引

- [A2A v1.0.1 协议](https://a2a-protocol.org/v1.0.1/specification/)
- [a2a-python：AgentExecutor](https://github.com/a2aproject/a2a-python/blob/main/src/a2a/server/agent_execution/agent_executor.py)
- [a2a-python：TaskUpdater](https://github.com/a2aproject/a2a-python/blob/main/src/a2a/server/tasks/task_updater.py)
- [官方 Python 教程：Agent Executor](https://a2a-protocol.org/latest/tutorials/python/4-agent-executor/)
- [官方 Task 生命周期](https://a2a-protocol.org/latest/topics/life-of-a-task/)

**下一章的问题**：跨系统交接靠协议，那么一个长任务进程崩溃之后，历史还在，为什么仍然接不上？
