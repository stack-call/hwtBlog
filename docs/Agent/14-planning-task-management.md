---
title: 第十四章：Planning——Agent 为什么会一边修 Bug 一边忘记目标？
sidebar_label: 第十四章：Planning
description: 从复杂修复任务反复调查推导计划、任务状态、依赖、重规划与适用边界
sidebar_position: 15
tags: [Agent, Harness, Planning, Codex, DeepAgents]
---



# 第十四章：Planning & Task Management——Agent 为什么需要计划？

上一章，我们已经让 Coding Agent 具备了执行能力：

- Agent Loop 决定什么时候调用模型、什么时候继续。
- Tool System 将模型的工具请求转换成真实操作。
- Execution Environment 提供代码运行环境。
- Permission System 控制哪些操作可以执行。

现在，Agent 已经能够自主读取代码、修改文件和运行测试。

但小李发现了一个奇怪的现象：

**Agent 明明一直在执行工具，却不一定朝着用户的目标前进。**

## 一、第一次事故：Agent 忙了两个小时，却没有完成任务

还是那个优惠券退款 Bug。

这一次，用户提出了更完整的要求：

> 修复优惠券退款错误，确保普通退款、部分退款都正确，并添加回归测试。不要改变现有订单 API。

Agent 开始执行：

```text
第 1 轮：搜索退款计算函数
第 5 轮：找到 refund.py
第 12 轮：修改退款公式
第 18 轮：运行测试
第 23 轮：发现部分退款仍然失败
第 30 轮：再次修改退款公式
第 38 轮：重新运行测试
第 45 轮：发现订单 API 参数不一致
第 53 轮：修改订单 API
第 61 轮：部分测试通过
第 70 轮：再次修改 refund.py
...
```

两个小时后，Agent 输出：

```text
已修复退款金额计算问题。
```

但用户检查发现：

1. 普通退款通过了。
2. 部分退款仍然有一个边界测试失败。
3. 订单 API 被意外修改。
4. 回归测试并没有补全。

小李很困惑：

“我们的 Agent Loop 不是一直正常运行吗？为什么最终结果还是不完整？”

### 问题不在 Agent Loop

回顾上一章，Agent Loop 负责：

```text
调用 LLM
   ↓
执行 Tool Call
   ↓
返回 Tool Result
   ↓
调用 LLM
   ↓
……
```

这个循环可以保证模型不断获得执行机会。

但它并不保证模型始终记得：

- 用户最初要求了什么。
- 哪些要求已经完成。
- 哪些要求还没有完成。
- 哪些约束绝对不能违反。
- 当前修改是否偏离整体目标。

这里出现了一个根本区别：

**Agent Loop 管理的是执行过程，而不是目标完成情况。**

换句话说：

> Agent 能够持续采取行动，不代表它能够持续管理目标。

那么怎么办？

小李决定先采用最简单的办法。

---

## 二、第一次改进：执行之前先列出步骤

小李没有立即引入复杂框架。

他只是在 System Prompt 中加了一句话：

> 对于复杂任务，先拆分任务，列出执行步骤，再开始修改代码。

这一次，模型生成了计划：

```text
目标：修复优惠券退款 Bug

1. 定位退款金额计算逻辑
2. 确认优惠券退款业务规则
3. 修复普通退款计算
4. 修复部分退款计算
5. 添加回归测试
6. 运行测试并检查 API 兼容性
```

然后再开始执行工具。

这看起来已经好多了。

因为原本模糊的任务被拆成了一组明确的子目标。

### 为什么任务拆解有帮助？

原来的目标：

```text
修复优惠券退款 Bug
```

实际上包含多种不同工作：

```text
理解问题
    ↓
定位代码
    ↓
确定预期行为
    ↓
修改实现
    ↓
验证正确性
```

每个阶段需要不同工具，也有不同的完成条件。

如果不拆解，模型可能刚修好一个测试，就错误地认为整个任务完成了。

拆解之后，至少可以观察到还有哪些工作没做。

这就是最简单的 **Task Decomposition（任务拆解）**。

但小李很快发现：只有计划还不够。

---

## 三、第二次事故：Agent 有计划，却没有按照计划执行

