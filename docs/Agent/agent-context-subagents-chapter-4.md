---
title: 上下文管理（四）：Subagent 的上下文隔离与任务交接
sidebar_label: 第四章：Subagent 与上下文隔离
description: 沿着优惠券 Coding Agent 的工程故事，推导出子 Agent 的上下文隔离、任务交接协议、isolated 与 fork 选择、结果回传和并行协作边界，并对照 DeepAgents 与 Codex 源码。
tags: [Agent]
sidebar_position: 5
---

# 第四章：为什么主 Agent 不应该知道子 Agent 的每一步？

> **接续前三章**：我们已经解决了 Context Overflow、Context Rot，以及跨会话 Memory 的生命周期问题。这一章继续沿用同一个**教学模拟项目**：修复电商系统的优惠券退款 Bug。
>
> 本文中的人物、日志、轮数和业务需求是教学场景；**源码对照**部分才是实际开源项目的机制。源码以 **2026-10-24** 阅读到的版本为参考，后续版本可能变化。代码片段刻意保留最关键的语句，其余用文字说明。

## 1. 第 220 轮：一个 Agent 同时调查三件事

优惠券退款 Bug 基本修好后，团队提出最后的上线要求：

> 检查订单金额、退款金额和对账金额是否始终一致；补齐回归测试，确认没有破坏旧逻辑。

这个任务自然分成了三个方向：

- **订单模块**：检查折扣如何分摊到各个商品。
- **退款模块**：检查部分退款和舍入规则。
- **测试模块**：设计覆盖订单、优惠券、退款组合的回归测试。

小李最初只用一个 Agent：先读订单代码，再读退款代码，再跑测试。它一路积累文件片段、错误日志和尝试失败的修改。

一个下午过去，主 Agent 又遇到了之前熟悉的问题：即使大型 Tool Result 已经卸载到文件系统，三个方向的中间调查记录还是不断挤占工作上下文。

小李问自己：**主 Agent 真有必要亲自阅读三个方向的每一条搜索结果吗？**

其实不需要。主 Agent 的真正工作是决定做什么、判断结果是否满足目标，并完成最终集成；不同模块的细节调查可以交给别的执行单元。

于是，他第一次尝试了子 Agent。

```text
                    Main Agent
                  上线前一致性检查
                          |
              +-----------+-----------+
              |           |           |
              v           v           v
           订单调查      退款调查      测试调查
              |           |           |
              +-----------+-----------+
                          |
                     汇总与验收
```

这里刚刚产生的需求，不是“再多一个聊天机器人”，而是**让繁重的中间过程留在局部，只把完成主任务所需的信息交回主 Agent**。

这个设计叫作 **Subagent Delegation（子 Agent 委派）**；从上下文管理角度看，它最重要的作用之一是 **Context Isolation / Context Quarantine（上下文隔离）**。

## 2. 第一次失败：把全部 History 复制给每个子 Agent

小李先用了最容易写出的实现：

```python
# 教学伪代码：有意保留初版缺陷
for task in subtasks:
    child = create_agent()
    child.run(messages=parent_messages + [task])
```

这样每个子 Agent 都了解父 Agent 的完整背景，看起来很稳妥。

但问题立刻出现：父 Agent 已经有很长的历史，三个子 Agent 各自再读取一遍。每个子任务的上下文都被父任务的冗余信息占据，计算与 Token 成本增长，子 Agent 甚至会被无关的旧决策干扰。

更糟糕的是，**父 Agent 的完整历史不等于子任务需要的上下文**。测试设计者不需要看订单模块几十次失败的 `grep`；退款审查者也不必继承实现者所有尚未验证的猜测。

因此小李作了第一次改进：每个子 Agent 启动时只接收一条委派任务，拥有自己的消息历史；它执行工具的中间过程不再自动加入主 Agent 的消息列表。

