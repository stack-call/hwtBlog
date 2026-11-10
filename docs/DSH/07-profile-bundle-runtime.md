---
title: "DSH 源码精读（七）：同一套 Agent Runtime，为什么能启动成两种应用？"
sidebar_label: "七、同一套 Runtime 怎么启动成两种应用"
sidebar_position: 9
slug: dsh-source-07-profile-bundle-runtime
description: "从仓库侦察助手的 Web 和一次性执行两种使用方式出发，逐步理解 DeepSeek Harness 的 Profile、Bundle、Patch 与 Loader 如何组合出真正运行的插件树。"
tags: [DeepSeek-Harness, Agent, 源码精读]
---

前六篇里，我们一直在帮助一个“仓库侦察助手”成长。它已经能接收请求、调用 `read` 工具、记录执行过程，也能在不改工具代码的前提下换掉文件系统实现。

但这些能力都只说明了**系统启动以后怎样工作**。如果连要安装哪些能力、要开放哪个入口都还没决定，Agent 就无从开始执行。

这篇只解决一个新问题：**既然 DSH 的运行能力能够独立替换，那么启动时究竟是谁决定把哪些能力装在一起？**

<!-- truncate -->

> [!NOTE] 阅读范围
> - 源码仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
> - 固定 Commit：[`5dda764ed3aa172535a7967b06ff95d9cbfe536a`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3aa172535a7967b06ff95d9cbfe536a)（`0.1.5-alpha.1`）。
> - 文中的真实源码均从该版本核对。配置对比基于真实配置文件；**本章没有在本地安装并启动 DSH**，命令输出不冒充实测。

## 1. 先不用架构图：为什么同一个助手要有两种启动方式？

假设我们想让用户以两种方式使用仓库侦察助手。

早上，用户打开浏览器，输入：

> 看看这个项目怎么启动，先不要修改文件。

助手读取 `package.json`，显示工具调用过程，最终给出回答。这个应用需要一个 Web 入口，还需要能把会话内容显示给浏览器的功能。

晚上，CI 流水线也要问同一件事。它不需要浏览器，甚至不需要常驻服务，只想在命令行里执行一次任务、打印答案，然后退出。

如果我们直接写两个启动函数，最容易想到的是：

```ts title="概念伪代码：两个启动函数"
async function startWeb() {
  installModel()
  installSession()
  installTools()
  installFileSystem()
  installWebServer()
}

async function startOneShot() {
  installModel()
  installSession()
  installTools()
  installFileSystem()
  installOneShotRunner()
}
```

这一版并没有错。只支持两种应用的时候，重复几行代码完全可以接受。

但我们继续使用它，就会遇到具体问题：我们给文件工具加了一条执行前检查，却忘了同步修改 `startOneShot()`，于是浏览器里生效的限制，在 CI 环境里没有生效。再增加 SDK、自动化入口后，每处重复装配都有机会漂移。

真正需要的不是一个更复杂的 Agent Loop，而是**让共享能力只有一份装配定义；不同应用只声明自己额外需要的入口**。

到了这里，我们才有必要认识 DSH 的 Profile 和 Bundle。

## 2. 先看真实配置：Web 和 Headless 到底差在哪里？

DSH 把一种可启动的应用组合叫作 **Profile**。先不要把它想成 JavaScript 类，它更接近“我这次要启动哪一套插件”的选择。

