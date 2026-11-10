---
title: "DSH 架构精读：从普通模块到 Cordis，再到可扩展的 Agent Runtime"
sidebar_label: "前传：从普通模块到 Cordis 与 Agent Runtime"
sidebar_position: 1
slug: dsh-cordis-plugin-architecture
description: "从一个项目侦察助手出发，循序推导插件、Context、Service、inject、Effect 和事件机制，再沿真实源码解释 DSH 怎样利用 Cordis 提供、扩展和消费 Agent 能力。"
tags: [DeepSeek-Harness, Cordis, Agent, 源码精读]
---

读过 DeepAgent 的源码后，我们大致已经知道一个 Agent 怎样调用模型、执行工具、接收工具结果并继续运行。于是初看 DeepSeek Harness（简称 DSH）时，很容易有这样的印象：**DeepAgent 通过 Middleware 扩展执行过程，DSH 则通过插件扩展执行过程；两者似乎只是扩展 API 的风格不同。**

这句话只说对了一部分。

因为 DSH 的插件不只是在模型或工具的执行位置插入代码。它还可以**提供整个服务、声明与其他服务的依赖、独立安装与卸载，并由不同的应用配置组合起来**。而执行这些“模块管理动作”的，并不是 DSH 的 Agent Loop，而是一个与 Agent 无关的通用框架——**Cordis**。

要理解 DSH，我们需要先回答一个更朴素的问题：一个本来可以直接调用函数的程序，究竟在什么条件下，需要一个能够管理插件的运行时？

本文不先画 DSH 的完整架构，不先列出全部包，也不从 `ctx.tools` 这样的 API 起步。我们沿着同一个“项目侦察助手”逐步增加约束：最初它只是一个读取仓库信息的普通程序，后来要支持独立安装功能、等待依赖、撤销功能，最后再把这些能力交给 Agent 使用。

<!-- truncate -->

