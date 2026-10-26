---
title: 第六章：Middleware 怎样真正接入 Agent Loop？
description: 沿 LangChain 1.4.7 的 create_agent 源码，研究 Middleware 方法识别、生命周期节点编译、Loop 入口与出口、Wrapper 组合及 DeepAgents 装配机制
tags: [DeepAgents, LangChain, LangGraph, Agent Loop, Middleware, 源码精读]
sidebar_position: 6
---

# 第六章：Middleware 怎样真正接入 Agent Loop？

## 一、从上一章留下的问题开始

上一章，我们已经知道为什么需要 Middleware。

假设我们正在开发一个 Coding Agent，负责修复电商优惠券部分退款 Bug。

最初，它只有：

```text
             START
               |
               v
             Model
               |
         是否调用工具？
               |
       +-------+-------+
       |               |
       v               v
      END            ToolNode
                       |
                       v
                     Model
```

后来，我们希望增加两个功能：

1. 每轮模型调用前检查当前消息数量。
2. 每次读取 `refund.py` 前后记录工具日志。

于是定义：

```python
from langchain.agents.middleware import AgentMiddleware


class AuditMiddleware(AgentMiddleware):

    def before_model(self, state, runtime):
        print("即将调用模型")
        return None

    def wrap_tool_call(self, request, handler):
        print("即将执行工具")
        result = handler(request)
        print("工具执行完成")
        return result
```

再传入：

```python
agent = create_agent(
    model=model,
    tools=tools,
    middleware=[
        AuditMiddleware(),
    ],
)
```

问题来了。

**我们只是定义了两个普通 Python 方法。为什么 Agent Loop 会在正确的位置执行它们？**

LangGraph 并不会自动理解：

```python
before_model()
```

这个方法名意味着什么。

它也不知道：

```python
wrap_tool_call()
```

应该包裹哪一段代码。

必须有人负责把这些方法与 Graph 的执行结构连接起来。

这个人就是：

```python
create_agent()
```

所以本章的任务不是再次介绍 Middleware 的作用，而是研究：

> **Agent Factory 如何把 Middleware 的声明变成真实的运行行为？**

---

## 二、第一步：Factory 必须先发现 Middleware 实现了什么

假设我们拥有两个 Middleware。

```python
class ContextMiddleware(AgentMiddleware):

    def before_model(self, state, runtime):
        ...
```

```python
class AuditMiddleware(AgentMiddleware):

    def wrap_tool_call(self, request, handler):
        ...
```

前者只想处理模型调用前的逻辑。

后者只想包裹工具执行。

如果 Factory 不加区分，把所有 Hook 都注册进去，就会产生大量没有实际逻辑的节点和回调。

更重要的是：

有些基础 Hook 并不适合直接执行。

例如 `AgentMiddleware` 的默认 `wrap_model_call()` 是接口占位实现，未提供同步实现时可能抛出 `NotImplementedError`。

所以 Factory 首先需要判断：

**某个 Middleware 是否真正实现了相应 Hook？**

### 2.1 最简单的想法

我们可能会这样写：

```python
if hasattr(middleware, "before_model"):
    ...
```

但这个方案不行。

为什么？

因为即使开发者没有重写：

```python
before_model()
```

子类也会从：

```python
AgentMiddleware
```

继承这个方法。

所以：

```python
hasattr(middleware, "before_model")
```

仍然返回 `True`。

换句话说：

**方法存在，并不意味着开发者实现了它。**

我们需要的是：

> 判断子类的方法是否与基类默认实现不同。

### 2.2 真实源码：比较方法对象

打开：

`langchain/agents/factory.py`

在 `create_agent()` 内找到：

```python
middleware_w_before_model = [
    m
    for m in middleware
    if (
        m.__class__.before_model
        is not AgentMiddleware.before_model
        or
        m.__class__.abefore_model
        is not AgentMiddleware.abefore_model
    )
]
```

这段代码非常值得认真理解。

先看：

```python
m.__class__.before_model
```

它取得当前 Middleware 类型实际使用的方法。

再看：

```python
AgentMiddleware.before_model
```

它取得基类的默认方法。

最后：

```python
is not
```

判断它们是否为同一个方法对象。

如果不同，说明当前类型提供了不同的实现。

例如：

```python
class MyMiddleware(AgentMiddleware):

    def before_model(self, state, runtime):
        print("执行自定义逻辑")
```

它满足：

```python
MyMiddleware.before_model \
    is not AgentMiddleware.before_model
```

因此会被收集进：

```python
middleware_w_before_model
```

### 为什么还要检查 abefore_model？

因为 LangChain 同时支持同步和异步执行。

```python
agent.invoke(...)
```

属于同步调用。

```python
await agent.ainvoke(...)
```

属于异步调用。

Middleware 可以实现对应的：

```python
before_model(...)
abefore_model(...)
```

所以 Factory 必须识别两种实现。

但需要注意：

**被识别为实现了某个 Hook，不意味着它同时支持同步和异步调用。**

如果只实现异步方法，实际使用同步路径时仍然需要遵守接口的支持规则。

### 到这里，我们解决了第一个问题

```text
用户注册 Middleware
          |
          v
Factory 遍历 Middleware
          |
          v
比较子类方法与基类方法
          |
          v
找出真正实现的 Hook
```

现在 Factory 知道：

```text
哪些 Middleware 需要 before_model

哪些 Middleware 需要 after_model

哪些 Middleware 需要 wrap_model_call

哪些 Middleware 需要 wrap_tool_call
```

但知道有哪些 Hook，还不代表知道什么时候执行。

接下来必须把它们放进 Agent Loop。

---

## 三、第二步：为什么 before_model 要变成 Graph Node？

假设我们只注册一个：

```python
class AuditMiddleware(AgentMiddleware):

    def before_model(self, state, runtime):
        print("即将调用模型")
```

我们希望执行：

```text
START
  |
  v
before_model
  |
  v
Model
```

但在上一章的基础 Agent Graph 中，根本不存在：

```text
before_model
```

这个节点。

只有：

```text
model
tools
```

所以 Factory 需要修改 Graph 结构。

这里就能看出 Middleware 的一个重要能力：

**某些 Middleware 不只是给现有节点增加一段代码，而是会让 Agent Graph 产生新的节点。**

### 3.1 真实源码：创建 Middleware Node

在 `factory.py` 中找到：

```python
for m in middleware:
    ...
```

处理 `before_model` 的分支包含：

```python
sync_before = (
    m.before_model
    if (
        m.__class__.before_model
        is not AgentMiddleware.before_model
    )
    else None
)

async_before = (
    m.abefore_model
    if (
        m.__class__.abefore_model
        is not AgentMiddleware.abefore_model
    )
    else None
)
```