查看固定版本的 [`packages/boot/app-boot/src/profile.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/profile.ts#L2054-L2097)，会发现官方预设的两种选择：

```ts title="真实源码节选：PROFILE_TEMPLATES 中的两个条目"
web: {
  bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'],
  patchReload: 'live',
},
headless: {
  bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless'],
  patchReload: 'startup',
},
```

这里只需要看 `bundles` 数组：两种模式的第一项都是 `dsh-base`；第二项才分别是 `dsh-web-app` 和 `dsh-headless`。

这意味着在“需要哪些运行能力”这个层面，DSH 不是为 Web 和一次性任务重新建造两套系统，而是从共同的基础上选择不同的应用入口。

其中：

- `dsh-base` 提供模型适配、工具、Session、执行能力以及相关基础服务的默认组合。
- `dsh-web-app` 增加浏览器应用所需的服务与交互入口。
- `dsh-headless` 增加一次性任务的启动和执行入口，不安装 Web/HTTP 那一套。

对应的配置可以直接看 [`web-app/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/bundle/web-app/cordis.patch.yml) 和 [`headless/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/bundle/headless/cordis.patch.yml)。

先暂停一下：我们已经知道为什么要选 Profile，却还不知道“Bundle”到底是什么。它是一个负责安装插件的对象吗？还是只是一个配置文件？

## 3. Bundle 不是第二个 Loop，而是一层可分发的插件配置

回到最简单的需求：共享部分不要在每个启动函数里复制粘贴。我们可以把“应该安装哪些插件”写进一份清单，让不同应用引用它。

DSH 的 **Bundle** 就承担这样的分发角色。每个 Bundle 是一个 npm 包，在包的 `package.json` 中指向它自己的插件配置补丁（Patch）。

看固定版本的 [`packages/bundle/headless/package.json`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/bundle/headless/package.json)：

```json title="真实源码节选：package.json 中的 dsh 声明"
"dsh": {
  "bundle": {
    "patch": "./cordis.patch.yml"
  }
}
```

它告诉 DSH：要使用这个 Bundle 的插件装配规则，就去读取相应的 `cordis.patch.yml`。

打开 [`headless/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/bundle/headless/cordis.patch.yml#L25-L30)，可以看到配置中存在 `headless-startup` 和 `headless-runner` 两条插件记录。前者从命令行参数得到任务；后者使用该任务启动一次执行。它们不是在重新实现模型循环，而是在已有基础能力上添加“执行一次并退出”的应用行为。

这就解释了一个容易混淆的关系：

- **Plugin** 是具体提供服务或功能的实现。
- **Bundle** 告诉装配系统安装哪些 Plugin、如何配置它们。
- **Profile** 决定本次启动采用哪些 Bundle。

本篇不会继续细拆一个 Bundle 的每条配置；我们要先回答：谁把这些文件真正读进来？

## 4. 选择了 Profile，谁负责把 Bundle 找出来？

假设用户选择 `headless`。程序至少要完成两件事：找到这一 Profile 需要的 Bundle，然后把每个 Bundle 指向的配置文件读出来。

现在看 [`loadProfileDirectory()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/profile.ts#L3248-L3304)。其中最关键的几行是：

```ts title="真实源码节选：读取 Profile 与 Bundle 声明"
const manifest = readProfileManifest(binName, dir)
const bundles = manifest.dsh?.profile?.bundles ?? []
```

它读取 Profile 目录下的 `package.json`，拿到 `dsh.profile.bundles`。接下来才对每个包执行 `resolveBundleDir()`，读取该 Bundle 的 `dsh.bundle.patch`，再经由 `loadOverlayPatches()` 解析内容。

所以，**Profile 不负责启动 Agent，也不直接执行工具**。到这一步，它只回答“有哪些配置层、这些层在哪里”。

为什么不让 Profile 直接持有完整配置？因为这样更难复用共同基础，也不方便用户仅修改某个功能的设置。因此我们还需要让配置层可以按照顺序叠加。

## 5. 只想修改 Web 端口，为什么不应该改 Bundle 源码？

现在 Web 模式已经可以运行，但我们希望把监听端口从配置中的 `3080` 改成 `3081`。

直接修改 `dsh-web-app` 包内的 `cordis.patch.yml` 可以解决一次问题，却有两个缺点：升级时容易被覆盖；而且这是某个用户的部署偏好，不应该变成所有安装者的默认值。

所以需要一个很小的新机制：**用户在官方 Bundle 之上，再覆盖自己的配置。** DSH 把这样的覆盖称为 Patch。

在固定版本的 [Web Bundle 源码](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/bundle/web-app/cordis.patch.yml#L392-L408) 中，`webserver` 是有稳定 `id` 的一条插件记录。这正好给了用户一个修改目标。

例如，下面是放入当前 Web Profile 的 `cordis.patch.yml` 的**教学配置示例，未执行**：

```yaml title="$DSH_HOME/profiles/web/cordis.patch.yml（教学示例）"
- id: webserver
  config:
    host: 127.0.0.1
    port: 3081
    compression: gzip
    compressionLevel: 1
    compressionThresholdBytes: 1024
```

这里的 `id: webserver` 不是让 DSH 再启动一台服务器，而是找到已有的插件记录并覆盖其配置。

要注意：**这个版本的 Patch 对 `config` 不是深度合并，而是替换整个配置对象。** 因此示例重新写出了需要保留的字段。如果只写 `port: 3081`，就不能想当然地认为其他原始字段会自动保留。

谁负责保证后写入的用户配置覆盖前面的 Bundle 配置？现在才需要跟进启动装配函数。

## 6. 真正的装配次序在 `composeProfile()`

打开 [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/apps/cli/src/profile-boot.ts#L1350-L1384)。`composeProfile()` 先加载 Profile，再把各个层按顺序取出来：

```ts title="真实源码节选：按顺序收集 Patch"
const homePatches = loadOptionalPatches(NAME, homePatchPath()) ?? []
const overlays = patchFiles.flatMap(file => loadOverlayPatches(NAME, resolve(file)))
const bundlePatches = profile.layers.flatMap(layer => layer.patches)
```

接下来，`composeEntries()` 用 `applyEntryPatches()` 把这些层应用到一个空的插件记录列表上。层的顺序是：

```text
Bundle 1（dsh-base）
    ↓
Bundle 2（web-app 或 headless）
    ↓
当前 Profile 的 cordis.patch.yml
    ↓
用户 Home 的 cordis.patch.yml
    ↓
命令行 --patch 指定的覆盖层
```

这一顺序不是为了画一张漂亮的分层图，而是解决一个明确问题：**官方默认与用户修改发生冲突时，以谁为准？**

在我们的例子里，先装入 Web Bundle 里的 `webserver`，再让用户 Profile 对同一 `id` 的配置进行覆盖，因此就可以得到端口 `3081` 的最终配置记录。

但此时它仍然只是**配置**，不是已经监听端口的服务器。接下来才进入真正的运行时。

## 7. 最终是谁把配置变成运行中的插件？

前一篇里我们已经知道 Cordis 能管理插件的注册与卸载。本篇只补上它在启动时的角色。

在 [`runProfile()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/apps/cli/src/profile-boot.ts#L1549-L1572) 中，组合好的 Patch 会被交给 `boot()`；而 [`boot()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/index.ts#L3315-L3369) 的关键动作是：

```ts title="真实源码节选：创建 Loader 并挂载配置"
const ctx = new Context()
await ctx.plugin(Loader)
await mountRootInclude(ctx, absoluteConfigPath, patches, bareModuleBaseUrl)
```

这三行分别完成三件事：

1. 创建 Cordis 的根上下文，作为这套运行系统的基础。
2. 安装 Loader，让运行时具备根据配置管理插件的能力。
3. 将根配置与叠加后的 Patch 交给 Loader 装配，实际创建配置里的插件实例。

紧接着，`boot()` 等待 Loader 完成激活，并检查启动结果；失败时会清理已经装入的部分上下文，而不是无声地留下半套系统。[查看 `boot()` 后续代码](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/index.ts#L3363-L3400)。

现在我们才能完整说出这条链：

```text
用户选择 web / headless
            ↓
读取 Profile：选出 Bundle 列表
            ↓
加载每个 Bundle 的插件配置
            ↓
叠加 Profile、Home、命令行 Patch
            ↓
Cordis Loader 创建实际插件
            ↓
形成可用的 Agent Runtime + 对应应用入口
```

整个过程并没有给 Agent Loop 增加新的推理能力。它解决的是：**如何在启动时可重复地组装出一套需要的运行系统。**

## 8. 怎样验证 Web 与 Headless 确实装了不同的东西？

我们可以先不调用模型，直接检查最终配置。固定版本的官方架构文档提供了 `--dump-config`：

```bash title="在已安装相应版本 dsh 的环境中执行（本文未实测）"
dsh --profile web --dump-config
dsh --profile headless --dump-config
```

第一个输出里，可以重点搜索 `id: webserver` 与 `id: web-runtime`；第二个输出里，可以重点搜索 `id: headless-runner`。这些标识来自本章已核对的两个 Bundle 配置文件。

注意，这里的预期是**装配配置差异**，不是证明服务器已经正常监听，也不是证明模型真的调用了工具。`--dump-config` 的价值，是在运行前回答“这次到底准备加载哪些插件”；要验证应用行为，还需真正启动对应 Profile 并执行任务。

也可以做一个更小的配置实验：在 Web Profile 用户 Patch 中只改端口，再次 dump 配置，检查 `webserver` 的最终配置是否变成 `3081`。修改前应备份原文件，避免影响正在使用的环境。本章没有执行该实验，因此不提供伪造的命令输出。

## 9. 现在才把完整架构拼起来

到了这里，前六篇讨论的组件都找到了自己的位置。

我们已经理解：模型适配器、工具、Session、Agent Loop 等能力能够作为插件参与组合；每个能力被装入后，仍按前面学过的正常执行路径工作。

本篇新增的只有启动前的一层选择：

```text
                      Profile
                    /         \
                  web       headless
                   |           |
           dsh-base +      dsh-base +
           web-app         headless
                   \         /
                 Patch 叠加
                      |
                 Cordis Loader
                      |
            实际运行的插件树
                      |
           Agent / Tool / Session
```

这张图现在才有意义，因为我们已经知道每个框为什么存在。

**Profile 解决“启动哪套组合”，Bundle 解决“如何分发一组插件配置”，Patch 解决“默认配置怎样被覆盖”，Loader 解决“怎样把配置转成运行中的插件”。** 它们都不替代真正的 Agent Loop。

有一个取舍也必须看到：配置层越灵活，排查问题时就越要弄清最终生效的是哪一层。DSH 的配置预览能力正是帮助我们回答这个问题。Profile 的可选择性也不自动提供执行隔离：Web 与 Headless 仍须依赖各自装配进来的权限和底层执行服务。

下一篇，我们再处理另一类现实问题：运行已经开始，模型或工具执行途中失败、用户取消，甚至进程突然退出。**记录了事件就一定能恢复吗？恢复以后会不会重复执行工具？** 这些问题需要沿一次具体的中断过程去读，而不是从 Checkpoint 一类术语讲起。

---

**本篇真实源码导航（均固定 Commit）**

- [Profile 默认组合：`PROFILE_TEMPLATES`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/profile.ts#L2054-L2097)
- [读取 Profile / Bundle：`loadProfileDirectory()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/profile.ts#L3248-L3304)
- [装配顺序：`composeProfile()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/apps/cli/src/profile-boot.ts#L1350-L1384)
- [应用启动：`runProfile()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/apps/cli/src/profile-boot.ts#L1549-L1572)
- [真正挂载：`boot()` 与 `mountRootInclude()`](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/boot/app-boot/src/index.ts#L3315-L3400)
- [Web 插件配置](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/bundle/web-app/cordis.patch.yml) / [Headless 插件配置](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/packages/bundle/headless/cordis.patch.yml)