> [!NOTE] 源码基线与阅读约定
> - **仓库**：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
> - **固定 Commit**：[`5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/commit/5dda764ed3aa172535a7967b06ff95d9cbfe536a)
> - **Cordis 源码**：DSH 仓库 `vendor/cordis` 引入的框架版本；本文只对照上述 Commit 中的 Cordis 文档及 DSH 使用方式。
> - **[概念伪代码]**：自己设计的最小方案，用来解释为什么需要下一层；**不能冒充 DSH 或 Cordis 实现**。
> - **[真实源码节选]**：来自固定 Commit，截取与当下问题直接相关的行；每处附上源码位置。
> - **[源码推导轨迹]**：依据实现/官方教程推导的预期运行行为；**未在本文环境中启动 DSH 或 Cordis**，不写成实测结果。
>
> 最核心的边界：**Cordis 负责如何安装、连接和清理模块；DSH 负责模块提供的 Agent 能力究竟是什么、怎样执行。**

## 1. 起点：一个普通程序已经能读懂项目

假设用户想知道一个前端仓库如何启动。我们先不要引入模型，让程序直接读取 `package.json` 中的脚本：

```ts title="概念伪代码：最初的仓库侦察程序"
async function inspectProject() {
  const pkg = await readPackageJson('./package.json')
  return pkg.scripts?.dev
}
```

对于一个只读本地仓库的固定应用，这样写完全合理。文件读取逻辑甚至可以直接使用 Node.js 的 `fs` API。如果唯一目标是输出 `vite` 或 `npm run dev`，到这里就已经够用了。

现在我们要增加一种功能：每次读取仓库文件时，记录一条审计信息。

最直觉的写法是在 `readPackageJson()` 内部直接输出日志。但我们希望审计能力由另一个模块维护，能够独立安装或关闭，而不必修改文件读取函数。于是第一步并不是创建什么插件框架，只是把**功能实现**和**安装动作**分开。

```ts title="概念伪代码：独立安装审计功能"
function installAudit(reader: FileReader) {
  reader.onRead((path) => console.log('read:', path))
}
```

这里的 `onRead()` 是我们**假设 FileReader 自己提供**的注册接口，不是 Cordis API。它的意义很简单：审计模块只管声明“读取时我要收到通知”，不负责文件读取的实现。

小应用用普通安装函数就能解决问题。**现在没有理由因为 DSH 使用 Cordis，就认定自己也必须使用 Cordis。**

但接下来改变的是应用的规模与装配方式，而不是审计功能本身。

## 2. 如果功能模块由配置安装，主程序还认识每个模块吗？

最初主程序明确写着：

```ts title="概念伪代码：主程序手工安装"
const reader = new FileReader()
installAudit(reader)
installSearch(reader)
```

这种方案的成本是：每安装一种新功能，都要改动主程序的装配代码，而且 `installAudit` 和 `installSearch` 需要什么参数，主程序必须提前知道。

现在我们增加一个更具体的要求：**第三方团队可以发布新模块，用户通过配置启用它，而不是让我们为每个新模块写一条安装语句。**

为此，一个很小的约定就够了：让所有功能模块都接收相同的安装入口。

```ts title="概念伪代码：统一安装约定"
const environment = { reader, logger }

function installAudit(environment: typeof environment) {
  environment.reader.onRead(path => environment.logger.info(path))
}

for (const install of configuredModules) {
  install(environment)
}
```

现在通用加载器不需要了解模块内部细节，只需要调用它们约定好的入口。这就是引入统一运行环境对象的理由。我们将 `environment` 称为 **Context（上下文）**，通常简写成 `ctx`。

注意顺序：**先有“统一安装入口”的需求，才出现 `ctx` 这个参数名。**

但这个方案仍然有一个局限：`environment.reader` 和 `environment.logger` 都是主程序提前创建好的。如果连文件读取模块自己都要通过配置安装呢？

在引入下一机制之前，我们先看 Cordis 怎样落实刚刚的“统一安装入口”。

### 从教学方案进入 Cordis：插件的最小形式

**[Cordis 真实教程代码]**：`docs/cordis-tutorial/01-first-plugin.md`。

```ts
import type { Context } from '@deepseek-ai/cordis'

export const name = 'hello'

export function apply(ctx: Context) {
  console.log('hello from my first plugin')
}
```

[查看官方教程与完整上下文](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/01-first-plugin.md)

这段代码并不涉及 Agent。我们只要认识两个约定：

- 模块导出 `apply(ctx)`，表示“插件激活时，我要安装的功能在这里”；`ctx` 由 Cordis 提供。
- `name` 是插件的名称元信息；它不是模型工具名，也不负责执行什么业务。

把它配置进教程的 `cordis.yml`：

```yaml
- name: './hello.ts'
```

官方教程使用 `vendor/cordis/bin.js` 创建根 Context、挂载 Loader，并让 Loader 读取这个配置。**Loader 负责发现、加载、挂载模块；`hello.ts` 只负责自己被激活后做什么。** 在它被激活前，我们不需要给它任何 Agent 能力。

现在已经知道 Cordis 的第一项职责：**提供模块安装运行时，让“安装哪些功能”不再硬编码在业务启动函数里。**

但是别把这件事夸大：Cordis 不会自动理解 `FileReader`，也不会因为某模块叫 `hello` 就赋予它任何读取权限。它只知道“这是一个可挂载的插件”。

## 3. Context 已经统一了入口，但能力究竟由谁提供？

回看我们自己的 `environment`：

```ts title="概念伪代码"
const environment = { reader: new FileReader() }
```

所有模块都能通过它拿到 reader，可 `FileReader` 本身依旧由主程序写死。现在我们想换一种实现，比如从本地文件系统换成一个受控的远端文件提供者。只要消费者直接 `new FileReader()` 或导入具体实现类，替换就会牵涉所有消费者。

最小需要的是：**让提供能力的模块和使用能力的模块只通过一个稳定名称建立关系。**

```ts title="概念伪代码：服务仓库，不是 Cordis 实现"
const services = new Map<string, unknown>()

function provide(name: string, instance: unknown) {
  services.set(name, instance)
}

function getService(name: string) {
  return services.get(name)
}
```

这已经是一个朴素的服务注册器。一个模块执行 `provide('reader', reader)`，其他模块只认 `'reader'`。消费者不需要知道提供者究竟是 `LocalReader` 还是 `RemoteReader`。

**Service（服务）** 这个词至此才有必要出现：它不是每次执行一个小动作的“工具”，而是**向其他模块公开的一项有稳定名称的能力或接口**。

### Cordis 真实源码：怎样提供一个 Service？

官方教程选了一个比文件读取更小的 `greeter` 服务，我们暂时用这个最小例子来核对 Cordis 的实际 API。

**[Cordis 真实教程代码]**：`docs/cordis-tutorial/03-services.md`。

```ts
export class GreeterService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'greeter')
  }

  greet(who: string) {
    return `Hello, ${who}!`
  }
}
```

[查看官方代码](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/03-services.md)

这段代码里面，我们只研究 `super(ctx, 'greeter')`：

**谁调用它？** 当 Cordis 挂载 `GreeterService` 这个服务型插件并构造服务实例时，构造函数调用父类注册服务。

**输入是什么？** `ctx` 是它正在加入的插件运行时上下文；`'greeter'` 是服务名称。

**发生了什么？** Cordis 把当前服务实例与稳定名称联系起来，让其他插件通过 Context 获取它。

**结果给谁消费？** 需要打招呼的消费者使用 `ctx.greeter.greet('world')`，真正实现 `greet()` 的仍然是 `GreeterService` 自身。

这一点是后面理解 DSH 的钥匙：**Cordis 不实现 `greet()`，只让消费者能找到负责 `greet()` 的服务。**

官方完整代码还包含：

```ts
export function apply(ctx: Context) {
  ctx.plugin(GreeterService)
}
```

它表示在插件激活时挂载一个服务型子插件。`Service` 子类也是 Cordis 支持的插件形态。这里的 `ctx.plugin()` 负责挂载；不是执行 `greet()`。

> [!NOTE] 一个容易混淆的 TypeScript 细节
> 官方示例通过 `declare module '@deepseek-ai/cordis' { interface Context { greeter: GreeterService } }` 告诉 TypeScript：`ctx.greeter` 具有什么类型。**这一步只是类型声明，不是运行时注册**；真正注册服务的是 `super(ctx, 'greeter')`。

现在 Context 不再只能持有主程序提前写死的对象；不同模块可以提供服务。可是，服务的消费者可能先被加载，提供者还没有安装。

## 4. 服务尚未准备好，插件应该直接报错吗？

假设我们把服务提供者和消费者交给同一个通用加载器。用户的配置中，消费者恰好先出现：

```yaml
- name: './consumer.ts'
- name: './greeter.ts'
```

如果消费者在服务不存在时直接执行 `ctx.greeter.greet(...)`，就会失败。我们当然可以要求用户调整顺序，可当模块有几十个，手工依靠排列文件顺序维护依赖就开始脆弱了。

于是这一次新增的需求非常明确：**插件不仅要说“我做什么”，还要说“我需要谁已经就绪才能开始做”。**

Cordis 允许插件声明必需服务：

**[Cordis 真实教程代码]**：`docs/cordis-tutorial/03-services.md`。

```ts
export const inject = ['greeter']