然后：

```python
before_node = RunnableCallable(
    sync_before,
    async_before,
    trace=False,
)
```

最后：

```python
graph.add_node(
    f"{m.name}.before_model",
    before_node,
    input_schema=resolved_state_schema,
    trace_policy=_node_trace_policy(m.trace_policy),
)
```

现在一个普通 Python 方法：

```python
AuditMiddleware.before_model()
```

被转成了一个可以由 LangGraph 调度的节点：

```text
AuditMiddleware.before_model
```

但请注意：

到这一步，只完成了节点注册。

**Graph 还没有因为这个节点存在，就自动把它放在 Model 前面。**

节点注册和节点连接是两回事。

这正是下一步。

---

## 四、为什么注册了节点，还必须专门修改入口？

先看没有 Middleware 的情况：

```text
START
  |
  v
Model
```

它可以简单写成：

```python
graph.add_edge(
    START,
    "model",
)
```

现在增加：

```text
AuditMiddleware.before_model
```

如果我们只注册这个节点，而不修改边：

```text
START ------> Model

AuditMiddleware.before_model
```

那么 Middleware 虽然存在，却永远不会被执行。

所以 Factory 必须重新决定：

**Agent 启动时，第一个应该执行的节点是什么？**

### 4.1 真实源码：entry_node

在 `factory.py` 中可以找到：

```python
if middleware_w_before_agent:

    entry_node = (
        f"{middleware_w_before_agent[0].name}"
        ".before_agent"
    )

elif middleware_w_before_model:

    entry_node = (
        f"{middleware_w_before_model[0].name}"
        ".before_model"
    )

else:

    entry_node = "model"
```

最后：

```python
graph.add_edge(
    START,
    entry_node,
)
```

这样：

没有 Middleware 时：

```text
START → Model
```

存在 `before_model` 时：

```text
START → before_model → Model
```

如果还存在 `before_agent`：

```text
START → before_agent → before_model → Model
```

这就解决了：

> Middleware 为什么能够在 Agent 启动时进入正确的执行位置？

因为 Factory 直接改变了 Graph 的入口边。

但这里又出现一个问题。

---

## 五、第三步：为什么必须区分首次入口和循环入口？

现在考虑退款修复任务。

假设我们同时定义：

```python
before_agent()
```

和：

```python
before_model()
```

其中：

```python
before_agent()
```

负责初始化当前任务的审计信息。

```python
before_model()
```

负责每次模型调用前检查上下文。

第一次执行：

```text
START
  |
  v
before_agent
  |
  v
before_model
  |
  v
Model
```

完全正确。

但如果模型决定读取文件：

```text
Model
  |
  v
ToolNode
```

工具执行后，下一步应该去哪里？

### 5.1 第一种错误设计：返回首次入口

假设我们简单地写：

```python
graph.add_edge(
    "tools",
    entry_node,
)
```

如果：

```python
entry_node = "Audit.before_agent"
```

那么实际流程会变成：

```text
START
  |
  v
before_agent
  |
  v
before_model
  |
  v
Model
  |
  v
Tools
  |
  v
before_agent
  |
  v
before_model
  |
  v
Model
```

问题很明显。

**每执行一次工具，就重新运行 before_agent。**

如果 `before_agent` 负责初始化资源、加载初始配置或记录任务开始事件，就可能发生重复初始化。

而我们真正想要的是：

```text
START
  |
  v
before_agent
  |
  v
before_model <------+
  |                 |
  v                 |
Model               |
  |                 |
  v                 |
Tools --------------+
```

因此需要两个不同的入口。

### 5.2 真实源码：entry_node 与 loop_entry_node

Factory 先确定首次入口：

```python
if middleware_w_before_agent:
    entry_node = (
        f"{middleware_w_before_agent[0].name}"
        ".before_agent"
    )
elif middleware_w_before_model:
    entry_node = (
        f"{middleware_w_before_model[0].name}"
        ".before_model"
    )
else:
    entry_node = "model"
```

然后单独确定循环入口：

```python
if middleware_w_before_model:

    loop_entry_node = (
        f"{middleware_w_before_model[0].name}"
        ".before_model"
    )

else:

    loop_entry_node = "model"
```

现在它们的职责就清楚了。

```text
entry_node：
    本次 Graph 执行从哪里开始？

loop_entry_node：
    工具执行结束后，下一轮从哪里开始？
```

这不是两个随意命名的变量。

**它们是在代码层面严格区分 Agent 级生命周期和 Model 级生命周期。**

### 5.3 工具执行完以后，真实路由返回哪里？

继续查看：

```python
graph.add_conditional_edges(
    "tools",
    RunnableCallable(
        _make_tools_to_model_edge(
            tool_node=tool_node,
            model_destination=loop_entry_node,
            structured_output_tools=structured_output_tools,
            end_destination=exit_node,
        ),
        trace=False,
    ),
    tools_to_model_destinations,
)
```

关键是：

```python
model_destination=loop_entry_node
```

这说明：

工具完成后，正常情况下并不是回到：

```python
entry_node
```

而是回到：

```python
loop_entry_node
```

所以 `before_agent` 不会因为每轮工具调用而重复执行。

注意，这里的“一次”是指当前 Agent Graph 的正常执行流程，不意味着同一个 Agent 对象未来每次独立 `invoke()` 都不会再次运行它。

---

## 六、第四步：模型执行后应该从哪里开始判断路由？

到这里，我们已经理解了：

```text
首次进入：
    before_agent → before_model → model

后续循环：
    before_model → model
```

但我们还忽略了：

```python
after_model()
```

假设某个 Middleware 希望在模型回答后记录：

```text
模型发起了哪个 Tool Call？
```

那么下一步路由必须等待这个 Hook 执行完成。

我们不能直接：

```text
Model → ToolNode
```

否则 `after_model` 还没来得及运行，工具就开始执行了。

正确流程应该是：

```text
Model
  |
  v
after_model
  |
  v
判断 Tool Call
  |
  v
ToolNode / END
```

因此，模型执行完成后，用来判断路由的位置也不能固定写死。

这就引出了第三个位置：

```python
loop_exit_node
```

### 6.1 真实源码：loop_exit_node

```python
if middleware_w_after_model:

    loop_exit_node = (
        f"{middleware_w_after_model[0].name}"
        ".after_model"
    )

else:

    loop_exit_node = "model"
```

它代表：

**当前这一轮 Model 阶段完成以后，应该从哪个节点检查是否进入 Tools 或结束。**

当存在 `after_model` 时：

```text
Model
  |
  v
after_model
  |
  v
Conditional Routing
```

