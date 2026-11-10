---
title: "DSH 源码精读（八）：Agent 中途停下后，到底能恢复什么？"
sidebar_label: "八、中途停下后到底能恢复什么"
sidebar_position: 9
slug: dsh-source-08-interruption-and-recovery
description: "从一次读取工具的中断出发，理解 DeepSeek Harness 如何区分主动取消、执行错误与进程崩溃，以及 Session 日志能恢复什么、不能恢复什么。"
tags: [DeepSeek-Harness, Agent, 源码精读]
---

前面七篇，我们让“仓库侦察助手”从一个简单的模型—工具循环，逐渐有了会话记录、工具执行边界、可卸载的插件、Agent 专属能力，以及可以组合的运行环境。

但还有一个没有回答的问题：**如果助手正要读取 `package.json`，用户点了“停止”，或者程序突然崩溃，下一次打开时应该看到什么？**

这个问题看起来像“继续执行”，实际上隐藏着三种不同的情况：有人主动叫停；某个步骤确实报错；程序消失得太突然，根本来不及记录结局。它们不能共享一个含糊的“失败后重试”方案。

本章只研究这条执行链的收尾与重建。我们不从 Checkpoint、恢复协议或异常类型定义开始，而是先让最简单的方案遇到真正的问题。

<!-- truncate -->

