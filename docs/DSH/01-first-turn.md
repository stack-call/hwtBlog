---
title: "DSH 源码精读（一）：一个问题，为什么要调用两次模型？"
sidebar_label: "一、为什么要调用两次模型？"
sidebar_position: 3
slug: dsh-source-01-first-turn
description: "先看一条请求如何执行，再对照 DeepSeek Harness 的几行真实源码，理解 Turn、Step 与工具反馈。"
tags: [DeepSeek-Harness, Agent, 源码精读]
---

如果你已经读过 DeepAgent，就知道 Agent 的基本动作：向模型提问，必要时执行工具，再让模型根据工具结果继续回答。

但在 DSH 里，这个过程究竟由谁推进？**为什么用户只问了一个问题，系统却可能调用模型两次？**

这一篇只回答这个问题。我们不从类名和目录开始，而是先看一条请求实际上需要经历什么。等出现必须由代码解释的疑问，再打开源码。

<!-- truncate -->

> [!NOTE] 本文阅读范围
> 仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)，固定 Commit：[`5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3aa172535a7967b06ff95d9cbfe536a)。
>
> 下面的项目、模型响应与工具结果是**教学情境**，不是 DSH 的真实运行日志。标有“真实源码节选”的代码均来自这个固定版本；本章没有启动 DSH 进行端到端测试。

## 1. 先不看源码：用户只问了一句话

假设我们给 Agent 一个很简单的任务：

> 看看这个仓库应该怎么启动。你可以读文件，但不要执行命令，也不要修改文件。

这个示例仓库有一个 `package.json`，其中的 `dev` 脚本运行 `vite`。但用户没有把文件内容贴给模型。

所以模型收到问题后，无法仅凭这句话准确知道启动命令。

它需要先做一件事：**请求读取 `package.json`**。

假设我们观察到如下交互：

```text
用户：这个仓库怎么启动？

模型（第一次）：我需要查看 package.json。
               → 提出读取文件的工具请求

工具：读取成功，发现 scripts.dev = "vite"

模型（第二次）：运行 npm run dev 可以启动开发服务。
```

这不是四次用户对话。用户只有一次提问，系统却需要**两次模型请求，中间夹着一次工具执行**。

此刻最重要的疑问出现了：第一次模型已经返回内容，为什么系统没有把它当作最终答案？是谁决定把工具结果交给模型，再请求一次？

## 2. 先区分两件事：回答一个问题，和调用一次模型

如果把“模型返回了”当成“用户任务结束了”，刚才的任务会在第一次请求后停止。用户只能看到“我需要读取文件”，而看不到启动命令。

因此，需要两种不同的范围：

- **Turn（一轮任务）**：从开始处理用户这次请求，到本轮执行真正结束。
- **Step（一次执行步骤）**：在正常路径中，发起一次模型请求，并处理这次回复带来的工具调用。

刚才的例子就变成：

```text
Turn：回答“仓库怎么启动？”
  ├─ Step 1：请求模型 → 模型请求读文件 → 工具返回内容
  └─ Step 2：请求模型 → 模型给出答案
Turn 结束
```

注意，**工具调用没有单独占据第三个 Step**。它属于发起工具请求的那次 Step。

这两个术语不是为了把普通循环包装得更复杂。它们解决的是一个具体的表达问题：系统需要知道“当前完成的是一次模型请求”，还是“整个用户任务已经完成”。

现在，我们已经知道源码中需要找什么：**判断一次 Step 应该结束整个 Turn，还是让 Turn 再运行一个 Step 的地方。**

## 3. 第一次看代码：模型没有调用工具时，发生什么？

在固定版本中，这个判断位于：

[`packages/core/agent-loop/src/agent.ts` → `ReactLoopAgent.step()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L443-L468)。

先看其中两行，不需要阅读整个函数。

**真实源码节选：**

```ts
const toolCalls = message.content.filter(block => block.type === 'tool-call')
if (toolCalls.length === 0) return { kind: 'completed' }
```

逐句对应刚才的情境：

第一行从模型回复中找到工具请求。第二行判断：**如果这次模型没有请求任何工具，这个 Step 就报告“完成”。**

于是第二次模型调用的情况很好理解：模型已经拿到 `package.json`，直接回答“运行 `npm run dev`”，不再提出工具请求。代码返回 `completed`。

这里的 `completed` 是返回给外层调度逻辑的结果，不是直接向用户发送的一条文本消息。

但第一次模型调用恰好相反：模型确实提出了工具请求。两行代码并不会结束它。那接下来呢？

## 4. 模型请求读文件之后，为什么还能继续？

接着往下看同一个函数。它调用 `executeToolCalls()` 来处理模型提出的工具请求，等工具处理完成，再返回一个状态给外层。

在我们的正常路径里，工具返回 `package.json` 的内容后，并没有要求直接结束本轮。因此，`step()` 返回的是 `null`。这里的 `null` 可以先理解成：

> **这一步做完了，但整轮任务还没有宣布完成。**

[对应的真实源码位置](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L462-L468)中，关键返回行为是：

```ts
return concluded ? { kind: 'completed' } : null
```

`concluded` 表示工具执行结果是否要求本轮结束。我们的读文件工具正常返回内容，没有提出这种要求，所以走到 `null`。

这里有一个容易混淆的地方：**并不是工具执行函数自己再次调用模型。** 它只负责处理工具请求，并把处理结果交还给上层执行过程。