不存在时：

```text
Model
  |
  v
Conditional Routing
```

然后 Factory 将模型到工具的条件路由挂在：

```python
loop_exit_node
```

上。

例如：

```python
graph.add_conditional_edges(
    loop_exit_node,
    RunnableCallable(
        _make_model_to_tools_edge(
            model_destination=loop_entry_node,
            structured_output_tools=structured_output_tools,
            end_destination=exit_node,
        ),
        trace=False,
    ),
    model_to_tools_destinations,
)
```

我们现在已经认识了三个重要位置：

```text
entry_node
loop_entry_node
loop_exit_node
```

它们不是为了增加抽象数量而存在。

而是分别解决：

- 本次任务从哪里开始。
- 每次工具完成后重新进入哪里。
- 本轮模型阶段结束后从哪里判断下一步。

---

## 七、第五步：为什么还要独立的 exit_node？

现在假设模型最终返回：

```python
AIMessage(
    content="退款代码已经修复，相关测试通过。",
    tool_calls=[],
)
```

基础 Agent Loop 会直接结束。

但我们还希望在结束前：

```python
after_agent()
```

记录整次任务的总结。

例如：

```text
本次任务调用模型 5 次
执行工具 4 次
最终状态：完成
```

如果模型没有 Tool Call 就直接进入：

```text
END
```

那么 `after_agent()` 无法执行。

所以结束路径也需要一个可配置位置。

### 7.1 真实源码：exit_node

```python
if middleware_w_after_agent:

    exit_node = (
        f"{middleware_w_after_agent[-1].name}"
        ".after_agent"
    )

else:

    exit_node = END
```

为什么取：

```python
[-1]
```

而不是：

```python
[0]
```

这里暂时留下一个小问题。

我们马上研究多个 Middleware 的执行顺序，就会知道原因。

现在我们已经得到了四个关键位置：

| 位置 | 它解决什么问题？ |
|---|---|
| `entry_node` | Agent 首次从哪里开始 |
| `loop_entry_node` | 每一轮模型调用从哪里开始 |
| `loop_exit_node` | 本轮模型调用后在哪里判断路由 |
| `exit_node` | 正常结束前从哪里进入收尾逻辑 |

把它们连起来：

```text
START
  |
  v
entry_node
  |
  v
loop_entry_node <--------------+
  |                            |
  v                            |
Model                           |
  |                            |
  v                            |
loop_exit_node                  |
  |                            |
  v                            |
是否存在待执行工具？            |
  |                            |
  +-- 是 --> ToolNode ----------+
  |
  +-- 否 --> exit_node
                  |
                  v
                 END
```

这可以说是本章最重要的 Graph 结构。

因为我们现在真正看到：

**Middleware 并不是运行在 Agent Loop 外面，而是改变了 Loop 的节点组织和进入、退出位置。**

---

## 八、多个 Middleware 的执行顺序由谁决定？

现在考虑两个 Middleware。

```python
middleware = [
    AuditMiddleware(),
    ContextMiddleware(),
]
```

我们简称：

```text
A = AuditMiddleware
B = ContextMiddleware
```

假设两者都实现了：

```python
before_model()
```

Factory 应该先执行谁？

一种合理规则是按照注册顺序执行：

```text
A.before_model
      |
      v
B.before_model
      |
      v
Model
```

### 8.1 真实源码：构建 before_model 链

```python
if middleware_w_before_model:

    for m1, m2 in itertools.pairwise(
        middleware_w_before_model
    ):

        _add_middleware_edge(
            graph,
            name=f"{m1.name}.before_model",
            default_destination=f"{m2.name}.before_model",
            ...
        )
```

最后一个 Middleware 连接到：

```python
model
```

例如：

```python
_add_middleware_edge(
    graph,
    name=(
        f"{middleware_w_before_model[-1].name}"
        ".before_model"
    ),
    default_destination="model",
    ...
)
```

因此，对于：

```python
middleware=[A(), B()]
```

得到：

```text
A.before_model
      |
      v
B.before_model
      |
      v
Model
```

这非常符合我们对“前置处理”的直觉。

但是，后置处理的顺序会不一样。

---

## 九、为什么 after_model 的顺序是反过来的？

继续假设：

```python
middleware=[A(), B()]
```

它们都实现：

```python
after_model()
```

如果前置处理是：

```text
A → B → Model
```

那么后置处理按照：

```text
Model → B → A
```

运行，就形成一种对称结构。

```text
A.before_model
      |
      v
B.before_model
      |
      v
    Model
      |
      v
B.after_model
      |
      v
A.after_model
```

这种设计有一个明显的好处。

假设 A 是外层审计，B 是内层上下文处理。

那么执行顺序就可以形成：

```text
A 开始审计
    |
    v
B 准备上下文
    |
    v
Model
    |
    v
B 观察模型输出
    |
    v
A 完成审计
```

### 9.1 真实源码：反向连接 after_model

Factory 中：

```python
if middleware_w_after_model:

    graph.add_edge(
        "model",
        f"{middleware_w_after_model[-1].name}.after_model",
    )
```

先从最后一个 Middleware 开始。

然后：

```python
for idx in range(
    len(middleware_w_after_model) - 1,
    0,
    -1,
):

    m1 = middleware_w_after_model[idx]
    m2 = middleware_w_after_model[idx - 1]

    _add_middleware_edge(
        graph,
        name=f"{m1.name}.after_model",
        default_destination=f"{m2.name}.after_model",
        ...
    )
```

所以顺序是：

```text
B.after_model
      |
      v
A.after_model
```

`after_agent` 也采用类似的反向连接方式。

因此上一节：

```python
exit_node = middleware_w_after_agent[-1]
```

就可以解释了。

它需要先进入最后注册的收尾 Middleware，再逐步回到前面注册的 Middleware。

### 一个非常重要的区别

**多个生命周期 Hook 的执行顺序，是 Factory 通过 Graph 的边决定的。**

它不是在运行时简单地执行：

```python
for middleware in middleware_list:
    middleware.after_model(...)
```

因为 `after_model` 的实际顺序需要反转。

到这里，我们已经理解了生命周期 Hook 是怎样接入 Graph 的。

但是还剩下另外两种 Hook：

```python
wrap_model_call()
wrap_tool_call()
```

它们的实现并不一样。

---

## 十、为什么 wrap_model_call 不需要变成 Graph Node？

现在回到之前的需求：

> 模型调用失败时自动重试一次。

我们已经知道：

```python
before_model()
```

是一个独立 Graph Node。

但如果想实现重试，能不能这样做？

```text
before_model
     |
     v
Model
     |
     v
模型调用失败
     |
     v
before_model
     |
     v
Model
```