> [!NOTE] 源码基线与验证范围
> 仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
>
> 固定 Commit：[`5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3aa172535a7967b06ff95d9cbfe536a)。本文仅引用这个版本的实现，不与 `master` 混用。
>
> 标记为“真实源码节选”的代码已与上述 Commit 核对；“教学事件示意”只用于解释事件之间的关系，**不是运行 DSH 得到的日志**。本文没有进行进程崩溃的端到端实测。

## 1. 最直觉的做法：再运行一遍，不就行了吗？

还是这个任务：

> 帮我看看仓库怎么启动。可以读取文件，不要执行命令，也不要修改项目。

模型发起了一个 `read` 请求，目标是 `package.json`。假设刚开始读取，程序便退出了。

这时最容易想到的办法是：重启 Agent，再把用户的原问题发一次。对于读取文件，这往往没有严重后果。可是“往往可以”并不能成为 Runtime 的通用恢复规则。

因为 Runtime 面对的并不总是读取操作。假设未来另一个 Agent 调用了“发布版本”工具：它刚把发布请求发出去，进程就崩溃，结果没来得及返回。重启后再次发布，可能产生两次外部副作用。

于是我们先得到一个很小、但重要的要求：

**恢复时，必须区分“已经知道发生了什么”和“只知道曾经尝试过”。不能仅凭缺失的结果，就认定工具没有执行。**

第三篇已经知道，DSH 使用 Session Event Log 保存执行事实。现在我们要进一步看：这些事实在异常退出时到底能留下多少，以及留下不完整的事实之后怎么办。

## 2. 先区分：用户点击“停止”和程序突然消失

假设用户在模型读取文件时点击停止。程序还活着，可以处理这个请求。最简单的实现，是让执行器持有一个取消信号，在等待模型或工具时检查它。

DSH 的 `ReactLoopAgent` 确实使用这个方向。下面是我们第一次需要看的源码。

**真实源码节选**：`packages/core/agent-loop/src/agent.ts`，`ReactLoopAgent.cancel()`。

```ts
cancel(cause: AgentCancelCause, options: CancelOptions = {}): void {
  if (!options.keepInbox) {
    this.inbox.clear()
    if (this.phase.kind !== 'idle') this.phase.wakeRequested = false
  }
  if (this.phase.kind !== 'idle') this.phase.abort.abort(cause)
}
```

[查看固定版本中的函数](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L137-L143)

不用先理解 `phase` 的全部状态。这里仅看两个动作：

- 默认清掉还没处理的输入，避免用户已点击停止，却又启动下一项待处理任务；`keepInbox` 允许调用方保留这部分输入。
- 对正在运行的工作发出取消信号。模型请求、工具执行等环节能据此停止或尽快退出。

注意第二点是**协作式取消**：发出信号不等于已经撤销外部世界发生的操作。一个不响应取消的底层命令，或者已经写入成功的远程服务，不会因为 `AbortSignal` 而自动回滚。

但只要执行器还活着，便有机会记录“为什么停止”。沿 `turn()` 的异常处理路径，会看到：

**真实源码节选**：`ReactLoopAgent.turn()` 中的取消分支。

```ts
if (signal.aborted) {
  turnEnds = { kind: 'aborted', reason: signal.reason as AgentCancelCause }
  throw error
}
```

随后 `finally` 负责将本轮的结束事实写入 Session，包含 `turn/end` 及其 `reason`。[查看 `turn()` 中的收尾代码](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L248-L330)。

所以，**主动取消**的正常路径不是简单丢弃当前执行，而是试图在退出时留下一个可解释的终点：这轮结束了，结束原因是 `aborted`。

这也解决了一个用户可观察的问题：重新打开历史时，系统可以区分“助手完成了任务”与“用户主动叫停”。

## 3. 如果是工具失败，而不是用户取消呢？

仍然读取 `package.json`。这次不发生崩溃，而是路径不存在或文件系统返回错误。

上一章我们已经看到，工具结果会写成 `tool/result`，它可以携带错误标记 `isError`。因此，**一条工具执行失败的反馈，不必等于整个 Agent 运行过程已经崩溃**。如果工具执行服务正常返回结构化的失败结果，这条反馈可以交给下一次模型请求，让模型解释错误、改查其他文件，或向用户报告无法读取。

但还存在另一种情况：模型请求本身或调度代码抛出了无法在当前步骤处理的错误。此时 `turn()` 会记录 `reason.kind = 'error'`，而不是伪装成成功或主动取消。具体可以在同一段 `turn()` 的 `catch` 中看到 `LlmError` 和普通错误被转成结构化失败信息。

在这里我们只需要学会区分：

| 发生了什么 | 应如何理解 |
|---|---|
| `tool/result` 带错误 | 某次工具执行有失败反馈，后续仍可能继续 |
| `turn/end` 是 `error` | 这一轮执行以错误结束 |
| `turn/end` 是 `aborted` | 这一轮因为取消而结束 |

这三种情况都还有一个共同条件：**运行时仍有机会提交事件**。

接下来换一种更麻烦的失败：不是取消，不是捕获异常，而是整个进程突然没了。

## 4. 真正的难题：程序退出时，连 `turn/end` 都没写下来

考虑下面的**教学事件示意**：

```text
turn/start                 # 用户这轮任务开始
step/start                 # 第一次模型请求开始
assistant/message          # 模型要求 read("package.json")
tool/call                  # DSH 记录了该工具请求
--- 进程在这里突然退出 ---
```

这不是一条完整的 Turn。缺少工具反馈，也缺少 `step/end` 和 `turn/end`。

如果我们的恢复代码简单假设“没有 `turn/end` 就从头执行”，就可能重复外部操作；如果简单忽略这些事件，模型历史里又可能留下一个没有对应结果的工具调用，导致下次请求不完整。

**真正需要的不是把旧的 `while` 循环从中断的那一行继续跑，而是先把已知历史整理成一段语义自洽、且不伪造执行结果的记录。**

这是 DSH 恢复设计最值得读的地方。

## 5. 重启时，谁来检查这段不完整历史？

在固定版本中，负责恢复 Agent 的入口位于 `packages/core/agent-loop/src/index.ts`，由 `resume()` 进入 `resumeWith()`。

它首先要求已经配置 Session Persistence：如果根本没有持久化后端，就不能通过这个入口找回以前的会话。然后，它取得该 Session 的写入所有权，读取磁盘上保存的事件。

这时才出现真正的恢复关键代码。

**真实源码节选**：`AgentLoop.resumeWith()`。

```ts
const persisted = coldRead.events
const closers = interruptedTurnClosers(persisted)
if (closers.length > 0) await handle.append(closers)
```

[查看固定版本中的恢复路径](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts#L836-L849)

这里先不要追 `handle` 的实现。只看三个动作：

1. `persisted`：取回**真正保存下来的**事件，而不是想象中进程曾经执行过的全部动作。
2. `interruptedTurnClosers()`：检查最后一轮有没有缺失的结束记录，并计算应该补上的事件。
3. `handle.append()`：把这些补充事件正式追加到同一条持久化历史中。

请留意，这里没有“重新运行失败的工具”的调用。它做的首先是**修复记录的完整性**。

那么，它究竟会补什么？去看这个名字很长、但职责很单纯的函数。

## 6. `interruptedTurnClosers()`：知道的记下来，不知道的承认不知道

文件：`packages/core/session/src/repair.ts`。

这个函数遍历已有事件，寻找最后一个尚未关闭的 Turn。它特别关注两种信息：模型是否提出了某个工具调用，以及是否有与它匹配的 `tool/result`。

假设恢复时看到：

```text
assistant/message  # 包含 read 的 tool-call，callId = c1
tool/call          # callId = c1
                  # 没有 tool/result
```

DSH 能得出的结论是：`c1` 被记录为发起了，但**没有持久化下来的完成结果**。它不能据此断言读取成功、失败，或者根本没有开始运行。

源码用一个很短的判断区分这种情况：

**真实源码节选**：`interruptedTurnClosers()`。

```ts
const started = callSeq !== undefined
```

这个判断所依赖的 `callSeq`，来自之前是否出现过对应的 `tool/call` 事件。[查看判断及生成结果的位置](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/repair.ts#L84-L120)。

如果已经记录 `tool/call`，恢复时追加的是带有 `TOOL_OUTCOME_UNKNOWN` 错误码的 `tool/result`；如果只有模型提出的 tool-call、却没有记录 `tool/call`，则用 `TOOL_NOT_STARTED` 标记。两者在源码中是两个不同常量：

```ts
export const TOOL_NOT_STARTED = 'TOOL_NOT_STARTED'
export const TOOL_OUTCOME_UNKNOWN = 'TOOL_OUTCOME_UNKNOWN'
```

[查看 `repair.ts` 的常量定义](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/repair.ts#L13-L15)

这里有一个必须准确理解的边界：**`TOOL_NOT_STARTED` 是日志层面的“没有记录调用开始”，不是对外部世界的绝对证明。** 事件可能尚未落盘，也可能进程在某个非常窄的时间窗口突然退出。因此，面对有副作用的操作，仍然不能只根据这个名字就盲目重试。

随后，函数会根据实际缺失的边界追加：

```text
[可能需要] tool/result（带中断错误）
[如果尚未关闭] step/end
turn/end（reason = interrupted）
```

这是**根据源码推导的顺序示意**。源代码按缺失情况生成事件，并不是每次都追加全部三项。[查看函数最后的补全逻辑](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/repair.ts#L120-L127)。

于是本章出现了一个很重要的区别：

- `aborted`：运行时活着，接到了取消信号，并在退出路径记录取消原因。
- `interrupted`：持久化历史显示上一次 Turn 没有正常闭合，**恢复过程事后**为它补上中断结局。

`interrupted` 不是旧进程在崩溃瞬间主动写下的事件。崩溃时，它根本没有那个机会。

## 7. 已经写入 Session，就一定会在崩溃后找到吗？

还不能这么说。

第三篇强调了 Session 是事件的事实来源，但这句话必须补上两个层次：运行中的事件已经追加到内存 Session，和事件**已经持久保存到存储后端**，并不总在同一个时刻完成。

DSH 固定版本的持久化设计允许将同步产生的 `session/event` 暂存到写入队列，再批量落盘；`session/flush` 则用于等待并检查这批写入是否已完成。官方 [Persistence 设计文档](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/subsystems/persistence.md) 对批量写入、显式 flush 和崩溃尾部处理作了说明。

因此，一次突然断电或强制结束进程之后，恢复入口只能相信**后端实际读出来的有效日志前缀**。它不能恢复尚未成功保存的事件，也不能重建丢失的模型流式片段。

这也解释了为什么 `resumeWith()` 使用 `coldRead.events` 开始修复，而不是凭空重新创建所有执行记录：恢复只能依据可验证的持久化事实。

## 8. 那么，DSH 的“恢复”到底恢复到了哪里？

回到最初的 `read("package.json")`。假设崩溃后能读到的是：

```text
turn/start
step/start
assistant/message  # 提出了 read(c1)
tool/call          # 已记录 c1
```

`interruptedTurnClosers()` 会据此补出错误结果，并关闭原来的 Step 与 Turn。**原来的 Turn 已经被标记为中断，不是在旧 Step 的某一行重新唤醒继续执行。**

接下来，新的 Agent 可以围绕这份完整历史接受后续输入。当用户说“继续看看这个项目”，模型可以读到此前的工具请求以及对应的中断反馈，决定是否再次读取文件。

这就是一种比较克制的恢复语义：

> 恢复已保存的历史，保留不确定性，让后续执行在明确的历史之上作出新决策，而不是声称可以从任意程序指令处无损续跑。

对我们这个只读文件的任务而言，重新执行读取通常可以接受。对“发布版本”“扣费”“发送通知”等具有外部副作用的操作，就必须先验证外部状态，或者使用幂等键、去重机制和用户确认等额外设计。**Session 日志本身不提供外部副作用的精确一次保证。**

这里也能看出与熟悉的 LangGraph Checkpoint 思路的区别：不能仅因为两者都能保存历史，就假定它们具有同样的执行恢复粒度。必须分别追踪保存的究竟是哪些事实、恢复从哪里重新开始，以及外部工具是否可能重复。

## 9. 本章的验证应该怎么做？

本章没有启动 DSH 做进程崩溃实验，因此下面列的是**可在固定 Commit 下复核的静态断言**，不是“已跑通过”的测试结果。

1. 在 `agent-loop/src/agent.ts` 的 `cancel()` 中，确认默认会清理 Inbox，并向当前运行阶段发出取消信号。
2. 在同文件的 `turn()` 中，确认运行中取消与一般错误会记录成不同的 `turn/end` 原因。
3. 在 `agent-loop/src/index.ts` 的 `resumeWith()` 中，确认它先读取持久化事件，再计算 `interruptedTurnClosers()`，并追加补全事件。
4. 在 `session/src/repair.ts` 中，确认“有 `tool/call` 无结果”和“只有模型请求、无 `tool/call`”对应两种不同错误码，最后补全 Step、Turn 的结束边界。

如果继续设计端到端实验，应该使用真正的持久化后端，并分别在“工具请求尚未记录”“工具请求已记录但结果未落盘”“结果已经落盘”三个位置制造崩溃；每次重启后对比原始存储事件、补全事件和模型看到的历史。只有实际执行过这些实验，才可以讨论可复现的恢复结果。

## 10. 回头看整个系列：DSH 为什么长成了现在的样子？

现在我们可以不依赖任何一张预先给出的庞大架构图，自己把前面的设计串起来。

最开始，仓库侦察助手只有一个模型和一个读取工具。用户连续交互后，需要明确 Turn 和 Step；工具请求开始变得不可信时，需要执行边界；执行历史需要保留时，Session Event Log 成为事实来源。功能越来越多时，需要插件注册、卸载与 Agent 专属能力；执行环境和应用形态变化时，又需要文件系统接口以及 Profile、Bundle 的装配机制。

最后这一章补上了最容易被“框架支持恢复”一句话掩盖的边界：**运行中的取消、可记录的失败，以及来不及记录的崩溃，是三种不同的事件。恢复首先要尊重事实的缺失，而不是掩盖它。**

所以我们读到的完整 DSH，并不是为了让最初那个 `while` 循环看起来更复杂，而是因为真实系统逐渐被要求做到更多：能承接输入、执行工具、保存事实、独立扩展、替换能力、组合部署，并在失败之后仍然清楚地表达“究竟发生了什么、还有什么不知道”。

这一轮源码精读到这里可以告一段落。更深入的专题可以继续研究事件投影、请求上下文构建、工具并发或恢复时的持久化一致性，但它们都应从新的具体问题开始，而不是为了填满组件目录而展开。

---

**本章源码导航（固定 Commit）：**

- [`ReactLoopAgent.cancel()` 与 `turn()`：取消、错误及结束原因](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts)
- [`AgentLoop.resumeWith()`：读取持久化日志并追加补全事件](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts)
- [`interruptedTurnClosers()`：按已知事实补全中断结局](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/session/src/repair.ts)
- [Persistence 文档：写入缓冲、flush 与崩溃后有效日志](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/subsystems/persistence.md)
