---
title: 第十一章：复杂任务如何规划？——Planning 与 Todo
description: 从优惠券部分退款 Bug 的遗漏与重复修改出发，沿 DeepAgents 0.7.32 和 LangChain 1.4.7 的实际源码，渐进理解显式计划、TodoListMiddleware、write_todos、PlanningState、Command 状态更新、模型反馈与并发边界
tags: [DeepAgents, LangChain, LangGraph, Planning, TodoListMiddleware, write_todos, Agent State, Middleware, 源码精读]
sidebar_position: 16
---

# 第十一章：复杂任务如何规划？——Planning 与 Todo

> **本章核心问题：** Agent 已经能循环调用工具、修改文件、执行测试，也能在危险操作前接受人工审批。为什么修复一个跨文件的退款 Bug 时，它仍可能重复做同一件事、遗漏关键检查，甚至过早宣布完成？怎样让“接下来要做什么”不再只是模型的一段文字，而成为可检查、可更新的执行状态？

## 一、从第十章留下的失败场景开始

上一章，我们的 Coding Agent 已经能够执行如下流程：

```text
读取 refund.py
      ↓
模型提出修改
      ↓
危险工具操作触发审批
      ↓
用户批准 / 拒绝 / 修改
      ↓
工具执行，结果返回 Agent Loop
```

审批机制解决的是**这一步能不能做**，却没有回答**整件事做完了没有**。

这次用户提出的任务仍然是：

> 修复电商系统中的优惠券部分退款精度 Bug。检查 `shop/refund.py` 与 `shop/coupon_service.py`，补足回归测试，运行相关 `pytest`，确认多次部分退款累计金额与业务规则一致。

Agent 很可能经历这样的过程：

```text
第 1 轮：read_file("shop/refund.py")
第 2 轮：edit_file("shop/refund.py", ...)
第 3 轮：execute("pytest shop/tests/test_refund.py")
第 4 轮：测试失败，再次 read_file("shop/refund.py")
第 5 轮：再次 edit_file("shop/refund.py", ...)
第 6 轮：模型回复“修复完成”
```

但其实它还没有看 `shop/coupon_service.py`，也没有验证多次部分退款的累计尾差。

**Agent Loop 允许它根据反馈继续行动，但 Loop 本身并不保证所有任务约束都会被记住或逐项完成。**

这里要补上的不是另一套循环执行器，而是一个更小的东西：**可以显式维护的任务清单**。

### 1.1 固定源码版本

本章继续使用系列课程的固定阅读基线：

```text
DeepAgents Python：0.7.32
DeepAgents Commit：e85c6b9
LangChain：       1.4.7
LangChain Commit：fbb8a35
```

这一章主要阅读三个入口：