理论上，我们可以设计一套复杂的 Graph 重试逻辑。

但它存在一个问题。

**我们希望重试的是同一次模型调用，而不是重新运行整个 Agent Loop 的生命周期。**

例如：

```text
before_model：
    检查消息并更新状态

Model：
    请求模型服务
```

如果模型服务超时，我们只想重新执行：

```python
model.invoke(...)
```

不一定希望重新执行全部前置 Hook。

所以更简单的设计是：

**把真正的模型调用变成一个 Handler，让 Wrapper 直接控制它。**

### 10.1 最小 Wrapper 实现

```python
def retry_wrapper(request, handler):

    try:
        return handler(request)

    except TimeoutError:
        return handler(request)
```

这里：

```python
handler(request)
```

就是继续执行模型调用。

Wrapper 可以决定调用几次。

它不需要在 Graph 中增加：

```text
retry_model
```

节点。

### 10.2 真实源码：Model Node 内部调用 Wrapper

打开 `factory.py`，找到：

```python
def _execute_model_sync(
    request: ModelRequest,
) -> ModelResponse:
    ...
```

真正执行模型：

```python
output = model_.invoke(messages)
```

然后在：

```python
def model_node(state, runtime):
    ...
```

中找到：

```python
if wrap_model_call_handler is None:

    model_response = _execute_model_sync(
        request
    )

else:

    result = wrap_model_call_handler(
        request,
        _execute_model_sync,
    )
```

现在已经完全清楚了。

没有 Wrapper 时：

```text
Model Node
    |
    v
_execute_model_sync()
    |
    v
model.invoke()
```

有 Wrapper 时：

```text
Model Node
    |
    v
wrap_model_call()
    |
    v
_execute_model_sync()
    |
    v
model.invoke()
```

所以：

**`wrap_model_call` 是 Model Node 内部的函数包装链，不是一个独立 Graph Node。**

这就是它与 `before_model` 最大的实现区别。

---

## 十一、多个 Model Wrapper 怎样组合？

现在出现一个更复杂的需求。

我们不仅希望记录模型调用，还希望在失败时重试。

假设：

```python
middleware=[
    AuditMiddleware(),
    RetryMiddleware(),
]
```

我们希望：

```text
Audit 开始
    |
    v
Retry 开始
    |
    v
真正调用模型
    |
    v
Retry 完成
    |
    v
Audit 完成
```

也就是说：

**第一个 Middleware 是最外层包装器。**

该如何实现？

我们不需要为每个 Wrapper 创建一个 Graph Node。

只需要把多个函数组合为一个函数。

### 11.1 先看普通 Python 版本

```python
def audit(request, handler):

    print("Audit Before")

    result = handler(request)

    print("Audit After")

    return result


def retry(request, handler):

    print("Retry Before")

    result = handler(request)

    print("Retry After")

    return result
```

现在把它们嵌套：

```python
def composed(request, real_model_call):

    def inner(req):
        return retry(req, real_model_call)

    return audit(request, inner)
```

执行：

```python
composed(request, real_model_call)
```

顺序就是：

```text
Audit Before

Retry Before

Real Model Call

Retry After

Audit After
```

这与 Middleware 注册顺序的关系是：

```text
[A, B]
   |
   v
A(
    B(
        real_model_call
    )
)
```

这叫函数包装组合。

### 11.2 真实源码：_chain_model_call_handlers()

在 `factory.py` 中找到：

```python
def _chain_model_call_handlers(
    handlers,
):
    ...
```

它的核心意图是：

```text
将多个 wrap_model_call
组合成一个统一的 handler
```

源码中存在：

```python
def compose_two(outer, inner):
    ...
```

最后采用从内到外的组合方式：

```python
composed_handler = compose_two(
    handlers[-2],
    handlers[-1],
)

for h in reversed(handlers[:-2]):

    composed_handler = compose_two(
        h,
        composed_handler,
    )
```

最终：

```text
middleware=[A, B, C]
```

得到：

```text
A(
    B(
        C(
            _execute_model_sync
        )
    )
)
```

请求按顺序进入：

```text
A → B → C → Model
```

返回结果反向经过：

```text
Model → C → B → A
```

这里还有一个实现细节：

真实模型包装链不只是简单返回 `AIMessage`，还需要兼容 `ModelResponse`、`ExtendedModelResponse` 和额外的 State Update Command。

因此 Factory 会对返回结果进行规范化，并累积 Middleware 产生的命令。

但这些处理的目的仍然是：

**在保留 Graph 状态协议的前提下，让多个 Wrapper 组成统一的模型执行链。**

---

## 十二、Tool Wrapper 为什么采用相似的设计？

现在我们已经解决了模型调用包装。

接下来考虑工具。

例如：

```python
edit_file("/refund.py", ...)
```

我们希望在真正修改之前：

```text
检查权限
记录审计信息
```

工具执行之后：

```text
记录是否成功
```

所以需要：

```python
wrap_tool_call()
```

但如果有多个 Tool Wrapper，也必须支持组合。

例如：

```python
middleware=[
    AuditMiddleware(),
    PermissionMiddleware(),
]
```

希望实际执行顺序是：

```text
Audit Before
    |
    v
Permission Check
    |
    v
Actual Tool
    |
    v
Permission Result
    |
    v
Audit After
```

### 12.1 真实源码：_chain_tool_call_wrappers()

在 `factory.py` 中：

```python
def _chain_tool_call_wrappers(
    wrappers,
):
    ...
```

可以看到：

```python
def compose_two(outer, inner):

    def composed(request, execute):

        def call_inner(req):
            return inner(req, execute)

        return outer(
            request,
            call_inner,
        )

    return composed
```

它与 Model Wrapper 的核心结构相同。

都是：

```text
Outer → Inner → Real Execution
```

区别在于真正执行的对象不同。

对于 Model：

```python
_execute_model_sync
```

对于 Tool：

```python
tool.invoke(...)
```

### 12.2 Tool Wrapper 最终在哪里执行？

Factory 创建 ToolNode 时：

```python
tool_node = ToolNode(
    tools=available_tools,
    wrap_tool_call=wrap_tool_call_wrapper,
    awrap_tool_call=awrap_tool_call_wrapper,
)
```

这说明：

Factory 负责组合 Tool Wrapper。

ToolNode 负责在真正执行工具时调用它。

因此完整过程是：

```text
Model
  |
  v
AIMessage.tool_calls
  |
  v
ToolNode
  |
  v
Outer Tool Wrapper
  |
  v
Inner Tool Wrapper
  |
  v
Actual Tool Execution
  |
  v
Inner Tool Wrapper
  |
  v
Outer Tool Wrapper
  |
  v
ToolMessage
```

