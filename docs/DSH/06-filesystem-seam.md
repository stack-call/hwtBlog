---
title: "DSH 源码精读（六）：换个文件系统，为什么不必重写 read 工具？"
sidebar_label: "六、换个文件系统为什么不必重写 read"
sidebar_position: 7
slug: dsh-source-06-filesystem-seam
description: "从仓库侦察助手的一次 read 请求出发，沿 DSH 的 tool-fs、ctx.fs、fs-local 和 fs-sandbox 源码，理解工具与执行环境如何解耦。"
tags: [DeepSeek-Harness, Agent, 源码精读]
---

前几篇，我们已经看到：模型发出工具请求，不等于工具立刻执行；Runtime 还要找到对应工具，检查是否允许执行，并把结果记入 Session。上一章又让两个 Agent 在同一进程中拥有不同的工具视图。

现在，先不增加新的 Agent，也不更换模型。我们只让“仓库侦察助手”换一个运行条件：**同一句 `read` 请求，在不同的文件执行环境里，应该仍然是同一个工具。**

这听上去只是实现细节，却能解释 DSH 为什么把文件工具和真正的文件系统实现放在不同的包里。

<!-- truncate -->

> [!NOTE] 源码基线
> 仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
>
> 固定 Commit：[`5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3aa172535a7967b06ff95d9cbfe536a)。本文引用的函数及文件路径均核对该版本，不与后续 `master` 混用。
>
> **真实源码节选**只保留当前推论必要的几行；**教学示意**只表达输入、输出和因果关系。本文完成的是静态源码核对，**未执行 DSH 端到端实验**。

## 1. 明明只是读文件，为什么需要多个包？

我们仍然让用户问：

> 看看这个仓库怎么启动。先读文件，不要修改任何东西，也不要执行命令。

模型产生的工具请求很简单：

```json title="模型发出的工具参数（示意）"
{"file_path":"package.json"}
```

假设这个工具只服务于我们电脑上的一个项目，那么最简单的实现就是：拿到 `file_path`，用 Node.js 的文件 API 读取它，返回文本。这种做法在单一环境中没有任何问题。

但现在考虑同一个工具在两个地方运行：

- **场景 A：**项目位于本机工作目录；工具直接读取本地文件。
- **场景 B：**产品将文件操作交给另一套执行实现，可能需要路径映射、限制修改，甚至通过别的执行环境访问文件。

用户没有改变提问，模型也不应该重新学习一套叫 `remote_read` 或 `sandbox_read` 的工具参数。它仍然只需要说：“读取 `package.json`。”

如果 `read` 工具内部写死了 `node:fs`，更换执行环境时就必须修改工具本身。读取规则、展示格式、模型可见的参数，都会被迫和底层存储方式绑在一起。

于是我们真正需要分开的，不是两个工具，而是两项职责：**决定读什么、怎么向模型呈现**，以及**决定到哪里、通过什么方式真正读取**。

先带着这个问题去看 `read` 的真实源码。

## 2. `read` 工具并没有直接读磁盘

找到 [`packages/fs/tool-fs/src/read.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts#L696-L718)。这里注册给模型的工具名是 `read`，必填参数是 `file_path`。这属于“模型看得见的接口”。

但真正执行时，关键的代码不是 `node:fs.readFile`。

**真实源码节选**（同文件 `execute()`，约第 823—840 行）：

```ts
const { target, info } = await resolveRegularReadTarget(ctx, exec, input.filePath)
```

紧接着，它根据文件大小选择一次读取还是流式读取：

```ts
const chunks = info.size === undefined || info.size >= caps.streamMinSize
  ? await ctx.fs.streamText(target, exec.signal)
  : [await ctx.fs.readText(target, exec.signal)]
```

两段代码第一次出现时，只需要理解三件事：

1. `input.filePath` 是从模型参数 `file_path` 校验、整理得到的路径。
2. `target` 代表文件系统已经识别出的目标，并不要求工具知道目标实际存放在哪里。
3. 真正取内容的是 `ctx.fs.readText()` 或 `ctx.fs.streamText()`。**`read` 工具没有自己打开本地文件。**

这里的 `ctx.fs` 可以先理解成一项服务：它对外承诺“我能解析路径、查元数据、读取文本”，至于内部用本地磁盘还是别的实现，调用者不需要知道。

此时，调用链只比以前多了一层：

```text
模型：read(file_path="package.json")
              ↓
      read 工具整理请求
              ↓
        ctx.fs 读文件
              ↓
      read 工具格式化结果
              ↓
           模型
```

注意这里的格式化结果：DSH 的 `read` 还负责行号、窗口截取和输出长度限制。这些属于模型如何使用文件内容，不属于文件在磁盘上如何打开。把它们留在工具侧，恰好解释了为什么不能把整个工具换成某个本地磁盘 API。

## 3. 为什么不直接把路径字符串传给 `readText()`？

你可能会问：既然 `ctx.fs` 已经能读文件，为什么不直接写 `ctx.fs.readText("package.json")`？前面还多出了一个陌生的 `target`。

想象用户打开了另一个项目，两个项目里都叫 `package.json`。如果我们只有这个相对路径字符串，就不知道它对应哪一个实际文件。对于不同后端，相同路径的身份表达也可能完全不同。

因此，`read` 先进行一次“路径解析”，再检查文件状态。

打开 [`packages/fs/tool-fs/src/read-target.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read-target.ts#L268-L297)。

**真实源码节选**：

```ts
const target = await ctx.fs.resolve(requestedPath, sessionResolveOptions(exec, requestedPath))
const info = await ctx.fs.stat(target, exec.signal)
```

第一行把模型提供的路径交给当前文件系统解析；第二行查询该目标的元数据。后面的源码会检查目标是否存在、是否为普通文件，不符合要求就返回文件系统错误。只有检查成功才把 `target` 与 `info` 交给读取函数。

这里还有一个和前面 Session 章节相连的细节：`sessionResolveOptions()` 会优先使用本次 Agent 会话的工作目录。相关实现在 [`packages/fs/tool-fs/src/session-cwd.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/session-cwd.ts#L293-L335)。

所以，两个会话都请求 `package.json`，不意味着它们要读同一个文件。**模型只描述相对路径；会话提供工作位置；后端负责把它解析为真实目标。**

这个目标在 DSH 中叫 `FsTarget`。它包含面向展示的路径 `displayPath`，以及后端自己解释的 `targetKey`。源码专门提醒使用方：`targetKey` 是不透明的，不能假定它是本地绝对路径。[查看 `FsTarget` 类型及注释](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs/src/types.ts#L590-L680)。

为什么要这么谨慎？本地实现可以用类似真实路径的标识；另一种实现完全可以使用文件 ID。**一旦工具偷偷解析 `targetKey`，就又把自己绑定回某一种后端。**

至此，我们已经得到完整的读取意图：

```text
模型提供 file_path
        ↓
按会话工作目录解析为 FsTarget
        ↓
检查目标存在且为普通文件
        ↓
通过 ctx.fs 读取
        ↓
格式化供模型阅读的文本
```

还剩最关键的疑问：`ctx.fs` 究竟是谁实现的？

## 4. 这时才需要认识 FileSystem：只规定能力，不规定实现

去看 [`packages/fs/fs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs/src/index.ts#L870-L934)。

**真实源码节选**（同类中两处关键声明）：

```ts
export abstract class FileSystem extends Service {
  constructor(ctx: Context) {
    super(ctx, 'fs')
  }
```

```ts
abstract resolve(path: string, opts?: { cwd?: string; signal?: AbortSignal }): Promise<FsTarget>
```

[`readText` 的抽象方法](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs/src/index.ts#L1048-L1079) 同样只声明“传入目标，返回 UTF-8 文本”。

这几行先不需要研究 Cordis 的 `Service` 内部。它们表达的设计决定已经足够清晰：**所有文件系统实现都要满足同一套操作约定；工具只调用这个约定。**

我们现在可以把三个角色说清楚了：

- `read` 是**使用者**：接收模型请求，决定如何展示结果。
- `FileSystem` 是**能力约定**：声明路径解析、元数据查询和读取等操作。
- 具体文件系统类是**实现者**：决定怎么完成这些操作。

DSH 的架构文档把这样的可替换连接点称为 *Capability Seam*。这个名字是我们已经理解设计之后才需要记住的，含义就是：**调用方依赖稳定的能力接口，而不是依赖某一种实现。**

## 5. 看一下真正访问本地磁盘的是谁

打开 [`packages/fs/fs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs-local/src/index.ts#L832-L874)。

这里的 `LocalFileSystem` 继承了 `FileSystem`，而且实现了刚才约定的方法。例如：

**真实源码节选**：

```ts
override async readText(target: FsTarget, signal?: AbortSignal): Promise<string> {
  return readWholeText({ displayPath: target.displayPath, targetKey: target.targetKey }, signal)
}
```

[查看上下文](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs-local/src/index.ts#L969-L977)

现在回到用户的 `read("package.json")` 请求，可以解释每一步是谁完成的：

| 动作 | 负责的位置 | 为什么由它负责 |
|---|---|---|
| 声明 `file_path` 和输出形式 | `tool-fs/read.ts` | 与模型交互有关 |
| 将相对路径变成文件目标 | `ctx.fs.resolve()` | 与当前后端的文件身份有关 |
| 检查类型、读取 UTF-8 内容 | `ctx.fs.stat/readText()` | 与实际文件操作有关 |
| 加行号、限制返回窗口 | `tool-fs/read.ts` | 与模型阅读体验和上下文成本有关 |

这就是本章真正的架构分界线：不是文件工具自己实现了一套可扩展系统，而是**工具把所有必须依赖执行环境的动作，委托给了文件系统服务**。

## 6. 真正更换一次实现：不改工具，换掉文件系统提供者

只知道接口存在，还不足以说明这个抽象有实际价值。我们需要一个能在固定版本中找到的替换例子。

DSH 在这一 Commit 里除了本地文件系统，还实现了 [`SandboxedFileSystem`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs-sandbox/src/index.ts#L544-L576)。它继承本地文件系统，但为文件修改加上按次检查的策略。

这解决了另一个真实需求：仓库侦察助手主要读文件，我们希望部署时可以限制它在同一环境里进行写入或编辑，而不必把 `read`、`write`、`edit` 各自改写一遍。

**真实源码节选**（`packages/fs/fs-sandbox/src/index.ts`）：

```ts
export class SandboxedFileSystem extends LocalFileSystem {
  static inject = ['sandboxPolicy']
```

以及它对写入操作的覆盖：

```ts
return super.writeText(await this.checkedTarget(target, sandboxPolicy), content, expected, signal)
```

[查看 `writeText()` 的上下文](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs-sandbox/src/index.ts#L601-L615)

现在才看得出“替换实现”的收益：工具包里的 `read` 参数没有改变，输出格式没有改变，Agent Loop 也不需要认识 `LocalFileSystem` 或 `SandboxedFileSystem`。装配时选择谁提供 `ctx.fs`，就决定了文件操作通过哪套实现完成。

**但这里必须把边界说准确。** 此版本的 `fs-sandbox` 主要约束**写入和编辑**：`read-only` 模式拒绝修改；普通读取继承本地实现，仍然可以读取文件。它不是“只能读取工作目录”的沙箱，更不是操作系统级的进程隔离。[查看源码中的安全边界说明](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs-sandbox/src/index.ts#L463-L509)。

所以这不是在证明“换上 Sandbox 后所有文件访问都安全了”，而是在证明一件更具体的事：**修改后端的执行与策略实现，不必重写模型可见的工具接口。**

同理，未来如果要使用真正的远程文件系统，也应当由满足 `FileSystem` 契约的另一种提供者完成；不能仅凭接口可替换，就声称本章固定 Commit 已经有可用的 SSH 文件系统实现。

## 7. 用源码复查，而不是凭图相信这个设计

本章没有运行 DSH，也没有用一个自制 Fake FileSystem 冒充框架测试。你可以在本地检出固定版本后，按下面的顺序**静态验证**我们刚才的结论：

```bash
# 前提：已经 clone 了 deepseek-harness 仓库
git checkout 5dda764ed3aa172535a7967b06ff95d9cbfe536a

# 工具是不是通过 ctx.fs 访问文件？
rg -n 'resolveRegularReadTarget|ctx\.fs\.(readText|streamText)' packages/fs/tool-fs/src

# 是否存在抽象约定和两个不同实现？
rg -n 'abstract class FileSystem|class LocalFileSystem|class SandboxedFileSystem' packages/fs

# fs-sandbox 究竟覆盖了哪些操作？
rg -n 'override async (readText|writeText|editText)' packages/fs/fs-sandbox/src
```

这些命令是**读者可执行的检查步骤**，不是本次已经执行过的终端记录。源码已经核对，但没有完成真实 DSH 的端到端行为测试。

如果要做进一步的真实实验，可以把两个配置分别接到相同的只读仓库任务上：保持 `dsh-tool-fs` 不变，先使用本地文件系统提供者，再使用附带相应策略服务的沙箱文件系统提供者。观察两者的 `read` 参数、读取结果格式是否一致；然后另设一个写入测试，验证 `read-only` 模式下底层写入会被拒绝。请在专用测试目录中进行，不能把“模型答了不能写”当成执行层真的拒绝写入的证据。

## 8. 回头看：这一章只多出了哪一个设计？

前面理解的 Agent 执行链是：

```text
模型 → 工具 → 结果 → 模型
```

这一章没有改变模型如何选择工具，也没有改变 Agent Loop 何时进入下一步。我们只是打开“工具执行”这一格，发现里面还应该分成两个职责：

```text
模型
  ↓
read 工具（参数、校验、结果展示）
  ↓
ctx.fs（稳定的文件系统能力约定）
  ↓
当前提供者（本地实现 / 带写入限制的实现）
  ↓
文件内容返回 read 工具，再返回模型
```

现在你应该能自己解释 DSH 为什么把 `tool-fs`、`fs`、`fs-local` 和 `fs-sandbox` 分开：**它们分别面对不同方向的变化。** 模型工具的参数和呈现方式可能变化，底层文件操作的实现可能变化，执行策略也可能变化；让每次变化都不必牵动所有层，才是这条分界线的价值。

代价同样真实：服务接口需要维护，装配时必须确保提供者存在，不同提供者的行为也需要一致性测试。对于只在本地运行的小脚本，直接调用本地文件 API 仍然可能是最简单的选择。

## 下一篇：这些能力到底是谁装配到一起的？

我们已经知道 DSH 可以让工具依赖 `ctx.fs`，而不是依赖 `LocalFileSystem`。但新的问题出现了：**同一个进程启动时，到底是谁决定装入本地文件系统、沙箱文件系统、哪些工具，以及哪一种 Agent Loop？**

如果每个使用方都要手工注册十几个服务，这套可替换架构就很难真正复用。

下一篇，我们再研究 Profile、Bundle 和启动装配。到那时，整张 Runtime 架构图中的“谁提供能力、谁消费能力”就会自然连起来。

---

**本章固定版本源码索引**：

- [`tool-fs/src/read.ts`：工具如何读取并渲染内容](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts#L823-L871)
- [`tool-fs/src/read-target.ts`：解析路径并检查普通文件](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read-target.ts#L268-L297)
- [`fs/src/index.ts`：`FileSystem` 能力约定](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs/src/index.ts#L870-L934)
- [`fs-local/src/index.ts`：本地文件系统实现](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs-local/src/index.ts#L904-L977)
- [`fs-sandbox/src/index.ts`：替换提供者及修改限制](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/fs-sandbox/src/index.ts#L544-L615)