那么谁发起第二次模型请求？我们还得往外走一层。

## 5. 第二次看代码：谁负责再运行一个 Step？

打开同文件中的 [`ReactLoopAgent.turn()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L248-L330)。

这个函数管理一整个 Turn。它会不断进入 Step，并根据每一步返回的结果决定何时停下。

我们先只定位一个调用语句。

**真实源码节选：**

```ts
const stepEnd = await this.step(decision)
```

你可以把它读成：“当前这一轮，现在执行一步，并等它告诉我这一步的结果。”

结合刚才的例子：

第一次执行 `step()`：模型要求读文件，工具完成读取，返回 `null`。对 `turn()` 来说，这说明任务还需要推进，于是它会进入下一次 Step。

第二次执行 `step()`：模型直接回答，没有工具调用，返回 `{ kind: 'completed' }`。

接下来 `turn()` 还会确认是否有需要立刻处理的下一步输入。在正常情况下，如果本轮有结束结果，且没有新的 next-step 输入，才会退出内部循环。对应的源码判断是：

```ts
if (turnEnds && this.inbox.nextStep.length === 0) break
```

[查看判断位置](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L294-L300)。

这就回答了本篇的核心问题：

**`step()` 负责完成当前这次模型请求及其工具处理；`turn()` 负责判断用户这一轮任务是否还需要下一次模型请求。**

## 6. 第二次模型请求，怎么知道第一次读到了什么？

还有一个必须补上的细节。

既然第一次 Step 的工具已经拿到了文件内容，第二次 Step 总得把它提供给模型。否则模型依然不知道 `package.json` 写了什么。

DSH 的正常路径是：工具执行后，把调用和结果记录到 Session；下一次构造模型请求时，再从 Session 派生模型可见的消息历史。

你可以先把 Session 理解为**本轮执行事实的记录来源**，这里不需要提前学习它的完整事件存储设计。

这条路径在源码里分别对应：

- [`tool-calls.ts`：工具调用与结果记录](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/tool-calls.ts)
- [`agent.ts`：`buildRequest()` 从 Session 派生模型消息](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L525-L589)

所以第二次请求不是“凭空知道”文件内容的。

```text
Step 1 读到文件
     ↓
工具结果被记录
     ↓
Step 2 构造请求时读到之前的结果
     ↓
模型据此回答
```

这里我们只验证**数据能够走到下一次请求**。为什么 DSH 选择 Session 事件日志，而不是简单保存一个 `messages` 数组，要等 Session 章节再解释。

## 7. 到这里，才把完整路径连起来

现在可以把本篇看到的几个动作串起来了。用户消息进入 DSH 后，默认驱动在合适时机开始执行 Turn；Turn 反复调用 Step；Step 请求模型、处理可能的工具调用，并把结果交还给 Turn；Turn 判断何时结束。

对应到固定版本的源码，用户输入通过 `ReactLoopAgent.followup()` 进入待处理队列，由 `wakeDriver()` 唤醒执行，随后走到 `kick()`、`turn()` 和 `step()`。这是整条执行路径的上游，但**现在才引入这些函数名**，因为我们已经知道它们要把什么任务交给谁。

[查看入口源码：`followup()` 与 `wakeDriver()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L118-L194)。

至此，我们才得到本篇最完整的控制流：

```text
用户提交问题
    ↓
消息等待处理
    ↓
Turn 开始
    ↓
Step 1：请求模型 → 读取文件 → 返回“尚未结束”
    ↓
Step 2：请求模型 → 生成答案 → 返回“完成”
    ↓
Turn 结束
```

这个图里没有 Cordis、插件注册、生命周期清理等内容，不是因为它们不存在，而是因为**理解两个 Step 为什么出现，并不需要先学习它们**。

## 8. 我们到底从源码确认了什么？

这次没有运行真实 DSH。下面是一条**由上述源码推导出的正常路径**，不是复制的运行日志：

| 时刻 | 发生了什么 | 为什么还要继续？ |
| --- | --- | --- |
| Step 1 开始 | 模型提出读取文件的请求 | 模型还没有足够的信息回答 |
| Step 1 结束 | 工具返回文件内容，`step()` 返回 `null` | 本轮尚未宣告完成 |
| Step 2 开始 | 新请求能使用上一步的工具结果 | 需要模型整合结果 |
| Step 2 结束 | 模型直接给答案，`step()` 返回 `completed` | 已得到结束信号 |
| Turn 结束 | 没有待处理的 next-step 输入 | 本轮可以收尾 |

这还不能证明模型一定会选择正确工具，也不能证明文件内容一定可信，更不表示用户要求的“只读”限制已经通过权限机制得到保证。我们只弄清了**一次成功的工具调用为什么能推动模型继续回答**。

## 9. 下一篇：既然工具只是读取文件，为什么还要有专门的执行边界？

这一篇里，我们故意把读取工具当成可以正常返回的操作。现在只改变一个条件：模型不再请求读取 `package.json`，而是请求一个本不应该读取的路径。

这时，判断要不要执行，应该由谁负责？工具返回错误后，谁负责把错误变成模型能够理解的反馈？

下一篇就从这个具体失败场景出发，研究 DSH 的工具执行流程。先让最简单的“调用函数”方案遇到限制，再进入真实工具代码。
