---
title: 六种 Coding Agent Harness 深入比较：上下文怎样组织与压缩，任务才能真正做好？
description: 用同一个登录 401 任务，深入解释上下文组织、外存与压缩、工具反馈和验收控制，并逐一对照六种 Coding Agent Harness 的公开机制。
date: 2026-11-10
slug: coding-agent-harness-six-systems
tags:
  - agent
  - harness
  - claude-code
  - codex
  - opencode
  - cursor
keywords:
  - Coding Agent
  - Agent Harness
  - 上下文工程
  - 上下文压缩
  - Claude Code
  - Codex
  - OpenCode
  - Cursor
  - DeepAgent
  - DeepSeek Harness
---

如果你读过 DeepAgent、LangGraph 或者 DeepSeek Harness（DSH），很容易把 Coding Agent 的差异归结为：有的使用 Middleware，有的使用插件，有的自己写 Runtime。

但换个角度想：**用户只关心任务有没有完成。** 同一个模型、同一个仓库、同一个任务，为什么换一个 Harness，找到的问题、执行的操作和最终结果都可能不同？

答案不是「谁的框架层级更多」，而是 Harness 在模型每次决策前后做了什么。本文把六个产品/框架放在**同一条任务链**上分别研究，尽量从公开代码和官方文档确认机制，而不是罗列功能名称。

<!-- truncate -->

> **范围与证据说明（2026-11-10）**  
> 本文比较 Claude Code、OpenAI Codex、OpenCode、Cursor、LangChain DeepAgent 与 DeepSeek Harness。Codex、OpenCode、DeepAgent 有公开源码，DSH 的实现以固定提交 [`5dda764ed3`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3) 为基线；其余项目的官方文档和开源仓库随时间变化，本文不把动态 `main/dev` 分支冒充固定版本。Claude Code、Cursor 的闭源内部算法不作推断，引用其官方公开行为。下文的“执行轨迹”是**教学推演**，不是我们跑出来的六款产品横向 Benchmark。所有官方资料入口放在文末并在正文相邻引用。

## 1. 先固定任务：同一个 Bug，六套 Harness

项目采用前后端分离，用户偶发收到 `401 Unauthorized`。问题可能来自前端令牌刷新竞态、后端校验、时间边界或缓存。用户的实际要求是：

> 定位偶发 401 的根因，做最小修复，运行有关测试；如果能复现问题，应展示修复前失败、修复后通过的证据。不要修改不相关文件。

如果只是一次 `model → tool → model` 循环，下面两种结果都可能出现：

**失败路径（教学推演）**：模型猜测是 `auth.ts` → 读取不相关代码 → 修改一个超时常量 → 执行测试时遇到环境问题 → 直接宣称已修复。

**更可靠的路径（教学推演）**：找到刷新逻辑和校验调用链 → 收集失败请求/日志 → 写出可失败的回归测试 → 修改竞态处理 → 执行测试并检查退出码与 diff → 给出验收证据。

两条路径可以使用**同一个模型**。改变的是每次决策之前可见的证据、工具能做的动作，以及 Harness 对失败和结束的处理。

因此，下文每研究一个 Harness，都回答三个核心问题：

1. **看见什么**：怎样发现相关代码，怎样防止长上下文丢掉关键证据？
2. **能做什么、得到什么反馈**：修改、运行与错误信息怎样经过执行系统？
3. **凭什么结束**：有没有可观察的验证关口？权限、长任务和恢复如何影响任务最终完成？

另外要区分：**有能力运行测试 ≠ 默认强制测试；提供 Hook ≠ 默认装有验收 Hook；有沙箱 ≠ 无条件允许操作。**

---

## 2. 深挖上下文工程：同一个模型，为什么会因为「看见的东西」不同而做错事？

前面我们提出了一个判断：Harness 的核心工作之一，是决定**下一次模型请求到底包含哪些信息**。这句话容易说，真正实现却很容易出错。我们先不要比较六个项目，单独把一次请求的上下文构造过程拆开。

### 2.1 模型没有直接访问整个仓库：它只看到当前请求被送进去的材料

以第 30 轮登录 401 排查为例。此前 Agent 已经：搜索过 120 个文件、读过 8 个源码片段、修改过两次代码、运行过三次测试，还看到过上千行日志。**这些东西存在于程序或磁盘上，不代表下一轮模型都能看到。**

为了建立直觉，先写一个**概念公式（非任何项目源码）**：

```text
下一次模型输入 =
    稳定指令（系统规则 / 项目约定）
  + 当前目标和不可违反的约束
  + 与目标有关的代码和运行证据
  + 已完成工作的压缩状态
  + 仍需精确保留的最近消息 / 工具调用结果
  + 当次可用工具的描述
```

这里的每一项都会消耗上下文预算。其中，**项目规则**可能长期不变，**工具 Schema**会影响模型能够选择什么行动，**代码和错误输出**会影响模型判断根因，**历史摘要**会影响模型是否重复已经排除的尝试。将这四种东西混成一个无限增长的消息数组，是最容易得到、也最容易失控的方案。

注意「模型消息历史」和「原始事实」不是同一个东西。原始事实可以在仓库、测试日志、数据库或事件日志中长期保存；模型输入只是它们在某一次请求中的**有界投影**。把原始事实保存得更完整，不等于每轮都要重新发送给模型。

### 2.2 最简单的办法：每轮都附上完整历史。它什么时候失效？

如果只有三轮对话，把先前所有消息附上即可。但到了第 30 轮，模型可能收到这样的内容：

```text
第 1～12 轮：搜索了大量不相关的鉴权文件
第 13～18 轮：尝试修改 session timeout，发现无法复现
第 19～24 轮：大量成功的测试日志和依赖安装输出
第 25～29 轮：终于发现并发刷新时出现相同 refresh token 被使用两次
第 30 轮：用户仍要求“找出根因并给出通过的回归测试”
```

这里至少出现两个矛盾。第一，模型的上下文窗口与单次生成预算有限；第二，**即使没有达到硬上限，无关的旧证据、已否定的猜测和重复日志，也可能让真正有用的信息变得不显眼**。

有一种更危险的失败：上下文自动截断了早期用户要求「不要修改无关文件」，或者把最后一次失败堆栈中 `refreshToken()` 的两条并发路径截掉。前者会破坏约束，后者会破坏证据。这就是为什么不能把“压缩率越高越好”当成优化目标。

我们真正需要的，不是把 30 轮历史变短，而是**让第 31 轮仍然具备做出正确决策所需的事实**。

### 2.3 先组织，再压缩：什么信息该常驻，什么应该按需取？

第一步不应该立刻调用另一个模型总结历史，而应该先按用途分层：

