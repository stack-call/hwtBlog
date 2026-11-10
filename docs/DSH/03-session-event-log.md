---
title: "DSH 源码精读（三）：为什么不直接保存 messages 数组？"
sidebar_label: "三、为什么不直接保存 messages 数组？"
sidebar_position: 4
slug: dsh-source-03-session-event-log
description: "从一次读取工具的结果和一次可能中断的执行出发，逐步读懂 DSH 的 Session Event Log 如何记录事实，又如何派生模型消息。"
tags: [DeepSeek-Harness, Agent, 源码精读]
---

上一章，我们跟着仓库侦察助手发起了一次文件读取：模型请求 `read` 读取 `package.json`，Runtime 检查请求，工具返回文件内容。下一步，模型才能根据结果回答“这个项目怎么启动”。

这里藏着一个没有回答的问题：**工具已经返回内容了，为什么还要把它写进 Session？直接追加到 `messages` 数组，不就能继续调用模型了吗？**

在只有一轮成功请求时，确实可以。DSH 的设计价值，恰恰要等到我们遇到一个普通消息列表难以解释的运行状态时，才会显现。

<!-- truncate -->

> **源码约定**：本文统一采用 [`deepseek-ai/deepseek-harness@5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3aa172535a7967b06ff95d9cbfe536a)。`真实源码节选` 保留该版本的核心语义；带有“教学示意”的记录是为了帮助观察数据变化，并非 DSH 实际运行输出。本次未在本地启动 DSH。

## 1. 先承认：只有成功路径时，messages 数组完全够用

先不看任何 DSH 源码。想象我们的助手只有下面这段历史：

```text
用户：这个仓库怎么启动？
模型：调用 read，读取 package.json
工具：返回 scripts.dev = "vite"
模型：使用 npm run dev 启动开发服务
```

最简单的实现，就是顺序保留四条消息。第二次请求模型时，传入前面三条即可；用户继续追问时，再把第四条也带上。

所以，**不能说 messages 数组天生错误**。如果唯一目标是让模型记住已经发生的对话，它就是一种合理方案。

但假设在模型请求读文件之后，用户点了“取消”；或者 Runtime 已把一次工具调用交给调度流程，但在结果写回前被中断。这时用户打开历史记录，可能会问：

> “刚才的文件读取到底执行了吗？还是模型只提出了读取请求？”

消息中出现“模型想调用工具”，不等于 Runtime 已经开始执行那个工具。对于只读文件，这个差异不一定严重；可一旦换成有外部副作用的命令，它就很重要了。

此时我们缺少的不是更多聊天文本，而是**能区分模型意图和运行时事实的记录**。

## 2. 第一次认识 Session：把发生的事依次记下来

DSH 把这类记录放在 `Session` 中。先不把它想成数据库，而把它想成一本只能在末尾添加新页的执行日志。

例如，一次正常读取在日志中**可能涉及**以下事件（教学示意，只保留本章需要的事件种类，省略系统消息、请求配置和具体编号）：

```text
user/message       用户提出问题
assistant/message  模型提出 read 工具请求
 tool/call          Runtime 记录一次工具调用已开始
 tool/result        Runtime 记录工具执行结果
assistant/message  模型给出最终回答
```

这里最值得注意的是相邻的两条：

- `assistant/message` 记录模型说了什么，包括模型要求调用哪个工具。
- `tool/call` 记录 Runtime 为那次工具调用建立的执行记录；它不等于底层工具函数已经真正运行。

**模型提出请求**和**Runtime 记录工具调用**，从此不再混为一件事。随后 `tool/result` 又告诉我们，工具结果（也可能是拒绝或取消等错误反馈）有没有被记下来。

它们拥有不同的事件类型，不是三段名字相似的文本。

### 到源码中确认 `tool/call` 从哪里来

打开 [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/tool-calls.ts#L244-L270)。上一章讨论的工具调度器，会通过以下辅助函数提交记录。

**真实源码节选：**

```ts
function appendToolCall(session: Session, turn: number, step: number, block: ToolCallBlock): SessionSeq {
  const event = session.append('tool/call', { turn, step, callId: block.id, name: block.name, arguments: block.arguments })
  return event.seq
}
```

先只看两处：`session.append('tool/call', ...)` 表示向 Session 追加“工具调用”事件；`event.seq` 是它在本次 Session 中的事件序号，供后面的结果关联。

工具结束时，另一个函数 `appendToolResult()` 组装工具结果消息，并追加 `tool/result`：

**真实源码关键两行（从同文件函数中节选，省略中间字段）：**

```ts
session.append('tool/result', {
  turn, step,
  message,
  // ...省略工具错误信息及展示数据
}, { surfaceOp: 'append', sourceEventSeqs: [callSeq] })
```

`sourceEventSeqs: [callSeq]` 把这条结果与前面已经记录的那次调用关联起来。这样读取记录时，不需要靠“上一条消息大概就是这次工具调用”来猜。

不过，这些 `appendTool...()` 函数只是写入的调用者；真正接收事件的是 [`Session.append()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/index.ts#L667-L706)。它从当前日志长度生成递增的 `seq`，验证并冻结事件，然后才将事件放入日志。下面只摘出两处关键语句：

```ts
seq: SessionSeq(this.log.length),
// ……先构造、验证事件
this.log.push(event as SessionEvent)
```

**真实源码关键语句，省略了两者之间的验证与组装步骤。** 这里的 `seq` 是记录位置，不是工具调用次数；它让不同事件能够有明确顺序，也让工具结果能指回先前的调用记录。

至此，Session 首先解决了一个具体问题：**把执行过程拆成可以辨认、可以关联的事实。**

不过新问题来了：模型并不需要知道“第几个事件写入日志”之类的实现细节。它只需要用户说了什么、自己请求了什么、工具返回了什么。

那我们把整本日志发给模型，是否反而增加了无用信息？

## 3. 模型需要的历史，不等于完整执行历史

把刚才那几条事件并排放置：

| Session 记录的事件 | 下一次模型请求需要什么？ |
| --- | --- |
| `user/message` | 用户的消息内容 |
| `assistant/message` | 模型此前的回复，包括其中的 Tool Call |
| `tool/call` | **不单独生成一条模型消息** |
| `tool/result` | 工具返回内容或错误反馈 |
| `turn/start`、`step/end` 等 | **不单独生成模型消息** |

这就解释了一个初看有些奇怪的设计：**日志记录的东西比模型看到的更多。**

注意，不是说 `tool/call` 这个事实对模型毫无用处，而是它的“工具调用请求”已经包含在 `assistant/message` 中。`tool/call` 作为单独的事件，是给 Runtime 和历史分析使用的执行记录，不需要再重复注入一次模型上下文。

DSH 为从完整事件中得到模型历史提供了一个方法：`deriveMessages()`。所谓 *derive*，这里先理解为“根据已记录的事件，算出当前模型应该看到的消息列表”。

### 第一处源码：哪些事件可以变成模型消息？

看 [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/surface.ts#L20-L25) 中的事件种类判断。

**真实源码节选：**

```ts
const SURFACE_EVENT_TYPES = new Set<string>([
  'system/message',
  'user/message',
  'assistant/message',
  'tool/result',
])
```

在这个固定版本中，只有这些类型有资格进入“模型可见的消息序列”。你会发现：**这里没有 `tool/call`，也没有 `turn/start`。**

这里的“模型可见序列”在源码里叫 *surface*。暂时把它理解成“从完整日志中挑出的、按顺序组织的消息来源”就够了；它更复杂的替换规则，等到研究上下文压缩时再介绍。

实际把单个事件变成消息的函数在同一个文件中，名叫 `deriveEventMessage()`。它对普通 `user/message` 返回用户消息，对 `tool/result` 返回其中的结果消息；不属于模型历史的事件则返回 `null`。具体见 [这段分支](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/surface.ts#L85-L119)。

### 第二处源码：消息列表是怎样生成的？

回到 [`packages/core/session/src/index.ts` 的 `Session.deriveMessages()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/index.ts#L782-L803)。我们现在只关注它读取已选择的事件并形成消息的部分。

**真实源码节选（摘出连续的核心片段）：**

```ts
for (const seq of nodes.slice(this.derivedNodes)) {
  const msg = this.deriveEventMessage(this.log[seq]!)
  if (msg) this.derived.push(msg)
}
this.derivedNodes = nodes.length
return [...this.derived]
```

从第一行到最后一行，发生了三件事：

1. `nodes` 指向当前有资格成为模型消息的事件序号；没有必要扫描所有无关的执行事件。
2. 对每个事件调用 `deriveEventMessage()`；能生成消息就加入结果。
3. 返回一个新的消息数组，供接下来调用模型；之前生成的结果还会缓存，避免每次都重新推导整段历史。

此处并不需要先掌握缓存字段的完整实现。关键是：**Session 保存的是事实，`deriveMessages()` 返回的是某个消费者——模型——需要的视图。**

## 4. 谁在下一次模型请求前调用 `deriveMessages()`？

如果 `deriveMessages()` 只是一个孤立的工具函数，这套设计还不能算讲通。我们必须找到谁真正用它。

回到第一章已经认识过的 `ReactLoopAgent`。它准备下一次模型请求时，在 [`buildRequest()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L525-L589) 里调用：

**真实源码关键语句：**

```ts
const boundaryMessages = session.deriveMessages()
```

在随后创建模型请求对象时，`boundaryMessages` 被放入 `messages` 字段。这个结果才被交给模型适配器，而不是把 `Session` 对象或整条日志直接送进去。

把本章已经读过的源码连起来，就是：

```text
工具执行结束
    ↓
appendToolResult() 写入 tool/result
    ↓
Session 保存事件
    ↓
下一次 buildRequest() 调用 deriveMessages()
    ↓
生成模型需要的 messages
    ↓
模型看到工具结果并继续回答
```

这时我们终于回答了开头的问题：**工具结果不是不能直接放进 messages；而是 DSH 选择让“下一次模型该看到什么”由已记录的执行事实统一推导，避免另存一份随时可能与执行记录不一致的可变历史。**

## 5. 再回到那个中断问题：多记一个事件到底有什么价值？

现在想象下面两种情况。

**情况 A：调度器明确跳过了某次工具调用。** 例如取消流程发现后续调用还没派发。在这一固定版本中，`appendSkippedToolCall()` 仍会补记一个 `tool/call`，再补记带有 `TOOL_ABORTED_BEFORE_DISPATCH` 错误信息的 `tool/result`。单看“有没有 `tool/call`”因此**不能**断定工具实际执行过。

**情况 B：调度器已经记录 `tool/call`，却因为内部故障没有记录对应 `tool/result`。** 这时日志保留着“调用已被纳入启动流程”的事实，但尚无可靠的结果记录。

两种情况都可能让用户看到“模型想读取文件”，但日志提供了不同的证据：A 有明确的“未派发”错误结果，B 是缺少结果的未决状态。对应逻辑可在 [工具调度器的取消分支](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/tool-calls.ts#L222-L242) 看到。

这也是为什么日志的独立事件值得存在：恢复逻辑可以依据记录区分“明确没派发”和“结果未知”，而不是只凭模型请求猜测。**但它不能证明工具没有产生副作用**：对于已经可能触及外部世界、尚未记录结果的操作，外部状态可能已经变化。

因此，“有事件日志”与“恢复执行一定安全”是两个不同的承诺。本章仅证明事件为判断提供了更可靠的依据，不讨论完整的崩溃恢复协议。

此外也要明确一个边界：**`Session.append()` 首先提交到内存日志，不等于已经写入磁盘。** 要跨进程保留记录，还需要配置持久化后端及相应的 flush 机制。DSH 的 [Session README](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/README.md) 明确区分这两层。

## 6. 一次不需要启动模型的源码核对

这次我们不再突然补一段 Fake Model 或额外 Python 框架。你可以直接在固定 Commit 中检查这四个事实：

1. [`tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/tool-calls.ts#L244-L270) 是否分别写入 `tool/call` 与 `tool/result`，并用序号关联它们？
2. [`session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/index.ts#L667-L706) 的 `append()` 是否为事件生成序号并将事件加入日志？
3. [`session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/surface.ts#L20-L25) 的模型消息来源里，是否确实排除了独立的 `tool/call`？
4. [`agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L525-L589) 的 `buildRequest()` 是否真正使用 `deriveMessages()` 的返回值？

这四个位置连起来，足以完成本章的**静态源码验证**。本文未运行 DSH 端到端用例，也没有把前面的教学示意伪装成实测日志。

## 7. 回看：这次究竟多了哪个机制？

最开始，我们只需要保存对话：

```text
messages → 下一次模型请求
```

但当我们需要区分模型想做什么、Runtime 实际开始了什么、工具最后留下了什么结果时，普通聊天历史就不再是唯一要记录的数据。DSH 因而选择：

```text
执行过程中发生的事实
        ↓
   Session 事件日志
        ↓
   deriveMessages()
        ↓
  下一次模型请求的 messages
```

这个选择解决的是**执行事实与模型视图分离**的问题。它并不保证文件系统安全，也不自动提供磁盘持久化或外部副作用的精确一次执行。

现在我们认识了三个实实在在的职责：Agent Loop 驱动执行，工具边界决定是否与如何执行，Session 记录发生过的事并推导下一次模型输入。

下一篇再讨论：**如果想在不修改这些核心代码的情况下增加审计、策略或其他能力，DSH 怎样让功能被注册、组合，并在卸载时真正撤销？** 到那时，再正式引入 Cordis 的插件生命周期，就有了必要的前提。