export function apply(ctx: Context) {
  console.log(ctx.greeter.greet('world'))
}
```

[查看对应位置](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/03-services.md)

到现在才解释 `inject`：它表示**这个插件激活之前，必须已经有名为 `greeter` 的服务**。它既不创建该服务，也不执行 `greet()`；它只是声明依赖。

按官方教程，这会产生如下行为：

```text
[源码推导轨迹]
Consumer 被挂载
  → 发现自己 inject 了 greeter
  → greeter 还不存在：保持 PENDING
  → GreeterService 提供 greeter
  → Consumer 进入激活流程，执行 apply(ctx)
  → 输出 Hello, world!
```

由此可以解释为什么官方说**插件的声明顺序不负责保证初始化顺序**。决定一个消费者能否激活的，是它依赖的服务是否存在。

还要再多看一步：如果运行过程中 `greeter` 服务被卸载，Cordis 不只是事后把 `ctx.greeter` 置空，仍让消费者继续运行。官方教程说明：**依赖它的插件也会被卸载，待服务恢复时重新激活**。这样可以避免一个本应依赖新服务的消费者无限持有已经失效的旧引用。

但这里的“重新激活”不等于业务操作自动重试；它讲的是插件实例的生命周期，不是一个 HTTP 请求或 Tool Call 的重试策略。

## 5. 服务能动态卸载，之前注册的监听器由谁清理？

回到项目侦察程序。审计模块安装时向 FileReader 注册了一个读取监听器；卸载审计模块后，我们不希望它继续收到读取通知。

最简单的方案，是安装函数返回一个撤销函数：

```ts title="概念伪代码"
function installAudit(reader: FileReader) {
  const removeListener = reader.onRead(path => console.log(path))
  return () => removeListener()
}
```

这样能工作。问题是如果每个插件除了监听器，还有定时器、文件监听、连接等多种资源，谁负责保存这些清理函数？当插件因为缺少依赖而被停用时，又由谁按正确的生命周期执行清理？

我们需要的不是另一种业务功能，而是**把“注册资源”和“撤销资源”成对交给插件运行时管理**。

Cordis 把这种可撤销的贡献称为 **Effect**。

**[Cordis 真实教程代码节选]**：`docs/cordis-tutorial/02-lifecycle-and-effects.md`。

```ts
ctx.effect(() => {
  const timer = setInterval(() => console.log('tick'), 200)
  return () => {
    clearInterval(timer)
    console.log('heartbeat cleaned up')
  }
})
```

[查看完整示例](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/02-lifecycle-and-effects.md)

这个例子用定时器，是为了看见资源清理的效果；它不涉及 Agent。

`ctx.effect()` 在激活期执行资源创建逻辑，并将返回的清理函数关联到当前插件。卸载该插件时，Cordis 调用清理逻辑。对于它本身提供的 `ctx.on()` 等注册接口，Cordis已经知道怎样在所属插件卸载时撤销，不需要每次再写手工删除逻辑。

官方文档还把**一次已装载插件的运行实例**称为 *fiber*。插件从等待、激活到卸载都有状态；例如 `PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED`，失败时还有 `FAILED` 分支。这个词现在才有必要引入，因为我们第一次需要精确地说“谁拥有这些清理函数，以及卸载的是哪个实例”。

这里也划出了一个非常重要的边界：**Effect 只能清理由它管理的资源，不能回滚已经发生的外部业务副作用。** 如果某插件刚才已经写了一个文件、发送了一封邮件或执行了数据库提交，卸载插件并不表示这些动作会自动撤销。

到此，我们已经理解了 Cordis 的四个主要价值：统一安装、服务提供、依赖解析、插件资源生命周期。它们都不依赖大模型。

但还有一个问题：有时消费者根本不需要持有某个服务的引用，它只是希望在事情发生时收到通知。

## 6. 如果只是想知道“刚才发生了什么”，还需要直接调用服务吗？

我们最初在文件读取函数中加审计，是为了知道“什么文件被读了”。如果有审计模块、统计模块和控制台展示模块，FileReader 不应该反过来知道它们每一个的存在。

最小方案：FileReader 发出一个事件，其他模块监听它。

这个行为模型在普通 JavaScript 里完全可以通过 `EventEmitter` 实现；这里之所以进入 Cordis，是因为我们希望**事件监听也随着插件生命周期被安装和卸载**。

Cordis 提供的是事件注册与分发机制。具体业务自己定义事件的名字和载荷。例如官方教程中的普通统计服务：

**[Cordis 真实教程节选]**：`docs/cordis-tutorial/04-events.md`。

```ts
this.ctx.emit('stats/report', name, next)
```

另一个插件使用：

```ts
ctx.on('stats/report', (name, count) => {
  console.log(`[stats] ${name} -> ${count}`)
})
```

[查看统计服务、事件声明及监听器的完整例子](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/04-events.md)

现在区分一下职责：

- **Cordis** 提供 `on`、`emit` 等订阅与分发机制，并管理监听器归属于哪个插件。
- **统计服务** 决定何时触发 `stats/report`，以及 `name`、`count` 的含义。
- **监听插件** 决定收到事件后要做什么。

这里有一个对你学习 DeepAgent 很重要的点：**Cordis 事件不只有“广播通知”一种语义**。如果一个模块想改变另一个模块的执行结果，甚至阻止后续执行，就需要一个带有委托关系的扩展点。

Cordis 的 `waterfall` 支持这种**环绕式执行**：监听器收到一个 `next()`，可以先调用 `next()` 再修改结果，也可以直接返回而不调用 `next()`。它与 Middleware 中的“包住下一个处理函数”很接近。[官方说明](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-primer.md)

但是不能因此把所有事件混成一类。普通通知事件不能仅靠返回值就自动阻止工具执行；某个事件能否拦截，取决于**定义它的服务选用了怎样的分发契约**。

### 现在，我们终于可以给 Cordis 下一个准确的定义

**Cordis 是一个通用的插件运行框架：它帮助应用安装模块、共享服务、声明依赖、管理可撤销的资源，并支持模块间事件通信。**

它**不知道 Agent 是什么**，不会替我们写模型调用、工具执行、文件系统或会话存储。它管理的是这些功能模块彼此如何组合、何时生效、何时退出。

因此，Cordis 的边界不是“凡是出现在 `ctx` 上的能力都由 Cordis 实现”。恰好相反：**Cordis 管理 `ctx` 所在的运行环境，但真正提供业务能力的是安装进来的各个模块。**

---

## 7. 正式进入 DSH：如何让这个插件系统提供 Agent 工具？

到目前为止，我们谈过的仍是一个普通仓库读取程序。现在我们希望让**模型**根据用户问题决定何时读取文件，并在得到文件内容后生成回答。

这时，原来的 `FileReader` 只是一个通用文件读取能力。Agent 还需要一项新的能力：**向模型介绍有哪些工具可用，接收模型发来的工具名和参数，找到对应工具并执行**。

我们完全可以在 Agent Loop 里维护一个 Map。但既然想让工具由独立插件安装，Tool Registry 就应该有一个稳定的访问入口。

这一步开始，**Cordis 和 DSH 的职责第一次明确交汇**。

### 7.1 不是 Cordis 自带 `tools`，而是 DSH 提供它

打开固定 Commit 的真实文件：

- `packages/core/tools/src/index.ts`
- 类：`ToolRuntime`
- [源码链接](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts)

**[DSH 真实源码节选]**：

```ts
export class ToolRuntime extends Service {
  static inject = ['systemPrompt']