- [DeepAgents `graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)：`create_deep_agent()` 怎样接收自定义 Middleware。
- [LangChain `todo.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py)：`TodoListMiddleware`、`PlanningState` 和 `write_todos` 的实际实现。
- [LangChain `factory.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py)：Middleware 提供的工具与状态结构怎样进入 Agent Graph。

一个容易被旧教程误导的事实必须先讲清楚：**在 DeepAgents `0.7.32` 的 `create_deep_agent()` 中，`TodoListMiddleware` 不是默认安装的核心 Middleware。** 本章将展示怎样显式配置它，不能把“DeepAgents 支持 Planning”偷换成“每个 DeepAgents 实例都会自动调用 `write_todos`”。

---

## 二、最简单的办法：把计划写进对话里，不行吗？

我们先不要引入任何新类。

### 2.1 一段自然语言计划已经能解决部分问题

最直接的做法是让模型先回复：

```text
计划：
1. 阅读 refund.py，定位金额计算。
2. 阅读 coupon_service.py，确认优惠分摊规则。
3. 修改计算逻辑。
4. 补充累计退款和尾差测试。
5. 运行 pytest，检查结果。
```

在很短的任务中，这完全可能有效。

但问题是：**这段文本究竟由谁维护？**

假设模型读取 `coupon_service.py` 后发现了新规则：

> 订单中最后一次部分退款需要吸收先前各次四舍五入产生的尾差。

最初的计划应当变化：

```text
1. 阅读 refund.py                      已完成
2. 阅读 coupon_service.py              已完成
3. 确定尾差归集规则                      新增 / 进行中
4. 修改计算逻辑                         待开始
5. 补充累计退款测试                     待开始
6. 运行 pytest                         待开始
```

若计划只是早先一条 `AIMessage` 中的普通文字，那么后续模型可以选择忽略它、重新描述它，甚至生成一份不一致的“新计划”。外部程序也很难可靠地问：**目前究竟还有几个未完成步骤？**

### 2.2 我们真正需要的不是更长的提示词

第一种补救方式是反复往系统提示词里加：

```text
请记住你的计划。
请不要忘记检查 coupon_service.py。
请完成所有步骤后再回答。
```

这些指令能提供行为引导，但它们没有给应用程序一个独立的数据入口。

例如，要在页面上展示进度，程序不得不从自由文本中猜测：

```python
# 概念伪代码：不建议依赖自然语言提取权威进度
progress = guess_progress_from_messages(messages)
```

我们现在遇到的约束是：

> **计划需要能被程序读取、更新，而不只是一段留在聊天记录里的文字。**

因此，下一步只引入一个最小概念：把计划表示为结构化数据。

---

## 三、先不使用框架：三种状态足以描述一个任务步骤

### 3.1 最小的 Todo 结构

对当前退款任务，一项待办只需要两个字段：

```python
# 教学化简：表示一项待办的数据结构
{
    "content": "阅读 shop/refund.py，定位金额计算",
    "status": "in_progress",
}
```

这里的 `content` 告诉我们要做什么；`status` 告诉我们做到哪里。

状态先只保留三种：

```text
pending       尚未开始
in_progress   正在进行
completed     已经完成
```

于是我们能把任务写成：

```python
# 教学化简：当前任务计划
plan = [
    {
        "content": "阅读 refund.py 和 coupon_service.py",
        "status": "in_progress",
    },
    {
        "content": "确认优惠分摊与累计尾差规则",
        "status": "pending",
    },
    {
        "content": "修改退款计算并补充回归测试",
        "status": "pending",
    },
    {
        "content": "运行 pytest 并检查全部相关测试",
        "status": "pending",
    },
]
```

现在，程序终于可以不依赖语言理解，直接计算：

```python
pending_count = sum(
    item["status"] != "completed"
    for item in plan
)
```

但这只是普通 Python 变量。

### 3.2 为什么还不能说“Agent 已经具有 Planning”？

因为我们的 Agent 是通过 LangGraph 执行的。Graph 的其他节点不会自动读取当前 Python 函数作用域中的 `plan`。一旦一个工具执行完毕、进入下一轮模型调用，新的执行步骤需要知道这份数据放在哪里。

如果我们把它塞进一条 `ToolMessage`：

```python
ToolMessage(
    content="当前任务进度：已完成读取，还需修改和测试",
    tool_call_id="call_001",
)
```

模型能读到文本，但程序仍然不能直接对某个待办步骤做可靠查询。

于是下一层需求出现了：

> **Todo 不应只存在于某条消息中，还要成为 Agent State 的一个字段。**

我们现在才有必要去看真实的 `PlanningState`。

---

## 四、真实源码：`PlanningState` 给 Agent 增加了什么？

打开固定版本的：

[`langchain/agents/middleware/todo.py`，第 23—40 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L23-L40)。

### 4.1 真实源码节选：`Todo`

```python
class Todo(TypedDict):
    """A single todo item with content and status."""

    content: str
    status: Literal["pending", "in_progress", "completed"]
```

这段定义确认了我们刚才的小设计：实际框架中 `Todo` 也就是**内容 + 状态**。

注意它在这一版本中没有声明：

- `id`：没有稳定的待办项 ID。
- `dependency_ids`：没有显式的任务依赖图。
- `assignee`：没有任务负责人字段。
- `retry_count`：没有逐任务重试计数。

这不是说这些能力永远不重要，而是**这个 Middleware 的目标是帮助模型维护计划，不是交付一套通用项目调度系统**。

我们暂时不需要引入更重的工作流概念。

### 4.2 真实源码节选：`PlanningState`

```python
class PlanningState(AgentState[ResponseT]):
    """State schema for the todo middleware."""

    todos: Annotated[NotRequired[list[Todo]], OmitFromInput]
```

这里首次出现 `PlanningState`，但它做的事情很简单：

**在 Agent 已有状态上增加一个可选的 `todos` 字段。**

`NotRequired` 表示新启动的任务不一定已有计划；`OmitFromInput` 是状态 Schema 的输入可见性标记，告诉 Factory 不把这个字段作为常规外部输入字段来暴露。它并不表示 `todos` 无法被 Graph 内部工具更新。

所以在一次工具执行前，我们可能看到：

```python
# 教学示意：State 中除了 messages 之外多出 todos
{
    "messages": [...],
    "todos": [
        {"content": "阅读相关代码", "status": "completed"},
        {"content": "修改精度逻辑", "status": "in_progress"},
        {"content": "运行回归测试", "status": "pending"},
    ],
}
```

这与之前学到的 `messages` 有明显区别。

- `messages` 负责保留模型、用户与工具之间的交互协议。
- `todos` 负责保存当前任务计划这一业务状态。

它们可以相互关联，但并不是同一个字段。

### 4.3 State 有了字段，为什么还要安装 Middleware？

如果仅仅声明：

```python
class PlanningState(...):
    todos: ...
```

模型不可能自动知道自己可以修改这份状态。还需要一个**允许模型明确发起计划更新**的入口。

这就引出了下一层：`write_todos`。

---

## 五、让模型更新计划：为什么 `write_todos` 是一个 Tool？

### 5.1 从“模型说它完成了”到“模型提交一次更新”

假设模型刚读完两份文件，它可能输出：

```text
我已经看过退款实现，接下来准备补充测试。
```

这句话不能自动更新 `todos`。

我们需要模型明确调用一个带结构化参数的工具，例如：

```python
# 教学示意：模型发起的 Tool Call 参数
{
    "name": "write_todos",
    "args": {
        "todos": [
            {
                "content": "阅读 refund.py 和 coupon_service.py",
                "status": "completed",
            },
            {
                "content": "确认优惠分摊与累计尾差规则",
                "status": "in_progress",
            },
            {
                "content": "修改退款计算并补充回归测试",
                "status": "pending",
            },
            {
                "content": "运行 pytest 并检查结果",
                "status": "pending",
            },
        ]
    },
    "id": "call_plan_001",
    "type": "tool_call",
}
```

模型提出更新，但它本身没有直接写入 LangGraph State 的权限。

谁来执行这次更新？答案仍然是我们在第九章学过的 `ToolNode`。

当前执行路径可以先画成一张很小的图：

```text
Model
  |
  | 产生 write_todos Tool Call
  v
ToolNode
  |
  | 执行 write_todos
  v
Agent State: todos 更新
  |
  v
Model 读取工具反馈，进入下一轮
```

这里的 `write_todos` 与 `edit_file` 有一个关键区别：

- `edit_file` 要修改的是 Backend 中的文件。
- `write_todos` 要修改的是 Agent Graph 中的任务计划状态。

### 5.2 真实源码：工具的输入 Schema

[`todo.py` 第 43—46 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L43-L46)：

```python
class WriteTodosInput(BaseModel):
    """Input schema for the `write_todos` tool."""

    todos: list[Todo]
```

这里没有 `tool_call_id`，因为模型只需要提供新的 `todos`。

调用 ID 由 LangChain 工具运行时提供，而不是要求模型在业务参数中自己拼接。

### 5.3 真实源码：`_write_todos()` 的核心实现

[`todo.py` 第 131—143 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L131-L143)：

```python
def _write_todos(
    runtime: ToolRuntime[ContextT, PlanningState[ResponseT]],
    todos: list[Todo],
) -> Command[Any]:
    """Create and manage a structured task list for your current work session."""
    return Command(
        update={
            "todos": todos,
            "messages": [
                ToolMessage(
                    f"Updated todo list to {todos}",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )
```

请认真观察这段代码的三处信息。

**第一处：`runtime` 从哪里来？**

它是 `ToolRuntime`，在真正执行 Tool 时由运行时注入。模型的 `args` 中不需要提供这个对象。

**第二处：返回的为什么不是字符串？**

因为它要做的并不只是告诉模型“计划已更新”，还要把 `todos` 这个字段提交给 Graph。

**第三处：为什么还要一起返回 `ToolMessage`？**

模型之前产生了一个带 ID 的工具调用。该次工具调用需要有对应的反馈消息，否则工具调用与结果之间的协议就不完整。

这里复用了第二章的消息协议：

```text
AIMessage.tool_calls[0].id = "call_plan_001"
                  |
                  v
ToolMessage.tool_call_id = "call_plan_001"
```

这说明规划并没有绕开 Agent Loop，反而是**借助与文件操作相同的工具协议完成状态更新**。

---

## 六、为什么一定要返回 `Command(update=...)`？

### 6.1 如果工具只返回一句成功消息

我们尝试一个更简单的写法：

```python
# 概念伪代码：只能反馈文本，不能完成本章要研究的状态提交

def write_todos_simple(todos):
    return "Todo list updated"
```

模型确实会收到一段 ToolMessage 文本，但应用程序里的 `state["todos"]` 没有因此更新。

而本章希望的结果是：

```python
state_before = {
    "todos": [
        {"content": "阅读代码", "status": "in_progress"},
        {"content": "运行测试", "status": "pending"},
    ]
}

state_after = {
    "todos": [
        {"content": "阅读代码", "status": "completed"},
        {"content": "运行测试", "status": "in_progress"},
    ]
}
```

所以这一次的工具结果至少要包含两类信息：

1. **状态更新**：将新的列表交给 LangGraph。
2. **工具反馈**：向模型确认刚才的调用已被处理。

固定版本选择了 `Command(update=...)`：

```python
# 真实源码的核心结构；省略 ToolMessage 内容
Command(
    update={
        "todos": todos,
        "messages": [ToolMessage(...)],
    }
)
```

这里 `update` 的消费者不是模型本身，而是执行 Graph 的 LangGraph Runtime。

可以把本次写入理解成：

```text
write_todos()
    |
    v
Command(update={"todos": 新列表, "messages": 工具反馈})
    |
    v
LangGraph 处理状态更新
    |
    +--> todos 通道：保存新任务列表
    |
    +--> messages 通道：追加相应 ToolMessage
    |
    v
下一轮 Agent Loop
```

**注意：这是概念层的数据流，不等于说 `Command` 直接修改原字典，也不意味着工具调用可以绕过 State 的合并规则。**

### 6.2 一次调用提交的是整个列表，不是某个 Todo 的补丁

再看真实函数：

```python
"todos": todos
```

这里没有：

```python
"todo_id": 3,
"new_status": "completed"
```

因此，固定版本的 `write_todos` 使用的是**整个列表的更新**，而不是“指定某个条目的状态增量操作”。

假设旧状态是：

```python
old_todos = [
    {"content": "阅读代码", "status": "completed"},
    {"content": "补充测试", "status": "in_progress"},
]
```

模型要增加“测试尾差”步骤，需要传入包含旧条目与新条目的整个列表：

```python
new_todos = [
    {"content": "阅读代码", "status": "completed"},
    {"content": "补充测试", "status": "in_progress"},
    {"content": "测试多次部分退款尾差", "status": "pending"},
]
```

而不是仅传：

```python
[{"content": "测试多次部分退款尾差", "status": "pending"}]
```

因为后者作为新的 `todos` 值，意味着先前的任务项不再存在于更新后的列表中。

**这既是设计上的简洁，也是后面并发问题的根源。**

不过现在先不谈并发，下一步要解决另一个更基本的问题：怎样让模型知道这个工具存在？

---

## 七、真正装配：`TodoListMiddleware` 如何同时提供工具与提示词？

### 7.1 为什么只把 `write_todos` 放进 tools 还不够？

我们可以把一个普通工具注册给 Agent：

```python
agent = create_agent(
    model=model,
    tools=[write_todos],
)
```

但这只是把工具提供给模型，并没有自动保证模型知道“复杂任务应先拆解，再及时更新状态”。

从任务管理的角度看，我们还希望告诉模型：

- 简单任务无需专门规划。
- 多步骤任务可以先调用 `write_todos`。
- 正在执行的步骤要标成 `in_progress`。
- 完成后及时更新 `completed`。
- 如果测试暴露新问题，可以修改计划。
- 最后更新计划不等于向用户交付最终答案。

问题来了：这些行为指令应如何和工具一起交给 Agent？

我们在第五、六章已经掌握 Middleware 的两种能力：

1. 构建阶段通过 `self.tools` 声明工具。
2. 模型调用阶段通过 `wrap_model_call()` 修改 Model Request。

LangChain 就是用这两种能力组合出 `TodoListMiddleware`。

### 7.2 真实源码节选：构造方法

[`todo.py` 第 176—201 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L176-L201)：

```python
state_schema = PlanningState

# 节选：真实源码中 __init__ 的关键赋值与构造
self.system_prompt = system_prompt
self.tool_description = tool_description
self.tools = [
    StructuredTool.from_function(
        name="write_todos",
        description=tool_description,
        func=_write_todos,
        coroutine=_awrite_todos,
        args_schema=WriteTodosInput,
        infer_schema=False,
    )
]
```

此处做了两件事。

**其一，声明自己的状态结构：**

```python
state_schema = PlanningState
```

它告诉 Agent Factory：本 Middleware 的业务需要 `todos` 字段。

**其二，声明自己的工具：**

```python
self.tools = [StructuredTool.from_function(...)]
```

这样就不要求 `create_deep_agent()` 的调用方同时手动传入同一个工具。

为什么注册 `func` 和 `coroutine` 两个执行函数？

因为 Agent 既可能通过 `invoke()` 同步运行，也可能通过 `ainvoke()` 异步运行。这个工具的状态更新逻辑相同，但要有相应的调用入口。

### 7.3 真实源码节选：在模型调用前补充规划指令

[`todo.py` 第 202—226 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L202-L226)：

```python
# 真实源码节选：wrap_model_call 的末段
new_system_message = SystemMessage(
    content=cast("list[str | dict[str, str]]", new_system_content)
)
return handler(request.override(system_message=new_system_message))
```

其中 `new_system_content` 会基于已有系统消息内容，附加 `self.system_prompt`；如果原来没有系统消息，则构造包含 Todo 说明的新消息。

我们可以用熟悉的第六章执行结构解释它：

```text
Model Node
   |
   v
TodoListMiddleware.wrap_model_call
   |
   | 在 SystemMessage 中加入规划指令
   v
handler(request.override(...))
   |
   v
真正的模型调用
```

所以它**不是**一个单独负责 Planning 的 Agent Loop；它仅仅为原 Agent Loop 提供了：

- 可使用的 `write_todos` 工具。
- 一个用于保存计划的 State 字段。
- 一组鼓励模型在合适时候更新计划的提示指令。

这三个机制配合，才形成可观察的 Planning 行为。

### 7.4 “模型被告知要规划”与“模型一定会规划”仍然不是一回事

看看源码自带工具说明的含义：

> 复杂、多步骤、可调整的任务适合使用 Todo；简单且步骤很少的任务不必额外创建清单。

这意味着该 Middleware **有意保留模型的判断空间**。

例如：

```text
用户：refund.py 在哪里？
```

Agent 直接搜索即可，没有必要先写一份计划。

而：

```text
用户：修复退款精度问题，补充边界测试，并确认没有破坏优惠分摊规则。
```

就很适合使用显式 Todo。

**Middleware 提供的是规划工具与使用指南，而不是强制性流程调度器。**

---

## 八、从 DeepAgents 入口出发：为什么 0.7.32 不会自动出现 `write_todos`？

### 8.1 先验证一件容易想当然的事

读到这里，可能会产生这样的猜测：

```python
agent = create_deep_agent(model=model)
```

既然叫 Deep Agent，是否默认就有 `write_todos`？

**对我们固定阅读的 `0.7.32 / e85c6b9`，答案是否定的。**

打开 DeepAgents 的 [`graph.py` 第 821—849 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L821-L849)，可以看到主 Agent 核心 Middleware 的装配路径。

源码节选：

```python
deepagent_middleware = [
    FilesystemMiddleware(
        backend=backend,
        custom_tool_descriptions=_profile.tool_description_overrides,
        permissions=permissions,
    ),
]

# 若有 inline subagents，会加入 SubAgentMiddleware
# 然后继续扩展核心列表：

deepagent_middleware.extend(
    [
        _ParentNotifierMiddleware(),
        create_summarization_middleware(model, backend),
        PatchToolCallsMiddleware(),
    ]
)
```

这一段没有创建 `TodoListMiddleware()`。

后面虽然还可能装配：

```text
HarnessProfile 提供的额外 Middleware
SkillsMiddleware
MemoryMiddleware
HumanInTheLoopMiddleware
用户自定义 Middleware
```

但 **默认核心栈本身没有 Todo**。

如果某个 HarnessProfile 或调用方显式提供相应 Middleware，那么另当别论；我们不能把“默认未装配”表述成“任何配置下都无法使用”。

### 8.2 正确的显式启用方式

对于这一版本，最直接的方式是：

```python
from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

agent = create_deep_agent(
    model=model,
    middleware=[TodoListMiddleware()],
)
```

这一段是**框架接口使用示例，未在本教程环境中对真实模型执行**。

注意：

```python
middleware=[TodoListMiddleware()]
```

不是要把 DeepAgents 的默认 Middleware 覆盖掉。

它是把一个自定义 Middleware 交给 DeepAgents，由 DeepAgents 在既有装配流程中合并。

### 8.3 真正的插入位置在哪？

[`graph.py` 第 885—903 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L885-L903) 包含：

```python
# 真实源码节选

deepagent_middleware = _apply_custom_middleware(
    deepagent_middleware,
    middleware or [],
    core_names=_main_core_names,
)
```

而 `_apply_custom_middleware()` 定义于同一文件的 [第 195—234 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L195-L234)。

它不是盲目 `list.extend()`：

- 如果新 Middleware 的 `.name` 与现有成员匹配，就**原位替换**。
- 如果是新名称，就插入到**最后一个核心 Middleware 之后**、Profile/缓存/Memory 等尾部组件之前。

在通常配置中，`TodoListMiddleware()` 就属于新增条目。

这对我们有什么意义？

它说明 `TodoListMiddleware` 可以继续利用已有的 `FilesystemMiddleware`、Summarization 和 ToolNode，而不必重写一套工具执行过程。

### 8.4 主 Agent 开启 Todo，Subagent 也一定有同样的 Todo 吗？

不能这样推断。

固定版本的 `create_deep_agent()` 会为子 Agent 另外组装 Middleware；主 Agent 的所有自定义 Middleware **并不总是自动复制给每一个子 Agent**。尤其默认 general-purpose 子 Agent 的处理路径，与显式声明的子 Agent、forked 子 Agent 有不同的继承规则。

因此，如果项目要求某个子 Agent 也维护自己的计划，应该检查它实际拿到的 Middleware 配置，而不是因为主 Agent 安装了 Todo 就假设全局共享。

子 Agent 的具体继承、隔离和委派机制留到第十二章再研究。

---

## 九、LangChain Factory 怎样把三个声明变成运行行为？

当前我们已经知道，TodoListMiddleware 声明：

```text
self.tools       → write_todos
self.state_schema → PlanningState
wrap_model_call  → 规划使用说明
```

但这些声明并不会自己执行。

还需要第六章研究过的 `create_agent()` 来组装 Graph。

### 9.1 工具从 Middleware 进入 ToolNode

打开固定版本的 [`factory.py` 第 998—1050 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L998-L1050)。

真实源码节选：

```python
middleware_tools = [
    t
    for m in middleware
    for t in getattr(m, "tools", [])
]

# 省略普通工具分类
available_tools = middleware_tools + regular_tools
```

因此，即使调用方没有显式写：

```python
tools=[write_todos]
```

Factory 也能从 `TodoListMiddleware.tools` 找到它。

随后，`available_tools` 会用于构造 ToolNode。

这解决了：**模型提出 `write_todos` 时，Graph 中谁负责找到实际执行函数？**

### 9.2 状态结构从 Middleware 进入 StateGraph

再看 [`factory.py` 第 1124—1141 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1124-L1141)。

真实源码节选：

```python
base_state = state_schema if state_schema is not None else AgentState

state_schemas: list[type] = [
    *(m.state_schema for m in middleware),
    base_state,
]

resolved_state_schema, input_schema, output_schema = (
    _resolve_schemas(state_schemas)
)
```

这就解释了：`PlanningState` 中的 `todos` 为什么会进入最终的 Graph State。

和第六章一致，这种合并是在**构建 Agent 时**完成的，不是运行时每轮临时修改 Graph 的字段定义。

### 9.3 规划指令怎样接入模型调用？

Factory 还会收集 `wrap_model_call`，把多个 Wrapper 组合成一个模型调用包装链。

所以 Todo 的指令插入位置是：

```text
Agent Loop 的 Model Node
           |
           v
组合后的 Model Wrapper 链
           |
           +--> TodoListMiddleware 修改 SystemMessage
           |
           v
真实模型调用
```

至此，三个缺失的环节都被补齐：

```text
TodoListMiddleware
      |
      +--> 提供工具 write_todos -----> ToolNode
      |
      +--> 提供字段 todos ----------> StateGraph
      |
      +--> 提供规划指导 -----------> Model Request
```

这是我们一路推导出来的结构，并不是一开始就要求记住的架构图。

---

## 十、沿退款任务追踪一次真实格式的计划更新

接下来，把前面分开的机制串成一次完整过程。

注意：以下模型判断和文件结果均为**教学构造的执行轨迹**，不是本章环境中真实模型的运行日志。

### 10.1 首轮：建立计划

用户提交任务：

```text
修复优惠券部分退款的精度问题；检查两个模块，修改代码并运行 pytest。
```

模型根据工具说明决定：任务包含多个相互依赖的步骤，先调用 `write_todos`。

```python
# 教学模拟：模型返回的工具调用
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "write_todos",
            "args": {
                "todos": [
                    {
                        "content": "阅读退款计算与优惠分摊代码",
                        "status": "in_progress",
                    },
                    {
                        "content": "明确精度和尾差规则",
                        "status": "pending",
                    },
                    {
                        "content": "修复代码并补足回归测试",
                        "status": "pending",
                    },
                    {
                        "content": "运行 pytest 并复核结果",
                        "status": "pending",
                    },
                ]
            },
            "id": "call_plan_001",
            "type": "tool_call",
        }
    ],
)
```

执行顺序：

```text
模型生成 write_todos
        |
        v
ToolNode 执行 _write_todos
        |
        v
返回 Command(update={"todos": [...], "messages": [...]})
        |
        v
LangGraph 更新 State
        |
        v
模型收到对应 ToolMessage，继续处理文件
```

### 10.2 第二轮：读取两份代码

模型随后决定读取文件：

```text
read_file("shop/refund.py")
read_file("shop/coupon_service.py")
```

工具反馈假设揭示：

```python
# 教学示例：可能存在的问题代码
refund_amount = round(allocated_discount * ratio, 2)
```

同时发现：

```text
业务约定：多次部分退款累计不能超过原优惠分摊总额，
最后一次退款须吸收之前的舍入尾差。
```

模型意识到：旧计划中“修改代码”之前还缺一项更具体的规则确认。

这时它不是另起一段空泛解释，而是再次发起 `write_todos`，用完整的新列表替换旧计划。

### 10.3 第三轮：更新计划而不是忘掉旧计划

```python
# 教学模拟：第二次 write_todos 的参数
updated_todos = [
    {
        "content": "阅读退款计算与优惠分摊代码",
        "status": "completed",
    },
    {
        "content": "明确精度和尾差规则",
        "status": "in_progress",
    },
    {
        "content": "验证最后一次退款吸收累计尾差的约束",
        "status": "pending",
    },
    {
        "content": "修复代码并补足回归测试",
        "status": "pending",
    },
    {
        "content": "运行 pytest 并复核结果",
        "status": "pending",
    },
]
```

这一次更新产生两种可观察变化：

1. `state["todos"]` 变成了新的计划。
2. `messages` 增加了带对应 `tool_call_id` 的工具反馈。

我们的任务不仅“有一个计划”，而且可以观察到**计划怎样因证据而变化**。

### 10.4 测试失败时，计划应该怎样变化？

假设修改后执行：

```bash
pytest shop/tests/test_refund.py
```

得到模拟结果：

```text
FAILED test_partial_refund_rounding
expected: Decimal('12.50')
actual:   Decimal('12.49')
```

如果模型此时把“运行测试”直接标为 `completed`，那只是**记录了执行过测试**，并不意味着**验证目标已经达成**。

更合适的更新是：

```python
# 教学模拟：测试未通过时的 Todo 状态
[
    {"content": "阅读退款计算与优惠分摊代码", "status": "completed"},
    {"content": "确认累计退款尾差规则", "status": "completed"},
    {"content": "修复代码并补足回归测试", "status": "in_progress"},
    {"content": "分析 test_partial_refund_rounding 失败原因", "status": "pending"},
    {"content": "相关 pytest 全部通过并检查回归风险", "status": "pending"},
]
```

但是这里又必须划清边界：

**真实 `TodoListMiddleware` 并不会读取 pytest 输出并自动判定哪个任务真的完成。**

它定义了状态与提示，实际是否根据失败反馈合理更新计划，仍取决于模型的工具使用行为和调用方额外的校验机制。

---

## 十一、一个新问题：两次 `write_todos` 能同时执行吗？

前面我们只考虑正常的顺序更新。

现在，Agent Loop 的模型可能一次生成多个 Tool Call。

例如：

```python
# 教学模拟：同一轮模型输出包含两个规划写入
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "write_todos",
            "args": {"todos": plan_a},
            "id": "call_plan_a",
            "type": "tool_call",
        },
        {
            "name": "write_todos",
            "args": {"todos": plan_b},
            "id": "call_plan_b",
            "type": "tool_call",
        },
    ],
)
```

如果这两个调用都提交：

```python
Command(update={"todos": plan_a})
Command(update={"todos": plan_b})
```

到底哪个应该生效？

我们在第六章已经看到：ToolNode 可以处理多个 Tool Call。但这并不意味着所有业务状态都可以无歧义地并行改写。

**整个 Todo 列表是一个整体写入目标，两个并发更新不存在自然的字段级合并规则。**

因此，固定版本专门为它提供了一个防护。

### 11.1 真实源码：在 `after_model` 检查重复调用

[`todo.py` 第 252—297 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L252-L297) 中的同步 Hook：

```python
# 真实源码节选：after_model 的关键判断
messages = state["messages"]
if not messages:
    return None