现在，我们终于能准确解释两种扩展机制。

### 生命周期 Hook

通过 Graph Node 和 Edge 接入：

```text
before_model → model → after_model
```

### 调用包装 Hook

通过嵌套函数接入：

```text
outer(inner(actual_call))
```

**这是 LangChain Middleware 架构最重要的两种接入方式。**

---

## 十三、到这里还有一个特殊问题：Middleware 能否改变循环方向？

我们已经知道：

```python
before_model()
```

可以检查当前 State。

但假设我们的 Coding Agent 已经调用了太多次模型。

例如：

```text
已达到预设模型调用上限。
```

我们希望：

**直接结束 Agent，不再调用模型。**

仅仅返回：

```python
None
```

显然不够。

因为正常 Graph 边仍然会进入 Model Node。

我们需要让 Middleware 不只是更新状态，还能改变后续执行路径。

这时才有必要引入：

```python
jump_to
```

### 13.1 最简单的需求

```python
def before_model(self, state, runtime):

    if budget_exhausted(state):
        return {
            "jump_to": "end"
        }

    return None
```

这里的：

```python
"jump_to": "end"
```

表示希望绕过后面的正常执行步骤。

但 Graph 不能仅凭这个字符串就自动跳转。

因为在编译时，Runtime 还需要知道该节点可能连接到哪些目的地。

所以 Middleware 必须提前声明：

> 我可能跳往 end。

### 13.2 真实源码：hook_config

在：

`langchain/agents/middleware/types.py`

中找到：

```python
def hook_config(
    *,
    can_jump_to=None,
):
    ...
```

它会为 Hook 方法附加跳转配置。

例如：

```python
from langchain.agents.middleware.types import (
    AgentMiddleware,
    hook_config,
)


class BudgetMiddleware(AgentMiddleware):

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):

        if len(state["messages"]) > 100:

            return {
                "jump_to": "end"
            }

        return None
```

这里的 `100` 只是用于说明的示例阈值。

现在 Middleware 不只说：

```text
当前要结束
```

还声明：

```text
编译 Graph 时，请允许这一跳转目标。
```

### 13.3 Factory 怎样使用跳转声明？

在 `factory.py` 中：

```python
_get_can_jump_to(
    middleware,
    "before_model",
)
```

读取相应 Hook 的跳转配置。

随后调用：

```python
_add_middleware_edge(...)
```

如果允许跳转，就不是简单添加固定边：

```python
graph.add_edge(...)
```

而是注册条件边：

```python
graph.add_conditional_edges(...)
```

然后根据 State 中的：

```python
jump_to
```

判断实际目标。

所以：

```text
before_model
      |
      v
  jump_to ?
      |
  +---+---+
  |       |
  v       v
model    end
```

这意味着 Middleware 可以影响：

**Agent Loop 接下来是否继续运行。**

### 13.4 为什么不能随便跳往任何节点？

因为跳转会影响整个执行结构。

如果 Middleware 可以任意跳过工具节点或跳入某个内部节点，就可能破坏消息协议或执行状态一致性。

因此，Factory 通过：

```python
can_jump_to
```

限定能够声明的跳转目标。

该固定版本中，主要支持：

```text
tools
model
end
```

具体哪些跳转在某个 Hook 中有意义，仍然需要结合业务约束和消息状态判断。

另外，不能把 `wrap_model_call` 返回的任意 `Command(goto=...)` 当成与生命周期 Hook 的 `jump_to` 完全等价。

在我们固定阅读的 `langchain==1.4.7` 源码里，模型 Wrapper 的额外 Command 处理尚不支持直接使用 `goto` 进行任意跳转，源码会明确抛出 `NotImplementedError`。

因此：

**改变 ModelRequest、返回模型响应、修改 State 和改变 Graph 控制流，是不同层次的能力。**

---

## 十四、Middleware 除了 Hook，还能在构建阶段提供什么？

现在我们已经理解 Middleware 在执行阶段如何工作。

但回到 DeepAgents，我们还会看到：

```python
FilesystemMiddleware(...)
```

它并不只是：

```python
wrap_tool_call()
```

还会声明：

```python
self.tools
```

为什么？

因为它希望 Agent 能使用：

```text
read_file
write_file
edit_file
grep
```

这些工具。

如果 Middleware 不能声明工具，就需要开发者手动提取并传给：

```python
create_agent(tools=...)
```

这样会让完整功能的装配变得繁琐。

因此，Middleware 还承担一项构建阶段的职责：

**向 Agent Factory 声明自己需要添加的能力。**

### 14.1 真实源码：收集工具

```python
middleware_tools = [
    t
    for m in middleware
    for t in getattr(m, "tools", [])
]
```

合并普通工具：

```python
available_tools = (
    middleware_tools + regular_tools
)
```

然后构建：

```python
ToolNode(
    tools=available_tools,
    ...
)
```

这是构建阶段发生的操作。

不是每轮 Agent Loop 都重新扫描和注册所有 Middleware 工具。

### 14.2 为什么 Middleware 还可以声明 State Schema？

假设我们要增加任务规划能力。

它需要保存：

```python
todos
```

但基本 Agent State 中可能没有这个字段。

因此，Middleware 可以声明自己的状态结构。

在 `factory.py` 中：

```python
base_state = (
    state_schema
    if state_schema is not None
    else AgentState
)
```

然后：

```python
state_schemas = [
    *(m.state_schema for m in middleware),
    base_state,
]
```

最后：

```python
resolved_state_schema, input_schema, output_schema = (
    _resolve_schemas(state_schemas)
)
```

这样，Middleware 可以扩展 Graph 可管理的状态字段。

而调用者显式提供的基础 State Schema，在该版本的字段合并顺序中具有更高的冲突优先级。

但这件事必须与 Hook 区分。

```text
Middleware 声明 tools：
    构建时增加可执行能力

Middleware 声明 state_schema：
    构建时增加状态结构

Middleware 实现 before_model：
    执行时增加 Graph 生命周期节点

Middleware 实现 wrap_model_call：
    执行时包裹真实模型调用
```

现在我们已经有了一个完整的 Middleware 接入模型。

---

## 十五、真正的 Middleware 装配过程

把前面的真实源码连起来，可以得到：

```text
                create_agent()
                      |
                      v
               接收 Middleware 列表
                      |
                      v
              检查哪些 Hook 被重写
                      |
            +---------+---------+
            |                   |
            v                   v
       Lifecycle Hooks      Wrapper Hooks
            |                   |
            v                   v
      注册 Graph Nodes     组合 Wrapper Chain
            |                   |
            v                   v
       计算四种入口位置    包裹 Model / Tool
            |                   |
            v                   |
        连接 Graph Edges        |
            |                   |
            +---------+---------+
                      |
                      v
              收集 tools / state_schema
                      |
                      v
                 构建 StateGraph
                      |
                      v
                    compile()
                      |
                      v
                Compiled Agent
```

