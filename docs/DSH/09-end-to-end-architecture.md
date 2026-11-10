---
title: "DSH 源码精读（九）：从启动到回答，终于把整条运行链串起来了"
sidebar_label: "九、从启动到回答的整条运行链"
sidebar_position: 11
slug: dsh-source-09-end-to-end-architecture
description: "沿一个只读仓库分析请求，把 DSH 的 Profile 装配、Agent 创建、Turn/Step、工具执行和 Session 投影连成一条可核对的运行链。"
tags: [DeepSeek-Harness, Agent, 源码精读]
---

前八篇，我们分别读过 DSH 的 Agent Loop、工具执行、Session、Cordis、Agent Scope、文件系统、启动装配和中断恢复。每个机制为什么出现，已经有了自己的解释。

但是，如果现在把源码关掉，我仍然可能回答不好一个最基本的问题：

> 用户在浏览器里问了一句“这个项目怎么启动”，DSH 究竟经历了什么，才把 `package.json` 的内容变成最后的回答？

这是本篇要解决的唯一问题。我们不再引入新的大模块，也不把前八章的术语重新罗列一遍，而是**从同一个请求的起点走到终点，沿途确认每一层为什么要把工作交给下一层**。

<!-- truncate -->

> [!NOTE] 本文的源码与验证范围
> - 仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
> - 固定 Commit：[`5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3aa172535a7967b06ff95d9cbfe536a)
> - 本章的**真实源码节选**均按该版本核对；“教学示意”只描述一条假设的正常成功路径。
> - 本章完成了对 GitHub 固定版本的**静态源码核对**，**没有**在本地启动 DSH、连接真实模型或获取真实 Session 日志。不能把下面的预期事件序列当作实测输出。

## 1. 先只看用户能观察到的事

我们仍然使用“仓库侦察助手”。待分析项目里有一份配置：

```json title="package.json（教学用的虚构文件）"
{
  "name": "repo-scout-demo",
  "scripts": {
    "dev": "vite",
    "test": "vitest"
  }
}
```

用户输入：

> 帮我看看项目怎么启动。只允许读取文件，不要运行命令，也不要修改任何内容。

这次我们假设模型采用最短的成功路径：第一次请求读取 `package.json`，工具成功返回配置；第二次模型调用根据 `scripts.dev` 给出建议“运行 `npm run dev`”。

```text title="教学示意：先不管实现细节"
用户提出问题
    ↓
模型：需要读取 package.json
    ↓
读取工具：返回 scripts.dev = "vite"
    ↓
模型：建议运行 npm run dev
    ↓
本轮结束
```

这个过程在第一篇里已经讲过。现在要补上的不是一个新的循环，而是另一条线：**谁把这些能力连接在了一起？**

如果没有文件系统提供者，工具从哪里拿文件？如果没有 Agent Loop，谁在收到工具反馈后再次调用模型？如果没有 Session，第二次模型请求从哪里获得第一次读取的结果？

我们先沿请求往前追一步：在用户发消息之前，DSH 必须已经启动。

## 2. 用户还没发消息：谁决定这次启动装哪些能力？

把所有服务写死在启动函数里，当然也能完成任务。但第七篇已经解释过：同一个助手可能以 Web、Headless 等方式运行，不应该为了换入口而复制一套工具与 Loop。

DSH 先根据所选 **Profile** 读取 Bundle 与用户补丁，再将配置交给插件装配流程。这里我们不重复解释补丁算法，只看它回答的具体问题：

> 本次运行到底有没有装入 Agent Loop、工具服务、文件系统实现以及相应的应用入口？

固定版本里可以定位三个源码入口：

- [`loadProfile()`：读取选定 Profile](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/profile.ts)
- [`composeEntries()`：将配置层组成最终插件条目](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/profile.ts)
- [`boot()`：创建 Cordis Context 并启动装配](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/index.ts)

这几个入口只决定**运行环境里有哪些可供使用的服务**。它们并没有替用户读取 `package.json`，也没有向模型发送问题。

用一句话分清：**装配阶段决定“谁在场”；执行阶段决定“谁做事”。**

现在假设我们启动的是具有所需能力的 Web Profile，并且这些插件都成功激活。用户发来请求时，下一步谁接住它？

## 3. 创建 Agent：为什么调用方不用直接 new 一个 Loop？

如果调用方自己 `new ReactLoopAgent(...)`，就必须直接知道这个类的构造参数、依赖，以及它如何被释放。第七篇学到的“可替换装配”就会被这里的硬编码抵消。

DSH 让对外的 Agent API 和默认驱动实现分开。`AgentRegistry.create()` 寻找已注册的工厂；默认 `AgentLoop` 将自己注册为工厂。

这一步最直接的证据只有一行。

**真实源码节选**：[`packages/core/agent-loop/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts)