last_ai_msg = next(
    (msg for msg in reversed(messages) if isinstance(msg, AIMessage)),
    None,
)
if not last_ai_msg or not last_ai_msg.tool_calls:
    return None

write_todos_calls = [
    tc for tc in last_ai_msg.tool_calls
    if tc["name"] == "write_todos"
]

if len(write_todos_calls) > 1:
    # 对每个冲突的 Tool Call 生成错误 ToolMessage
    ...
```

源码在检测到超过一次 `write_todos` 调用时，会为这些调用生成带 `status="error"` 的工具反馈，然后返回：

```python
# 真实源码的返回形式
return {"messages": error_messages}
```

这段逻辑存在于 `after_model`，而不是 `_write_todos()` 中。

为什么？

因为要检测“同一轮模型输出里有几个 `write_todos`”，最合适的观察点就是**模型已经产生 Tool Calls、但 ToolNode 还未执行它们**的地方。

### 11.2 它如何影响后续路由？

第六章讲过，LangChain 在 `after_model` 之后才判断是否有待执行的工具。

在固定版本的 [`factory.py` 第 1829—1874 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1829-L1874)，路由函数会：

1. 找到最近一条 `AIMessage` 的 Tool Calls。
2. 收集其后已经存在的 `ToolMessage.tool_call_id`。
3. 只把没有对应工具反馈的 Tool Call 作为待执行调用送往 ToolNode。

因此，`after_model` 为冲突的 `write_todos` 调用提前生成错误 ToolMessage 后，这些调用就不再作为尚待执行的普通工具请求继续下发。模型下一轮可以看到错误并重新生成一次合法更新。

这是一处典型的设计演进：

```text
允许模型写计划
      ↓