这张图按照职责分组，并不表示 `factory.py` 的 Python 语句严格按照图中的上下顺序执行。

真正重要的是两种时机。

### 构建阶段

```text
解析 Middleware
注册节点
组合 Wrapper
收集 Tools 和 State Schema
连接路由
编译 Graph
```

### 执行阶段

```text
Graph Runtime 调度生命周期节点
Model Node 内执行模型包装链
ToolNode 内执行工具包装链
Runtime 根据状态推进循环
```

所以：

**Middleware 的装配在构建阶段完成，而它定义的处理逻辑在运行阶段被执行。**

---

# 十六、实验：两个 Middleware 的实际执行顺序

现在用真实 LangChain Agent 验证。

这次实验重点不再是模型智能，也不再是退款算法。

我们只想观察：

**同一个 Agent Loop 注册两个 Middleware 后，生命周期 Hook 与 Wrapper 究竟怎样执行？**

仍然保留同一个退款任务。

执行过程简化为：

```text
模型请求读取 refund.py
    |
    v
工具返回退款源码
    |
    v
模型返回最终分析
```

这意味着：

```text
模型调用：2 次
工具调用：1 次
```

我们用两个 Middleware：

```text
Outer
Inner
```

让它们都实现：

```text
before_agent
before_model
wrap_model_call
after_model
wrap_tool_call
after_agent
```

这样可以同时观察 Graph 节点顺序和函数包装顺序。

## 16.1 安装环境

为了保持源码与运行环境一致：

```bash
pip install "langchain==1.4.7"
```

这个实验不依赖真实模型 API Key。

需要的 LangGraph 和 LangChain Core 由依赖管理器安装相应兼容版本。

## 16.2 完整代码

创建：

`test_middleware_compilation.py`

```python
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)

from langchain_core.messages import (
    AIMessage,
    ToolMessage,
)

from langchain_core.tools import tool


# =========================================
# 1. 用于记录真实调用顺序
# =========================================

events: list[str] = []


# =========================================
# 2. 定义一个真实工具
# =========================================

@tool
def read_refund() -> str:
    """Read the demonstration refund source."""

    events.append("TOOL:read_refund")

    return (
        "def allocate_coupon(total, count):\n"
        "    return [round(float(total) / count, 2)] * count\n"
    )


# =========================================
# 3. 定义一个 Fake Model
# =========================================

class ScriptedModel(FakeMessagesListChatModel):

    def bind_tools(
        self,
        tools: Any,
        **kwargs: Any,
    ):
        # Fake Model 的工具调用已经预先准备。
        return self


model = ScriptedModel(
    responses=[
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_refund",
                    "args": {},
                    "id": "call-001",
                }
            ],
        ),
        AIMessage(
            content=(
                "已读取退款源码。"
                "当前金额分摊需要检查总额守恒问题。"
            )
        ),
    ]
)


# =========================================
# 4. 定义可重复使用的追踪 Middleware
# =========================================

class TraceMiddleware(AgentMiddleware):

    def __init__(self, tag: str):
        self.tag = tag

    @property
    def name(self) -> str:
        # 两个实例必须具有不同名字，
        # 以免注册 Graph Node 时发生冲突。
        return f"Trace{self.tag}"

    def before_agent(self, state, runtime):

        events.append(
            f"{self.tag}:before_agent"
        )

        return None

    def before_model(self, state, runtime):

        events.append(
            f"{self.tag}:before_model"
        )

        return None

    def wrap_model_call(self, request, handler):

        events.append(
            f"{self.tag}:model_enter"
        )

        response = handler(request)

        events.append(
            f"{self.tag}:model_exit"
        )

        return response

    def after_model(self, state, runtime):

        events.append(
            f"{self.tag}:after_model"
        )

        return None

    def wrap_tool_call(self, request, handler):

        events.append(
            f"{self.tag}:tool_enter"
        )

        response = handler(request)

        events.append(
            f"{self.tag}:tool_exit"
        )

        return response

    def after_agent(self, state, runtime):

        events.append(
            f"{self.tag}:after_agent"
        )

        return None


# =========================================
# 5. 注册两个 Middleware
# =========================================

agent = create_agent(
    model=model,
    tools=[read_refund],
    middleware=[
        TraceMiddleware("Outer"),
        TraceMiddleware("Inner"),
    ],
)


# =========================================
# 6. 先检查编译后的 Graph
# =========================================

graph_nodes = set(
    agent.get_graph().nodes
)

expected_nodes = {
    "model",
    "tools",
    "TraceOuter.before_agent",
    "TraceInner.before_agent",
    "TraceOuter.before_model",
    "TraceInner.before_model",
    "TraceOuter.after_model",
    "TraceInner.after_model",
    "TraceOuter.after_agent",
    "TraceInner.after_agent",
}

assert expected_nodes <= graph_nodes

# Wrapper 不会成为同名的独立 Graph Node。
assert not any(
    "wrap_model_call" in node
    or "wrap_tool_call" in node
    for node in graph_nodes
)


# =========================================
# 7. 执行 Agent
# =========================================

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": (
                    "检查 refund.py 中的"
                    "优惠券分摊精度问题。"
                ),
            }
        ]
    },
    config={"recursion_limit": 50},
)


# =========================================
# 8. 观察执行顺序
# =========================================

for index, event in enumerate(events, 1):
    print(f"{index:02d}. {event}")


# =========================================
# 9. 验证完整 Middleware 顺序
# =========================================

expected_events = [
    # 本次 Agent 入口
    "Outer:before_agent",
    "Inner:before_agent",

    # 第一轮模型调用
    "Outer:before_model",
    "Inner:before_model",
    "Outer:model_enter",
    "Inner:model_enter",
    "Inner:model_exit",
    "Outer:model_exit",
    "Inner:after_model",
    "Outer:after_model",

    # 工具执行
    "Outer:tool_enter",
    "Inner:tool_enter",
    "TOOL:read_refund",
    "Inner:tool_exit",
    "Outer:tool_exit",

    # 第二轮模型调用
    "Outer:before_model",
    "Inner:before_model",
    "Outer:model_enter",
    "Inner:model_enter",
    "Inner:model_exit",
    "Outer:model_exit",
    "Inner:after_model",
    "Outer:after_model",

    # 正常结束
    "Inner:after_agent",
    "Outer:after_agent",
]

assert events == expected_events


# =========================================
# 10. 验证工具结果
# =========================================

tool_messages = [
    message
    for message in result["messages"]
    if isinstance(message, ToolMessage)
]

assert len(tool_messages) == 1

assert tool_messages[0].tool_call_id == "call-001"

assert "allocate_coupon" in str(
    tool_messages[0].content
)


print("\nMiddleware composition verified.")
```