```ts
ctx.effect(() => ctx.agents.setFactory(this), 'agentLoop.setFactory()')
```

这里 `this` 是当前的 `AgentLoop` 服务。`ctx.agents` 不需要写死“永远由哪一个实现创建 Agent”，只需要调用当前注册的工厂。

这个调用关系可以简写为：

```text title="教学化简：创建关系，不是运行时的完整调用栈"
宿主应用
  └─ ctx.agents.create(...)
       └─ 已注册的 Agent 工厂
            └─ 实际 Agent 实例
```

它解决的是**谁负责创建一个可运行的 Agent，以及使用完谁负责释放**。但刚创建出来的 Agent 还不等于已经执行用户任务。

下面才是第一条用户消息进入 Loop 的时刻。

## 4. 用户消息进入：为什么不直接调用模型？

如果没有任何正在执行的任务，直接触发模型也能工作。但是一旦已有上一轮任务尚未结束，或者用户连续发来两条消息，外部输入就需要先被安排，而不能理所当然地启动两个互不协调的循环。

因此，这个版本的 `ReactLoopAgent` 有一个很直白的入口：

**真实源码节选**：[`packages/core/agent-loop/src/agent.ts` 的 `followup()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts)

```ts
followup(input: UserMessage): void {
  this.send(input, 'next-turn', true)
}
```

三个参数在这个场景里只需要理解两个：**把消息送入下一轮的输入队列**，以及**必要时唤醒执行器**。接下来 `send()` 把消息放入 Inbox，`wakeDriver()` 负责决定是否启动执行驱动。

现在读者有了一个可观察的区别：应用收到请求，不一定立刻发生模型调用。模型调用要等到 Agent 驱动真正认领这条消息。

这不是 Cordis 在替模型推理，也不是 Inbox 在替模型做规划。它们只是安排“什么时候开始处理哪条输入”。

## 5. 第一次 Step：哪段代码把消息真正变成模型请求？

当输入进入执行器后，`kick()` 推动一个个 Turn，`turn()` 再推进其中的 Step。

**真实源码节选**：[`ReactLoopAgent.kick()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts)

```ts
while (await this.turn()) {}
```

这一行容易产生误解：它不是“调用一次模型就循环一次”。`turn()` 自己还会推进一个或多个 Step。

为了看懂我们的例子，只记住：一次用户请求占一个 Turn；第一次模型请求以及随后产生的读取工具属于第一个 Step；模型根据读取结果给出答案，属于第二个 Step。

但还有一个容易忽略的环节：第一次请求不能只有用户说的那句话。模型至少需要知道它能使用什么工具，以及模型历史里有哪些消息。

固定版本的正常路径中，`preStep()` 通过 `systemPrompt.assemble(...)` 汇集当前请求需要的提示内容与可见工具；随后 `step()` 准备模型调用，并把允许进入本次请求的输入写入 Session。`buildRequest()` 再从 Session 派生并冻结这次发送给模型的消息。

我们用两段非常短的真实源码确认其中最关键的交接：

**真实源码节选**：[`preStep()` 中的请求装配](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts)

```ts
const assembly = await this.loopCtx.systemPrompt.assemble(assembleContextFor(this, signal))
```

**真实源码节选**：[`buildRequest()` 中的模型历史生成](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts)

```ts
const boundaryMessages = session.deriveMessages()
```

第一行回答“模型**可以知道**什么”，第二行回答“模型这次**实际收到的历史**从哪里来”。两者不是一回事：可见工具与系统提示的装配，不等于工具已经执行；Session 能保存执行事件，也不意味着每个事件都原封不动送给模型。

现在模型才有机会提出具体请求：读取 `package.json`。

## 6. 模型想调用 `read`：为什么 Loop 不自己去读文件？

假设第一次模型返回的内容包含 `read` 工具请求，参数是 `file_path: "package.json"`。

如果 Loop 直接调用 Node.js 的文件 API，第二、五、六篇讨论过的参数验证、Agent 可见性、策略判断和可替换文件系统就全都被绕开了。

所以 Loop 只负责识别模型是否产生工具请求，然后把它交出去。

