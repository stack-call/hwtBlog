---
title: "DSH 源码精读（五）：为什么两个 Agent 看到的工具可以不同？"
sidebar_label: "五、两个 Agent 为什么看到不同的工具"
sidebar_position: 7
slug: dsh-source-05-agent-scope
description: "从同一进程里的只读仓库侦察助手与发版助手出发，逐步理解 DSH 的 agent.ctx、Scope、工具可见性限制和执行期保护。"
tags: [DeepSeek-Harness, Agent, 源码精读]
---

上一章，我们为仓库侦察助手增加了工具执行审计，并发现通过插件监听 `tools/result`，就不用把审计逻辑硬塞进 Agent Loop。

但现在出现了一个新要求：**同一个 DSH 进程里，要同时运行两个助手，而且它们不能使用同一套工具。**

第一个仍是我们的仓库侦察助手。用户只是想知道项目怎样启动，它只需要读取文件。第二个是发版助手，需要在用户授权的工作流中运行构建或测试命令。

问题不是“模型会不会按提示词行事”，而是：**如果工具已经装进同一个 Runtime，怎样让仓库侦察助手没有执行 `bash` 的能力，却不影响发版助手？**

<!-- truncate -->

> [!NOTE] 源码基线
> 仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
>
> Commit：[`5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3aa172535a7967b06ff95d9cbfe536a)（沿用前四章）。
>
> 本文标注“真实源码节选”的代码来自这个版本；“教学配置”和“预期行为”不冒充已执行的 DSH 实验。为了集中理解注册作用范围，本文假设 `read` 和 `bash` 已由宿主组合提供为两个可继承的工具，并使用原生工具展示模式（`native`）。不同 Profile 或 Preset 的实际工具集合可能不同。

## 1. 两个助手共享一份工具表，会发生什么？

先忘掉 DSH，想象我们写了一个极简工具表：

```text
工具表
  read  → 读取文件
  bash  → 执行 Shell 命令
```

在只有一个 Agent 时，这没有问题。模型收到工具定义，想读取文件就调用 `read`，想执行命令就调用 `bash`。

现在创建两个 Agent：

| Agent | 应该能做什么 | 不应该能做什么 |
| --- | --- | --- |
| 仓库侦察助手 A | `read` | `bash` |
| 发版助手 B | `read`、`bash` | 仍需遵守执行策略与沙箱限制 |

如果两个 Agent 都从同一个工具表拿全部工具，A 也会看到 `bash`。我们可以在系统提示词中写“不要执行命令”，但那只是对模型的指令；一旦模型仍然生成 `bash` 调用，Runtime 不应把提示词视为授权检查。

一种直观方案是创建两份工具表：A 一份、B 一份。对于两个手工创建的 Agent，这种方案也能工作。

但如果以后创建几十个 Agent，每个插件都向工具表注册新工具，我们就必须考虑：共享的工具怎样继承？某个 Agent 的专属工具放在哪里？这个 Agent 结束时，谁移除它的注册？

于是新的需求出现了：**共享同一套工具注册服务，但查询时带上“我正在为哪个 Agent 查询”的身份。**

## 2. 先不谈 Scope：让查询知道“是谁在看”

我们先把刚才的工具表想成两层：

```text
                  公共工具
                  read、bash
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
      侦察助手 A           发版助手 B
      只保留 read          保留 read、bash
```

这里的重点不是新建两套运行时，而是**同一份公共注册，根据调用者得到不同的工具视图**。A 和 B 都能继承公共工具，但 A 可以遮蔽它不应使用的部分。

这个“根据身份区分注册和查询作用范围”的机制，DSH 称为 **Scope（作用域）**。

不过我们还没有回答最关键的问题：这个身份从哪里来？

在 DSH 创建实际执行对象时，`ReactLoopAgent` 的构造函数里有两行代码：

**真实源码节选**｜[`packages/core/agent-loop/src/agent.ts`：`ReactLoopAgent` 构造函数](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/agent.ts#L88-L103)

```ts
this.scope = createScope(loopCtx, this)
this.ctx = this.scope.ctx
```

这里第一次遇到 `agent.ctx`：它不是第二个 Agent Loop，也不是一套新的模型客户端，而是**带着该 Agent 身份的注册上下文**。

`createScope(loopCtx, this)` 用当前 Agent 对象作为身份标识，生成一个专属的上下文。之后，插件如果通过这个 `agent.ctx` 注册工具或限制，就能表达“这项注册属于这个 Agent”。

注意它和上一章的 `ctx` 只差一个归属问题：普通 `ctx` 面向当前插件可访问的服务；`agent.ctx` 额外携带了当前 Agent 的作用域标识。具体的 Cordis 依赖解析不是本章重点。

## 3. 限制 A 的工具：为什么不是删除公共 `bash`？

现在我们来解决最初的问题。

我们不能直接从公共注册表删除 `bash`，因为 B 还需要它。真正需要的是：**在 A 看工具列表时隐藏 `bash`，B 的视图不变。**

DSH 提供 `tools.restrict()`。在本文假设的组合里，可以为 A 配置如下限制：

**教学配置示意**（使用真实 API；不是完整可运行的插件）

```ts
agentA.ctx.tools.restrict({ deny: ['bash'] })
```

这行代码没有卸载 `bash` 工具，而是把对 `bash` 的限制登记在 A 的作用域内。

为什么限制必须通过 `agent.ctx` 注册？我们直接去源码中核对。

**真实源码节选**｜[`packages/core/tools/src/index.ts`：`ToolRuntime.restrict()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L989-L1023)