Agent 在最初生成了六个步骤。

但执行到第 40 轮后，模型又开始重新调查最初已经确认的退款业务规则。

而且，它忘记了自己还需要补充回归测试。

为什么？

因为最初生成的计划只是一段普通聊天消息。

随着 Agent Loop 不断执行，新的工具结果和模型消息持续加入上下文。

原来的计划可能：

- 被大量工具输出淹没。
- 在 Context Compaction 时被压缩。
- 因为新的局部问题而不再被模型关注。

小李意识到：

**计划不能只是在开始时说一次，它必须能在整个任务执行期间被持续维护。**

于是，他设计一个结构化任务列表。

```json
{
  "tasks": [
    {
      "content": "定位退款计算逻辑",
      "status": "completed"
    },
    {
      "content": "修复普通退款",
      "status": "completed"
    },
    {
      "content": "修复部分退款",
      "status": "in_progress"
    },
    {
      "content": "添加回归测试",
      "status": "pending"
    },
    {
      "content": "验证订单 API 兼容性",
      "status": "pending"
    }
  ]
}
```

现在每个任务都具有明确状态。

最简单的状态只需要三个：

| 状态 | 含义 |
|---|---|
| `pending` | 尚未开始 |
| `in_progress` | 正在执行 |
| `completed` | 已完成 |

### 为什么要结构化？

假设模型只是输出：

```text
我已经完成前两个任务，正在处理第三个任务。
```

Harness 很难可靠地基于这句话更新系统状态。

但如果它调用：

```text
write_todos(...)
```

Harness 就能获得结构化数据，并把它存入 Agent State。

现在：

```text
LLM
 |
 | write_todos
 v
Tool Runtime
 |
 v
Task State
 |
 +--> UI 展示当前进度
 |
 +--> Agent 后续继续读取
 |
 +--> Checkpoint 保存
```

到这里，我们自然引入了 **Task Tracking（任务进度追踪）**。

但要注意：

**任务列表是 Agent 对当前工作的记录，不是已经发生的事实本身。**

例如，模型将某个任务标记为 `completed`，不代表测试一定通过。

这件事后面还会成为一个新问题。

---

## 四、源码对照：DeepAgents 如何实现结构化任务列表？

现在去看真实项目。

打开：

[LangChain TodoListMiddleware 源码](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/todo.py)

虽然我们研究的是 DeepAgents，但当前的 Todo 能力主要复用 LangChain 的 Middleware 实现。

### 4.1 任务状态长什么样？

在源码中可以看到：

```python
class Todo(TypedDict):
    content: str
    status: Literal[
        "pending",
        "in_progress",
        "completed",
    ]
```

这与我们刚才推导的结构基本相同。

更重要的是，源码还定义了：

```python
class PlanningState(AgentState[ResponseT]):
    todos: Annotated[
        NotRequired[list[Todo]],
        OmitFromInput
    ]
```

这段代码说明：

**Todo 不是单纯的一段聊天文本，而是 Agent State 中的结构化字段。**

`OmitFromInput` 使该字段不作为直接外部输入使用，而是由 Agent 的状态机制维护。

### 4.2 模型怎么更新 Todo？

再看 `write_todos`。

源码中一个关键操作是：

```python
return Command(
    update={
        "todos": todos,
        "messages": [
            ToolMessage(
                f"Updated todo list to {todos}",
                tool_call_id=tool_call_id,
            )
        ],
    }
)
```

这是真实实现中的核心结构，省略了外围函数定义。

这里同时做了两件事：

**第一件事：更新 Agent State。**

```python
"todos": todos
```

**第二件事：向对话历史记录工具结果。**

```python
"messages": [ToolMessage(...)]
```

为什么两个都需要？

因为它们解决的是不同问题。

State 使任务进度能够作为结构化数据保存和继续使用；ToolMessage 则让模型知道这次工具调用已经执行成功。

这和前面学过的 History / State 分离是同一种设计思想。

### 4.3 DeepAgents 为什么不默认启用 Todo？

当前 DeepAgents 从 v0.7 开始，不再默认加入 `TodoListMiddleware`。

如果需要，可以显式启用：