  constructor(ctx: Context, config: Config = {}) {
    super(ctx, 'tools')
    // ...
  }
}
```

这里的新概念只有一个：**DSH 把工具管理器实现成了 Cordis Service**。前面已经看过 `GreeterService`，所以我们现在可以逐行解释。

`extends Service`：复用 Cordis 的服务发布和插件生命周期机制；它没有让 Cordis 自动具备 Tool Calling 能力。

`super(ctx, 'tools')`：把这个 DSH 实现的服务以 `tools` 这个稳定名称提供出去。

`static inject = ['systemPrompt']`：ToolRuntime 自己需要 DSH 提供的 `systemPrompt` 服务；这也是一个**插件依赖声明**，不是一个模型消息。

直到现在，我们才第一次写出：

```ts
ctx.tools
```

这行代码不再突然：`ctx` 是 Cordis 提供的服务访问上下文，`tools` 这个服务由 **DSH 的 ToolRuntime** 注册提供，后面的具体方法则由 **DSH ToolRuntime 的类接口**定义。

### 7.2 为什么 ToolRuntime 还需要 systemPrompt？

只注册一个函数名是不够的。模型要知道工具叫什么、参数怎么写，Agent 执行器又要用同一套定义找到工具。工具服务因此需要把工具描述接到模型的请求装配过程。

这时才出现第二个服务。ToolRuntime 构造函数中的真实调用是：

```ts
ctx.systemPrompt.tools(context => this.wireSchemas(context.scope))
```

[查看 `ToolRuntime` 构造函数附近](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts)

这段代码表达的是：由 DSH 工具服务提供“当前可见的工具 Schema”，并交给 DSH 的提示/请求装配能力使用。这里的 `context.scope` 暂时不展开，只理解为“当前请求使用哪个范围的工具”；它不是 Cordis 随便发明的一个安全权限。

可以看出，**Cordis 管理的是 `systemPrompt` 服务的存在与注入；工具 Schema 怎么生成、怎么合并，是 DSH 的工作。**

### 7.3 现在才能解释真正的 `read` 插件

回到我们的仓库侦察助手。用户提出的是：“先读取 `package.json`，告诉我如何启动，不要执行命令。”

DSH 确实包含名为 `read` 的文件读取工具，它并不是由 ToolRuntime 写死在核心里，而是由专门的文件工具插件注册。

看 `packages/fs/tool-fs/src/index.ts` 的真实入口：

**[DSH 真实源码节选]**：

```ts
export const name = 'tool-fs'
export const inject = ['tools', 'fs', 'systemPrompt']