**真实源码节选**：[`ReactLoopAgent.step()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts)

```ts
const toolCalls = message.content.filter(block => block.type === 'tool-call')
if (toolCalls.length === 0) return { kind: 'completed' }
```

对本次任务而言，因为确实存在 `tool-call`，`step()` 不会在这里结束，而是进入 `executeToolCalls()`。

从这一刻开始，要严格分清三个角色：

- **模型**提出“我想读取这个文件”；
- **工具运行时**决定这个请求如何解析、能不能执行，以及如何返回结果；
- **文件系统提供者**负责在实际环境中读取文件。

对于 `read` 工具，固定版本确实通过 `ctx.fs` 访问底层文件能力，而非在 Loop 中直接读取磁盘。

**真实源码节选**：[`packages/fs/tool-fs/src/read.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts)

```ts
const chunks = info.size === undefined || info.size >= caps.streamMinSize
  ? await ctx.fs.streamText(target, exec.signal)
  : [await ctx.fs.readText(target, exec.signal)]
```

先别追问 `streamMinSize` 的精确数值。对于本篇，只需要看最后两行：读取操作被委托给 `ctx.fs`，因此工具定义不必与某一种磁盘实现绑死。

这里的前提仍然是：模型请求的工具已被允许执行，路径在底层提供者支持的范围内。`ctx.fs` 并不天然代表安全沙箱。

## 7. 工具已经读完，为什么不直接把结果塞给模型？

现在假设文件确实读到了 `scripts.dev = "vite"`。

最简单的 Agent 循环会直接把结果 `append` 到消息数组里，然后调用模型。但第三篇已经看到，DSH 想保留的不只是模型可见文本，还包括谁发起了工具调用、它是否有结果、这次请求属于哪一个 Step。

因此，调度器先将工具请求与结果提交给 Session。固定版本的 `executeToolCalls()` 负责这段调度，`appendToolCall()` 提交 `tool/call`，对应处理随后提交 `tool/result`。