## 16.3 预期观察到什么？

关键输出应该是：

```text
01. Outer:before_agent
02. Inner:before_agent

03. Outer:before_model
04. Inner:before_model
05. Outer:model_enter
06. Inner:model_enter
07. Inner:model_exit
08. Outer:model_exit
09. Inner:after_model
10. Outer:after_model

11. Outer:tool_enter
12. Inner:tool_enter
13. TOOL:read_refund
14. Inner:tool_exit
15. Outer:tool_exit

16. Outer:before_model
17. Inner:before_model
18. Outer:model_enter
19. Inner:model_enter
20. Inner:model_exit
21. Outer:model_exit
22. Inner:after_model
23. Outer:after_model

24. Inner:after_agent
25. Outer:after_agent

Middleware composition verified.
```

代码基于 `langchain==1.4.7` 的源码编写；当前运行环境没有安装 LangChain，因此尚未执行该实验。以上是待本地验证的预期执行顺序，而不是已经确认的运行日志。

## 16.4 这个实验验证了哪些设计结论？

第一：

```text
Outer.before_agent
Inner.before_agent
```

只在本次 Agent 入口执行。

第二：

```text
Outer.before_model
Inner.before_model
```

每轮模型调用前都会执行。

第三：

```text
Outer.model_enter
Inner.model_enter
Inner.model_exit
Outer.model_exit
```

证明多个模型 Wrapper 是嵌套关系。

第四：

```text
Outer.tool_enter
Inner.tool_enter
真实工具执行
Inner.tool_exit
Outer.tool_exit
```

证明工具 Wrapper 也具有外层与内层的嵌套关系。

第五：

```text
Inner.after_model
Outer.after_model
```

以及：

```text
Inner.after_agent
Outer.after_agent
```

说明后置 Hook 使用反向执行顺序。

第六：

编译后的 Graph 可以看到：

```text
TraceOuter.before_model
TraceInner.before_model
```

却不会看到：

```text
TraceOuter.wrap_model_call
TraceInner.wrap_tool_call
```

对应的独立节点。

因为前者是 Graph Node。

后者是节点内部的调用包装链。

至此，我们终于不是只知道 Middleware 有哪些 Hook，而是能够解释：

**它们到底怎样由 Factory 接入 Agent Loop。**

---

## 十七、重新回到 DeepAgents：为什么这套机制非常重要？

现在，我们已经理解了 LangChain 的 Middleware 装配机制。

再去看：

```python
create_deep_agent(...)
```

就会发现它做的事情非常自然。

打开固定版本：

`deepagents/graph.py`

找到：

```python
deepagent_middleware = [
    FilesystemMiddleware(
        backend=backend,
        ...
    ),
]
```

再根据配置添加：

```python
create_summarization_middleware(...)
```

```python
PatchToolCallsMiddleware()
```

以及可能启用的：

```text
SkillsMiddleware
MemoryMiddleware
SubAgentMiddleware
HumanInTheLoopMiddleware
```

最后：

```python
return create_agent(
    model,
    tools=_tools,
    middleware=deepagent_middleware,
    ...
)
```

它与本章的实验本质上是同一种装配方式。

区别只是：

我们的实验注册：

```python
TraceMiddleware("Outer")
TraceMiddleware("Inner")
```

而 DeepAgents 注册的是具有真实工程用途的 Middleware。

例如：

```text
FilesystemMiddleware：
    提供文件工具，并处理相应的执行逻辑

SummarizationMiddleware：
    处理长历史造成的模型上下文问题

SkillsMiddleware：
    为 Agent 提供可读取的专项技能内容

MemoryMiddleware：
    为 Agent 提供项目或持久知识

SubAgentMiddleware：
    提供任务委派能力

HumanInTheLoopMiddleware：
    为指定操作增加人工审批
```

这些功能的具体 Hook 组合、State 字段和执行行为并不完全相同。

但它们共用同一套：

```text
AgentMiddleware 声明
        |
        v
create_agent 解析与组合
        |
        v
LangGraph Runtime 执行
```

所以，现在我们已经能够理解 DeepAgents 很重要的一部分架构思想：

> **DeepAgents 不需要为每一项新功能重写 Agent Loop。它主要通过声明和组合新的 Middleware，再利用 LangChain Agent Factory 将这些行为放入既有的循环结构。**

---

## 十八、完整装配链路：把本章放回整个系列

现在可以从 DeepAgents 入口出发，重新走一遍。

```text
                  create_deep_agent()
                          |
                          v
                  初始化 Model / Backend
                          |
                          v
                  构建 Middleware 列表
                          |
                          v
                    create_agent()
                          |
                          v
                 收集 Middleware Tools
                          |
                          v
                 收集 Middleware State
                          |
                          v
                  检测哪些 Hook 被重写
                          |
             +------------+------------+
             |                         |
             v                         v
       生命周期 Hook              包装调用 Hook
             |                         |
             v                         v
       注册 Graph Node           组成 Wrapper Chain
             |                         |
             v                         |
       计算入口和出口位置              |
             |                         |
             v                         |
         连接 Graph Edges              |
             |                         |
             +------------+------------+
                          |
                          v
                    graph.compile()
                          |
                          v
                   Compiled Agent
                          |
                          v
                      agent.invoke()
                          |
                          v
                   LangGraph Runtime
                          |
                          v
                      Agent Loop
```

这时，Agent Loop 已经不只是：

```text
Model → Tools → Model
```

而是一个具有可扩展生命周期的执行系统。

但是它的基本决策—执行—反馈逻辑仍然没有改变。

这正是 Middleware 架构最有价值的地方：

**保持核心机制稳定，让不同的工程能力围绕核心机制组合。**

---

## 十九、设计收益与代价

经过源码分析，现在可以更加具体地评价这种设计。