```python
from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

agent = create_deep_agent(
    model=model,
    middleware=[TodoListMiddleware()],
)
```

为什么一个支持复杂任务的 Harness，反而会把 Planning 设为可选？

因为显式维护 Todo 本身也有成本。

例如：

```text
模型调用 write_todos
        ↓
更新任务列表
        ↓
返回 ToolMessage
        ↓
下一轮继续工作
```

这会增加工具调用和 Token 消耗。

对于只需要修改一行代码的任务，这些额外操作可能完全没有必要。

因此：

**Planning 不是所有 Agent 任务的必要步骤，而是复杂任务中一种可选择的组织机制。**

这也是 DeepAgents 当前设计非常值得学习的地方。

---

## 五、第三次事故：任务列表显示完成，但代码实际上没有修好

现在小李已经给 Agent 增加了 `write_todos`。

某次执行后，任务列表显示：

```text
[completed] 定位退款问题
[completed] 修改退款逻辑
[completed] 添加回归测试
[completed] 运行所有测试
```

看起来全部完成了。

但是 CI 中仍然有一个测试失败。

小李检查历史后发现：

Agent 在运行测试之前，就把“运行所有测试”标记为了 completed。

为什么？

因为状态是模型自己写的。

模型认为自己已经完成了某项工作，但它的判断可能不准确。

于是我们发现：

**Progress State 只是执行过程中的声明，不能自动等同于任务完成的客观证据。**

### 第三次改进：定义 Completion Criteria

小李重新设计任务：

原来：

```text
任务：修复部分退款
```

现在：

```text
任务：修复部分退款

完成条件：
- 使用优惠券后的部分退款金额正确
- 对应单元测试通过
- 普通退款行为没有回归
```

这时，任务完成不再完全依赖模型的主观判断。

我们可以让 Harness 根据工具执行记录验证：

```text
任务：修复部分退款
        |
        v
代码修改成功？
        |
        v
相关测试通过？
        |
        v
没有违反原有约束？
        |
        v
允许标记为已验证完成
```

例如，我们可能维护两个状态：

```text
Agent 声明状态：completed

验证状态：verified / failed / unknown
```

这样可以区分：

- 模型认为完成。
- 实际证据显示完成。

需要注意：上面的完成条件和验证状态是我们自己设计的加强方案，**不是说 DeepAgents 或 Codex 的 Todo 工具默认会自动运行这些验证**。

这一点非常关键。

### Todo 工具不是验证器

`write_todos` 只是更新状态。

它不等于：

```text
pytest
```

也不等于：

```text
检查所有验收条件
```

所以一个成熟 Harness 需要明确：

**Planning 管理目标与进度；Verification 检查结果是否真实满足目标。**

这两个模块可以协作，但不能互相替代。

---

## 六、第四次事故：计划已经制定，但执行中发现计划本身是错的

Agent 最初制定了下面的计划：

```text
1. 修复 refund.py
2. 运行测试
3. 添加回归测试
```

但执行到第一步时，模型发现：

真正的问题不在 `refund.py`。

原因是优惠券折扣在 `coupon.py` 中已经被错误分摊，导致后续退款金额从源头上就不正确。

如果 Agent 严格按照原计划执行，就会在错误位置反复修改。

这时小李产生了一个新问题：

**计划制定之后，是否应该强制 Agent 一直遵守？**

显然不应该。

因为计划制定时，Agent 拥有的信息通常是不完整的。

随着工具调用不断进行，它会发现新的代码关系和业务事实。

因此计划必须允许更新。

这就是 **Replanning（重新规划）**。

### Replanning 为什么必要？

原来的理解是：

```text
错误发生在 refund.py
```

新的证据是：

```text
coupon.py 的折扣分摊已经错误
refund.py 只是使用了错误的金额
```

于是计划应该调整：

```text
原计划：

1. 修改 refund.py
2. 测试
3. 添加回归测试


新计划：

1. 确认 coupon.py 的折扣分摊公式
2. 修复 coupon.py
3. 检查 refund.py 是否仍需修改
4. 补充退款回归测试
5. 执行完整验证
```

注意，这不是 Agent 偏离目标。

恰恰相反：