export function apply(ctx: Context, config: Config): void {
  // ...
  applyReadTool(ctx, {
    // 从配置解析得到的读取限制，省略
  })
  // ...
}
```

[查看完整插件入口](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/index.ts)

此刻把两种职责分开看：

- **Cordis** 看 `inject`：这个插件只有等 `tools`、`fs`、`systemPrompt` 等服务就绪才能激活。
- **DSH** 在 `apply` 内决定注册哪些文件工具，并给它们怎样的参数、输出与执行逻辑。

但 `applyReadTool()` 到底干了什么？去 `packages/fs/tool-fs/src/read.ts` 核对。

**[DSH 真实源码节选]**：

```ts
ctx.tools.register(defineTool({
  name: 'read',
  description: 'Read a UTF-8 text file and return line-numbered content.',
  parameters: {
    file_path: { type: 'string', required: true, description: 'Path to read, resolved by the filesystem backend.' },
    // ...
  },
  // ...
}))
```

[查看 `applyReadTool()` 源码](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts)

我们现在可以完整解释 `ctx.tools.register()` 的来历：

1. `ctx` 是插件激活时收到的 Cordis Context。
2. `ctx.tools` 是 DSH 的工具服务，不是 Cordis 的内置工具注册表。
3. `register` 是 DSH 工具服务暴露给其他插件的能力。
4. `defineTool` 是 DSH 定义模型工具 Schema、参数校验与执行逻辑所使用的接口。
5. `file_path` 是模型调用这个工具时提交的参数；它不是插件安装配置，也不是 Cordis 依赖名称。

这是本文最重要的第一次“跨框架”源码交接：**Cordis 让插件得以在正确的生命周期里访问服务；DSH 的工具服务决定如何接收工具注册。**

但是，模型提出 `read(file_path='package.json')` 时，真正访问磁盘的是谁？

## 8. DSH 的“插件化”不是给每个工具都包装一次 Cordis

继续查看同一份 `read.ts`。其执行函数会解析文件目标，再通过 DSH 的文件系统服务读取：

**[DSH 真实源码节选]**：

```ts
const { target, info } = await resolveRegularReadTarget(ctx, exec, input.filePath)

const chunks = info.size === undefined || info.size >= caps.streamMinSize
  ? await ctx.fs.streamText(target, exec.signal)
  : [await ctx.fs.readText(target, exec.signal)]
```

[查看执行路径](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts)

到这里，读者应该看到一个关键事实：**`read` 工具并没有自己固定使用 Node.js 本地文件系统实现。** 它通过 `ctx.fs` 使用已经装配好的文件服务。

我们可以把这条路径写成一条**数据/调用链**，而不是插件装配链：

```text
[DSH 执行路径，省略与当前问题无关的分支]
模型产生 read(file_path='package.json')
  → DSH Agent Loop 把 Tool Call 交给工具执行流程
  → DSH ToolRuntime 找到注册的 read 定义
  → read 的 execute 运行
  → ctx.fs 解析 / 读取文件
  → 工具结果返回执行调用方
```

现在对照一下 Cordis 到底参与了哪里：

**在装配阶段**，它帮助文件服务、工具服务、`tool-fs` 插件建立依赖并完成注册。

**在执行阶段**，真正解析工具名、校验输入、读取文件、整理输出的是 DSH 的工具和文件系统实现。Cordis 可能为 DSH 定义的扩展事件提供调度机制，但**并不是它自己判断 `read` 应该读哪个路径**。

所以也不能把“换一个文件系统插件”简单等同于“获得沙箱隔离”。具体文件系统提供者怎样限制路径与读写、操作系统是否真的隔离，是 DSH 服务实现和宿主环境必须另外保证的事情。

## 9. 不新增工具，只想拦截一次执行，插件在哪里介入？

到这里，我们知道一个插件如何新增 `read`。但我们还想实现审计：每次文件工具执行后记日志。总不能为了增加审计插件就去改 `read.ts`。

这个问题前面在通用 Cordis 阶段已经遇到过。我们现在只是把它投射到 DSH 的工具执行服务上。

DSH 工具服务定义了自己的运行事件，如 `tools/result`。**事件名字与何时发出，由 DSH 决定；注册与分发事件监听器，由 Cordis 提供机制。**

[DSH 官方 Cordis 教程的监听示例](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/07-into-the-harness.md)：

```ts
export const inject = ['tools']