| 设计 | 解决的问题 | 代价 |
|---|---|---|
| 方法重写检测 | 只注册真正实现的 Hook | 依赖继承和方法身份比较 |
| 生命周期 Hook 转 Graph Node | 让 Hook 具有独立调度位置 | Graph 结构变复杂 |
| `entry_node` | 明确本次 Agent 的起点 | 需要处理不同 Hook 组合 |
| `loop_entry_node` | 避免重复执行 `before_agent` | 首次入口和循环入口必须严格区分 |
| `loop_exit_node` | 确保 `after_model` 先于工具路由 | 条件边不再总是挂在 Model 上 |
| `exit_node` | 支持 Agent 正常结束时的收尾逻辑 | 异常和中断场景仍需额外处理 |
| Model Wrapper | 支持重试、缓存、模型切换 | 多层嵌套影响调试难度 |
| Tool Wrapper | 支持权限检查、审计、工具拦截 | 多个 Wrapper 存在顺序依赖 |
| `jump_to` | Middleware 能够改变控制流 | 必须约束跳转目标及状态一致性 |
| Middleware Tools / State | 扩展能力可以自带工具和状态定义 | 构建阶段需要解决名称及 Schema 冲突 |

其中一个值得注意的工程问题是：

**Middleware 的顺序已经成为系统行为的一部分。**

例如：

```python
middleware=[
    CacheMiddleware(),
    AuditMiddleware(),
]
```

和：

```python
middleware=[
    AuditMiddleware(),
    CacheMiddleware(),
]
```

可能并不等价。

如果缓存位于最外层，命中缓存时可以直接跳过内部审计 Wrapper。

如果审计位于最外层，它就有机会观察缓存命中这一行为。

所以 Middleware 的可组合性不是“顺序永远无关”。

恰恰相反，组合顺序本身就是需要设计和测试的内容。

---

## 二十、本章总结

我们从一个简单问题开始：

> 只是在 Middleware 中重写了 `before_model()`，为什么 Agent 就知道执行它？

首先，我们发现 Factory 必须识别哪些 Hook 被重写。

所以它比较：

```python
m.__class__.before_model
```

与：

```python
AgentMiddleware.before_model
```

是否为同一个方法。

然后，我们发现仅仅识别 Hook 不够。

必须让它成为 Graph 的执行步骤。

所以 Factory 创建：

```text
Middleware.before_model
```

节点。

但创建节点还不够。

需要修改 Graph 的连接方式。

于是引出了：

```text
entry_node
loop_entry_node
loop_exit_node
exit_node
```

然后，我们又发现某些扩展并不是一个独立执行步骤，而是需要控制真实的模型或工具调用。

于是 Factory 通过：

```python
_chain_model_call_handlers()
```

和：

```python
_chain_tool_call_wrappers()
```

创建包装链。

最后，我们进一步看到：

Middleware 还可以声明自己的：

```python
tools
state_schema
```

甚至在允许的情况下修改：

```python
jump_to
```

影响后续路由。

最终得到了一个完整的认识：

> **LangChain 的 Middleware 并不是一组运行时随意调用的回调。Agent Factory 会在构建阶段分析 Middleware 的声明，将生命周期 Hook 编译为 Graph 节点，将模型与工具 Wrapper 组合为嵌套调用链，再把这些节点和调用链接入同一个 Agent Loop。**

这也就解释了为什么 DeepAgents 能够在不重写基础 Agent Loop 的情况下，不断增加复杂能力。

---

# 下一章：DeepAgents 如何装配自己的 Agent Harness？

现在我们已经研究清楚：

```text
Agent Loop 怎样构建？
Middleware 为什么需要？
Middleware 怎样接入？
```

但还没有完整回答：

> **DeepAgents 的 `create_deep_agent()` 究竟选择了哪些 Middleware？为什么以这样的顺序注册？哪些能力默认启用，哪些能力需要显式配置？**

下一章就回到：

```python
create_deep_agent(...)
```

按照真实源码的装配顺序阅读。

但我们的阅读方式不会变成机械地罗列组件。

仍然沿着统一的 Coding Agent 任务：

```text
修复优惠券部分退款精度 Bug
```

去思考：

```text
最初只有 Agent Loop
        |
        v
需要访问项目文件
        |
        v
添加 FilesystemMiddleware
        |
        v
需要防止长任务上下文失控
        |
        v
添加 SummarizationMiddleware
        |
        v
需要支持任务委派
        |
        v
添加 SubAgentMiddleware
        |
        v
需要技能和长期知识
        |
        v
添加 Skills / Memory
        |
        v
需要执行权限控制
        |
        v
添加 HITL
```

最后对照真实 `graph.py`，还原 DeepAgents 的完整装配过程。

我们也会明确区分：

**默认装配、按需启用、模型 Profile 影响，以及调用者自定义 Middleware。**

---

## 本章源码导航

### LangChain 固定版本

本章使用：

```text
langchain==1.4.7
```

**Middleware 协议：**

https://github.com/langchain-ai/langchain/blob/langchain%3D%3D1.4.7/libs/langchain_v1/langchain/agents/middleware/types.py

**Agent Factory：**

https://github.com/langchain-ai/langchain/blob/langchain%3D%3D1.4.7/libs/langchain_v1/langchain/agents/factory.py

建议按照本章问题顺序阅读：

```text
create_agent()
    |
    v
middleware_w_before_model
    |
    v
graph.add_node(
    "<middleware>.before_model"
)
    |
    v
entry_node
    |
    v
loop_entry_node
    |
    v
loop_exit_node
    |
    v
exit_node
    |
    v
_add_middleware_edge()
    |
    v
_chain_model_call_handlers()
    |
    v
_chain_tool_call_wrappers()
    |
    v
graph.compile()
```

### DeepAgents 固定版本

```text
DeepAgents：0.7.32
Commit：e85c6b9
```

**入口：**

https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py

重点：

```text
create_deep_agent()
    |
    v
deepagent_middleware
    |
    v
应用配置与自定义 Middleware
    |
    v
create_agent(
    middleware=deepagent_middleware
)
```

---

## 本章自检

读完本章，应该能够不看源码，自己解释：

1. Factory 怎样知道一个 Middleware 是否重写了某个 Hook？
2. 为什么 `hasattr()` 不能正确完成这项判断？
3. 为什么 `before_model` 会成为 Graph Node？
4. 为什么只有注册节点，还不能保证它被执行？
5. `entry_node` 与 `loop_entry_node` 为什么必须不同？
6. `after_model` 为什么需要影响条件路由的位置？
7. 多个 `before_model` 与 `after_model` 的执行顺序为什么不同？
8. 为什么 `wrap_model_call` 不必成为独立 Graph Node？
9. 多个 `wrap_model_call` 如何组成外层和内层？
10. `wrap_tool_call` 最终由哪个组件调用？
11. `hook_config(can_jump_to=...)` 为什么影响 Graph 编译？
12. Middleware 声明的 `tools` 和 `state_schema` 分别在什么时候生效？

如果能回答这些问题，应该已经能够从源码上理解：

**一个基础 Agent Loop，是如何变成可扩展的 Agent Harness 的。**