```ts
const scope = scopeOf(this.ctx)
```

这一行先提取当前上下文携带的作用域标识；紧随其后的 `if (scope === undefined)` 分支会抛错，拒绝在全局上下文上调用 `restrict()`。

这不是偶然的参数校验。它在防止一个非常具体的失误：本想禁止 A 使用 `bash`，结果却在全局上下文里添加限制，让 B 也无法使用。

在这个版本中，`restrict()` 还会检查被限制的名称是否属于可继承的已知工具；传入不存在的名称会报错。多份限制会取交集，而不会互相放宽。

现在可以预测两份视图：

| 查询视角 | `read` | `bash` |
| --- | --- | --- |
| A 的工具视图 | 可见 | 不可见 |
| B 的工具视图 | 可见 | 可见 |

**这是根据固定版本源码推导的预期行为，不是本次启动 DSH 后采集的日志。**

## 4. 工具为什么会真正从 A 的模型请求中消失？

刚才我们只是登记了限制，还没有证明模型确实看不到 `bash`。

工具服务需要在生成模型可见工具列表时，重新计算该 Agent 的视图。看 [`ToolRuntime.view()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L1074-L1115)。它先取得公共工具和沿作用域链继承来的工具，再检查限制。

**真实源码节选**（仅截取其中的判断）

```ts
if (layers.every(layer => layer.admits(name))) visible.set(name, definition)
```

可以把它读成一句话：**只有沿途每一层都允许继承这个工具，它才进入当前 Agent 的可见集合。**

紧接着，`ToolRuntime.wireSchemas()` 在原生工具模式下从这份可见集合生成模型请求所需的工具定义；`ToolRuntime` 的构造过程把这个提供者接到 `systemPrompt` 的工具组装接口。见 [`wireSchemas()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L906-L931) 与 [工具提供者注册](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L763-L773)。

因此，A 请求模型时不会得到被遮蔽的 `bash` 定义，B 则仍然可以得到它。

但这里还留下了一个危险的漏洞：**模型看不到 `bash`，不等于它绝不会生成一个名字叫 `bash` 的调用。** 我们必须确认执行时也使用了同一份视图。

## 5. 模型硬要调用 `bash`，会不会绕过限制？

这是整个章节最值得核对的设计点。

如果 Runtime 只在构造模型提示词时过滤工具，却在执行时直接按全局工具名查找，那么 A 虽然看不到 `bash`，仍有可能通过一个显式调用执行它。

DSH 的 `ToolRuntime.get()` 不是从全局字典直接取函数，而是从**该作用域的可见工具集合**里查找。

**真实源码节选**｜[`ToolRuntime.get()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L1116-L1127)

```ts
return this.view(scope).visible.get(name)
```

这行代码很短，却让我们把前后两件事连上了：给模型展示工具、执行时解析工具，都从 `view(scope)` 这份视图出发。

真实派发时还会经过 `resolveExecution()`；找不到可执行的定义，就会转化为 `UNKNOWN_TOOL` 一类的工具错误，而不是调用被隐藏的工具函数。相关代码在 [`resolveExecution()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L1128-L1146) 与 [实际派发位置](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L1445-L1455)。

所以我们可以区分两种结果：

```text
A 正常请求 read   → 工具可见 → 继续进入执行流程
A 试图请求 bash  → 作用域中找不到 → 返回工具错误
B 正常请求 bash  → 工具可见 → 继续进入执行策略与工具执行流程
```

注意最后一行：B 看得见 `bash`，不代表任何命令都自动获准执行。它仍然要经过上一章研究的工具执行边界。

## 6. 到这里是否已经实现“只读权限”？还没有

一个容易误解的地方是：`restrict()` 限制的是**继承来的工具**，不是当前 Agent 自己注册的工具。

这个设计并非漏洞的偶然结果。比如一个子 Agent 可能需要专门的结构化结果提交工具；如果继承能力白名单把这个专属工具也遮蔽，子 Agent 就无法提交答案。

因此，`ToolRuntime.view()` 会在过滤继承工具后，再加入当前作用域自己注册的工具。这意味着：即使 A 限制了继承来的 `bash`，另一个可信插件仍可能给 A 注册一个专属的新工具；单靠 `restrict()` 不能表达“无论以后注册什么，都只能读取文件”的安全规则。

