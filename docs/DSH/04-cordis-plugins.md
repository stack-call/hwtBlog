---
title: "DSH 源码精读（四）：加一条日志，为什么会引出 Cordis？"
sidebar_label: "四、加一条日志为什么会引出 Cordis"
sidebar_position: 5
slug: dsh-source-04-cordis-plugins
description: "从仓库侦察助手的一次工具结果审计出发，逐步理解 DeepSeek Harness 的事件监听、插件生命周期、服务与依赖声明。"
tags: [DeepSeek-Harness, Cordis, Agent, 源码精读]
---

前几篇我们已经沿着一条具体路径走过：用户让仓库侦察助手分析项目，模型提出 `read` 请求，工具读取 `package.json`，结果进入 Session，下一次模型请求才看见它。

现在要加的功能非常小：**记录每次工具请求最终是成功还是失败**。除此之外，我们不想改变模型行为、不想修改文件，也不想改变 Agent Loop。

这么小的需求，为什么值得专门用一章讨论？因为它会逼出一个比“怎么打印日志”更重要的问题：**一个功能怎样进入系统，又怎样完整地退出系统？**

<!-- truncate -->

> [!NOTE] 本文源码基线
> 仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)，固定 Commit：[`5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3aa172535a7967b06ff95d9cbfe536a)。与前几篇一致，不混用 `master`。
>
> 本文所示 DSH 源码来自该版本；教学插件是依据该版本事件接口写出的示例，不冒充仓库原文件。本章未在本地启动 DSH，运行轨迹均是预期而非实测。

## 1. 最简单的做法：执行完工具就打印

继续用同一个例子。用户问：“这个仓库怎么启动？”模型请求读取 `package.json`。我们希望额外看到类似这样的记录：

```text
[tool-audit] read ok
```

最直接的改法，是找到工具执行结束的地方，顺手写一句日志：

```ts
// 概念伪代码：不是 DSH 源码
const result = await executeTool(call)
console.log(`[tool-audit] ${call.name} ${result.isError ? 'failed' : 'ok'}`)
return result
```

它完全能够解决**目前只有这一项审计功能**的问题。如果只做一个自己使用的小脚本，我们甚至可以停在这里。

但接下来发生了一件事：另一位开发者还想统计工具耗时；某个部署需要审计，另一个部署不要；在线调试时，我们希望打开审计，结束后再关掉。

如果所有功能都直接改工具执行的核心函数，那么每增加一个旁路能力，都要修改这个中心位置。而且“关闭功能”可能意味着再次修改代码或维护更多条件分支。

于是，新需求出现了：**工具仍由原来的代码执行，但执行完成后，允许其他功能得知结果。**

## 2. 先不谈插件：工具结果需要一个通知出口

一种朴素的改进，是在工具执行完成后向外发送通知。真正执行工具的地方不必知道“审计插件”是什么，只负责告诉外界：有一条最终结果产生了。

这时才进入 DSH 的真实类型定义：[`packages/core/tools/src/index.ts` 中的 `Events`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L2485-L2498)。

**真实源码节选：**

```ts
/**
 * @mode emit
 */
'tools/result'(this: Scoped<ToolRuntime>, exec: Readonly<ToolExecution>, result: Readonly<ToolExecutionResult>): undefined
```

这里最重要的不是 TypeScript 语法，而是三件事：

- `tools/result` 是工具运行系统公开的**通知名称**；
- `exec` 告诉监听者“这次是哪一个工具调用”，`result` 告诉它最终结果；
- `@mode emit` 意味着这是一个**观察通知**，监听者的返回值不用于改变工具结果，事件派发也不等待异步监听任务完成。

换成我们的场景：工具原本照常读取 `package.json`，产生最终结果以后，审计功能可以收到一条关于 `read` 的通知。工具不必反过来调用 `audit.log()`。

> 注意：这里的“结果”不等于“工具函数一定实际执行过”。被策略拒绝等失败，也可能形成最终工具结果。因此这类日志只能称为“工具结果审计”，不能仅凭它推断底层文件读取一定发生过。

这个通知解决了**核心代码如何把信息告诉外界**，但还有下一个问题：谁来订阅？订阅者怎样独立安装？

## 3. 把审计功能单独装进去

DSH 使用 Cordis 管理这样的扩展。现在只需要认识一个最小概念：**插件，就是可以安装到运行环境里的一段功能。**

我们可以依据刚才核对的 `tools/result` 契约，写一个很小的插件：

```ts title="tool-audit.ts（教学插件，非仓库原文件）"
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-tools'

export const name = 'repo-scout-tool-audit'
export const inject = ['tools']

export function apply(ctx: Context) {
  ctx.on('tools/result', (exec, result) => {
    console.log(`[tool-audit] ${exec.name} ${result.isError ? 'failed' : 'ok'}`)
  })
}
```

这段代码值得慢慢读，而不是一眼跳过：

首先，`apply` 是插件实际装载时执行的入口。`ctx` 是 Cordis 提供给它的运行环境入口；它让插件找到所需服务、注册事件监听，而不必修改工具执行器。

其次，`ctx.on('tools/result', ...)` 的意思是：“当这个通知出现时，调用我的函数。”`exec.name` 对应工具名，例如 `read`；`result.isError` 表示最终结果是否失败。这些字段都来自该版本真实的工具类型定义：[`ToolExecutionInput`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L2709-L2731) 与 [`ToolExecutionResult`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L3129-L3173)。

最后，`inject = ['tools']` 说的是：“只有工具服务已经就绪，我才开始安装。”如果工具服务尚不存在，Cordis 不应让这个依赖它的插件直接运行。这不是在申请工具使用权限，而是在声明**启动所需的依赖**。

于是我们首次得到了不修改工具核心逻辑的审计能力。正常情况下，预期的可观察过程是：

```text
模型请求 read(package.json)
        ↓
工具系统处理请求，形成最终结果
        ↓
通知 tools/result
        ↓
审计插件打印 [tool-audit] read ok
```

这里并没有声称已经在真实 DSH 中打印出这一行：它是根据事件合同推导的预期轨迹。也要注意，`console.log` 只是演示输出，不是可靠的持久化审计系统。

## 4. 为什么能安装，还不够？

现在假设我们在开发时打开审计，调试完成后把插件卸载。最容易犯的错误是：插件的代码不再工作了，但此前注册的监听函数仍被事件系统持有。

后果是什么？下一次工具执行，旧监听函数仍然收到通知；重新安装后，还可能出现两份审计输出。

```text
第一次安装 → 一份监听器
卸载但忘记清理 → 旧监听器仍在
第二次安装 → 两份监听器
一次工具结果 → 打印两次
```

因此，单单有“注册事件”还不够，我们需要**注册和撤销成对出现**。这正是 Cordis 插件生命周期最关键的价值之一。

官方固定版本的 [Cordis 生命周期教程](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/02-lifecycle-and-effects.md) 明确写出：`ctx.on()` 本身就是受插件生命周期管理的注册，插件卸载后，监听器会自动撤销。

这时不用急着看 Cordis 所有状态。只需要理解两种动作：

```text
安装插件 → 注册 tools/result 监听器
卸载插件 → 释放这个监听器
```

如果有一个 Cordis 无法替我们自动清理的外部资源，比如文件监视器或计时器，就需要显式告诉 Cordis 怎样收尾。官方教程给出过下面的真实模式。

**真实官方教程源码节选：**

```ts
ctx.effect(() => {
  const timer = setInterval(() => console.log('tick'), 200)
  return () => {
    clearInterval(timer)
    console.log('heartbeat cleaned up')
  }
})
```

[`docs/cordis-tutorial/02-lifecycle-and-effects.md`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/02-lifecycle-and-effects.md)

读法很简单：安装时创建资源，返回的函数负责销毁资源。这个“安装时生效、卸载时撤销”的注册被称为 **Effect**。

注意我们写的审计插件**不需要再包一层 `ctx.effect()`**，因为 `ctx.on()` 已经按照这个约定替我们管理监听器。

## 5. 原来 Agent Loop 自己也遵循这套规则

到这里，插件似乎只是给系统增加一点日志。但 DSH 的设计不止于此：连默认 Agent Loop 都以可注册的服务形式接入。

为什么这很重要？因为外部代码只想“创建一个 Agent”，不应该被迫依赖某个具体的 Loop 类。否则换掉执行器，所有创建 Agent 的代码都可能要跟着修改。

来看两处很短的真实源码。

首先，在 [`packages/core/agent-loop/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts#L2641-L2645) 中：

```ts
export class AgentLoop extends Service implements AgentFactory {
  static inject = ['agents', 'sessions', 'llm', 'tools', 'systemPrompt', 'sessionProjections']
```

只读现在必需的部分：`AgentLoop` 是一个 **Service**，也就是能通过 Cordis 运行环境被其他部分找到的能力实现；它还声明自己需要 `agents` 等服务先准备好。我们不必现在解释数组里每一个服务，只要知道：默认 Loop 不是单独在真空中启动的。

接着看同文件的注册动作：

**真实源码节选：**

```ts
ctx.effect(() => ctx.agents.setFactory(this), 'agentLoop.setFactory()')
```

[`AgentLoop` 构造过程](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts#L2743-L2753)

这里的 `this` 就是当前 Agent Loop 实例。`ctx.agents.setFactory(this)` 把它登记为 Agent 创建工厂，外部之后就可以通过 `ctx.agents.create(...)` 走到这个实现。

关键是：`setFactory()` **会返回一个撤销注册的函数**。它的真正实现位于 [`packages/core/agent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent/src/index.ts#L2175-L2205)，里面包含这样的收尾动作：

```ts
// 真实源码节选（省略中间的校验与其他实现）
this.factory = { target }
return () => { this.factory = undefined }
```

现在可以把因果关系连起来了：默认 Loop 被安装时，向 Agent 服务登记“由我负责创建 Agent”；默认 Loop 卸载时，登记的工厂会被撤销，避免留下一个指向已退出实现的入口。

这才是“一切皆插件”更值得理解的含义：它不是把所有文件换个名字叫插件，而是让**能力的接入和退出都有统一的边界**。不过，这不意味着可以在执行中随意替换 Loop 且不中断任务；正在运行的 Agent 仍有自己的生命周期和清理规则。

## 6. 回看这条架构是怎样长出来的

我们最初只想记录一次 `read` 结果。一步步发生的是：

```text
工具执行完 → 顺手打印日志
    ↓ 其他功能也要观察结果
工具系统发出 tools/result 通知
    ↓ 审计功能需要独立装卸
插件通过 ctx.on() 订阅通知
    ↓ 卸载时不能留下旧监听器
Cordis 管理注册的生命周期
    ↓ 不只是审计，Loop 实现也需要可装卸
Agent Loop 作为 Service 注册创建工厂
```

今天真正增加的概念其实只有三个层次：**通知用于解耦“发生事件”和“响应事件”；插件用于独立组织功能；可撤销的注册让功能能够安全退出。** 需要服务启动顺序时，才用 `inject` 表达依赖。

再明确一下边界：`tools/result` 是观察已形成的结果，不是授权工具运行的地方；`ctx.on()` 自动卸载监听器，不意味着异步日志已经可靠落盘；Cordis 负责组合和生命周期，不会自动解决工具权限、外部副作用或故障恢复问题。

## 7. 不启动模型，也能怎样核对这章？

本章没有做 DSH 端到端实测。读者可以在固定 Commit 下做三项静态核对，而不用 API Key：

1. 打开 `packages/core/tools/src/index.ts`，找 `tools/result`，确认它确实是 `emit` 通知，并核对参数 `exec`、`result`。
2. 打开 `docs/cordis-tutorial/02-lifecycle-and-effects.md`，确认 `ctx.on()` 属于自动撤销的注册，`ctx.effect()` 用于清理额外资源。
3. 打开 `packages/core/agent-loop/src/index.ts` 和 `packages/core/agent/src/index.ts`，确认 `setFactory(this)` 注册后，会返回清空工厂槽位的撤销函数。

这三项核对能支持本文的设计解释，却不能证明我们写的教学插件已经被某个 Profile 成功装载。要做真实运行测试，还需要确定插件装配入口、启动命令和当前 Profile 配置；这些内容留到后面的装配章节。

## 下一篇：为什么两个 Agent 不能共用同一套插件规则？

我们给仓库侦察助手加上了审计。但假设同一个进程中还运行着另一个 Agent：一个只允许读文件，另一个可以在获得批准后修改文件。

如果所有插件注册都对所有 Agent 生效，就又会遇到问题：**能力是全局存在的，还是只属于某个 Agent？**

下一篇，我们将从这个具体冲突出发，研究 DSH 怎样控制不同 Agent 能看见哪些工具、哪些策略只作用于特定执行范围。那时再引入作用域，而不是现在提前背下一整套术语。

---

**本章源码导航（均为固定 Commit）：**

- [`ToolRuntime` 事件契约：`tools/result`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L2485-L2498)
- [`ToolExecution` 与 `ToolExecutionResult` 类型](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L2709-L2731)
- [`Cordis` 生命周期和 Effect 官方教程](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/02-lifecycle-and-effects.md)
- [`AgentLoop` 服务和工厂注册](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts#L2641-L2645)
- [`AgentRegistry.setFactory()` 的撤销函数](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent/src/index.ts#L2175-L2205)