```text
Main Agent Context                  Refund Subagent Context
------------------                  -----------------------
任务与总体计划                       明确的退款审查任务
委派 task(...)       ------------->   read_file / grep / test
等待工具结果                         更多局部工具调用
最终审查报告         <-------------   最终结论
```

注意，隔离的是**对话消息历史**，不必然隔离工作目录、工具权限、运行时配置或文件系统。子 Agent 是否能读写相同文件，取决于具体 Harness 和沙箱配置，不能仅凭 `isolated` 一词推断它有安全隔离。

### 源码对照：DeepAgents 怎样制造“干净的子上下文”？

打开 [`deepagents/middleware/subagents.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py)，定位 **`_validate_and_prepare_state()`**。

默认的 `isolated` 分支中，最关键的一行就是：

```python
subagent_state["messages"] = [HumanMessage(content=description)]
```

这不是简单地给父 Agent 再追加一条消息；它在准备子 Agent 状态时，**明确用委派任务替换了子 Agent 的模型消息历史**。其他允许传递的状态字段会经过筛选，因此“消息隔离”并不意味着一切状态都从零开始。[源码](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py)

再沿着调用链读下去：

```text
父 Agent 调用 task(description, subagent_type)
    → _validate_and_prepare_state(...)
    → subagent.invoke(subagent_state, ...)
    → _return_command_with_state_update(...)
    → 父 Agent 收到 ToolMessage
```

最重要的不是 API 名称，而是两个**边界**：

1. **输入边界**：哪些父级信息允许进入子 Agent？
2. **输出边界**：哪些子级结果允许返回父 Agent？

## 3. 第二次失败：上下文隔离了，关键业务规则也丢了

退款子 Agent 很快返回：

> 部分退款测试可能有金额偏差，建议使用通常的四舍五入处理。

小李一看就发现问题：项目要求 `ROUND_HALF_UP`，而且退款金额必须以实际支付金额为基准。这些规则在父 Agent 的前 200 轮历史里已经确认，但子 Agent 从头启动，只收到一句“检查退款模块”。

隔离成功了，却导致**上下文交接失败**。

为什么？因为我们把两个问题混在了一起：

- 不让子 Agent 继承所有历史，是为了避免污染。
- 让子 Agent 拥有完成任务的必要背景，是为了保证正确性。

前者不能替代后者。

小李意识到，`task` 不该只是“做什么”的短标题，而应当是一份**可独立执行的任务交接单**。

### 任务交接应该写什么？

```text
目标：独立检查部分退款计算是否正确。

已确认规则：
- 基于订单实际支付金额退款。
- 折扣按商品份额分摊。
- 金额舍入采用 ROUND_HALF_UP。

检查范围：refund.py、tests/test_refund.py。
限制：只分析与运行测试，不修改生产代码。

输出：发现的问题、证据（文件/行号/测试命令）、
未确认的假设、建议的下一步。
```

这里的规则来自父 Agent 已确认的事实；测试范围和权限限制是当前委派要求。它们比大量原始历史更短，却通常更有用。

我们由此得出 **Task Handoff Contract（任务交接契约）**：

| 交接内容 | 为什么需要 |
| --- | --- |
| 目标与完成条件 | 防止子 Agent 只做局部调查却不知道“完成”是什么 |
| 已确认事实和限制 | 防止重复猜测、违背业务规则 |
| 文件路径 / Artifact 引用 | 让子 Agent 按需恢复证据，而不是复制所有日志 |
| 读写范围与权限 | 降低越权修改和并行冲突风险 |
| 预期输出与验证证据 | 让主 Agent 能判断结果是否可靠 |

**这与第一章的 Offloading 是连在一起的**：大的证据放在文件里，委派任务只传递必要结论和可回读路径。

## 4. 第三次失败：子 Agent 完成任务，却回传了一本“流水账”

现在子 Agent 已经知道自己的任务了。它调查 25 轮，找到问题，返回给主 Agent。

结果它把全部搜索记录、文件片段、测试日志、每一轮尝试都写进了最终回复，超过 18k tokens。

主 Agent 刚刚获得的上下文空间，又被子 Agent 的返回值填满。

小李发现，**只隔离输入还不够，必须限制输出的形状与体积**。

于是他规定，子 Agent 返回的是可以被主 Agent 验收的结果，而不是完整过程：

```json
{
  "status": "needs_fix",
  "finding": "部分退款存在舍入不一致",
  "evidence": ["refund.py:87", "test_partial_refund_with_coupon"],
  "artifacts": ["/artifacts/refund-review.txt"],
  "next_action": "补充金额分摊的边界测试"
}
```

这是一份**教学用交接数据结构**，不是 DeepAgents 的固定内置 Schema。实际项目还可增加 `confidence`、`changed_files`、`tests_run` 和 `unresolved_questions` 等字段。

### 源码对照：DeepAgents 怎样把子结果放回父对话？

还是看 [`subagents.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py) 的 **`_return_command_with_state_update()`**。