写入的是整个列表
      ↓
同一轮出现多个完整列表 → 更新冲突
      ↓
需要在实际执行前识别冲突
      ↓
after_model 生成错误反馈
      ↓
下一轮模型修正调用
```

### 11.3 这个检查解决了所有并发一致性问题吗？

没有。

该 Hook 的判断条件是：**同一条 AIMessage 里存在多个 `write_todos` Tool Call**。

它不能凭此解决：

- 两个独立 Agent 执行同时写入同一个共享计划。
- 不同线程对共享任务计划进行竞态修改。
- 某次更新基于陈旧计划而覆盖了别人的修改。
- 外部业务系统与 Agent State 之间的分布式一致性。

这一章只需认识：**框架保护了一种明确而常见的冲突场景，但没有把 Todo 变成分布式事务数据库。**

---

## 十二、为什么 Todo 不等于真正的任务调度器？

回头看当前我们得到的工具能力：

```python
write_todos(todos=[...])
```

它做的事情是**记录和更新计划**。

而以下动作不会仅仅因为更新 Todo 就自动发生：

```text
Todos: “读取 refund.py”
       ≠ 自动调用 read_file

Todos: “修改精度逻辑”
       ≠ 自动调用 edit_file

Todos: “运行 pytest”
       ≠ 自动执行测试命令

