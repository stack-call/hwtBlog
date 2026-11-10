---
title: "DSH 源码精读（二）：为什么模型说要调用工具，不代表工具就能执行？"
sidebar_label: "二、工具请求为什么要过执行边界？"
sidebar_position: 3
slug: dsh-source-02-tool-boundary
description: "沿一次 read 工具请求，理解 DeepSeek Harness 为什么把模型请求、工具定义、执行许可和结果反馈分开。"
tags: [DeepSeek-Harness, Agent, 源码精读]
---

上一章，我们看到“仓库侦察助手”要回答项目怎样启动，通常会先读取 `package.json`，再让模型根据读到的内容回答用户。

可是上一次，我们把“读取文件”画成了一个简单的箭头：

```text
模型要求读文件 → 读取成功 → 把内容交回模型
```

这条路径漏掉了一个重要问题：**模型提出的工具请求，为什么一定可信？**

如果模型给出的参数不是文件路径，而是数字呢？如果用户只允许读文件，模型却请求写入文件呢？在这些情况下，直接按模型的输出调用函数，就不再是一个足够稳妥的设计。

这篇不研究全部工具系统，只沿一次文件读取请求，看看 DSH 如何让“模型想执行”与“程序准许执行”成为两件事。

<!-- truncate -->

> [!NOTE] 源码版本
> 仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
>
> 固定 Commit：[`5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3aa172535a7967b06ff95d9cbfe536a)，与第一章一致。
>
> 本文所有标为“真实源码节选”的内容均核对这一版本。示例中的模型输出和执行轨迹是**教学构造与源码推导**，没有声称启动 DSH 完成了真实集成测试。

## 1. 先把要执行的事情说清楚

仍然是那个只读项目分析助手。项目有这样一个文件：

```json title="package.json（教学示例）"
{
  "scripts": {
    "dev": "vite"
  }
}
```

用户问：“这个项目怎么启动？只看文件，不要修改。”模型决定读 `package.json`。

第一章为了便于理解，把工具叫作 `read_file(path)`。现在进入真实源码，需要把这个教学名字换成 **DSH 实际注册的 `read` 工具**。它接受的路径参数叫 `file_path`，因此在默认原生工具调用模式下，我们要追踪的请求可以表示为：

```json title="模型可能生成的工具参数（教学构造，非实测）"
{"file_path":"package.json"}
```

这里的 JSON 不是程序已经读取成功的证明。它只是模型表达的一项**请求**：请替我读取这个路径。

为了执行这项请求，DSH 首先必须知道两件事：`read` 究竟对应什么功能，以及传什么样的参数才算有效。

## 2. 工具需要先有一份“约定”

我们很容易写一个函数 `read(path)`，但模型并不会天然知道这个函数叫什么、参数是什么。于是需要向模型提供一份可理解的工具说明。

DSH 的文件工具在 [`packages/fs/tool-fs/src/read.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts#L61-L74) 注册。先只看其中几行：

**真实源码节选：**

```ts
name: 'read',
description: 'Read a UTF-8 text file and return line-numbered content.',
parameters: {
  file_path: { type: 'string', required: true, description: 'Path to read, resolved by the filesystem backend.' },
  offset: { type: 'number', description: '1-based first line to return. Defaults to 1.' },
  limit: { type: 'number', description: `Maximum number of lines to return. Defaults to ${caps.limit}.` },
},
```

先不要关心 `offset`、`limit`。对眼前的任务而言，最关键的是：**工具叫 `read`，而且必须给出字符串形式的 `file_path`。**

这些字段并不是给 TypeScript 编译器看的装饰。DSH 的 `ToolRuntime` 会从注册的工具定义中提取 `name`、`description` 和 `parameters`，组织成模型可见的工具说明；真正执行函数以及其他内部信息不会一并送给模型。[参见 `schemaOf()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L1172-L1184)。

因此，当模型提出 `read({"file_path":"package.json"})` 时，程序至少可以按这份约定判断：它请求的是已知工具，参数形状也看起来合理。

但是，“看起来合理”还不等于参数一定正确。

## 3. 如果模型把路径写成数字呢？

假设模型这次输出：

```json title="错误参数（教学构造）"
{"file_path": 42}
```

如果我们把它直接送进文件读取函数，读取代码就必须自己处理这种不合法输入。工具一多，每个作者都要反复写类似的基础校验，很容易遗漏。

因此 DSH 的 `defineTool()` 不只是帮我们声明参数，还在实际执行工具主体之前做验证。

在 [`packages/core/tools/src/schema.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/schema.ts#L549-L553) 可以看到：

**真实源码节选：**

```ts
async execute(args: unknown, exec: ToolRunContext): Promise<JsonValue> {
  const violations = validate(args)
  if (violations.length > 0) throw new ToolArgsError(violations)
  return userExecute(args as InferArgs<S>, exec) as Promise<JsonValue>
},
```

这里把代码分成两部分就能看懂：先用 `validate(args)` 检查参数；只有没有错误时，才会调用作者真正写的 `userExecute(...)`。

所以，输入 `{"file_path":42}` 时，**实际读取文件的函数不会被调用**。错误会被包装为工具执行失败，错误类别是 `INVALID_ARGS`。这不是模型自动纠正了自己，而是 Runtime 在函数执行前挡住了不合法输入。[错误类型定义](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/schema.ts#L427-L436)。

注意一个容易误会的细节：这段参数验证发生在 `defineTool` 包裹的工具执行函数内。稍后要看的运行前策略检查，可以先于这段验证发生。因此不能简单地说“所有策略都只会收到已经通过参数校验的输入”。

现在我们已经解决了“输入形状不可靠”的问题。可是，参数完全合法也不代表它有执行权限。

## 4. 参数合法，为什么仍然可能不允许执行？

用户刚才明确说了：只读文件，不要修改。

假设模型现在提出的是 `write` 工具调用。它可能给出了完整、格式正确的文件路径和新内容。如果程序只负责参数验证，这次写入就可能顺利通过。

我们缺的不是又一条参数规则，而是一个不同的问题：**即使这个调用格式正确，当前任务是否允许它执行？**

DSH 在真正执行工具主体前，安排了一个策略决策位置。先看 [`ToolRuntime.prepareExecution()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L1372-L1416) 的这一小段：

**真实源码节选：**

```ts
const gate = await this.ctx.waterfall(
  carrier, 'tools/pre-execute', exec,
  () => Promise.resolve<PreToolDecision>({ kind: 'allow' }),
)
```

这里的 `waterfall` 暂时只要理解为：**运行已注册的检查逻辑，并得到一项决策**。返回结果可以是允许、拒绝，或者请求批准。我们还不需要学习它怎样注册监听器、按什么顺序调用插件，那是 Cordis 章节的主题。

这次检查还没直接访问文件。它发生在工具主体启动前，允许外部策略决定是否继续。

在同一个函数中，DSH 随后确定最终拒绝原因：

**真实源码节选：**

```ts
const denialReason = decision.kind === 'allow'
  ? this.guardReason(exec)
  : decision.reason
```

这里有个重要的设计动作：即使前面的可扩展检查返回“允许”，仍可以再经过 `guardReason()` 做最终限制。只要得到拒绝原因，就形成错误结果，而不是继续执行文件操作。

**这并不表示 DSH 默认会自动把自然语言中的“不要修改”变成权限规则。** 要真正限制写入，必须装配能够执行这项规则的策略，或者在更底层的文件系统环境中限制写入权限。Prompt 是对模型的指导，不等于执行权限。

我们也不要把所有安全能力都归到这一层。例如读取 `../secret.txt` 是否越界，最终需要由实际的路径解析、文件系统后端及其权限配置正确处理，不能仅靠工具名称或者模型参数的 JSON 格式判断。[`read-target.ts` 中路径会交给 `ctx.fs.resolve()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read-target.ts#L17-L31)。

到这里，才可以说：一次工具请求已经有机会被批准进入真正执行阶段。

## 5. 被允许以后，哪一行才真的执行文件读取？

前面我们一直在讨论注册、参数和决策，都还没真正碰文件。

那么，真正执行工具的地方在哪里？

在 [`ToolRuntime.dispatchToolBody()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts#L1438-L1465) 里，沿正常路径只需要看下面几行：

**真实源码节选：**

```ts
const tool = this.resolveExecution(exec.name, exec.agent, exec.parent !== undefined)
if (!tool) throw new ToolNotFoundError(exec.name)
state.bodyInvoked = true
const returned = await tool.execute(exec.arguments, exec)
```

`resolveExecution()` 负责找到当前调用确实能执行的工具定义；如果找不到，就产生未知工具错误。真正把参数交给工具作者代码的是最后一行：`tool.execute(...)`。

对于我们的 `read`，它最终会调用文件系统服务读取文本。在 [`read.ts` 的 execute 部分](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts#L127-L152) 可以找到：

**真实源码节选：**

```ts
const { target, info } = await resolveRegularReadTarget(ctx, exec, input.filePath)
```

这行先解析并检查读取目标；之后，较小的文件可以通过 `ctx.fs.readText(...)` 读取，较大或大小未知的文件则走流式读取，再把内容整理为带行号的结果。

所以本章终于能把“模型说读文件”解释清楚：**模型给出的只是工具名和参数，是否执行、执行哪个实现、从哪里读文件，都由 Runtime 和它调用的服务决定。**

这也说明了为什么不能把一个有效的 Tool Call 等同于“已经有权限执行的操作”。

## 6. 如果失败，模型下一步会看到什么？

回想第一章：工具结果要回到 Session，下一次模型请求才能看到反馈。现在我们可以更准确一点——回去的结果不仅可能是文件内容，也可能是拒绝执行、参数错误或工具内部错误。

DSH 的工具执行返回的是带有 `isError` 标记的结果。Agent Loop 在工具调用前后，分别记录 `tool/call` 和 `tool/result`；记录结果时还会保留与这次调用的关联。[`agent-loop/src/tool-calls.ts` 的 `appendToolCall()` / `appendToolResult()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/tool-calls.ts#L244-L270)。

因此，可以从源码推导出两条不同的正常可处理路径：

```text
路径 A：模型提出 read({file_path: "package.json"})
    → 执行检查通过
    → 文件读取成功
    → 写入工具结果
    → 下一 Step 可以根据文件内容回答

路径 B：模型提出 read({file_path: 42})
    → 参数检查失败
    → 不进入 read 工具的实际读文件主体
    → 写入错误工具结果（INVALID_ARGS）
    → 下一 Step 可以看到失败反馈并决定怎么处理
```

以上是依据固定版本源码得到的**预期轨迹**，不是本次实际启动 DSH 记录的日志。模型收到错误后会不会自行修正，取决于模型和下一次请求的上下文，Runtime 不保证它一定做对。

## 7. 回头看：工具为什么不能只是一个函数？

我们最初以为只需要：

```text
模型 → read 文件
```

现在已经知道，真实运行需要多回答几个问题：它是不是已注册的工具？参数是否符合约定？这次任务允许它执行吗？执行主体成功或失败后，如何把结果反馈给模型？

因此，按照本章已经读过的代码，我们可以把原来的小箭头扩展为：

```text
模型的工具请求
      ↓
找到当前可用的工具
      ↓
运行前策略检查（可能拒绝）
      ↓
调用工具主体（参数校验发生在这里的包装层）
      ↓
成功结果或错误结果
      ↓
记录 tool/result → 下一 Step
```

这不是要求读者立即记住 `ToolRuntime` 的每个方法。只需要抓住 **“请求、准入、执行、反馈”是不同的动作**，就已经理解了这个执行边界为什么有必要存在。

本章刻意没有展开多个工具并行执行、工具的作用域、PTC 模式、完整策略插件的生命周期。因为在理解一次普通读取请求以前，这些细节只会掩盖主线。

最后还剩一个自然问题：既然工具的成功和失败都会被记录，我们能不能只把“模型最终看到的 messages 数组”保存下来？为什么 DSH 还要专门区分 `tool/call`、`tool/result` 等会话事实？

**下一篇，我们就从一次失败的读取请求出发，研究 DSH 为什么需要 Session Event Log。**

---

### 本章核对的源码

- [`packages/fs/tool-fs/src/read.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts)：`read` 工具的真实声明、参数和执行主体。
- [`packages/core/tools/src/schema.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/schema.ts)：`defineTool()` 的参数验证。
- [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts)：工具可见性、策略检查与执行调度。
- [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/tool-calls.ts)：工具调用身份与结果如何回到 Session。