| 信息类型 | 在 401 案例中的内容 | 更合适的处理方式 |
|---|---|---|
| 稳定约束 | 不改无关文件；必须运行回归测试 | 作为明确指令保留，不寄希望于历史摘要记住 |
| 项目知识 | 测试使用 `pnpm test auth`；鉴权位于 `src/auth/` | 项目规则按范围装配，长文档按需读取 |
| 当前目标 | 复现并修复并发刷新导致的 401 | 当前任务状态里明确保留 |
| 精确证据 | 两次请求时间、错误码、关键堆栈、测试断言 | 保留原文片段、文件路径和行号 |
| 大体积原始材料 | 3,000 行日志、完整构建输出 | 文件或持久存储中保存，模型只接收索引和摘要 |
| 已完成探索 | 排除了时钟偏移、服务端超时配置 | 形成简短结论，附“为什么排除” |
| 当次工具能力 | `read`、`grep`、`bash` 等具体描述 | 根据当前权限和任务装配，不盲目展示全部工具 |
| 最近交互 | 刚修改的补丁、刚失败的测试输出 | 尽量原样保留，避免摘要抹去细节 |

这张表不是六款产品的共通内部数据结构，而是**可以自己实现、也能用于源码对照的上下文组织原则**。

假设模型窗口是 128k tokens，一个**纯教学预算**可以这样分：固定指令和工具 12k、最近交互 20k、当前证据 25k、历史摘要 12k、预留模型输出 24k、剩余弹性预算 35k。这个数字并非任何产品默认配置；它只是说明：**必须为新工具结果和后续推理留下空间**，而不能把窗口塞满才开始处理。