Todos: “已完成”
       ≠ 代码一定正确、测试一定通过
```

真正执行文件操作的，仍然是第九章中的 ToolNode、FilesystemMiddleware 和 Backend。

真正让 Agent 再次思考的，仍然是 Agent Loop。

如果需要把任务 A 与任务 B 的依赖、调度、重试预算和完成条件写成**必须遵守的控制流**，就需要更明确的 Workflow/Graph 设计或业务层校验，而不是单靠 `TodoListMiddleware`。

### 12.1 一个对照例子

**情形 A：柔性的工作清单**

```text
模型看到：
  1. 阅读代码 — completed
  2. 修复逻辑 — in_progress
  3. 执行测试 — pending

模型自行决定下一次工具调用。
```

这是本章 `TodoListMiddleware` 的范畴。

**情形 B：必须遵守的固定执行关卡**

```text
只有「编译通过」才能进入「全量测试」；
只有「测试全部通过」才能发布；
只有人工审批通过才能修改生产配置。
```

这类约束应当由明确的 Graph 路由、工具权限与业务验证实现。

两者不是互相取代的关系，而是解决不同问题。

---

## 十三、最小可验证实验：不用模型，也能看见计划怎样变化

前面讨论了很多真实源码，但实际行为理解最好再通过一段最小代码验证。

这里刻意不使用 OpenAI API、不安装 LangChain，也不伪造 ToolNode 的完整实现。实验只验证我们刚才推导出的两个基础性质：

1. `write_todos` 式的更新会**整体替换**当前 Todo 列表。
2. 状态列表与工具反馈是两种独立的结果。

### 13.1 可运行的纯 Python 实验

将下面代码保存为 `chapter11_minimal.py`，使用 Python 3.10+ 运行：

```python
from copy import deepcopy