如果我们的要求是“**A 的执行策略必须拒绝所有非 `read` 工具**”，就需要在实际工具执行边界增加一条不能被后续监听器放宽的拒绝规则。DSH 对应的是 `tools.guard()`。

**教学配置示意**（真实 API，非完整插件）：

```ts
agentA.ctx.tools.guard(exec => {
  if (exec.name !== 'read') return '仓库侦察助手仅允许读取文件'
})
```

这个例子只演示工具名级别的执行限制；它不检查 `read` 的路径，也不构成文件系统隔离。真实只读环境还要限制工具实现和底层文件访问，不能依赖模型遵守提示词。

源码中 `guard()` 也按上下文登记，执行时会检查全局与匹配的作用域 Guard；只要有一个 Guard 返回拒绝原因，后面的 Guard 不能把它重新批准。参见 [`guard()` 和 `guardReason()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L1024-L1051)。

于是我们终于分清两个不同的需求：

- **作用域 / Restriction：** 决定某个 Agent 继承并看到哪些注册能力。
- **执行策略 / Guard：** 决定一次已经提出的工具调用是否还必须被拒绝。

Scope 本身服务于可信、同进程插件之间的注册隔离，并不是进程沙箱，也不能替代操作系统权限。DSH 的 [scope 包文档](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/scope/README.md) 对这个边界有明确说明。

## 7. Agent 结束后，这些专属限制由谁清理？

回到第四章。那里我们已经知道：插件加进去的监听器和注册，应该在所属生命周期结束时清理。

现在 A 的工具限制、Guard 和专属注册都关联到它的 `agent.ctx`。如果 A 结束了，这些资源不应该残留到另一个 Agent。

前面 `ReactLoopAgent` 构造函数中的 `createScope()` 除了赋予身份，还创建了一个**拥有这些注册资源的生命周期范围**。

在默认 Loop 的销毁路径里，可以找到：

**真实源码节选**｜[`packages/core/agent-loop/src/index.ts`：Agent 销毁流程](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts#L548-L557)

```ts
await machine.scope.dispose()
```

它出现在取消执行并等待 Agent 空闲之后。该 Scope 负责撤销归属于它的注册：这不仅解决“谁能看到”的问题，也解决“谁负责移除”的问题。

到这里，我们可以把上一章和这一章连起来：上一章 Cordis 管理**插件注册的生命周期**；这一章 Scope 进一步让**某个 Agent 的注册既有可见边界，又有清理归属**。

## 8. 回看：我们到底增加了哪一层设计？

最开始，所有 Agent 共用一份工具表：

```text
Agent A ─┐
         ├── 共享工具表：read、bash
Agent B ─┘
```

有了“两个 Agent 能力不同”的需求后，才逐步变成：

```text
                     共享工具注册表
                           │
             根据 Agent 身份计算视图
                    ┌──────┴──────┐
                    ▼             ▼
                 Agent A       Agent B
                 read          read、bash
                    │             │
                 执行检查       执行检查
                    │             │
             允许或拒绝       允许或拒绝
```

**Scope 不是为了让架构显得更复杂，而是避免“工具注册是全局的”直接推导出“所有 Agent 的能力都必须相同”。**

我们已经沿源码证实了三件事：Agent 创建时有专属上下文；工具展示按该上下文计算可见集合；工具执行再次依据调用者的可见集合解析定义。至于目录权限、命令隔离、沙箱逃逸等真正的安全问题，不属于 Scope 自己能够保证的范围。

### 如何核对，而不靠记忆函数名？

克隆仓库并检出固定 Commit 后，可以依次搜索下面几个最小入口：

```bash
rg -n 'createScope\(loopCtx, this\)' packages/core/agent-loop/src/agent.ts
rg -n 'restrict\(filter|private view\(|get\(name: string' packages/core/tools/src/index.ts
rg -n 'scope.dispose\(' packages/core/agent-loop/src/index.ts
```

本章已对以上源码与官方文档做静态核对，**没有在本次环境启动完整 DSH 验证两 Agent 的运行轨迹**。上文工具可见性表格属于可进一步写集成测试验证的预期结果。

## 下一篇：工具可见性不同了，但文件到底在哪儿读取？

现在 A 和 B 可以拥有不同的工具视图。然而，只要 `read` 工具内部直接读取本机磁盘，A 就只能观察运行 DSH 的那台机器。

如果明天仓库放到了远程工作区，难道要重写 `read`、`edit`、`bash`，以及所有调用它们的 Agent 代码？

下一篇，我们从同一个 `read('package.json')` 请求出发，研究 DSH 怎样把**“需要读取文件”**和**“在哪里、由谁实际读取文件”**拆开。这时才有理由引入文件系统的能力接口、Provider 与 Sandbox。