**真实源码节选**：[`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/tool-calls.ts)

```ts
const event = session.append('tool/call', {
  turn, step, callId: block.id, name: block.name, arguments: block.arguments
})
```

注意这段代码的输入并不是文件内容，而是这次工具请求的身份与参数。真正的工具结果另外记录，并与请求关联。

这就把两种事实分开了：“模型提出过什么请求”与“执行器得到了什么反馈”。

也正因为有这层区别，第八篇讨论崩溃恢复时，DSH 才能区分“未开始的调用”与“结果未知的调用”，而不会把它们都误判成成功。

但回到正常路径，此时仍有一个问题：结果写进 Session 后，是谁让模型再次工作？

## 8. 第二次 Step：工具不负责再次请求模型

工具执行完毕，并不自己调用模型。调度器把结果交回当前 Step；当前 Step 向外层 Turn 返回“还需要继续”的状态，外层 Turn 于是开启下一个 Step。

第二次构造请求时，`buildRequest()` 又一次调用 `session.deriveMessages()`。这一次的模型历史已经包含前面提交的 `assistant/message` 中的工具请求，以及相应的 `tool/result` 反馈。

所以第二次模型请求看到的是这样一段因果连续的历史：

```text title="教学示意：模型可见的简化消息历史"
用户：这个项目怎么启动？
助手：调用 read(file_path="package.json")
工具：返回 scripts.dev = "vite"
```

**这不等于 Session Event Log 只保存了这三条记录。** 日志里还有 Turn、Step、执行请求与其他事实；它们只是没有全部原样变成给模型的消息。

这一次，假设模型返回普通文本，没有工具请求。刚才看过的判断条件才满足：

```ts
if (toolCalls.length === 0) return { kind: 'completed' }
```

外层 Turn 再确认没有欠下下一步输入等工作，便将这一轮正常结束，记录 `turn/end`。

至此，用户终于得到答案：“可以执行 `npm run dev` 启动开发服务。”

这里的“正常结束”，并不等于真的在本地运行过 `npm run dev`，更不等于验证了项目一定能启动。因为用户一开始就禁止执行命令，系统完成的是**基于只读证据的建议**，不是启动测试。

## 9. 现在才画完整链路图

前面每一步都已经解释过为什么存在，我们才有资格把它们合在一起：

```text title="教学化简：控制流与关键数据交接，非单个函数的调用栈"
启动阶段
  Profile / Bundle / 用户配置
           ↓
       Cordis Loader
           ↓
  注册 Loop、工具、Session、文件系统等能力

执行阶段
  宿主通过 Agent API 创建/取得 Agent
           ↓
      followup(用户输入)
           ↓
       Inbox / Driver
           ↓
       Turn 1 开始
           ↓
       Step 1
         ├─ 装配提示与可见工具
         ├─ 从 Session 派生模型历史
         ├─ 模型要求 read(package.json)
         ├─ Tool Runtime 解析并执行
         │     └─ ctx.fs 读取文件
         └─ Session 记录 tool/call、tool/result
           ↓
       Step 2
         ├─ 再次从 Session 派生历史
         └─ 模型输出最终回答
           ↓
       Turn 1 结束
```

注意这不是一张“所有框都有箭头直接互相调用”的 UML 图。它把启动装配、运行控制和关键数据流放在同一张**教学汇总图**里，方便复述。想查精确调用关系，仍应回到对应函数。

现在能看到：前八篇不是八个并列功能，而是一条请求经过工程约束后自然形成的职责分工。

## 10. 三个容易在源码精读时说错的结论

**第一，Cordis 不负责决定模型下一步做什么。** Cordis 负责插件装配、服务与生命周期；决定是否继续 Step 的是 Agent Loop。插件可以在声明的扩展点干预行为，但不能把 Cordis 等同于 Agent 的推理算法。

**第二，Session 不是一份直接透传给模型的完整事件列表。** 它先保存执行事实，再根据请求边界派生模型历史。`tool/call` 的执行记录与 `assistant/message` 中的模型工具请求，角色不同。

**第三，工具的可见性与实际执行环境不是一个限制。** 某个 Agent 看不到工具，并不能说明本地磁盘已经隔离；即使某项工具请求获准执行，也必须由底层文件或进程提供者保证实际访问边界。

如果能把这三点说清楚，就已经能够正确描述 DSH 大部分重要的架构边界。

## 11. 如何自己核对这条链，而不是只相信文章？

本篇没有端到端启动 DSH，因此更适合做一项**静态源码追踪练习**。在本地拿到仓库后，先固定版本：

```bash
# 在已经克隆的 deepseek-harness 仓库目录中执行
# 提醒：checkout 会改变当前工作区；请先确认没有未提交的修改。
git checkout 5dda764ed3aa172535a7967b06ff95d9cbfe536a
```

然后沿**同一个请求**依次打开以下位置，不必从头读完每个文件：

| 此刻遇到的问题 | 去哪里找证据 | 只回答一个问题 |
|---|---|---|
| 启动时要装什么？ | `packages/boot/app-boot/src/profile.ts` | Profile 如何变成配置层？ |
| Agent 是谁创建的？ | `packages/core/agent/src/index.ts`、`agent-loop/src/index.ts` | 谁注册默认工厂？ |
| 用户消息进来以后呢？ | `packages/core/agent-loop/src/agent.ts` | `followup()` 怎样唤醒执行？ |
| 怎么决定继续 Step？ | 同一文件的 `turn()` / `step()` | 有 Tool Call 时返回什么？ |
| 工具结果去了哪里？ | `packages/core/agent-loop/src/tool-calls.ts` | `tool/result` 由谁写入？ |
| 下一次模型看见什么？ | `agent.ts` 的 `buildRequest()` | 为什么要调用 `deriveMessages()`？ |
| 实际文件由谁读？ | `packages/fs/tool-fs/src/read.ts` | 为什么调用 `ctx.fs`？ |

这项练习能够帮助你在源码中**重建静态因果链**，但它不等于真的验证了某一个 Web 请求或某个模型提供商的运行结果。如果要做下一步的端到端验证，需要固定 Profile 配置、模型适配器、测试仓库和事件收集方式，并留下原始日志与版本信息。

## 写在最后：把 Agent 与 Harness 的区别讲清楚

第一篇里，我们拥有的不过是一个简单的“模型 → 工具 → 模型”循环。现在多了这么多层，并不是因为模型变得更聪明，而是因为真实运行系统需要回答更多问题：

- 有哪些能力可用，谁负责装配？
- 用户的输入在什么时候被处理？
- 模型提出的请求，谁决定是否实际执行？
- 工具反馈如何进入下一次模型请求？
- 执行中途退出后，哪些事实还能被可靠地解释？

如果一个系统永远只处理单次、短小、受信任的任务，许多机制确实可以省略。DSH 的价值在于，它把这些工程边界显式化，让能力得以独立组合、替换和管理；代价则是更多的服务接口、事件与生命周期复杂度。

**学会 DSH 的架构，不是记住它有哪些 Package，而是能够沿一条请求指出：为什么这一层不能简单地交给上一层处理。**

这也正是本系列从“一个能工作的 Agent”最终走向“一个可组合的 Agent Runtime”的理由。

---

**固定版本源码入口**：

- [App Boot / Profile](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/profile.ts)
- [Agent Registry](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent/src/index.ts)
- [Agent Loop：创建与服务注册](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts)
- [ReactLoopAgent：驱动与请求](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts)
- [工具调用调度](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/tool-calls.ts)
- [读取工具](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts)