def make_update(todos, tool_call_id):
    """教学化简：模拟 Command(update=...) 的数据形状。"""
    return {
        "update": {
            "todos": deepcopy(todos),
            "messages": [
                {
                    "type": "tool",
                    "tool_call_id": tool_call_id,
                    "content": "Updated todo list",
                }
            ],
        }
    }


def apply_update(state, command):
    """教学化简：模拟本实验所需的字段更新，不是 LangGraph Reducer。"""
    update = command["update"]
    state["todos"] = deepcopy(update["todos"])
    state["messages"].extend(update["messages"])


state = {"messages": [], "todos": []}

initial_plan = [
    {"content": "阅读 refund.py", "status": "in_progress"},
    {"content": "运行 pytest", "status": "pending"},
]
apply_update(state, make_update(initial_plan, "call_1"))

assert len(state["todos"]) == 2
assert state["todos"][0]["status"] == "in_progress"
assert state["messages"][-1]["tool_call_id"] == "call_1"

revised_plan = [
    {"content": "阅读 refund.py", "status": "completed"},
    {"content": "核对 coupon_service.py 的尾差规则", "status": "in_progress"},
    {"content": "运行 pytest", "status": "pending"},
]
apply_update(state, make_update(revised_plan, "call_2"))

assert len(state["todos"]) == 3
assert state["todos"][0]["status"] == "completed"
assert len(state["messages"]) == 2
assert state["messages"][-1]["tool_call_id"] == "call_2"