export function apply(ctx: Context) {
  ctx.on('tools/result', (exec, result) => {
    console.log(`[tool-logger] ${exec.name}`, result.isError)
  })
}
```

**[教学化简]**：这段监听结构依据官方插件示例写出，简化了结果格式化和导入；不是逐字粘贴的完整示例。

前面的 Cordis 知识此时全部派上用场：

- 监听插件可以独立加载。
- `inject` 让它等 `tools` 服务就绪才激活。
- `ctx.on` 在插件被卸载时自动撤销监听。
- ToolRuntime 在自己的执行流程中发出 `tools/result` 通知。
- 监听插件不需要知道是哪一个 Agent Loop 或哪一个文件工具产生了结果。

不过，**观察结果不等于拦截执行**。假如需求变成“读取某些路径之前必须拒绝执行”，就需要 DSH ToolRuntime 所定义的执行前检查及环绕式 Hook，而不是靠 `tools/result` 的监听器返回一个 `false`。

Cordis 提供 `waterfall` 这种带 `next()` 的环绕机制；DSH 的工具管线确实有 `tools/execute` 等约定好的执行扩展位置。[查看 DSH 工具事件契约](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts)。

此处与 DeepAgent Middleware 的联系才有了准确的位置：**在具体的工具执行边界上，DSH 也允许插件通过带委托关系的 Hook 包裹或影响执行**。但 Cordis 的范围不止 Hook，它还管理服务提供者与整个插件生命周期。

## 10. 如果要替换的不是一个工具，而是整个 Agent Loop？

到此为止，我们做的是“工具服务不变，增加或影响其中的工具”。

现在再提高一个要求：不同产品希望使用不同的 Agent 执行驱动，但 UI 和 SDK 不应该跟着改写创建 Agent 的代码。

最简单的架构解法，是让外部使用一个稳定的 Agent 创建入口，让具体执行器负责提供创建实现。换句话说：**公共 Agent 接口与 Agent Loop 的实现分离**。

DSH 在这一固定版本中确实采用了这种组织。

打开 `packages/core/agent-loop/src/index.ts`：

**[DSH 真实源码节选]**：

```ts
export class AgentLoop extends Service implements AgentFactory {
  static inject = ['agents', 'sessions', 'llm', 'tools', 'systemPrompt', 'sessionProjections']

  constructor(ctx: Context, config: Config) {
    super(ctx, 'agentLoop')
    // ...
  }
}
```

[查看 AgentLoop 的服务声明](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts)

这里其实有两层不同机制：

**Cordis 层**：AgentLoop 是一个服务型插件；它声明运行所需的 DSH 服务，激活时由 Cordis 管理这些依赖。

**DSH 层**：AgentLoop 实现 `AgentFactory`，即 DSH 的 Agent 创建契约。Cordis 并不知道什么是 AgentFactory，也不知道创建一个 Agent 后怎样跑 Turn 或 Step。

那具体执行器怎样接上公共 Agent 创建入口？看 AgentLoop 构造函数中的另一段源码：

```ts
ctx.effect(() => ctx.agents.setFactory(this), 'agentLoop.setFactory()')
```

[查看注册位置](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts)

现在逐项对照：

- `ctx.agents`：**DSH 的 Agent Registry 服务**，由 DSH 定义注册工厂的方法。
- `setFactory(this)`：把当前的 AgentLoop 作为创建 Agent 的实现交给 Registry。这不是 Cordis 定义的通用 Agent 算法。
- `ctx.effect(...)`：**Cordis 提供的可撤销注册机制**，让这个工厂注册归属于 AgentLoop 插件，卸载时能清理。

这段代码尤其值得注意：**仅仅让 `AgentLoop` 继承 Cordis 的 Service，还不足以让 UI 知道怎样创建 Agent。** DSH 还需要自己的 `AgentFactory` 契约和 `setFactory()` 注册；Cordis 只为这项注册提供生命周期管理。

因此，两种可扩展性终于清楚区分了：

```text
能力扩展：在同一个 ToolRuntime 中注册更多工具 / Hook
实现替换：让某个稳定服务或工厂契约由另一种插件实现
```

前者更接近 DeepAgent Middleware 的工作位置；后者是 DSH 所依赖的插件运行时装配能力。二者都需要兼容接口，不能随意替换任意实现。

## 11. 最后一步：既然模块都可安装，谁决定运行哪套组合？

我们已经有工具、文件服务、模型适配器、Agent Loop、审计等独立模块。此时系统功能已经足够丰富，接下来的问题变成：**Web 应用与无界面执行程序，需要的是不是同一套插件？**

如果每个应用入口都手写所有安装语句，会重复维护大量基础配置。最直接的办法，是把插件组合写成可以分发和覆盖的配置。

这也是 DSH 的 **Profile / Bundle** 出现的地方。但请不要把它们误认为 Cordis 本身的 Agent 功能。

- **Cordis Loader**：负责按配置发现并挂载插件，处理插件生命周期。
- **DSH Bundle**：分发一组预设的 Cordis 插件配置和相应代码。
- **DSH Profile**：为 `web`、`headless` 等应用形态选择和叠加这些 Bundle 与用户配置。

[DSH 官方架构文档：Profiles and bundles](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/architecture.md)

这一步解决的是启动装配，不是一次 Tool Call 如何执行。**Profile 不是 Session，Bundle 不是 Agent Loop，Loader 也不是模型调度器。**

现在我们才有资格画完整架构，因为里面每项机制都已经有了出现理由。

## 12. 回看完整架构：始终分开“装配”和“执行”

先看**装配关系**：

```text
                    [DSH] Profile / Bundle / 用户配置
                                   │
                                   ▼
                         [Cordis] Loader
                                   │
                     安装插件 / 管理依赖与 Effect
                                   │
                  ┌────────────────┴────────────────┐
                  ▼                                 ▼
          [DSH] 服务提供者                    [DSH] 能力贡献者
       tools / fs / agents / llm ...        tool-fs / 审计 / 策略 ...
                  │                                 │
                  └──────── 通过 Context 协作 ────────┘