**重新规划是为了在新证据出现后，继续保持对原始目标的忠实。**

但是，如果 Agent 每执行一次工具都重新生成整份计划，也会增加成本并导致计划频繁波动。

因此需要区分：

- 计划范围内的正常执行：不必重新规划。
- 发现原先假设错误：应该更新计划。
- 用户改变目标或约束：必须重新评估。
- 新增阻塞或依赖：需要调整步骤。
- 只是一次普通测试失败：不一定要求整体重新规划。

到这里，我们逐渐意识到：

> 计划是当前认知下的行动假设，而不是不可更改的执行脚本。

---

## 七、第五次改进：Plan 和 Task List 是不是同一回事？

前面，我们把计划写成了一个 Todo 列表。

这在简单任务中完全够用。

但现在任务越来越复杂。

例如：

```text
目标：修复优惠券退款系统

A. 定位优惠券业务规则
B. 确认退款计算入口
C. 修复优惠券折扣逻辑
D. 修复退款金额计算
E. 添加回归测试
F. 执行全部测试
```

这些任务并不是完全独立的。

例如：

```text
A ──> C ──┐
          ├──> E ──> F
B ──> D ──┘
```

只有完成 A、B 的调查，才有充分依据修改 C、D。

而 E、F 又依赖前面的实现。

现在，小李需要比普通 Todo 更丰富的信息：

```text
Task
  - id
  - description
  - status
  - dependencies
  - completion_criteria
  - evidence
```

但这并不意味着我们必须立刻实现完整的 DAG 调度器。

### 为什么不要过早引入 DAG？

如果任务只是：

```text
修改一行代码
运行一个测试
```

显式依赖图就显得多余。

只有当出现多任务并发、复杂依赖或失败恢复时，任务图才真正有价值。

因此可以把任务管理分成三个层次：

| 层次 | 适合场景 |
|---|---|
| 普通 Plan 文本 | 提前思考执行方案 |
| 结构化 Todo List | 追踪多步骤进度 |
| Task Graph / DAG | 管理依赖、并行和调度 |

在这里还需要区分：

**Planning**：思考为了完成目标应该做哪些事。

**Task Tracking**：记录哪些事正在做、哪些已经做完。

**Task Scheduling**：决定哪些任务现在可以执行，以及是否可以并行。

它们有联系，但不完全相同。

一个 Harness 可以支持 Planning，却不实现复杂的 DAG Scheduler。

---

## 八、源码对照：Codex 为什么区分 Plan Mode 和 update_plan？

现在去看 Codex。

这部分特别容易误解。

Codex 中存在两个名称非常相似、但职责不同的能力：

- `Plan Mode`
- `update_plan`

### 8.1 update_plan 是进度工具

打开：

[Codex plan_tool.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/plan_tool.rs)

核心结构：

```rust
pub enum StepStatus {
    Pending,
    InProgress,
    Completed,
}
```

以及：

```rust
pub struct PlanItemArg {
    pub step: String,
    pub status: StepStatus,
}
```

这与 DeepAgents 的 Todo 结构很相似。

它们都表达：

```text
一项任务 + 当前状态
```

再看：

[Codex PlanHandler](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/plan.rs)

当模型调用 `update_plan` 时，Handler 的关键操作是：

```rust
session
    .send_event(
        turn.as_ref(),
        EventMsg::PlanUpdate(args),
    )
    .await;
```

这里的重点是：

**Codex 把 Plan 更新转换成 Runtime Event。**

这样，其他组件可以接收并展示最新进度。

但它并不意味着 Runtime 会按照 Todo 列表自动调度全部任务。

例如：

```text
update_plan:
  修复退款逻辑 -> in_progress
```

不会自动执行：

```text
edit_file("refund.py")
```

模型仍然需要通过正常的 Tool Calling 流程提出行动。

所以：

**update_plan 是状态管理工具，不是 Agent Loop 的替代品。**

### 8.2 Plan Mode 是另一回事

Codex 还有单独的 Plan Mode。

阅读：