# 整体更新的风险：只提交一个条目会丢失另外两个条目。
apply_update(
    state,
    make_update(
        [{"content": "运行 pytest", "status": "in_progress"}],
        "call_3",
    ),
)
assert len(state["todos"]) == 1
assert state["todos"][0]["content"] == "运行 pytest"
assert len(state["messages"]) == 3

print("PASS: 计划整体替换、状态更新与工具反馈分离")
```

### 13.2 这个实验验证了什么？

它验证了一个精确而有限的结论：

> 当计划更新的语义是“写入整个 `todos` 列表”时，调用方必须提交希望保留的完整列表；同时，可以独立维护用于反馈的工具消息。

它**没有**验证：

- LangGraph `Command` 的真实内部执行器。
- `ToolRuntime` 的运行时注入。
- 模型是否主动调用 `write_todos`。
- `after_model` 的真实节点路由。
- DeepAgents 是否正确访问当前项目文件。

这些部分需要在安装指定兼容依赖后，通过框架级集成测试完成。

### 13.3 真实框架接口示例：显式安装 Todo

在安装相应版本及模型提供方依赖后，可以按如下方式构建 Agent：

```python
# 框架接口示例：需要本地安装依赖和配置模型凭证；本章未实际运行
from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_openai import ChatOpenAI

model = ChatOpenAI(model="你的可用模型名称")

agent = create_deep_agent(
    model=model,
    middleware=[TodoListMiddleware()],
)

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": (
                    "请修复 shop/refund.py 的优惠券部分退款精度问题，"
                    "检查 coupon_service.py，补足测试并运行 pytest。"
                ),
            }
        ]
    }
)

print(result.get("todos", []))
```

请特别留意：这里**不能直接声称一定会打印四个任务或全部 `completed`**。是否生成、更新及完成待办，由模型选择及实际执行条件决定。

另一个现实约束：当前 Agent 必须拥有适合该操作的 Backend，并且若要执行 `pytest`，Backend 需要提供相应命令执行能力；仅有 Todo 不会凭空使 Python 测试运行。

---

## 十四、把本章机制拼回已经理解的 Agent Harness

我们先回看从第七章到这里增加了哪些能力。

**第七章**：`create_deep_agent()` 把 Model、Middleware、Backend 等组件装配起来。

**第八章**：Context Middleware 组织发送给模型的上下文。

**第九章**：文件工具通过 ToolNode 与 Backend 实际执行。

**第十章**：敏感工具操作可能在执行前中断等待审批。

**第十一章**：任务步骤被写入独立的 Todo 状态，模型可以利用 Tool Call 更新它。

现在才可以画出这一章完整的执行关系：

```text
用户：修复优惠券部分退款精度 Bug
                |
                v
         create_deep_agent()
                |
                | 显式注册 TodoListMiddleware()
                v
          LangChain create_agent()
                |
                | 收集 middleware.tools
                | 合并 middleware.state_schema
                | 接入 wrap_model_call / after_model
                v
             Agent Graph
                |
                v
             Model Node
                |
                | 模型可见 write_todos 使用说明
                v
           AIMessage.tool_calls
                |
         +------+-------------------+
         |                          |
         v                          v
    write_todos                 read_file / edit_file
         |                          |
         v                          v
 Command(update=...)        FilesystemMiddleware
         |                          |
         v                          v
  todos + ToolMessage             Backend
         |                          |
         +------------+-------------+
                      |
                      v
                Agent State
                      |
                      v
                 下一轮 Model