```

这里不是一次请求的调用顺序，而是“运行时有哪些能力、谁提供、谁消费、由谁管理安装”的关系。

再看**执行调用链**：

```text
[DSH] 外部调用 Agent
        │
        ▼
[DSH] Agent Loop：向模型服务发出请求
        │
        ▼
[DSH] 模型输出 read(file_path='package.json')
        │
        ▼
[DSH] ToolRuntime：查找、验证并调度工具
        │               │
        │               └─ DSH 定义的执行 Hook
        ▼
[DSH] read 工具：使用已装配的 fs 服务读文件
        │
        ▼
[DSH] 工具结果返回，Agent Loop 决定下一步
```

Cordis 并没有从第一步到最后一步逐个“执行 Agent”。**它在前一条装配关系中建立服务依赖和注册生命周期；在后面的执行链里，只在 DSH 明确使用 Cordis 服务访问或事件机制的位置参与。**

这正是我们此前最容易混淆的两个层次。

### 将所有职责压缩成一个对照表

| 问题 | Cordis 负责 | DSH 负责 |
|---|---|---|
| 功能模块怎么安装？ | 插件挂载与生命周期 | 决定有哪些具体插件、怎样配置 |
| 模块怎样找到另一项能力？ | Context、Service 机制 | 提供 `tools`、`fs`、`agents` 等服务 |
| 依赖缺失怎么办？ | `inject` 所表达的依赖激活 | 声明当前插件需要哪些服务 |
| 卸载时清理什么？ | Effect、监听器等注册归属 | 定义各项注册、释放自身资源 |
| 谁定义工具参数？ | 不负责 | `defineTool`、ToolRuntime |
| 谁执行 `read`？ | 不负责具体读文件逻辑 | `read` 工具 + `fs` 服务提供者 |
| 谁决定工具能否执行？ | 不提供通用的 Agent 权限策略 | DSH 工具执行/策略边界及底层提供者 |
| 谁驱动模型继续运行？ | 不负责 | Agent Loop |
| 谁保存执行事实？ | 不负责会话持久化 | Session 相关模块 |
| 谁选择 Web / Headless 插件组合？ | Loader 挂载配置项 | DSH Profile / Bundle 生成配置 |

尤其要注意：**Cordis Events 不等于 DSH Session Event Log**。前者是运行中模块通信和扩展的一种机制，后者是会话执行事实的记录体系。名称中都出现 Event，但不能画成同一条数据流。

## 13. 一个无需模型 API Key 的核验实验

理论讨论到这里应该接受实际核验。我们不必先启动一个完整 Agent，也不必消耗模型 Token：官方 Cordis 教程提供了真正调用 DSH 工具执行管线、同时监听结果的最小实验。

下面给出**在本地克隆固定 Commit 后可复现的步骤**。这不是本文环境里的实测记录，预期输出来自同一 Commit 的官方教程。

```bash
# 在你自己的终端中执行
git clone https://github.com/deepseek-ai/deepseek-harness.git
cd deepseek-harness
git checkout 5dda764ed3aa172535a7967b06ff95d9cbfe536a
pnpm install
mkdir -p tmp/cordis-tutorial
cd tmp/cordis-tutorial
```

在此目录中按官方 [第七节完整示例](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/07-into-the-harness.md) 创建两个文件：`greet-tool.ts` 和 `tool-logger.ts`。

这里需要把贯穿的 `read` 场景暂时换成更小的 `greet`，只是为了让实验**不依赖文件系统提供者、真实仓库或模型服务**。它验证的是同一套插件/工具/事件交接机制，并不是更换正文主线。

`greet-tool.ts` 中，插件先通过 `ctx.tools.register(defineTool(...))` 注册一个工具，再手动调用 `ctx.tools.execute(...)`；由于不使用模型，工具调用由实验代码自行构造。`tool-logger.ts` 通过 `ctx.on('tools/result', ...)` 接收工具结果。

`cordis.yml` 配置为：

```yaml
- name: '@deepseek-ai/dsh-system-prompt'
- name: '@deepseek-ai/dsh-tools'
- name: './tool-logger.ts'
- name: './greet-tool.ts'
```

为什么要加 `dsh-system-prompt`？我们已经在 `ToolRuntime` 的真实源码看到：它 `inject` 了 `systemPrompt`。如果没有提供者，工具服务自身就不能完成激活，后面的插件也不会按期执行。

执行命令：

```bash
node --import tsx ../../vendor/cordis/bin.js
```

**官方教程给出的预期输出**：

```text
[tool-logger] greet -> Hello, Cordis!
tool replied: [{"type":"text","text":"Hello, Cordis!"}]
```

为什么观察者先打印？因为 `tools/result` 是 DSH 工具服务完成结果处理的一部分，早于调用方从 `execute()` 的 Promise 收到最终返回值。这一点可以在官方第七节的文字说明中核对。

这个实验验证了三个不同层次：

1. **Cordis 装配**：服务与插件可以通过配置加载，依赖满足后激活。
2. **DSH 工具管线**：通过已注册的 ToolRuntime 执行模型可调用的工具。
3. **Cordis 事件 + DSH 事件契约**：审计插件不用改工具，也能监听 DSH 定义的结果事件。

它**没有验证模型会不会主动选择工具**，也没有验证完整 Agent Loop、会话恢复或安全沙箱。

如果想再做一个最简单的失败实验，可以先把配置中的 `@deepseek-ai/dsh-system-prompt` 删除，观察工具服务因缺少必需依赖而无法按正常路径激活。然后恢复配置，再删除 `tool-logger.ts` 的条目，验证工具调用不需要观察者也能运行；这正是“工具执行”和“可选审计扩展”解耦的可观察表现。以上两项是**建议实验，尚未在本次环境执行**。

## 14. 回到 DeepAgent：相似之处与真正不同的地方

你已经熟悉 DeepAgent 的 Middleware，所以不必重复解释“Hook 为什么能够包住一次工具调用”。

如果只是给工具调用增加日志、校验或前后处理，DSH 的某些工具 Hook 与 DeepAgent Middleware 的确解决着相近问题。

但回顾这一整章，我们实际上研究了**两个互相补充、却不相同的问题**：

**第一个问题：行为怎样被扩展？**

某次工具调用发生时，能否观察结果、修改结果、在前后插入逻辑？这里可以讨论事件、环绕式 Hook 和 Middleware。DSH 的工具执行 Hook 属于这一层。

**第二个问题：扩展功能本身怎样被纳入运行系统？**

谁安装插件？它需要哪个 Service？服务还没有就绪怎么办？服务被替换后要不要重新激活依赖插件？卸载插件后是谁清理工具注册、事件监听及工厂注册？这里讨论的是 Cordis 的插件运行时。

第二个问题不是第一个问题换了个 API 名称，它是更广泛的**软件模块组织与生命周期问题**。

同样不能因此说 Cordis 是“更强的 Middleware”，或者 DSH 比 DeepAgent 更先进。DeepAgent 也可以借助自身或其他基础设施实现类似能力。这里描述的是 **DSH 选择把模块装配、依赖和生命周期交给 Cordis 管理，再在这套机制上实现 Agent** 这一工程选择。

最后再把关键逻辑复述一遍：

> 我们先有一个能工作的仓库读取程序。新增模块后，希望它们能独立安装，于是需要统一的插件入口；模块之间需要共享能力，于是需要 Context 与 Service；服务可能晚到，于是需要声明依赖；插件可能卸载，于是需要生命周期与 Effect；模块想观察别人的行为，于是需要事件。在这套与 Agent 无关的 Cordis 基础设施建立以后，DSH 才把工具注册、文件访问、模型和 Agent Loop 实现成可组合的服务及插件。插件既可以增加一个工具，也可以在 DSH 定义的 Hook 上影响行为；更进一步，它还能提供不同的服务或执行驱动。最后再由 Profile 和 Bundle 选择如何装配成应用。

**这里的“先后”是为了学习而设计的推导顺序，不代表 DSH 或 Cordis 项目的真实开发历史。**

理解这条线后，再读 DSH 的 Scope、Session、Checkpoint/恢复或文件系统实现，就能准确判断：它们分别是 **Cordis 提供的通用机制**，还是 **DSH 在通用机制之上定义的 Agent 业务能力**。这就是读懂 DSH 架构最重要的分界线。

---

## 源码阅读索引

以下链接全部指向本文固定 Commit，可以按文章出现的顺序打开，而不是按照仓库目录“报菜名”阅读：

| 想验证什么 | 真实位置 |
|---|---|
| 插件怎样通过统一入口安装？ | [Cordis Tutorial 01：First plugin](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/01-first-plugin.md) |
| Effect 与插件实例卸载怎样关联？ | [Cordis Tutorial 02：Lifecycle and effects](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/02-lifecycle-and-effects.md) |
| Context 中的服务怎样提供与依赖？ | [Cordis Tutorial 03：Services](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/03-services.md) |
| 事件怎样通知或拦截？ | [Cordis Tutorial 04：Events](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/04-events.md) |
| Cordis 对 DSH 暴露哪些主要机制？ | [Cordis Primer](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-primer.md) |
| `tools` 服务究竟由谁提供？ | [ToolRuntime](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/tools/src/index.ts) |
| `read` 工具在何处注册/执行？ | [tool-fs 插件](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/index.ts) · [read.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/fs/tool-fs/src/read.ts) |
| Agent Loop 怎样成为可撤销的工厂实现？ | [AgentLoop](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/core/agent-loop/src/index.ts) |
| 无 API Key 的真实工具管线实验 | [Cordis Tutorial 07：Into the harness](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/cordis-tutorial/07-into-the-harness.md) |
| Profile / Bundle 与整套插件装配如何对应？ | [DSH Architecture](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/architecture.md) |