[Codex Plan Mode Prompt](https://github.com/openai/codex/blob/main/codex-rs/collaboration-mode-templates/templates/plan.md)

Plan Mode 的目标是：

> 在实际执行之前，先把实现方案讨论清楚。

例如，用户要求：

```text
重新设计整个订单退款架构。
```

如果 Agent 立刻修改代码，风险可能很高。

更合理的流程是：

```text
用户提出需求
      |
      v
Plan Mode
      |
      +--> 分析当前架构
      +--> 识别设计取舍
      +--> 提出方案
      +--> 必要时询问用户
      |
      v
确定计划
      |
      v
执行阶段
```

Codex 的 `PlanHandler` 中还有一个值得注意的判断：

```rust
if turn.mode() == ModeKind::Plan {
    return Err(...);
}
```

这里的意思是：`update_plan` 作为进度清单工具，不允许在 Plan Mode 中使用。

为什么要这样设计？

因为两者本来就不是同一种机制：

| | Plan Mode | update_plan |
|---|---|---|
| 目的 | 制定和讨论实施方案 | 更新任务进度 |
| 所属层面 | Agent 工作模式 | Runtime Tool |
| 是否直接执行修改 | 主要用于规划，不直接实施 | 不会自动执行任务 |
| 使用阶段 | 执行之前的方案设计 | 执行期间的进度跟踪 |

这说明：

**“先制定方案”和“执行中追踪任务”可以被设计成两个独立能力。**

没有必要因为名字都叫 Plan，就认为它们属于同一套逻辑。

---

## 九、源码对照：OpenCode 如何处理 Planning？

OpenCode 的设计又不太一样。

我们先看它的 Plan Agent。

官方文档：

[OpenCode Agents](https://opencode.ai/docs/agents/)

OpenCode 提供不同用途的 Agent。

其中：

- Build Agent 用于实际开发工作。
- Plan Agent 主要用于规划和分析。

Plan Agent 默认限制文件编辑和写入；但 Shell 命令仍按权限配置处理，所以不能简单认为 Plan Mode 是操作系统级的只读沙箱。

### 9.1 为什么要分离 Plan 与 Build？

假设用户提出：

> 把优惠券系统重构成策略模式。

这可能涉及大量文件和架构调整。

如果 Agent 一边分析，一边修改代码，那么用户还没决定是否接受方案，代码就可能已经改变了。

OpenCode 提供的一种工作方式是：

```text
Plan Agent
    |
    +--> 阅读代码
    +--> 分析架构
    +--> 讨论方案
    |
    v
用户决定实施
    |
    v
Build Agent
    |
    +--> 修改文件
    +--> 运行测试
    +--> 交付结果
```

这体现的是：

**规划阶段与实施阶段可以采用不同的工具权限。**

它与 Codex 的 Plan Mode 在目标上接近，但具体实现方式并不相同。

### 9.2 OpenCode 是否支持 Todo？

这里需要特别注意版本。

OpenCode V1 曾提供 `todowrite` 等工具。

OpenCode V2 早期移除了这些工具，但在 2026 年 10 月的 v2.1.0 中，又将 `todowrite` 作为实验性能力恢复。

当前 V2 可以通过：

```json
{
  "experimental": {
    "todo": true
  }
}
```

启用 Todo。

它会向模型提供 `todowrite` 工具，允许模型维护当前 Session 的任务列表。

例如：

```json
{
  "todos": [
    {
      "content": "修复普通退款",
      "status": "completed"
    },
    {
      "content": "修复部分退款",
      "status": "in_progress"
    },
    {
      "content": "补充回归测试",
      "status": "pending"
    }
  ]
}
```

为什么把它做成实验性、可选的能力？

因为显式维护任务列表并不一定提高所有任务的效率。

它有额外的调用、状态更新和界面维护成本。

这一点与 DeepAgents 将 Todo 改成可选能力的方向相似。

但两者的底层实现不同，不能简单视为完全相同的架构。

源码和文档入口：

- [OpenCode V2 Config](https://dev.opencode.ai/v2/docs/config/)
- [OpenCode Changelog](https://opencode.ai/changelog)
- [OpenCode Agents](https://opencode.ai/docs/agents/)

---

## 十、第六次事故：多个 Subagent 并行时，谁负责总体进度？

现在 Coding Agent 已经能处理复杂任务。

小李决定使用 Subagent 加速退款系统的修复。

```text
Main Agent
    |
    +--> Subagent A：检查优惠券分摊逻辑
    |
    +--> Subagent B：检查退款金额计算
    |
    +--> Subagent C：编写回归测试
```

三个 Subagent 同时工作。

但 Main Agent 很快发现：

A 已经完成调查，B 仍然在修改代码，C 编写的测试却依赖 B 尚未完成的修改。

如果 Main Agent 只等待三个最终结果，就无法很好地管理依赖。

这时我们自然需要：

```text
任务状态
    +
任务依赖
    +
子 Agent 执行状态
```

例如：

```text
Task A：检查优惠券规则
  status = completed

Task B：修复退款代码
  status = in_progress
  depends_on = [A]

Task C：补充回归测试
  status = pending
  depends_on = [B]
```

注意，任务列表的状态与 Subagent 的运行状态也不完全一样。

例如：

```text
Subagent B 执行结束
```

只能说明它完成了一次子任务执行。

如果它没有完成修复，Main Agent 不能直接把 Task B 标记为 completed。

因此，父 Agent 需要根据子任务返回的结果和证据更新整体任务状态。

这就是 Task Management 与 Multi-Agent Orchestration 的交汇点。

但只有确实需要依赖调度时，我们才值得引入复杂的 Task Graph。

---

## 十一、Planning 与 Agent Loop 到底是什么关系？

经过前面的推导，我们终于可以把几个概念放回统一架构。

```text
                 User Goal
                     |
                     v
               Planning Layer
                     |
                     v
                 Task State
                     |
                     v
                 Agent Loop
                     |
          +----------+----------+
          |                     |
          v                     v
      LLM Decision          Context Builder
          |
          v
       Tool Call
          |
          v
       Tool Runtime
          |
          v
       Tool Result
          |
          v
      Agent Loop
          |
          +--> 更新 Task State
          |
          +--> 发现新问题，重新规划
          |
          +--> 继续执行
          |
          +--> 验证并结束
```

这张图是逻辑架构，不代表所有 Harness 必须实现同名模块。

其中最重要的关系是：

**Agent Loop 管理行动循环，Planning 管理目标分解，Task Management 管理执行进度。**

三者共同工作，但并不互相替代。

例如：

```text
Task：添加退款回归测试
```

只是一个需要完成的目标。

真正执行它，仍然需要模型提出：

```text
read_file(...)
edit_file(...)
execute("pytest ...")
```

再由 Agent Loop 和 Tool Runtime 完成实际操作。

因此，写入 Todo 并不等于完成任务。

---

## 十二、如果自己实现 Planning，应该怎样设计？

现在我们不需要复制任何一种框架。

从最小方案开始即可。

### 第一阶段：只保存目标和任务状态

```python
from dataclasses import dataclass
from typing import Literal

@dataclass
class Task:
    id: str
    description: str
    status: Literal[
        "pending",
        "in_progress",
        "completed",
    ]
```

这是教学级实现。

这一版就足以支持结构化 Todo List。

### 第二阶段：当任务完成情况无法确认时，引入验证证据

```python
@dataclass
class Task:
    id: str
    description: str
    status: str
    completion_criteria: list[str]
    evidence: list[str]
```

现在可以记录：

```text
任务：修复部分退款

完成条件：
- 对应测试通过
- 普通退款没有回归

证据：
- pytest tests/test_refund.py 通过
- git diff 已检查
```

不过测试结果只能证明对应测试覆盖的行为，不能自动证明所有业务逻辑正确。

### 第三阶段：只有出现并行依赖时，再加入 Dependency

```python
@dataclass
class Task:
    id: str
    description: str
    status: str
    depends_on: list[str]
```

这样才能进一步构建 Task Graph。

但随之而来的还包括环检测、任务取消、依赖失效和并发更新等问题。

所以不要提前引入不需要的复杂度。

---

## 十三、一个容易忽视的问题：计划本身应该放在 Context 还是 State？

这里可以关联之前研究的 Context Management。

假设计划包含二十个任务。

如果每次调用 LLM，都把全部历史计划和全部更新记录发送给模型：

```text
Plan V1
Plan V2
Plan V3
Plan V4
...
```

上下文会迅速变长。

而且模型可能同时看到相互矛盾的计划版本。

例如：

```text
Plan V1：修改 refund.py

Plan V3：不要修改 refund.py，改为修复 coupon.py
```

现在模型可能搞不清哪个计划是当前有效版本。

一个更合理的设计是：

```text
Task Store / Agent State
       |
       v
Current Plan
       |
       v
Context Builder
       |
       v
LLM
```

也就是：

**在结构化 State 中维护当前计划，在 Context 中按需提供当前有效计划。**

旧计划可以保存在 Event History 中，用于审计和恢复，但不必把所有旧版本都放入工作上下文。

我们之前学到的 History / State / Context 分离，再次发挥作用。

这也说明 Planning 不只是 Prompt Engineering。

它会与：

- Context Management
- Checkpoint
- Event History
- Subagent
- Observability

产生直接联系。

---

## 十四、三个开源 Harness 的设计对比

| 设计问题 | DeepAgents | Codex | OpenCode |
|---|---|---|---|
| 显式任务跟踪 | `TodoListMiddleware` | `update_plan` | 实验性 `todowrite`（V2） |
| 基础任务状态 | pending / in_progress / completed | pending / in_progress / completed | 结构化 Todo |
| 计划保存方式 | Agent State | PlanUpdate Event | Session Todo |
| 规划是否默认启用 | v0.7 起可选 | 独立能力 | V2 实验性可选 |
| 规划与实施分离 | 可通过 Agent/Middleware 配置实现 | Plan Mode | Plan Agent / Build Agent |
| 是否自动执行清单 | 否 | 否 | 否 |

三者最大的共同点是：

**任务列表本身并不负责执行任务。**

真正执行工作，仍然依赖模型决策和 Agent Loop。

它们之间主要的差别是：

- DeepAgents 偏向通过 Middleware 给 Agent 增加结构化任务管理能力。
- Codex 明确区分规划讨论模式与执行进度更新工具。
- OpenCode 通过不同 Agent 模式区分规划和实施，并允许按需开启 Todo 工具。

这些不同方案并不能简单排出优劣。

是否需要显式规划，要由任务规模、模型能力、执行成本和可观测性要求共同决定。

---

## 十五、怎样验证 Planning 是否真的提高了 Agent 能力？

小李终于完成了 Planning 模块。

但他不想只凭感觉判断这套设计是否有效。

于是准备了两组实验。

### 实验 A：不启用 Todo

Agent 直接执行复杂退款修复任务。

记录：

- 是否完成全部用户要求。
- 是否重复调查同一个问题。
- 是否修改了不该修改的 API。
- 总工具调用次数。
- 总 Token 成本。
- 总执行时间。

### 实验 B：启用 Todo

同样的任务，启用结构化任务列表。

记录相同指标。

最后比较：

| 指标 | 期望改善方向 |
|---|---|
| 任务完成率 | 提高 |
| 遗漏需求数量 | 降低 |
| 重复工具调用 | 降低 |
| 无关代码修改 | 降低 |
| Token 成本 | 不一定降低 |
| 执行时间 | 不一定缩短 |

为什么成本和时间不一定改善？

因为显式规划会增加额外开销。

它可能帮助复杂任务少走弯路，也可能让简单任务承担不必要的状态更新。

所以衡量 Planning 的关键，不是模型是否写出了漂亮的任务列表。

而是：

**启用 Planning 后，Agent 是否更可靠地完成了用户真正要求的任务。**

---

## 十六、本章的五个核心问题

### 1. Planning & Task Management 是什么？

Planning 负责把复杂目标组织成可执行的子目标；Task Management 负责维护任务状态、进度和必要的依赖关系。

### 2. 为什么需要？

因为 Agent Loop 虽然能连续执行动作，但不能单凭循环机制保证模型持续关注全部用户目标。

### 3. 没有它会怎样？

简单任务通常不会有问题。

但复杂任务可能出现重复工作、遗漏要求、偏离目标以及进度不可见等问题。

### 4. 实际怎么做？

先使用普通计划文本，再根据需要引入结构化 Todo、完成条件、Replanning 和任务依赖。

同时将计划状态与真实执行结果关联。

### 5. 在整个 Harness 中处于什么位置？

它位于任务目标与 Agent Loop 之间，并与 Context、State、Checkpoint、Subagent 和 Verification 配合。

Planning 负责确定目标和步骤，但不替代实际执行。

---

## 十七、独立验证：换一个新的工程任务

假设现在用户提出：

> 将单体订单系统拆分成订单服务和支付服务，保持现有 API 兼容，并完成数据库迁移。

这是一个比修复优惠券 Bug 更复杂的任务。

我们已经能够从问题出发推导：

首先，需要梳理现有架构和接口约束。

然后，确定哪些功能属于订单服务，哪些属于支付服务。

接下来，规划数据库迁移与兼容性策略。

最后，执行代码修改、测试和部署验证。

如果任务存在并行开发，就需要任务依赖和 Subagent 协调。

如果执行中发现某个服务无法独立拆分，就应该根据新证据进行 Replanning。

如果模型声称拆分完成，还需要外部测试验证接口兼容性，而不是只看 Todo 是否全部完成。

这说明我们已经不只是理解了一个 `write_todos` 工具，而是能够针对复杂工程任务推导出所需的 Planning 能力。

---

## 本章总结

我们最初的 Coding Agent 已经拥有 Agent Loop。

它能够不断调用模型、执行工具、观察结果。

但它仍然可能在复杂任务中遗漏目标、反复尝试或错误宣布完成。

于是，我们逐步引入：

```text
复杂目标
   |
   v
Task Decomposition
   |
   v
Structured Task List
   |
   v
Progress Tracking
   |
   v
Replanning
   |
   v
Completion Verification
```

最终建立了一个关键认识：

> **Agent Loop 让 Agent 能够持续行动；Planning 让行动围绕目标组织；Task Management 让进度可追踪；Verification 判断目标是否真正完成。**

这些机制相互协作，但不应混为一谈。

最好的 Planning System 不一定是功能最多的系统，而是能够以合理成本帮助 Agent 更可靠地完成复杂任务的系统。

## 参考源码与阅读顺序

### DeepAgents / LangChain

- [TodoListMiddleware 实现](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/todo.py)
  - 重点：`Todo`、`PlanningState`、`write_todos`、`Command(update=...)`
- [DeepAgents 官方介绍](https://github.com/langchain-ai/docs/blob/main/src/oss/deepagents/overview.mdx)
  - 重点：为什么 Task Planning 是可选能力
- [DeepAgents Changelog](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/CHANGELOG.md)
  - 重点：v0.7 取消默认 `TodoListMiddleware`

### Codex

- [Plan 状态定义](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/plan_tool.rs)
  - 重点：`StepStatus`、`PlanItemArg`
- [PlanHandler 实现](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/plan.rs)
  - 重点：`EventMsg::PlanUpdate`
- [Plan Mode Prompt](https://github.com/openai/codex/blob/main/codex-rs/collaboration-mode-templates/templates/plan.md)
  - 重点：Plan Mode 与 `update_plan` 的区别

### OpenCode

- [OpenCode Agents 文档](https://opencode.ai/docs/agents/)
  - 重点：Plan Agent 与 Build Agent
- [OpenCode V2 配置文档](https://dev.opencode.ai/v2/docs/config/)
  - 重点：`experimental.todo`
- [OpenCode 更新日志](https://opencode.ai/changelog)
  - 重点：v2.1.0 恢复实验性的 `todowrite`

正式研究源码时，应固定具体 Commit，避免后续版本更新造成接口差异。

---

**下一章：Human-in-the-Loop——Agent 已经有权限审批机制，为什么还需要一种更通用的暂停、等待、恢复执行能力？**

我们会从一个新的工程问题开始：

Coding Agent 已经分析出两种不同的退款架构方案，但无法自行决定应该采用哪一种。

它既不应该直接修改代码，也不应该结束任务。

这时我们需要让 Agent 主动暂停，等待用户补充决策，然后从原来的执行位置继续工作。

这将引出更完整的 Human-in-the-Loop、Interrupt、Resume 和 Durable Execution 设计。
