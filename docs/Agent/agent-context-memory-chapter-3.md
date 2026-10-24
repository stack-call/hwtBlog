---
title: 上下文管理（三）：Agent 的长期记忆
sidebar_label: 第三章：长期记忆
description: 沿着优惠券 Coding Agent 的连续工程故事，从任务结束后的记忆丢失，推导出 Memory 的生命周期、存储隔离、加载、更新、纠错和权限边界，并对照 DeepAgents 与 Codex 源码。
tags: [Agent]
sidebar_position: 4
---

# 第三章：Agent 的长期记忆从哪里来？

> **接续前两章**：第一章解决 Context Overflow，第二章解决“上下文没满却仍然用不好信息”。本章继续沿用同一个**教学模拟项目**：修复电商系统的优惠券退款 Bug。
>
> 故事中的人物、轮数、日志与业务规则是为了推导设计而虚构的；标为**源码对照**的函数和机制则附有开源项目的实际代码入口。文中的方案设计不一定等同于某一个框架的完整实现。

## 1. 第二天：Agent 昨天学会的规则，今天怎么不见了？

优惠券退款 Bug 已经修复。小李关闭了 Agent 的会话。第二天，团队给 Agent 一个新任务：

> 修复订单拆单后，部分退款金额计算错误的问题。

Agent 又开始调查 `refund.py`。但它没有沿用昨天已经确认的规范：

> 退款金额必须基于实际支付金额；折扣需要按项目规则分摊；金额舍入使用 `ROUND_HALF_UP`。

小李感到奇怪：**昨天不是已经做了摘要，还保存了完整 History 吗？**

问题其实出在“保存的范围”。昨天的 Summary、工作文件、Checkpoint，可能属于那个特定的 Thread；今天新开的 Thread 没有自动获得昨天的上下文。完整历史可以用于审计和恢复，但**“能够恢复旧任务”不等于“新任务会自动继承旧知识”**。

这是我们第一次真正需要一种跨任务机制。

小李问：是不是只要把昨天的 Summary 复制到今天的 Prompt 就行？

他试了。但那份 Summary 还包含“第 42 轮回滚了某次补丁”“两个测试失败”“下一步检查分摊函数”等**昨天才有意义的临时状态**。它们不但没有帮助新任务，反而让 Agent 误以为旧任务仍未完成。

于是新问题出现了：**什么信息值得跨任务保留？**

## 2. 第一次改进：把“任务状态”和“可复用知识”分开

小李把昨天的总结拆成两份：

```text
昨天的任务状态（不自动带进新任务）
- refund.py 已修改
- 38 个测试通过，2 个失败
- 下一步核对部分退款边界条件

可复用的项目知识（新任务可能需要）
- 退款基于实际支付金额
- 优惠券需按项目规则分摊
- 金额统一使用 ROUND_HALF_UP
- 修改退款逻辑后运行指定测试集
```

到这里，我们再给新机制起名字：**Long-term Memory（长期记忆）**。

它不是“把所有过去对话都永久放进 Prompt”，而是从经历里挑出**适合未来复用的信息**，持久保存，并在适合的场景重新提供给 Agent。

我们已经有三种不同的东西：

| 对象 | 回答的问题 | 典型生命周期 |
| --- | --- | --- |
| History / Event Log | 过去发生过什么？ | 按审计或保留策略保存 |
| Working Context / Task State | 这次任务做到哪一步？ | 当前任务 / Thread |
| Long-term Memory | 下一次类似任务应当知道什么？ | 跨 Thread，直到被更新或删除 |

**为什么 Compaction Summary 不能直接等同于 Memory？**

因为它们优化的目标不一样。Compaction 的目标是**让当前任务继续**；Memory 的目标是**让未来任务受益**。二者可以使用相同的总结技术，但筛选标准和生命周期不同。

## 3. 第二次改进：有文件不代表真的能跨会话

小李先把规则写到：

`/memories/AGENTS.md`

测试当天，新任务果然能读到规则。他以为完成了长期记忆设计。但 Agent 服务重启、或者换一个新 Thread 后，规则又消失了。

小李终于意识到：**文件路径只是访问方式；文件到底能活多久，取决于其背后的存储。**

### 源码对照：DeepAgents 的 Backend 路由