这里还出现一个与任务质量间接相关的效率细节：静态指令放在稳定前缀、变动内容放在后面，更有利于支持精确前缀缓存的模型接口。Codex 的公开技术文章专门解释了这一点。**缓存通常首先改善延迟和成本，不自动让答案更正确**。参考：[Unrolling the Codex Agent Loop](https://openai.com/index/unrolling-the-codex-agent-loop/)。

### 2.4 不是所有历史都值得压缩：先让模型能拿到正确的证据

如果第 30 轮还在靠猜测定位 401，那么把所有历史总结得很精炼也不够。更直接的改进往往是：把「问题描述」映射到「相关代码与运行证据」。

对于同一任务，有两条不同的信息路径：

```text
路径 A：把整个 src/auth 目录直接放进模型请求
路径 B：先搜 refresh token 的定义和调用 → 找到并发入口
        → 按行读取相关函数 → 只加入可核对的证据
```

路径 B 的优势不是代码读得更少本身，而是模型在关键判断时能看到**高相关、可追溯、及时更新**的信息。这里的检索可以是精确关键字、符号索引、语义检索、目录导航或者它们的组合；不同任务不一定是语义检索更优。比如异常码 `TOKEN_REUSED` 的精确查找，关键词搜索反而可能更直接。

Cursor 公开说明其代码库上下文支持相关文件自动检索，也可以用 `@file`、`@code` 精确指定目标；大型文件还可能先以类名和函数签名等结构性视图呈现，让模型选择再展开。后者叫 **文件凝缩（file condensation）**，和总结对话历史不是同一个机制。参考：[Working with Context](https://docs.cursor.com/en/guides/working-with-context)、[Summarization](https://docs.cursor.com/en/agent/chat/summarization)。

### 2.5 五种经常被统称为「压缩」的操作，其实解决不同问题

现在假设真正找到错误日志，但一次 `pnpm test auth` 输出了 3,000 行。最简单的方案是把 3,000 行原样放进下一轮模型输入；它可能占据不成比例的预算。

这时可以先做**卸载（offloading）**：原始日志写到文件，例如 `/tmp/auth-test-03.log`，工具结果只返回概要、失败行和可再读取的路径。这样不是删除证据，而是改变证据进入模型的方式。

如果日志中有 200 行完全相同的依赖下载进度，还可以做**裁剪（pruning）**：去掉可确定的重复噪声。但不能对所有失败行都只保留最后十行，因为早期的原始异常可能比结尾的汇总更重要。

如果已经完成第一轮调查，需要保留“为什么排除了时钟问题”，就用**摘要（summarization）**：把许多轮调查转成状态记录。

如果某个探索子任务要遍历整个仓库，主 Agent 只需要它的结论，则用**子任务隔离（isolation）**：让调查者在独立上下文里读大量材料，只交付路径、证据和不确定性。

最后，如果某段代码几轮前已经读过、现在又被修改，最可靠的做法往往不是靠记忆，而是**重新读取当前文件（re-retrieval）**，避免依据旧代码做判断。

| 机制 | 是否保留完整原始内容 | 什么时候最有用 | 主要风险 |
|---|---|---|---|
| Offloading | 通常在外部存储保留 | 大型日志、完整文件、工具输出 | 路径失效，后续无法取回 |
| Pruning | 被删除部分未必保留在模型视图 | 重复进度、过时工具输出 | 错删首个异常或关键参数 |
| Summarization | 要看实现是否单独保存原始记录 | 历史探索、阶段结束、窗口压力 | 错误总结、丢精确信息 |
| Isolation | 任务自己的上下文独立存在 | 并行调查、大量中间搜索 | 子 Agent 把错误结论带回主 Agent |
| Re-retrieval | 从当前事实源重新获取 | 代码或外部状态已变化 | 消耗额外调用与时间 |

所以一个实用的优先顺序常常是：**减少无关检索 → 卸载大输出 → 去除可确定噪声 → 保留最近原文 → 压缩较早历史**。这不是所有 Harness 写死的统一流程，而是一套适合做消融实验的工程假设。

### 2.6 什么是好的摘要？比较两个具体版本

假设任务已经排除了三种原因，并发现刷新令牌竞争条件。下面都是**教学虚构示例**，不是实际 Agent 运行结果。

**摘要 A：**

> 我们排查了登录 401，发现认证模块有问题，修改了一些文件，测试还需继续。

它很短，但下一轮模型几乎得重新调查：什么问题？证据在哪里？改动是什么？失败信息是什么？

**摘要 B：**

```text
目标：修复多标签页同时刷新令牌引发的偶发 401；不得修改无关文件。
当前证据：
- src/auth/refresh.ts:88-126 有刷新入口；两个标签页并发进入。
- 日志 /tmp/auth-test-03.log 的 141、168 行显示同一旧 refresh token 被提交两次。
- 后端会轮换 refresh token；第二次提交返回 TOKEN_REUSED（具体值已核对）。
已经排除：
- 时钟漂移：服务端与客户端时间差低于重试触发阈值；见调查记录。
- 静态 timeout 配置：调整后复现不变，已回退该尝试。
当前改动：
- 修改 src/auth/refresh.ts 的同进程去重逻辑；尚未验证跨标签页行为。
验证状态：
- pnpm test auth：最近一次 FAIL，失败用例为 concurrent-refresh；不是权限错误。
下一步：
- 重新读取当前 refresh.ts；补充跨标签页复现；运行相关测试并检查 diff。
未知：
- 是否还有 Service Worker 同时发起刷新。不要将其写成已确认结论。
```

摘要 B 更长，却保留了下一步决策真正需要的**目标、约束、证据、已排除假设、实际改动、测试状态、下一步和未知项**。这些位置和行号只是示例，实际系统应从已存在的文件、日志、测试结果中抽取，不能凭模型猜测生成。

这里有一条必须坚持的约束：**摘要应区分「观察」「推断」「尚未验证」**。特别是“测试通过”不能只因模型曾计划运行就被写进历史摘要；应保留真实命令、退出码、执行时对应的代码版本或 diff 标识。

### 2.7 什么时候触发压缩？不只是等 token 用光

最简单的策略是：估算 token，超过阈值才摘要。它能避免请求硬失败，但可能在最不合适的时机触发。例如模型刚收到一份关键失败堆栈，摘要器立刻把时间顺序压成一句“发生了认证错误”。

更合理的策略至少区分三种情况：

1. **预算压力**：接近窗口限制，必须给下一次模型调用腾空间。
2. **阶段切换**：已经定位根因、准备从调查进入修复，旧的搜索细节可以浓缩成阶段结论。
3. **突发大输出**：单条日志已经非常大，优先处理这一条输出，而不是把整段对话重新总结。

同时应保护**正在发生的动作与结果**：一次工具调用和它的结果应当可以关联，最后一段精确失败证据不要随意截断。即使历史被压缩，也要保证后续模型能找到之前保存的原始材料；否则一旦摘要写错，就没有办法反查。

### 2.8 六种 Harness 具体是怎样组织与压缩上下文的？

到这里再看产品差异，就不会停留在“它支持 Compaction”的功能表面。

**Claude Code：分清常驻规则、按需加载与压缩后的重新注入。** Anthropic 的公开说明区分根目录 `CLAUDE.md`、按路径加载的规则、Skills、子 Agent 和 Hooks。这些信息不是一股脑儿全部放进主上下文：Skill 可先暴露名称和简短描述，完整内容在需要时进入；子 Agent 在独立窗口内探索，主会话主要接收结果。Claude Code 压缩后会重新注入部分项目规则、记忆等，并按官方文档描述重新读取若干最近修改的文件。**这是一种“摘要旧对话 + 重新建立当前工作环境”的思路**，而不是只留下单段历史摘要。不同版本的重注入细节可能变化，不能把公开行为推断成完整内部算法。参考：[Steering Claude Code](https://claude.com/resources/articles/steering-claude-code-skills-hooks-rules-subagents-and-more)、[Explore the context window](https://code.claude.com/docs/en/context-window)。

**Codex：项目约定有层次；压缩不等同于用户可编辑摘要。** Codex 的 `AGENTS.md` 会按全局、仓库到工作目录的顺序收集适用指令，而不是默认读取仓库每个目录。Codex 的公开技术文章解释：静态前缀有利于缓存；当达到自动压缩阈值，会使用 Responses 的压缩能力，把较长历史替换成更短、可继续的上下文项。压缩输出可能含有对模型连续性有用的非普通文本状态，因此不能把它误写成“始终由一段人工可编辑 Markdown 摘要实现”。参考：[AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[Unrolling the Codex Agent Loop](https://openai.com/index/unrolling-the-codex-agent-loop/)。

**OpenCode：明确保留最近一段原文。** OpenCode 当前 V2 Compaction 方案在接近窗口限制时总结较早对话，并保留最近约 **15,000 tokens** 的原始内容；默认预留缓冲区比例，并允许配置留存大小和触发策略。如果请求因上下文过长被提供方拒绝，也有压缩后重试的路径。这解决了“近期失败堆栈被全部摘要掉”的一部分风险，但 15k 是公开默认值，不是适合所有模型和任务的最优值。其文档还提示：若固定提示词和工具 Schema 已占据几乎整个窗口，仅压缩对话历史没有用。参考：[OpenCode Compaction（V2）](https://opencode.ai/docs/compaction/)。

**Cursor：文件凝缩与聊天摘要是两件事。** 当模型需要读一个大文件时，Cursor 的公开文档描述会根据可用空间展示文件的结构性片段，例如类、方法、函数签名，必要时再展开；当聊天变长时，旧对话可以被摘要。前者改变**一次读取的粒度**，后者改变**跨轮历史的长度**。代码库检索及 `@file` 指定上下文又解决“先找到哪个文件”的问题。Cursor 未完整公开内部排序和具体压缩算法，所以不应假定它和某个开源 Harness 使用了相同的摘要提示词。参考：[Cursor Summarization](https://docs.cursor.com/en/agent/chat/summarization)、[Working with Context](https://docs.cursor.com/en/guides/working-with-context)。

**DeepAgent：先外存大内容，再摘要历史。** LangChain 在 Deep Agents 的上下文管理文章中描述了分阶段处理：单次工具返回超过 **20,000 tokens** 时，卸载到文件并留下路径与预览；上下文压力较高时，旧写入/编辑工具调用的大参数也可被卸载；当这些办法仍不足以腾空间，再总结历史，并把原始消息保存到文件系统中以便重新找回。文章中的 **85%** 等阈值属于当时公开的实现策略，可能随版本变动。这个设计的核心是：**能无损转移的先不要有损总结**。参考：[Context Management for Deep Agents](https://www.langchain.com/blog/context-management-for-deepagents)、[Deep Agents Overview](https://github.com/langchain-ai/docs/blob/main/src/oss/deepagents/overview.mdx)。

**DSH：先区分完整事件日志与本次可见历史，再对后者做替换。** 在我们固定的 [`5dda764ed3`](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3) 中，Session 保存执行事件，模型消息由 `deriveMessages()` 派生。压缩服务可以在 `agent/pre-step` 的预算压力检查中介入，也可以在上下文溢出后的错误路径中触发；它可先调用工具结果裁剪，再决定是否总结。成功压缩时，原始事件没有被简单删除，而是用带 `surfaceOp: replace` 的新消息替换模型可见历史的一段，并单独记录压缩过程事件。**这里改善的是可控地改变模型看到的历史，同时保留可审计的原始事件**；不是“只要使用 Event Log，摘要就必然正确”。参考：[固定版本 Compaction 文档](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/subsystems/compaction.md)、[固定版本 Session 文档](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/subsystems/session.md)。

从这六种实现可以得到一个有用的比较：Claude Code 强调压缩后**重建工作环境**；Codex 对模型连续性与前缀缓存特别敏感；OpenCode 明确采用**摘要 + 近期原文**；Cursor 让**文件本身先以较小视图呈现**；DeepAgent 优先**外存原始内容**；DSH 则强调**持久事件与模型可见历史的分离**。这些是公开机制中值得研究的差异，不是效果排行榜，也不是说其他产品不具备相似方法。

### 2.9 什么时候压缩反而会让 Agent 变差？

拿当前登录 401 任务来说，以下五种情形都可能出现反效果：

- **证据误压缩**：原始日志的并发顺序被总结成“令牌偶发失败”，根因线索消失。
- **结论污染**：一次尚未验证的猜测被摘要写成“已确认原因”，模型从此不再检查。
- **约束遗忘**：用户“不改其他文件”的要求没有独立保留，摘要遗漏之后出现大范围重构。
- **存储路径失效**：工具输出被外存，但后续会话不能访问原始文件；所谓可回读只是空话。
- **状态过期**：摘要里的代码版本已经被新补丁覆盖，模型继续依据旧片段修改。

因此，压缩的目标不应只用“请求 token 下降多少”衡量。**真正要考察的是：缩短输入之后，模型还能不能做出同样正确、甚至更正确的下一步决策。**

### 2.10 怎样证明上下文工程真的提升了效果？

可以做一个不需要横跨六款产品、也更容易得到因果结论的消融实验。固定模型、401 任务、仓库提交、测试与可调用工具，比较四套上下文策略：

| 策略 | 唯一变化 | 最需要观察的指标 |
|---|---|---|
| A. 原始历史 | 不主动压缩，直到窗口限制 | 是否溢出、总 token、是否被旧噪声干扰 |
| B. 只做摘要 | 旧消息用概要替换 | 是否遗忘关键错误码与文件路径 |
| C. 外存 + 摘要 | 大输出先落盘，再摘要旧历史 | 回读次数、事实丢失率、完成率 |
| D. 外存 + 摘要 + 近期原文 | 另外保留最近一段工具反馈原文 | 最近测试证据保留、完成率及成本 |

至少记录这些指标：`最终隐藏测试通过`、`是否找到根因`、`关键证据在下一轮是否仍可取得`、`无关文件改动`、`摘要中的错误事实数量`、`token/延迟`、`需要重新调查的次数`。最好在**压缩发生之前和之后各设置一个检查点**：请一个独立程序比对原始日志中已确认的事实、文件路径和测试退出码是否仍在模型可访问的上下文或外部存储中。

上面的 A/B/C/D 是**建议的实验方案，本文没有实际跑出四组成功率**。只有在任务和模型控制住以后，才能认真谈“压缩提升了效果”，否则更低的 token 数只说明它省钱，并不能证明它更会修 Bug。

---

## 3. 深挖工具反馈与验证：为什么「执行过命令」不等于「Agent 获得了正确结论」？

上下文组织解决的是**模型下一次看见什么**。但如果工具返回的事实本身模糊，即使把它完整塞进上下文，模型仍然可能做出错误判断。

### 3.1 一个 `npm test` 的结果至少包含四种不同事实

在登录 401 场景里，模型想运行 `pnpm test auth`。如果我们只返回 `failed`，就丢掉了执行原因。

```text
场景 A：退出码 1，concurrent-refresh 断言失败
场景 B：权限拒绝，Shell 根本没有启动测试进程
场景 C：超时，测试仍在运行或已经被终止
场景 D：退出码 0，但运行的是旧缓存中的测试、没有覆盖本次修改
```

四种情况都可能被粗糙 UI 显示为“没通过”，但下一步正确动作完全不同：A 应调查业务逻辑，B 应按权限策略处理执行环境，C 应观察进程状态或重试策略，D 应重新确认验证覆盖和代码版本。**工具反馈质量，就是让模型能区分这些行动分支。**

因此更好的工具结果，至少要能回答：执行了什么（命令与参数）、在哪里执行（工作目录/环境）、有没有启动、如何结束（退出码、信号、超时、取消、拒绝）、产生了哪些可追溯证据。不是说每家都用同一个 JSON 字段，而是要避免这些信息被汇总成一句含糊的自然语言。

### 3.2 不是把完整终端输出喂给模型，而是把「判断所需的信号」组织好

假设失败输出很长。一种教学化工具回报可以长这样：

```text
command: pnpm test auth
cwd: /repo
status: completed
exit_code: 1
failed_test: concurrent-refresh
key_error: expected exactly 1 refresh, received 2
artifact: /tmp/auth-test-03.log
next_read_hint: lines 135-175
```

这是**为讲解设计的示意格式，不是 Codex、Cursor 或 DSH 的真实工具协议**。它保留了模型修复需要的关键信号，又让完整原始证据能回读。对运行时生成的日志、前端网络请求和编译器报错，可以采用同一原则。

但不能过度预处理：如果工具预览器错误地把 `TOKEN_REUSED` 所在行过滤掉，模型可能被误导。因此**原始输出应保持可访问，摘要要允许追溯来源**。

### 3.3 工具请求、权限批准、实际执行、结果写回是四个不同阶段

这就是比较 Harness 时应该看的一条真正有用的执行链：

```text
模型提出工具请求
  → 检查工具是否存在、参数是否合法
  → 检查是否允许在当前环境执行
  → 实际执行并处理进程/文件系统反馈
  → 给模型返回关联的结果
```

如果第二步被拒绝，绝不能在消息里宣称“测试已运行并失败”。如果进程结果未知，也不能把它写成已成功。Codex 的公开 Rust [`ToolOrchestrator`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs) 明确管理审批、沙箱尝试与特定拒绝后的处理；DSH 的 ToolRuntime 有自己的执行管线与事件结果；Claude Code 则公开了工具前后 Hook 和权限系统。它们的实现位置不同，但都在解决“把模型意图转成真实环境结果”的工程问题。

对于长期运行的命令，还应区分**启动命令成功**、**进程持续运行**、**进程最终退出**。一个获得进程 ID 的成功回报，不等于测试已经通过。

### 3.4 为什么「模型没有再调用工具」不能作为任务验收条件？

现在模型修改完 `refresh.ts`，返回“问题已修复”，并且没有下一次工具调用。普通 ReAct 循环自然会结束。

但用户最初要求的是“运行测试并确保修复”。因此需要区分：

```text
模型自然停止：模型决定不继续行动
任务通过验收：外部检查确认预期条件满足
```

为了减少“自信但未验证”的结果，可以采用如下**教学化结束判定**：

```text
准备结束
  ├─ 本次 diff 是否包含允许范围之外的文件？是 → 不可通过验收
  ├─ 是否执行过与本次改动对应的测试？否 → 继续或报告无法验证
  ├─ 测试退出码是否为 0？否 → 提供失败信息并继续/请求帮助
  ├─ 是否仍存在未处理的权限阻塞？是 → 请求用户决策
  └─ 记录测试证据与当前代码版本 → 可以形成最终报告
```

这是建议的验证流程，**不是任何产品默认内置的统一状态机**。Claude Code 的 `Stop` Hook、DSH 的 `agent/turn-stopping` 等适合接入这样的检查；DeepAgent、Codex、OpenCode 也能通过工作流、工具和外部 CI 组织验证。但不存在一种天然可靠的规则，可以凭 `Tool Call` 的存在就断言任务已经验收。

特别是对于网页视觉修复，单元测试通过可能仍不足够。Cursor 的浏览器工具可以为模型提供实际页面、Console/Network 反馈；但如果用户没有打开浏览器或没有重现问题，不能把“有浏览器工具”写成“已做端到端验证”。

### 3.5 如何避免验证本身成为新的死循环？

如果 Harness 每次 `Stop` 都要求再跑一遍测试，那么模型可能永远不能结束；如果测试本身有随机失败，Agent 也可能不断修改正确代码。

一个可用的验证策略需要同时说明：**验证条件、最大尝试次数、失败退出条件和人工接管时机**。例如：

- 业务测试失败：反馈具体断言，允许有限次修复；但不能无限重复同一命令且无新证据。
- 环境权限失败：停止业务修改，走明确的批准或报告路径。
- 测试不稳定：记录多次运行及差异，不把一次偶然通过当成充分证明。
- 外部服务不可用：说明哪些验证未完成，不把阻塞伪装成成功。

这里的工程目标不一定是“始终让 Agent 继续”，而是**让它知道何时应该继续、何时应该请求帮助、何时必须如实停止**。这也是长任务比简单 ReAct Loop 更难的原因。

### 3.6 如果只做一个真正能检验效果的实验，应该测什么？

对同一个 401 任务，可以固定模型、工具集合和测试环境，只改变两种 Harness 行为：

| 对照 | 方案 A | 方案 B | 观察 |
|---|---|---|---|
| 工具反馈 | 只返回 `failed` | 返回退出原因、关键断言、原始日志路径 | 错误修复方向、重试次数、成功率 |
| 结束控制 | 模型不调用工具就结束 | 必须核对本次 diff 对应的测试证据 | 未验证完成比例、延迟、成本 |

这样的实验才有机会回答：**是哪个 Harness 决策实际改变了结果？** 如果同时换了模型、检索算法、工具、测试集和预算，就无法把好坏归因给某一个机制。

---

## 4. Claude Code：通过项目知识、隔离探索和执行边界改善结果

### 4.1 为什么第一步不只是让模型自己 `grep`？

如果整个项目有几千个文件，模型需要先知道目录习惯、测试命令以及哪些部分不能改。Claude Code 使用项目指令文件 `CLAUDE.md`（现也支持 `AGENTS.md`）、规则文件和自动记忆；这些内容帮助它在开始查找之前获得约束与惯例。对于日志、搜索命中等高体积材料，它支持将调查工作交给独立上下文的子 Agent，主 Agent 只接收所需结论。

对应到登录 401：主 Agent 不必携带「搜索过哪些几十个无关模块」的全部输出。一个只读调查子 Agent 可以寻找刷新令牌调用链，交回涉及的文件、调用关系和待验证假设。

这里的提升路径是：**减少主 Agent 的噪声，而不是让所有文件一次性进入上下文。** 子 Agent 也可能遗漏证据，因此结论应保留可复查的路径和证据。

官方依据：[项目指令与记忆](https://code.claude.com/docs/en/memory)、[子 Agent 的独立上下文](https://code.claude.com/docs/en/sub-agents)。

### 4.2 模型决定运行 Shell，不意味着直接放行

Claude Code 的工具扩展点和权限系统有不同职责。典型执行边界可以理解成：

```text
模型提出 Bash / Edit
       ↓
PreToolUse（可以检查、拒绝或请求进一步处理）
       ↓
权限判断与实际执行
       ↓
PostToolUse / PostToolUseFailure（观察或反馈结果）
       ↓
结果进入下一次模型决策
```

这是**概念化顺序**，不是 Claude Code 公开完整内部状态机。`PreToolUse` 能影响授权决策；`PostToolUse` 可追加信息、在支持的环境中调整工具输出；权限模式与沙箱是独立约束。能修改 Bash 参数和能保证 Shell 安全并不是一回事。

如果 `npm test` 因权限不够而失败，工具错误不是「代码测试失败」；只有区分**权限拒绝**和**断言失败**，模型才不会错误地修改业务逻辑。

官方依据：[Hooks 参考](https://code.claude.com/docs/en/hooks)、[权限与安全](https://code.claude.com/docs/en/permissions)。

### 4.3 模型说“已修复”时，谁可以让它继续？

Claude Code 最适合与“任务真正完成”联系起来研究的机制是 **`Stop` Hook**。默认的模型自然结束不表示测试通过；但你可以配置一个验收 Hook，在模型准备结束时检查测试证据。若不满足约束，Hook 可以阻止本次停止，并向模型提供继续工作的理由。

假设用户要求“回归测试必须通过”，验收逻辑可以是这样的**概念伪代码**：

```text
on agent_wants_to_stop:
  if no_verified_test_result_for_current_diff:
      block_stop("尚无针对本次改动的通过证据")
  else:
      allow_stop()
```

注意：这不是 Claude Code 的真实函数签名，也不是默认配置。它表达的是可由 `Stop` Hook 实现的规则。真实 Hook 有自己明确的 JSON 决策契约；`decision: "block"` 可以阻止结束，`reason` 解释继续工作的原因。无限制阻止结束也会造成循环，因此应该带次数、超时和人工退出条件。

官方依据：[Stop decision control](https://code.claude.com/docs/en/hooks#stop-decision-control)、[Claude Code Hooks 最佳实践](https://support.claude.com/en/articles/14554000-claude-code-power-user-tips)。

### 4.4 对效果的真正影响，以及边界

Claude Code 在这条任务里值得研究的，不是「有几个 Hook」，而是**项目约束怎样进入上下文、探索输出怎样隔离、执行结果如何变成下一步决策、结束时如何接入验收**。

它不能自动保证修复正确：子 Agent 结论可能有误，Hook 没配置就不会替你强制验收，测试本身也可能没有覆盖真实故障。由于完整产品内核并未作为稳定的官方开源实现发布，本文不把网上流传的泄露代码分析当作可复现的正式源码契约。

---

## 5. Codex：把工具执行中的“授权、尝试、失败原因”变成显式流程

### 5.1 它在模型调用前做什么？

Codex 通过分层 `AGENTS.md` 收集项目约定：全局、仓库根目录、当前工作目录更深层的指令按顺序组合；也支持 Skills，以元信息发现合适的技能，再按需读完整内容和参考资料。长任务通过上下文压缩，避免每一次都重新携带完整历史。

放到登录 401：仓库指令可能告诉 Agent “该服务使用 pnpm，单元测试命令是 `pnpm test auth`，不要直接修改生成的客户端 SDK”。这比在开始时向模型塞入一长串通用编码规则更容易作用到正确位置。

官方依据：[AGENTS.md 的发现顺序](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[Codex Skills 与定制](https://learn.chatgpt.com/docs/customization/overview)、[Codex Prompting Guide](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide)。

### 5.2 关键源码：`ToolOrchestrator` 不是工具函数本身

Codex 的开源 Rust Runtime 有一个十分值得阅读的入口：

- [`codex-rs/core/src/tools/orchestrator.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs)

文件开头直接说明它协调工具授权、沙箱选择与沙箱拒绝后的重试。它的 `ToolOrchestrator::run()` 拿到工具实现、工具请求和运行时上下文，然后做审批判断、选择首次执行环境，并处理特定执行失败。

**真实源码签名的缩减节选**（公开 `main` 分支，路径会随版本变化）：

```rust
pub async fn run<Rq, Out, T>(
    &mut self,
    tool: &mut T,
    req: &Rq,
    tool_ctx: &ToolCtx,
) -> Result<OrchestratorRunResult<Out>, ToolError>
```

这里最重要的并不是 Rust 泛型，而是分工：模型的工具请求先进入编排器，而不是无条件交给进程执行器。`ToolCtx` 带着本次调用需要的环境与策略，编排器据此决定**在哪里执行、何时需要批准、是否允许重新尝试**。

在登录 401 的案例里：

```text
执行 pnpm test
   ├── 测试断言失败 → 把失败事实反馈模型修代码
   ├── 沙箱拒绝访问 → 按策略申请许可或解释限制
   └── 测试成功 → 可以作为验证证据的一部分
```

这是为了说明**不同错误语义**而画的教学分叉，不是 `run()` 的逐行翻译。源码中的权限升级存在策略限制与额外审批，绝不是“失败就自动脱离沙箱”。

### 5.3 Codex 的有效改进点在哪里？

Codex 值得借鉴的是让执行具备明确的**受控重试语义**：任务失败可能是业务错误，也可能是执行环境不足。如果 Harness 误把后者当成前者，模型会在错误方向修改代码；若能正确传达拒绝原因并提供经批准的下一步，才有机会继续完成任务。

另一方面，Codex 也支持 Shell、文件补丁、计划工具、上下文压缩以及跨 Turn 的会话控制。官方公开文章强调，同一 Harness 会管理会话状态、工具、审批与执行边界，但并不承诺“每次模型自然结束都已经通过测试”。

官方依据：[Codex 开源 Harness](https://developers.openai.com/blog/codex-as-a-platform)、[ToolOrchestrator 源码](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs)。

### 5.4 为什么不应把 Codex 简化成“自主运行测试”？

允许调用测试命令，不等于默认存在一个强制测试关口。Codex 的测试行为可受到模型指令、仓库约定、工具能力、外部 CI 或定制工作流影响。对 Codex 的判断应落在**具体工作流是否配置了验收条件**，而不是因为它拥有 `shell` 就推导出“保证成功”。

---

## 6. OpenCode：让长任务的信息预算和低效循环变成显式规则

### 6.1 从“读代码”到“控制阅读范围”

OpenCode 提供不同职责的 Agent（如 `build`、`plan`、`explore`），模型能够使用文件搜索、读取和 Shell 等工具，Agent 配置可以限制它能做哪些操作。在我们的任务里，`explore` 适合只读调查登录路径；真正需要修改代码时，才进入允许编辑的执行模式。

真正影响结果的不是“有多少种 Agent 名称”，而是：**只读调查能否低成本找到证据，执行 Agent 是否拥有正确的编辑与验证能力**。

官方依据：[OpenCode Agents](https://opencode.ai/docs/agents/)。

### 6.2 一个具体的长上下文方案：摘要 + 近期原文

OpenCode 公开说明了自动 Compaction 的策略：接近上下文限制时，将较早历史压缩成摘要，同时保留最近一部分对话原文。官方文档中当前默认的最近原文预算约为 **15,000 tokens**，可以配置。

```text
压缩前：早期搜索 + 多次编辑 + 工具输出 + 刚失败的测试
压缩后：历史摘要 + 最近原文（保留近期错误细节）
```

这里保留近期原文非常重要。假设上一步的失败测试暴露了 `refreshToken()` 两次并发调用的时间顺序；如果摘要把它改写成一句“认证逻辑可能有问题”，模型便失去了关键证据。

这也是压缩的代价：摘要会丢信息。保留最近原文、给摘要明确的“目标/已完成/下一步/阻塞”结构，可以降低风险，但不能完全消除。

官方依据：[OpenCode Compaction](https://opencode.ai/docs/compaction/)。

### 6.3 模型一直执行同一条失败命令，怎么办？

OpenCode 的公开会话处理逻辑中有 **`doom_loop` 检查**。它观察近期工具调用，在出现相同工具与相同参数的重复调用时走权限决策，避免无意义的重复执行。相关实现可从 OpenCode 的历史 `session/processor.ts` 源码及仓库记录核对；当前分支重构后不应把旧路径当作恒定 API。

例如：

```text
bash("pnpm test auth") → 相同失败
bash("pnpm test auth") → 相同失败
bash("pnpm test auth") → 触发重复调用策略
```

它只能识别一类**完全相同的重复行为**；把命令稍微改写、但仍没有进展时不一定能识别。另一方面，如果策略设为 `ask`、又在无法交互的环境执行，等待批准本身也可能造成停滞。所以“重复调用检测”是否真正有用，还取决于运行模式与权限决策。

官方依据：[OpenCode 权限规则](https://opencode.ai/docs/permissions/)、[相关源码](https://github.com/anomalyco/opencode/search?q=doom_loop&type=code)。

### 6.4 OpenCode 更像是在解决哪些任务失败？

它的重点在于：让工具调用行为、权限和上下文预算都可以被观察与调整。比如两次测试失败后要不要让模型继续试、允许它编辑哪些路径、多久摘要一次历史。

**但“工具结果记录完整”“存在 Plan Agent”“有格式化器”都不等于已经验证业务修复。** 在没有明确验收条件时，模型仍可能选择停下。

---

## 7. Cursor：减少“找不到代码”和“只能凭代码猜 UI”的问题

Cursor 特别值得分析，因为它与终端型 Agent 不同：编辑器上下文、代码库检索和浏览器反馈本来就是它工作环境的一部分。但核心算法并未完整开源，因此这里只讨论官方公开的可确认机制。

### 7.1 语义检索如何改变 Agent 的第一步？

在大型仓库里，问题描述是“登录后偶发跳回登录页”，真实代码可能叫 `renewSession()`，关键函数未包含 `login` 或 `401` 字样。

普通关键词查找未必能立即找到它。Cursor 公开介绍其代码库索引和语义检索：把自然语言查询映射到相关代码片段，再让 Agent 决定要读哪些文件。Cursor 在其特定内部代码库问答评估中报告，语义搜索带来**平均约 12.5% 的准确率提升**，具体收益随模型变化。

这一数据是 Cursor 的**特定内部评测结果**，不能解释为一般 Coding Agent 成功率提升 12.5%，更不能用来直接排名六个 Harness。

官方依据：[Improving Agent with Semantic Search](https://cursor.com/blog/semsearch)、[代码库索引](https://cursor.com/blog/secure-codebase-indexing)。

### 7.2 前端 Bug 的真正证据有时不在源码中

如果登录 401 只在浏览器产生，模型单看后端测试未必能定位根因。Cursor 的 Browser 工具允许 Agent 观察页面截图、控制台日志和网络请求。尤其有价值的是它将大量浏览器日志保存为文件，让 Agent 通过搜索和定向阅读取出相关行，而不是把所有日志直接送给模型。

```text
打开登录页 → 复现 401 → 检查 Network/Console
                              ↓
                         锁定失败请求
                              ↓
                         回到相关代码
                              ↓
                         修改并再次复现
```

这是**把运行环境的真实观察变成模型输入**，不是给模型提供更多背景理论。

官方依据：[Cursor Browser](https://cursor.com/docs/agent/tools/browser)、[Debug Mode：运行时日志与人工复现](https://cursor.com/blog/debug-mode)。

### 7.3 验证与回滚是两件事

Cursor 的 Checkpoints 会在 Agent 执行期间保存代码改动的可恢复快照。操作错误时可恢复文件，但**恢复 Checkpoint 不会删除聊天消息**。这个机制改善的是试错成本和可回退性，而不是“自动判断这次修复已经通过验收”。

官方依据：[Cursor Agent 与 Checkpoints](https://cursor.com/docs/agent/overview)。

### 7.4 为什么 Cursor 的闭源部分不应被猜测？

我们能确认它使用语义检索、Rules、浏览器工具、日志处理和检查点；但不能据此倒推出它内部如何排序每一次检索候选、如何给工具结果打分、精确何时自动压缩。源码研究到这里的正确做法是明确证据边界，而不是把产品宣传语当算法实现。

---

## 8. DeepAgent：把文件存储和子任务隔离用于上下文工程

你可能最熟悉这一套，但为了可比较，仍然从同一个登录 401 任务开始。

### 8.1 为什么它有大量文件工具，却不只是为了“能修改文件”？

DeepAgent 的虚拟文件系统可以连接内存、磁盘、持久化 Store 或沙箱 Backend。文件操作不仅用于修改项目，也能当作 Agent 的**外部工作记忆**。

假设测试产生 3,000 行日志。最简单的做法是把全部内容放进对话；但这会挤占上下文。DeepAgent 的 Context Management 允许将大型输出卸载到文件，在消息中留下可重新读取的位置，后续只取需要的部分。

因此要区分：

- **Offloading**：内容仍然存着，只是不随每次请求原样进入模型；
- **Summarization**：用简短总结替代一部分历史，可能丢失细节；
- **SubAgent Isolation**：调查过程放在独立上下文，主 Agent 接收精炼结果。

它们的目的不是“显得能力丰富”，而是控制模型下一轮看到的信息密度。

官方依据：[Deep Agents Overview](https://docs.langchain.com/oss/python/deepagents/overview)、[DeepAgent Context Engineering](https://docs.langchain.com/oss/python/deepagents/context-engineering)。

### 8.2 如何让工具、规划与验证协作？

DeepAgent 通过 `create_deep_agent()` 组装工具、中间件、任务计划、Backend 和可选子 Agent，使用 LangGraph 的执行基础设施推进模型/工具循环。对于登录 401，典型可行路径是：

```text
主 Agent 确定“定位复现 / 最小修复 / 验证”目标
       ↓
子 Agent 只读调查前端刷新逻辑
       ↓
主 Agent 修改代码
       ↓
Sandbox Backend 的 execute 运行测试
       ↓
失败结果返回模型，继续调整
```

这是一个**可能的执行策略**，不是 DeepAgent 强制的默认步骤。它的 Todo 工具可以帮助保持计划，Middleware 可以调整模型和工具行为，LangGraph 的中断能力可将敏感操作交给人批准。

关键限制是：规划工具不等于验证器；模型勾选 Todo，不等于 pytest 通过。要想把“测试通过”变成硬条件，仍需增加可执行的验收逻辑或外部 CI 关口。

官方依据：[Deep Agents Overview](https://docs.langchain.com/oss/javascript/deepagents/overview)、[Deep Agents Docs Source](https://github.com/langchain-ai/docs/blob/main/src/oss/deepagents/overview.mdx)。

### 8.3 与 Codex 的不同点是什么？

两者都可以读文件、执行命令、管理上下文。DeepAgent 特别方便的是**让使用者定制任务策略与能力组合**；Codex 开源 Runtime 更突出**工具执行审批、沙箱尝试和恢复控制**的明确实现。这是架构侧重，不是高低排名。想比较实际修复能力，仍需要固定模型、工具环境和验收集。

---

## 9. DeepSeek Harness（DSH）：将请求上下文和工具结果变成可复查的运行事实

DSH 的 Cordis 插件机制已经很容易吸引注意力，但如果只关心“Agent 能否完成登录 401 修复”，更值得读的是固定版本 [`5dda764ed3` 的架构文档](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/architecture.md)。

### 9.1 它在每一次模型请求前做什么？

DSH 默认 Agent Loop 以 **Turn** 组织用户的一轮任务，以 **Step** 组织一次模型请求及其工具调用。真正关键的是，请求不是直接由一个随手追加的普通数组生成：系统先组装提示词与当步可见工具，记录进入会话的消息，再从 **Session Event Log** 派生出模型历史。

在固定版本的官方执行描述中，核心数据路径是：

```text
装配系统提示和工具 Schema
       ↓
接受本步输入，提交到 Session
       ↓
derive / freeze 当前模型历史
       ↓
调用模型，保存 assistant/message
       ↓
工具执行与 tool/result 记录
       ↓
下一个 Step 再从历史构造模型请求
```

这个设计最直接的收益是：**模型看到的信息可以由已记录的事实重建，失败和重试也更容易区分已提交与未提交的行为**。它本身不保证选取的上下文就是相关的；相关性仍取决于提示词、工具和上下文策略。

依据：[固定版本架构中的 Turn Flow 与 Session Log](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/architecture.md)。

### 9.2 工具调用为什么不能等同于执行成功？

DSH 的工具执行路径包含参数处理与执行扩展点。官方给出的阶段包括：

```text
Tool Call → tools/pre-execute → tools/execute
          → tools/post-execute → tool/result
```

这些是 **DSH 自己定义的执行语义**，不是 Cordis 天然理解“工具调用”。Cordis 负责事件和插件装配，DSH ToolRuntime 决定何时检查、执行、返回以及写入结果。

若 `read` 成功但返回了无关文件，Agent 依旧可能判断错误；若 Shell 命令被权限阻止，工具结果必须清楚表达限制，而不能伪造成测试断言失败。

### 9.3 它能阻止模型过早结束吗？

DSH 的 `agent/turn-stopping` 在一轮任务准备自然结束时触发；外部逻辑可在此时追加下一步输入，让本轮继续。由此可以构建“未见测试证据则继续验证”的策略。

但这是**可实现的扩展行为**，不是所有 DSH 运行配置都自动强制测试。插件负责的验收规则是什么、是否有死循环保护，以及失败该怎样向用户报告，仍需具体实现。

依据：[固定版本的 Agent 事件与 Turn Flow](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/architecture.md)。

### 9.4 DSH 的真实取舍是什么？

DSH 最值得借鉴的是把模型请求、执行事实和实时干预分清：会话日志记录已提交的事实，实时事件允许插件在特定边界介入，工具服务完成实际执行。Cordis 让这些策略可组合、可替换，但**是否提高任务成功率仍取决于装配了哪些具体策略**。

因此，学习 DSH 时不要问“为什么它插件这么多”，而要问：它把哪些影响模型决策的信息写入日志？下一步模型能看到什么？一次工具失败会产生什么结果？Turn 的结束权究竟掌握在谁手里？

---

## 10. 重新把六种实现放进同一条任务链

把我们刚才得到的机制放回登录 401 修复任务中，会得到一张更有用的比较表。它列的是**公开可验证的设计着力点**，不是品牌优劣。

| Harness | 找到证据时的代表机制 | 执行及反馈的代表机制 | 决定是否继续的代表机制 |
|---|---|---|---|
| Claude Code | 项目指令、独立探索子 Agent、Compaction | 权限 + Pre/Post Tool Hooks | 可配置 `Stop` Hook 外部验收 |
| Codex | 分层 `AGENTS.md`、Skills、Compaction | `ToolOrchestrator` 审批/沙箱/拒绝重试 | 模型流程 + 指令/外部验收，非默认保证 |
| OpenCode | Explore/Build/Plan、摘要 + 近期原文 | 工具结果状态、权限、重复调用检查 | 模型行为与可配置规则；重复阻断不是测试验收 |
| Cursor | 语义检索、Rules、编辑器上下文 | 浏览器日志/网络/截图、终端 | 浏览器与测试反馈、人工复现；Checkpoints 负责回退 |
| DeepAgent | 文件卸载、总结、子 Agent 隔离 | Backend 与 Middleware、沙箱工具 | 计划、工具验证、可加人工或外部验收 |
| DSH | 提示词/工具组装、Session 派生历史 | ToolRuntime 管线与持久工具结果 | 可在 `agent/turn-stopping` 追加工作 |

### 10.1 哪些机制最可能直接影响最终结果？

在登录 401 的任务里，**直接改变模型下一步判断**的是正确的代码片段、失败请求和测试结果；**直接改变可行动作**的是工具接口、权限和执行环境；**直接改变结束条件**的是外部测试与验收规则。

同样的 `middleware`、`plugin`、`graph` 名称不能替代这些具体行为。相反，两套非常不同的架构，只要在相同任务上给模型相同的信息、可行行动与反馈，就可能表现很接近。

### 10.2 哪些“高级功能”并不一定提升效果？

- **更多上下文**：可能掩盖真正的证据；压缩又可能删掉关键时间顺序。
- **更多工具**：会增加工具选择成本；检索相关性与输出格式通常比绝对数量重要。
- **更多子 Agent**：可以隔离噪声，也可能引入重复调查、延迟和冲突修改。
- **更严格的权限**：提高安全性，但未设计好审批路径时可能阻断本可完成的任务。
- **更强的自动重试**：可能解决瞬时故障，也可能重复不安全的外部操作。
- **更多验证 Hook**：如果验收逻辑错误或没有退出机制，反而会陷入无止境循环。

这些不是否定机制价值，而是说明：**Harness 优化是一组有代价的工程决策。**

---

## 11. 如果要实测六种 Harness，该怎样设计一个不自欺的实验？

仅凭文档和源码，我们能确认各家*如何实现某种机制*，却不能推出某家的整体成功率更高。真正比较效果，至少要做到：

1. **固定任务与验收**：同一个仓库快照、同一个可复现的 401、同一组隐藏测试和 diff 限制。
2. **尽可能控制模型因素**：优先使用同模型、同温度/推理预算；无法控制时，就把结果标为产品级比较而不是 Harness 因果实验。
3. **记录中间轨迹**：首次命中的相关文件、工具失败类别、复试次数、上下文大小、权限等待、最终测试状态。
4. **定义独立成功标准**：退出码、回归测试、差异检查、隐藏测试；不能把“模型回答说已经修复”记作成功。
5. **做逐项消融**：在可控 Harness 内分别关闭语义检索、压缩、子 Agent、结束 Hook，观察差异。闭源产品不能改的机制，应诚实写为不可消融。
6. **报告成本和失败原因**：除了成功率，还要统计 token、时间、无意义重复、被权限卡住、误改文件和未验证结束。

一种建议的实验记录是：

| 指标 | 解释 |
|---|---|
| Hidden tests pass | 真正任务是否通过独立验收 |
| Evidence coverage | Agent 是否找到关键调用链/失败日志 |
| First useful tool | 第几次工具调用才接触到有效证据 |
| Invalid attempts | 因误判工具错误、权限或重复调用造成的浪费 |
| Unverified finish | 模型声称完成但缺少对应测试证据 |
| Token / latency | 成本与耗时 |
| Unsafe or unrelated edits | 非必要改动或越权行为 |

上面是**建议实验设计**，本文没有执行六款 Agent 的横向评测，因此不会生成虚假的成功率排行榜。

## 12. 最终认识：Harness 不是模型的“外壳”，而是决策条件的设计

经过六个项目，我们可以把最有用的研究主线压缩成三句话：

**第一，Harness 决定模型能看到什么证据。** Cursor 的语义检索、Claude Code 和 DeepAgent 的子 Agent 隔离、OpenCode 的摘要与近期原文、Codex 的项目指令与压缩、DSH 的请求/会话投影，都是在处理这个问题，但用法和保证并不相同。

**第二，Harness 决定模型能怎样行动、怎样理解行动的结果。** Codex 的沙箱编排、Claude Code 的工具和权限 Hook、Cursor 的浏览器运行反馈、OpenCode 的工具状态、DeepAgent Backend、DSH 的工具管线，决定“模型想做”如何转化为“环境实际发生了什么”。

**第三，Harness 决定什么时候有理由结束。** 只有模型自然停止，不等于真实任务验收。测试命令、浏览器复现、外部 Hook、人工批准、CI 和回归检查，是让 Agent 从“觉得完成”走向“有证据完成”的关键手段。

所以，下一次研究新的 Agent 项目，可以不急着找它的 `AgentLoop` 或 Plugin 系统，而是沿着一条真实任务追问：

> 这一步模型为什么看到了这些信息？它为什么能够执行这个动作？失败结果怎样影响下一次决策？最后是谁、根据什么证据允许它结束？

这些问题的答案，比它叫 Middleware、Cordis、StateGraph 还是 ToolOrchestrator，更接近“为什么这个 Harness 真正会干活”。

---

## 参考与可继续阅读的源码入口

以下均为本文使用的官方资料或公开源码。Claude Code 和 Cursor 部分主要是**公开行为与契约**；Codex、OpenCode、DeepAgent 和 DSH 可进一步做函数级精读。

- **Claude Code**：[Hooks Reference](https://code.claude.com/docs/en/hooks) · [Project Memory](https://code.claude.com/docs/en/memory) · [Subagents](https://code.claude.com/docs/en/sub-agents)
- **Codex**：[ToolOrchestrator 源码](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs) · [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) · [Codex as a Platform](https://developers.openai.com/blog/codex-as-a-platform)
- **OpenCode**：[Agents](https://opencode.ai/docs/agents/) · [Compaction](https://opencode.ai/docs/compaction/) · [Permissions](https://opencode.ai/docs/permissions/) · [OpenCode 仓库](https://github.com/anomalyco/opencode)
- **Cursor**：[Semantic Search](https://cursor.com/blog/semsearch) · [Browser](https://cursor.com/docs/agent/tools/browser) · [Debug Mode](https://cursor.com/blog/debug-mode) · [Agent / Checkpoints](https://cursor.com/docs/agent/overview)
- **DeepAgent**：[Python Overview](https://docs.langchain.com/oss/python/deepagents/overview) · [官方文档源码](https://github.com/langchain-ai/docs/blob/main/src/oss/deepagents/overview.mdx)
- **DSH**：[固定 Commit 架构文档](https://github.com/deepseek-ai/deepseek-harness/blob/5dda764ed3/docs/architecture.md) · [固定代码树](https://github.com/deepseek-ai/deepseek-harness/tree/5dda764ed3)