它会优先使用子 Agent 的 `structured_response`（如果配置了结构化输出）；否则从子消息中寻找最后一条非空的 `AIMessage` 文本。之后将其包装为对应工具调用的 `ToolMessage`，而不是把所有子 Agent 的消息直接拼进父 Agent 的历史。

关键逻辑可以概括为：

```python
# 依据源码简化，只展示决定父 Agent 看什么的部分
content = serialize(structured_response) or last_nonempty_ai_text
parent_tool_result = ToolMessage(content, tool_call_id=tool_call_id)
```

这里的 `serialize` 和 `last_nonempty_ai_text` 是**解释性占位函数，并非仓库原函数名**。源码还会在允许的情况下回传部分经过筛选的状态更新；因此不能理解成“子 Agent 只能传一条文本、绝不会影响共享状态”。

如果你需要稳定的交接格式，可在 DeepAgents 子 Agent 配置中使用 `response_format`。它让父 Agent 接收到可解析的结构化结果，而不只是自由文本。[官方说明](https://docs.langchain.com/oss/python/deepagents/subagents)

## 5. 第四次失败：隔离得太彻底，子 Agent 只好重新调查一遍

故事又出现了一个转折。

主 Agent 已经定位退款金额错误的确切代码，读过相关业务规则和测试；现在只是想让另一个 Agent **接着完成修复并验证**。

如果还使用 `isolated` 模式，子 Agent 往往需要重新读文件、重新确认调用链，再理解为什么这么改。

小李这才发现，不同子任务对上下文的需求可能截然相反：

- **实现者（Worker）**：接着父 Agent 的调查往下做，继承历史可能节省大量重复工作。
- **审查者（Reviewer）**：独立检查父 Agent 的结论，不继承其推测反而可能降低偏见。

因此，子 Agent 的上下文模式需要可以选择，而不是永远“全部继承”或“完全隔离”。

### `isolated` 与 `fork`：只改变一个条件

| | `isolated` | `fork` |
| --- | --- | --- |
| 子 Agent 的消息起点 | 委派任务描述 | 父 Agent 的有效会话上下文，再追加新任务 |
| 优点 | 干净、聚焦、更容易独立验证 | 保留前期调查结果、减少重复检索 |
| 代价 | 必须明确交接关键背景 | 带入无关历史、消耗更大初始上下文 |
| 适合 | 独立评审、独立探索 | 继续修改已经调查清楚的代码 |

### 源码对照：DeepAgents 的 `fork` 不是简单复制原始数组

在 [`SubAgent.mode`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py) 中，`isolated` 是默认模式，`fork` 是**实验性功能**。

`_validate_and_prepare_state()` 在 `fork` 分支会使用 **`_fork_messages(...)`**。继续追到 `_fork_messages()`，可以看到它会处理父 Agent 的有效消息（包括已发生的 Summarization），去掉尾部正在发起委派的工具调用，并追加“你正在作为子 Agent 继续任务”的说明和新任务描述。

所以 `fork` 更接近**父 Agent 当前可用工作上下文的一次分支延续**，不能简单理解为“无条件复制磁盘上所有原始 History”。[源码](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py)

而且，这里也能和上一章的 **KV Cache** 联系起来：如果 Fork 保持父 Agent 已处理的 Prompt 前缀稳定，推理引擎可能复用部分前缀缓存；相反，如果对子 Agent 的 System Prompt 和工具定义进行了不同的前缀重写，缓存收益可能下降。**能否命中仍取决于模型服务端的实现和缓存状态**。[LangChain 设计文章](https://www.langchain.com/blog/organizing-context-in-a-multi-agent-harness)

## 6. Codex 怎么做：子 Agent 是一个受控的独立执行线程

DeepAgents 的 `task()` 默认是同步委派：主 Agent 等子任务执行完成再接收结果。Codex 提供的多 Agent 机制则更强调对子线程的创建、状态管理和后续协作。

沿着公开源码看：

```text
spawn_agent 工具调用
    → handlers/multi_agents/spawn.rs
    → 准备子 Agent 的配置及上下文模式
    → AgentControl / spawn 请求
    → 创建子线程并交付任务
```

在 [`codex-rs/core/src/tools/handlers/multi_agents/spawn.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents/spawn.rs) 中，可以看到一个决定是否 Fork 父历史的关键语句：

```rust
let fork_mode = args.fork_context.then_some(SpawnAgentForkMode::FullHistory);
```

再跟到 [`codex-rs/core/src/agent/control/spawn.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/agent/control/spawn.rs)：当请求包含相应 Fork 模式时，运行时会准备父历史并把它作为子线程初始历史；否则走另一路创建流程。子线程还要继承经过处理的配置与执行环境，不能把上下文复制和权限继承混为一谈。

这里值得学的不是某个工具参数，而是：**Subagent 的上下文初始化，是运行时生命周期管理的一部分，不是普通的 Prompt 拼接。**

## 7. 第五次失败：并行子 Agent 都改了同一个文件

小李觉得任务拆分成功，于是让订单 Agent 和退款 Agent 同时修改代码。

两者碰巧都修改了 `refund.py`。一个改变折扣分摊公式，另一个更新返回金额的舍入逻辑。某个 Agent 运行测试时，读到的却是另一位 Agent 正在修改中的版本。

这是新的问题：**上下文已经隔离，但共享工作区里的写入没有隔离。**

因此，多 Agent Harness 必须单独设计对共享资源的协调策略：

1. **最简单的策略**：调查任务可并行，但主 Agent 统一执行代码修改和集成。
2. **并行写入策略**：给每个子 Agent 分配互不重叠的文件或模块所有权；必要时使用独立工作树或快照。
3. **验收策略**：子 Agent 返回改动位置与测试结果，主 Agent 复核 Diff，并在统一代码状态下重新运行测试。

这与 Context Isolation 是两个正交的维度：

| 维度 | 要控制什么 |
| --- | --- |
| 对话上下文隔离 | 谁能看哪些消息、证据和任务背景 |
| 资源与权限隔离 | 谁能读写哪些文件、运行什么命令 |
| 结果合并 | 哪些子任务的状态与代码改动可以提交给主 Agent |

Codex 的 `spawn_agent` 相关工具定义中，也明确强调对子任务的写入范围进行拆分，避免并行工作修改互相重叠的代码区域。[源码](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents_spec.rs)

## 8. 小李最终设计了怎样的 Subagent Harness？

经历了这五次失败，他不再把“开多个 Agent”当作目标，而是设计了一套有边界的委派机制：

```text
                       Parent Agent
             任务拆解、验收标准、最终集成
                              |
                    Delegation Contract
                    任务/事实/权限/产物
                              |
                 +------------+------------+
                 |                         |
           Isolated Child              Forked Child
          独立验证/调查                继续父任务工作
                 |                         |
           局部工具历史                局部工具历史
                 +------------+------------+
                              |
                结构化结果 + Artifact 引用
                              |
                      Parent Validation
                    证据、Diff、测试复核
```

真正要保存的，不只是 `messages`，还有**委派边界**本身：任务 ID、父子关系、使用的上下文模式、允许访问的资源、任务产物与验证记录。

对照本章涉及的核心概念，我们可以回答最初提出的五个问题：

| 问题 | 本章得到的答案 |
| --- | --- |
| 是什么？ | Subagent 是独立执行局部任务的 Agent；上下文隔离是对其输入和输出信息边界的管理。 |
| 为什么需要？ | 避免父 Agent 的工作上下文被每个子任务的中间搜索、日志和尝试填满。 |
| 没有会怎样？ | 一个 Agent 承担全部细节，或多 Agent 之间无节制复制长历史、返回大量无用结果。 |
| 实际怎么做？ | 用 `task` / `spawn_agent` 委派；选择 `isolated` 或 `fork`；以契约传入必要信息；以结果和证据返回。 |
| 处于体系哪里？ | 它连接 Context Builder、Tool Runtime、Artifact Store、权限系统与多 Agent 协调器。 |

### 如何验证自己真的掌握了？

拿同一个优惠券项目做两组 A/B 测试：

- **测试 A：继续修复已定位 Bug**。比较 `isolated` 与 `fork` 的重复文件读取次数、总 Token、耗时，以及测试通过率。预计 Fork *可能*更省重复调查，但不保证总成本一定更低。
- **测试 B：独立审核修复方案**。比较两种模式发现真正缺陷的能力，观察继承父 Agent 判断是否影响独立性。

同时记录：父 Agent 最终增加了多少 Token、子 Agent 输出中有多少可验证证据、两个 Agent 是否碰撞修改了同一文件。不要只统计“创建了多少子 Agent”。

再换一个类似但不同的任务——**修复库存扣减与支付回调的并发一致性问题**：让一个子 Agent 调查事务边界，一个独立审查者找竞争条件，主 Agent 最终整合。能否推导正确的上下文模式、交接信息、写入权限和验收条件，比记住 `fork` 参数更重要。

## 9. 源码阅读索引

按“为什么需要它 → 从哪里调用 → 怎样实现 → 代价是什么”的顺序读，而不是逐行读：

| 研究点 | 推荐源码入口 | 阅读重点 |
| --- | --- | --- |
| 子 Agent 的输入边界 | [DeepAgents `subagents.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py) | `TaskToolSchema`、`_validate_and_prepare_state()` |
| 子 Agent 如何交接结果 | [DeepAgents `subagents.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py) | `task()`、`_return_command_with_state_update()` |
| 继承父上下文的细节 | [DeepAgents `subagents.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py) | `_fork_messages()`、压缩事件如何作用于 Fork |
| 子线程创建与权限配置 | [Codex `multi_agents/spawn.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents/spawn.rs) | `fork_context`、深度限制、Spawn 请求 |
| Fork 历史如何进入子线程 | [Codex `agent/control/spawn.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/agent/control/spawn.rs) | `prepare_forked_history()`、`initial_history` |
| 设计讨论 | [LangChain：Organizing Context in a Multi-Agent Harness](https://www.langchain.com/blog/organizing-context-in-a-multi-agent-harness) | Worker 与 Reviewer 为什么应选不同上下文模式 |

> [!TIP] 本章最重要的一句话
> **Subagent 不是把工作拆开就结束了，而是要设计两道信息边界：给子 Agent 什么上下文，以及让它向主 Agent 交回什么。** `isolated` 和 `fork` 是两种不同的上下文交接策略；共享文件、权限与最终验收则需要另外管理。