[DeepAgents 官方 Memory 文档](https://github.com/langchain-ai/docs/blob/main/src/oss/deepagents/memory.mdx) 给出一种典型配置：普通工作文件走 `StateBackend`，`/memories/` 目录走 `StoreBackend`。

下面根据该文档保留**与存储路由有关的关键配置**（省略 `model`、`store` 的创建）：

```python
agent = create_deep_agent(
    model=model,
    memory=["/memories/AGENTS.md"],
    backend=CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": StoreBackend(
                namespace=lambda rt: ("shop-project",)
            ),
        },
    ),
    store=store,
)
```

这里需要理解的不是 API 名称，而是**目录前缀决定数据走哪种存储**：

```text
/workspace/test.log  ──→ StateBackend（当前 Thread 的工作文件）
/memories/AGENTS.md  ──→ StoreBackend（跨 Thread 的持久文件）
```

查看 [DeepAgents `backends/composite.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/backends/composite.py)，真实实现中有这一行：

```python
self.sorted_routes = sorted(routes.items(), key=lambda x: len(x[0]), reverse=True)
```

为什么要按前缀长度倒序？因为未来可能还有 `/memories/projects/` 这样的更具体路径。**更具体的路由应优先匹配**，不然项目专属存储可能被通用记忆路由抢走。

还要注意：`StoreBackend` 是否能在**进程重启后**持久，取决于底层 Store。如果示例使用 `InMemoryStore`，它可以演示**同一进程的跨 Thread 访问**，但不能据此推断数据库级的持久化。上线时应使用真正持久化、具备备份和隔离能力的 Store。

这一步我们推导出来的机制不是“记忆文件”，而是**Storage Lifetime + Namespace + Path Routing**。

## 4. 第三次改进：记忆已经保存，怎样进入下一次 LLM 请求？

小李现在已经确保 `/memories/AGENTS.md` 能被新 Thread 访问。

但他发现：**记忆存在 Store 里，不等于模型知道它存在。** 模型只能看到 Harness 为它组装的请求。

于是，需要在适当的时机读取 Memory，并把它加入当前上下文。

### 源码对照：DeepAgents 的 MemoryMiddleware

在 [`middleware/memory.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/memory.py) 中，`MemoryMiddleware.before_agent()` 有如下关键逻辑（摘取真实源码中的两段）：

```python
if "memory_contents" in state:
    return None

results = backend.download_files(list(self.sources))
```

这两行暴露了两个设计决定：

1. **在 Agent 执行前加载指定 Memory 来源**，而不是要求模型自己猜应该读取哪个路径。
2. **状态里已有加载结果时跳过重复加载**，避免同一次 Thread 执行中的冗余读取。

同文件的 `modify_request()` 随后从 `request.state` 读取 `memory_contents`，经 `_format_agent_memory()` 整理，再通过 `append_to_system_message()` 加入系统消息；`wrap_model_call()` 使用修改后的 Request 调用模型。

因此实际调用链可以概括为：

```text
持久化 Store
    ↓  before_agent() / download_files()
MemoryState.memory_contents
    ↓  modify_request()
System Message + Memory
    ↓  wrap_model_call()
LLM
```

这里顺便暴露一个边界：**保存和读取不是同一个动作**。记忆文件可能已被外部更新，但当前 Thread 缓存的 `memory_contents` 仍是先前版本。是否重新加载，必须由 Harness 的状态失效 / 刷新策略决定，不能凭“文件变了”就推断模型立刻知道。

## 5. 第四次改进：什么都写进 AGENTS.md，为什么又不好用了？

数周后，`AGENTS.md` 越写越长：退款规则、报表分析经验、数据库迁移方法、前端部署步骤全都混在一起。

每个新任务都会加载这些内容，Prompt 再次变得臃肿。

小李回忆起第二章的 **Progressive Disclosure（渐进披露）**，于是把知识再分开一次：

- **稳定且经常需要的项目约束**：写进 `AGENTS.md`，作为持续可用的项目说明。
- **复杂但不一定每次用的工作流程**：写成 `SKILL.md`，只在相关任务需要时加载。
- **有证据、但暂时只与单个任务相关的细节**：放在任务文件、源码或历史记录中，按需查询。

这不是单纯按“重要/不重要”分类，而是按**访问频率、使用方式、存活范围**共同决定放在哪里。

### 源码对照：Codex 也采用分层项目指令

在 [Codex `codex-rs/core/src/agents_md.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/agents_md.rs) 中，项目指令的发现逻辑会从项目根目录沿路径向当前工作目录收集 `AGENTS.md`（并支持配置的备用文件名），再组装进本次运行的指令上下文。

[Codex 的 `AGENTS.md` 说明](https://learn.chatgpt.com/docs/agent-configuration/agents-md) 还描述了全局与项目级说明的合并、覆盖文件和大小限制。

但要准确区分：Codex 读取 `AGENTS.md`，证明它有**跨任务复用项目指令**的入口；这**不等于** Codex 会自动把每次聊天总结并写进 `AGENTS.md`。加载规则和记忆写入策略，是两个独立的功能。

## 6. 第五次改进：长期记忆一旦记错，会比临时上下文更危险

一天，团队调整了退款规则：原先的折扣分摊方式被新的财务规范取代，代码和测试已经更新。

但 Agent 仍然从 Memory 中读到旧规则，并且每次新开任务都继续按照旧规则修改代码。

小李这才发现：**Memory 的持久性既是价值，也是风险。错误信息会被稳定、反复地重新注入。**

如果让模型在每次任务结束后直接执行：

```text
append(summary, AGENTS.md)
```

看似不断积累经验，实际可能同时存下互相矛盾的规则。

一个更稳妥的设计是把“记忆提取”与“记忆入库”分开：

```text
Agent 的本次发现
    ↓
候选记忆：它适合跨任务吗？
    ↓
证据核对：代码 / 测试 / 文档 / 用户确认
    ↓
与旧记忆比较：新增？冲突？已过期？
    ↓
更新、替换或拒绝写入
    ↓
下一任务按范围加载
```

例如昨天 Agent 以为“部分退款必须向下取整”，这是从一条失败日志里猜出来的；后来财务规范确认“使用 `ROUND_HALF_UP`”。**失败日志只是观察，不能自动提升为项目规则。**

如果是我们自己设计 Memory 系统，可以为候选事实维护来源、所属项目、确认状态和最后验证时间。这里是**推荐的扩展设计**，不是宣称 DeepAgents 内置了相同的数据结构。

### 源码对照：DeepAgents 怎样支持修改和整理 Memory？

DeepAgents 的 [Memory 文档](https://github.com/langchain-ai/docs/blob/main/src/oss/deepagents/memory.mdx) 说明，Agent 可以使用 `edit_file` 更新有写权限的 Memory 文件；也介绍了单独的 **Background Consolidation Agent**：在会话之间读取历史，合并关键发现，剔除旧信息。

这说明“读取记忆”和“维护记忆质量”不是同一条调用链。`MemoryMiddleware` 负责加载，写入由文件工具、存储策略或单独的整理任务负责。

这里也必须留下工程上的判断：**是否应当写入高优先级项目规则，不能完全交给一次未经验证的 LLM 总结。** 对共享规则，合理的做法是要求证据或人工审核。

## 7. 第六次改进：项目记忆会不会泄漏给另一个用户？

Agent 从内部工具发展成多租户服务。用户 A 负责项目 Alpha，用户 B 负责项目 Beta。

如果两人都把 Memory 写到相同的 Store Namespace，B 的下一次模型请求可能读到 A 的项目规则，甚至访问 A 的私有信息。

这不是摘要质量问题，而是**存储作用域与权限隔离问题**。

小李引入三种不同的范围：

| 记忆范围 | 例子 | 建议隔离键 |
| --- | --- | --- |
| 任务级 | 当前 Bug 的调查进度 | `thread_id` |
| 项目级 | 本仓库的退款规则 | `project_id` |
| 用户级 | 某位用户的使用偏好 | `user_id` |

必要时还要组合，例如 `(assistant_id, project_id, user_id)`。键的具体形式并不重要；重要的是**哪个主体有权读写哪些知识**。

[DeepAgents Memory 文档中的 `StoreBackend(namespace=...)` 示例](https://github.com/langchain-ai/docs/blob/main/src/oss/deepagents/memory.mdx) 正是通过 Namespace 实现 agent-scoped 或 user-scoped Memory。文档同时提醒：共享政策应优先设为**只读**，敏感写入可以加审批；并发写同一文件可能出现覆盖冲突，需要整理或并发控制。

又一次，我们被问题逼出了新的设计：**Memory 不只是存储和检索系统，还是一个有数据所有权、权限和更新规则的系统。**

## 8. 现在把整个架构重新接起来

从第 30 轮 Context Overflow，到今天的跨任务知识管理，我们的 Coding Agent 逐步形成了如下流程：

```text
                 用户开始一个新任务
                         |
                         v
             确定身份、项目与 Thread
                         |
          +--------------+--------------+
          |                             |
          v                             v
    读取长期 Memory                恢复当前 Task State
 (项目规范 / 用户偏好)          (当前任务的进度与文件)
          |                             |
          +--------------+--------------+
                         v
                  Context Builder
         (挑选相关事实、控制 Token、处理冲突)
                         |
                         v
                       LLM
                         |
                         v
               Tool / Code / Test
                         |
                         v
                  原始事件进入 History
                         |
                         v
                    任务完成之后
                         |
                         v
                生成候选长期记忆
                         |
                         v
                  验证与冲突处理
                         |
                         v
                写回持久化 Memory
```

这张图是**我们推导出的推荐架构**，不是某个开源项目原封不动的内部模块图。

以前我们只关心“怎么把信息放进上下文”；现在更清楚了：在决定放什么之前，还要决定**谁的信息、属于哪次任务、是否仍然有效、谁能修改，以及何时刷新**。

## 9. 本章用五个问题复盘

**是什么？** 长期记忆是将未来可复用的信息持久保存，并通过 Harness 在适当范围、适当时机提供给 Agent 的机制。

**为什么需要？** 新 Thread 不应该重新发现每一条稳定的项目规则，也不应该继承所有旧任务的临时噪声。

**没有它会怎样？** Agent 每次从零调查相同的约束；如果盲目复制旧 Summary，又容易误用昨天的任务状态。

**实际怎么做？** 将工作状态与长期知识分开；通过持久 Backend 和 Namespace 保存记忆；在模型调用前加载；用可验证的写入、更新、过期与权限策略维护质量。

**处于体系哪里？** Memory 属于 Context Management 的**跨任务信息供给层**。它连接 Storage、Context Builder 与安全边界，但不能取代 History、Compaction 或实时工具查询。

### 一次独立验证：把条件换掉

假设今天要开发的是企业内部的 **SQL 数据分析 Agent**，不同部门使用不同的数据口径。昨天财务部门更改了净收入定义，销售部门仍在使用旧定义。

沿着本章的推理，我们不应该把“最新财务口径”直接加到全公司共享的记忆文件，而应先核对其生效范围、版本与来源，再更新相应部门的只读规则，并验证其他部门不会错误继承。

这说明我们真正学会的不是 `AGENTS.md` 这个文件名，而是：**对跨会话知识进行生命周期管理的能力**。

## 本章源码阅读索引

建议按下面的顺序读，每次只追一个具体问题，不用一次阅读完整文件：

1. **[DeepAgents Memory 文档](https://github.com/langchain-ai/docs/blob/main/src/oss/deepagents/memory.mdx)**：先看 `memory=`、Scope、读写权限、Background Consolidation，理解产品级行为。
2. **[DeepAgents `backends/composite.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/backends/composite.py)**：看 `CompositeBackend.__init__()` 与路径匹配，理解为什么存活范围由 Backend 决定。
3. **[DeepAgents `middleware/memory.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/memory.py)**：依次看 `before_agent()`、`modify_request()`、`wrap_model_call()`，追踪 Memory 从文件到 System Message 的调用链。
4. **[Codex `codex-rs/core/src/agents_md.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/agents_md.rs)**：看项目指令发现与拼接，理解层级化项目上下文，而不要把“读取指令”误认为“自动写入记忆”。

## 下一章预告：当多个 Agent 同时工作，Context 应该怎样隔离和传递？

退款项目越来越大：一个 Agent 调查订单，一个 Agent 修改优惠券模块，第三个 Agent 运行测试。主 Agent 如果收集三者全部中间过程，上下文又会膨胀；但如果只收最终一句“完成”，又不知道能否相信结果。

下一章我们会从这个矛盾出发，推导 **Subagent Context Isolation、Handoff Contract、共享工件与结果验证**，并对照真实 Harness 的子 Agent 调度设计。

> [!TIP] 上下文管理与 KV Cache 的关系
> Context Management 决定模型看什么，KV Cache 决定哪些 Token 的计算可以复用。
>
> - 上下文长度 ≠ 实际计算量：KV Cache 可以减少重复计算，但不会减少上下文占用的 Token 数量。
> - 前缀稳定有利于缓存命中：如果每轮只在末尾追加消息，推理引擎通常可以复用已有前缀的 KV Cache。
> - 修改历史可能导致缓存失效：Compaction、修改 System Prompt 或替换旧消息，会改变 Token 前缀，使变化位置之后的缓存难以复用。
> - 压缩与缓存需要权衡：Compaction 虽然可能降低缓存命中率，却能减少上下文长度，避免溢出并降低后续推理成本。
>
> 设计原则：尽量保持前缀稳定、增量追加新消息，在必要时再进行上下文压缩。不要为了缓存命中率牺牲上下文质量。