```

需要准确理解这张图：

- `write_todos` 不修改代码文件。
- `read_file` / `edit_file` 不负责维护 Todo 状态。
- `after_model` 的重复调用检查发生在工具执行前。
- 多个 Middleware 共同参与构建 Graph，但 Planning 不取代 Agent Loop。
- 计划的 `completed` 不是由框架自动验证的业务事实。

到这里，Agent Harness 的规划能力已经有了准确的边界。

---

## 十五、本章结论：Planning 不是再造一个 Loop，而是让任务进度成为状态

本章从一个具体问题开始：

> Agent 虽然能够反复修改、测试和重试，却可能忘记跨文件约束、遗漏必要步骤或过早结束。

最初我们尝试让模型写一段自然语言计划。它对简单任务有用，但程序无法可靠地从中维护和读取任务状态。

于是我们只增加了一个机制：**结构化 Todo 列表**。

然后，为了让这份列表进入现有 Agent Loop，我们逐层需要：

1. `Todo`：定义一项任务的 `content` 和 `status`。
2. `PlanningState`：让 `todos` 成为 Agent State 的字段。
3. `write_todos`：允许模型提交整个任务列表更新。
4. `Command(update=...)`：同时更新 State 与工具反馈消息。
5. `TodoListMiddleware`：声明工具、状态结构，并在模型请求中补充使用说明。
6. `after_model`：检测同一轮多个 `write_todos` 调用产生的冲突。
7. `create_agent()`：把工具、状态和 Middleware Hook 装配进已有 Agent Graph。

从使用者角度看，显式启用的关键代码并不复杂：

```python
from langchain.agents.middleware import TodoListMiddleware
from deepagents import create_deep_agent

agent = create_deep_agent(
    model=model,
    middleware=[TodoListMiddleware()],
)
```

但现在我们知道了它背后发生了什么，也明白它**没有**自动保证什么。

> **Planning 让 Agent 的任务意图变成可观察的 State；能否真实完成任务，仍然要依赖工具执行、结果验证与必要的控制流约束。**

---

## 十六、下一章：一个 Agent Loop 为什么还要委派给另一个 Agent Loop？

现在，退款任务已有可维护的计划：

```text
1. 核对优惠分摊业务规则       completed
2. 修复退款金额计算           in_progress
3. 添加跨场景回归测试         pending
4. 执行相关测试并确认结果     pending
```

但很快，我们会遇到另一个具体困难。

假设任务扩大了：

- 一个方向要审查优惠券金额分摊规则。
- 一个方向要补充完整的退款测试矩阵。
- 主 Agent 还要修改代码、管理审批和整合结论。

如果这些工作全部交给同一个 Agent Loop，所有中间信息会不断混在同一上下文中。

我们自然会问：

> **能不能把一个相对独立的任务交给另一个 Agent，由它拥有自己的工具调用循环，再把结果返回主 Agent？**

这就是下一章的主题：

**第十二章：一个 Agent Loop 如何调用另一个 Loop？——Subagents 与任务委派。**

---

## 附：固定版本源码导航

| 想验证的问题 | 固定源码入口 |
|---|---|
| DeepAgents 的 `create_deep_agent` 参数中如何接收 `middleware` | [`graph.py#L262-L282`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L262-L282) |
| 默认核心 Middleware 是否包含 Todo | [`graph.py#L821-L849`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L821-L849) |
| 自定义 Middleware 如何合并和原位替换 | [`graph.py#L195-L234`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L195-L234) |
| 用户 Middleware 在主 Agent 中的最终装配位置 | [`graph.py#L885-L903`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L885-L903) |
| 最后怎样交给 LangChain `create_agent()` | [`graph.py#L920-L942`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L920-L942) |
| Todo 与 PlanningState 的结构 | [`todo.py#L23-L40`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L23-L40) |
| `write_todos` 输入参数和返回 `Command` | [`todo.py#L43-L46`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L43-L46)、[`#L131-L143`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L131-L143) |
| Todo 使用时机和完成条件的提示说明 | [`todo.py#L48-L118`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L48-L118) |
| `TodoListMiddleware` 怎样声明工具与状态 | [`todo.py#L152-L201`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L152-L201) |
| `wrap_model_call` 怎样增加 SystemMessage | [`todo.py#L202-L251`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L202-L251) |
| `after_model` 怎样检测多个 `write_todos` | [`todo.py#L252-L317`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/todo.py#L252-L317) |
| Factory 怎样收集 Middleware Tools | [`factory.py#L998-L1050`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L998-L1050) |
| Factory 怎样合并 State Schema | [`factory.py#L1124-L1141`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1124-L1141) |
| `after_model` 在 Graph 中的执行位置 | [`factory.py#L1529-L1550`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1529-L1550) |
| 路由如何过滤已收到反馈的 Tool Call | [`factory.py#L1829-L1874`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1829-L1874) |

**本章核心产出：** 读者可以从一个“模型遗忘任务步骤”的真实问题出发，解释为什么需要结构化计划、为什么计划被放进 State、`write_todos` 为什么返回 `Command`、为什么使用 Middleware 同时注入工具和提示词、为什么同一轮多个 Todo 写入被特殊处理，并能够区分**可观察的计划状态**与**真正可靠的任务调度和结果验证**。
