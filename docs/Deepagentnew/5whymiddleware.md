---
title: 第五章：为什么 Agent Loop 需要 Middleware？
description: 从一个可运行的 Agent Loop 出发，逐步推导 Middleware 的设计动机，理解生命周期 Hook、模型调用包装、工具调用包装及其与 DeepAgents 的架构关系
tags: [DeepAgents, Agent Loop, Middleware, LangChain, LangGraph, 源码精读]
sidebar_position: 5
---

# 第五章：为什么 Agent Loop 需要 Middleware？

## 一、先回到上一章已经完成的 Agent Loop

上一章，我们研究了 LangChain 的：

```python
create_agent(...)
```

知道它负责将模型和工具组织成可执行的 Agent Graph。

例如：

```python
agent = create_agent(
    model=model,
    tools=[
        read_refund,
        patch_refund,
        run_refund_tests,
    ],
)
```

调用：

```python
agent.invoke({
    "messages": [
        {
            "role": "user",
            "content": "修复退款精度 Bug 并运行测试",
        }
    ]
})
```

内部形成：

```text
                START
                  |
                  v
                Model
                  |
            有 Tool Call？
                  |
          +-------+-------+
          |               |
          v               v
         否              是
          |               |
          v               v
         END             Tools
                          |
                          v
                        Model
```

这张图已经具备 Agent 的基本能力。

假设模型执行了三轮工具操作：

```text
第一轮：
    读取 refund.py

第二轮：
    修改 refund.py

第三轮：
    运行 pytest
```

当测试通过以后，模型返回最终回答，Agent Loop 结束。

到这里，我们拥有的是一个**功能完整但扩展能力有限的基础 Agent Loop**。

接下来，我们开始为它添加真实工程需求。

---

## 二、提出第一个需求：每次模型调用前记录消息数量

我们的 Coding Agent 在修复退款问题时，会不断积累消息：

```text
HumanMessage：
    修复 refund.py

AIMessage：
    调用 read_file

ToolMessage：
    refund.py 的源码

AIMessage：
    调用 edit_file

ToolMessage：
    文件修改成功

AIMessage：
    调用 execute

ToolMessage：
    pytest 失败日志
```

随着循环持续执行，消息会越来越多。

因此，我们希望在每次模型调用前打印：

```text
当前模型即将接收 7 条消息
```

最直接的办法是什么？

修改：

```python
model_node()
```

### 2.1 第一种实现：直接写进 Model Node

我们可以想象在第四章的 Model Node 中加入：

```python
def model_node(state, runtime):

    messages = state["messages"]

    print(
        "本轮消息数量：",
        len(messages),
    )

    response = model.invoke(messages)

    return {
        "messages": [response]
    }
```

这是教学化简代码，不是 LangChain 原样实现。

它能够解决问题。

模型每执行一次，都会打印当前消息数量。

```text
Model 第一次执行：
    1 条消息

Model 第二次执行：
    3 条消息

Model 第三次执行：
    5 条消息
```

看起来并没有必要增加新的抽象。

**此时，简单方案是成立的。**

我们先不要急着引入 Middleware。

继续增加需求，观察它什么时候开始不够用。

---

## 三、第二个需求：模型调用前还需要压缩上下文

假设模型上下文越来越长。

我们希望在模型调用前检查：

```python
if token_count(messages) > threshold:
    ...
```

然后压缩部分历史。

于是 Model Node 变成：

```python
def model_node(state, runtime):

    messages = state["messages"]

    print(
        "消息数量：",
        len(messages),
    )

    if token_count(messages) > threshold:
        messages = summarize_history(messages)

    response = model.invoke(messages)

    return {
        "messages": [response]
    }
```

现在它同时负责：

```text
执行模型调用
统计消息
管理上下文
```

暂时还能接受。

但很快，业务又提出要求：

> 如果模型调用失败，希望自动重试一次。

于是：

```python
def model_node(state, runtime):

    messages = state["messages"]

    print(
        "消息数量：",
        len(messages),
    )

    if token_count(messages) > threshold:
        messages = summarize_history(messages)

    for attempt in range(2):

        try:
            response = model.invoke(messages)
            break

        except Exception:

            if attempt == 1:
                raise

    return {
        "messages": [response]
    }
```

现在 Model Node 又增加了：

```text
重试策略
```

继续增加需求：

```text
调用前加载项目指令
调用前检查 Token 预算
根据任务选择不同模型
记录模型调用耗时
记录模型输出
处理结构化输出
```

最后，Model Node 可能变成：

```python
def model_node(state, runtime):

    load_memory()

    prepare_skills()

    check_token_budget()

    summarize_if_needed()

    select_model()

    try:
        response = call_with_retry()
    except Exception:
        ...

    log_model_response()

    validate_output()

    update_statistics()

    return ...
```

问题开始显现。

### 3.1 真正的问题不是代码行数增加

真正的问题是：

**本来只负责执行一轮模型调用的 Model Node，现在混入了大量可以独立变化的策略。**

例如：

```text
项目 A：
    需要上下文压缩

项目 B：
    不需要上下文压缩

项目 C：
    需要模型重试

项目 D：
    需要动态模型选择
```

如果所有能力都写进 Model Node，就要不断增加：

```python
if enable_summarization:
    ...

if enable_retry:
    ...

if enable_memory:
    ...
```

甚至需要为不同 Agent 复制一份 Model Node。

这样就违背了第四章创建 `create_agent()` 的初衷：

**我们希望复用同一个基础 Agent Loop，而不是每创建一种 Agent，就修改一次核心执行逻辑。**

于是产生一个新的设计需求：

> 能不能保持 Model Node 不变，却允许外部组件在模型调用前后增加处理逻辑？

这才是我们真正需要 Middleware 的原因。

---

## 四、先设计一个最简单的扩展点

既然我们需要：

```text
模型调用前执行额外逻辑
```

最简单的方案是让 Model Node 接收一个可选函数：

```python
def model_node(
    state,
    before_model=None,
):

    if before_model is not None:
        before_model(state)

    response = model.invoke(
        state["messages"]
    )

    return {
        "messages": [response]
    }
```

现在开发者就可以定义：

```python
def log_message_count(state):

    print(
        "本轮消息数量：",
        len(state["messages"]),
    )
```

然后交给 Model Node。

这样不需要修改核心模型调用逻辑，就能添加日志能力。

这是一个很有用的变化。

我们第一次得到了：

**扩展点。**

### 4.1 什么是扩展点？

不需要背定义。

只需要理解：

> 核心流程在某个明确位置，允许外部逻辑参与执行。

例如：

```text
原来：

Model Node
    |
    v
model.invoke()


增加扩展点后：

Model Node
    |
    v
before_model()
    |
    v
model.invoke()
```

模型调用行为没变。

但模型调用前可以插入额外处理。

不过，如果我们需要两个处理函数呢？

```python
log_message_count()
check_token_budget()
```

显然，单个函数参数又不够了。

所以，我们可以进一步支持多个处理函数。

---

## 五、从单个函数发展为一组处理器

最简单的设计：

```python
def model_node(
    state,
    before_model_handlers,
):

    for handler in before_model_handlers:
        handler(state)

    response = model.invoke(
        state["messages"]
    )

    return {
        "messages": [response]
    }
```

现在可以注册：

```python
before_model_handlers = [
    log_message_count,
    check_token_budget,
]
```

执行过程：

```text
                Model Node
                    |
                    v
             log_message_count
                    |
                    v
              check_token_budget
                    |
                    v
                model.invoke()
```

我们解决了两个问题：

1. 核心模型调用不必知道每种扩展策略。
2. 可以按需要组合多个处理函数。

但新的问题又出现了。

### 5.1 并不是所有处理逻辑都应该在模型调用前执行

例如：

**记录模型输出**

需要在模型返回以后执行。

**检查测试命令是否允许执行**

应该在工具真正执行之前处理。

**统计一次 Agent 的总耗时**

可能只需要在整个 Agent 结束时执行。

如果只有：

```python
before_model_handlers
```

显然无法合理放置这些逻辑。

于是，我们必须继续研究：

**Agent Loop 内部究竟有哪些不同的处理位置？**

这就自然引出了生命周期。

---

## 六、为什么需要区分 Agent 生命周期与 Model 生命周期？

先画出基础循环。

```text
                 START
                   |
                   v
                 Model <----------+
                   |              |
                   v              |
             是否调用工具？       |
                   |              |
          +--------+------+       |
          |               |       |
          v               v       |
         END             Tools ----+
```

仔细观察：

```text
START
```

只经过一次。

但是：

```text
Model
```

可能执行很多次。

例如修复退款问题：

```text
第一次模型调用：
    决定读取 refund.py

第二次模型调用：
    决定修改代码

第三次模型调用：
    决定运行测试

第四次模型调用：
    看到测试失败，继续修复

第五次模型调用：
    测试通过，返回最终答案
```

现在考虑两个需求。

### 需求 A：任务开始时初始化一次环境

例如：

```python
initialize_task()
```

只需要在本次 Agent 运行开始时执行一次。

### 需求 B：每次模型调用前检查上下文

例如：

```python
check_context()
```

需要每一轮执行。

如果将两者都放进同一个：

```python
before_model()
```

就可能反复执行：

```python
initialize_task()
```

造成不必要的开销，甚至重复创建资源。

所以我们至少需要区分两个位置。

```text
START
  |
  v
before_agent             本次运行开始时
  |
  v
before_model <---------+
  |                    |
  v                    |
Model                   | 每次模型调用前
  |                    |
  v                    |
Tools ------------------+
```

现在才有理由引入两个不同的 Hook 名称：

```python
before_agent()
```

以及：

```python
before_model()
```

### 6.1 真实源码：AgentMiddleware

打开：

`langchain/agents/middleware/types.py`

找到：

```python
class AgentMiddleware(
    Generic[StateT, ContextT, ResponseT]
):
    ...
```

这是 LangChain 定义 Middleware 能力的基础类。

在其中可以找到：

```python
def before_agent(
    self,
    state: StateT,
    runtime: Runtime[ContextT],
) -> dict[str, Any] | None:
    ...
```

以及：

```python
def before_model(
    self,
    state: StateT,
    runtime: Runtime[ContextT],
) -> dict[str, Any] | None:
    ...
```

这两种方法看起来很相似。

它们都接收：

```python
state
runtime
```

都可以返回：

```python
dict
```

或：

```python
None
```

但它们的执行位置不同。

**`before_agent` 属于本次 Agent 运行的入口阶段。**

**`before_model` 属于每轮模型调用前的阶段。**

为什么它们可以返回字典？

因为有些前置处理不只是打印日志，还可能需要更新 State。

例如：

```python
return {
    "task_status": "initialized"
}
```

这里暂时只理解这种返回值表达的是状态更新。

具体怎样合并 State，会在后面的 State 章节研究。

---

## 七、为什么还需要 after_model？

现在我们能够在模型调用前执行处理。

但是还需要一个功能：

> 每次模型产生响应后，记录它是否请求了工具。

例如：

```python
AIMessage(
    tool_calls=[
        {
            "name": "run_refund_tests",
            "args": {},
            "id": "call-003",
        }
    ]
)
```

我们希望打印：

```text
本轮模型请求执行 run_refund_tests
```

这个逻辑必须在模型响应产生以后执行。

因此，增加一个新的处理位置：

```text
before_model
     |
     v
    Model
     |
     v
 after_model
     |
     v
是否调用工具？
```

这样，模型执行过程就拥有：

```text
模型调用前
模型执行
模型调用后
```

三个阶段。

### 7.1 真实源码

在 `AgentMiddleware` 中：

```python
def after_model(
    self,
    state: StateT,
    runtime: Runtime[ContextT],
) -> dict[str, Any] | None:
    ...
```

这里同样接收：

```python
state
runtime
```

并允许返回状态更新。

它适合处理：

```text
观察模型输出
记录模型行为
检查某些响应条件
在继续路由前修改 State
```

但要注意一个重要的执行顺序：

**`after_model` 发生在本轮模型响应产生之后、下一步工具执行之前。**

所以：

```text
after_model
```

不等于：

```text
after_tool
```

如果我们希望记录工具的执行结果，应该另找一个接入位置。

这是下一步需要解决的问题。

---

## 八、为什么还需要 after_agent？

现在我们拥有：

```text
before_agent
before_model
after_model
```

但如果希望统计：

```text
本次 Agent 一共执行了多少轮？
最终是否完成？
总共调用了哪些工具？
```

应该放在哪里？

如果放在：

```python
after_model()
```

就会每轮执行一次。

但我们希望在 Agent 正常结束时汇总。

因此需要：

```python
after_agent()
```

于是整个生命周期变成：

```text
                     START
                       |
                       v
                  before_agent
                       |
                       v
                  before_model <---------+
                       |                 |
                       v                 |
                     Model               |
                       |                 |
                       v                 |
                   after_model           |
                       |                 |
                 有 Tool Call？          |
                       |                 |
             +---------+---------+       |
             |                   |       |
             v                   v       |
            否                  是       |
             |                   |       |
             v                   v       |
         after_agent           Tools -----+
             |
             v
            END
```

### 8.1 真实源码

```python
def after_agent(
    self,
    state: StateT,
    runtime: Runtime[ContextT],
) -> dict[str, Any] | None:
    ...
```

它代表：

**Agent 本次执行走到正常结束阶段后，可以进行的收尾处理。**

不过，不能因此假设：

> 只要 Agent 开始执行，after_agent 就一定会被调用。

如果出现未处理异常，或者执行在中断点暂停，并未正常进入结束路径，就不能把它当成无条件执行的 `finally`。

这个区别在设计审计、资源回收或安全处理时尤其重要。

---

## 九、到这里，我们仍然缺少一种能力

现在我们已经有四个处理位置：

```text
before_agent
before_model
after_model
after_agent
```

它们主要通过：

```python
state
runtime
```

观察当前执行状态，并且可以提交状态更新。

但继续考虑前面的需求：

> 模型调用失败时，需要自动重试。

如果用：

```python
before_model()
```

能实现吗？

不能完整实现。

因为 `before_model()` 在模型调用之前执行。

它还拿不到这一次模型调用可能抛出的异常。

那么：

```python
after_model()
```

呢？

也不行。

如果模型调用已经抛出异常，正常的 `after_model()` 执行路径可能根本不会到达。

我们真正需要的是：

**在外部包住整个模型调用。**

这与普通的“调用前执行”和“调用后执行”不同。

---

## 十、由模型重试需求推导 Wrapper

假设模型调用代码是：

```python
response = model.invoke(messages)
```

我们希望：

```python
try:
    response = model.invoke(messages)
except Exception:
    response = model.invoke(messages)
```

这里最关键的能力是：

**外部逻辑必须拥有是否执行、何时执行和执行几次模型调用的控制权。**

因此，可以将真正的模型调用包装为一个函数：

```python
def execute_model():
    return model.invoke(messages)
```

然后交给外部处理器：

```python
def retry_model(handler):

    for attempt in range(2):

        try:
            return handler()

        except Exception:

            if attempt == 1:
                raise
```

调用：

```python
response = retry_model(execute_model)
```

这是一种函数包装模式。

它与前面的生命周期 Hook 有本质区别。

前面的：

```python
before_model()
```

由执行流程自动调用一次。

而现在：

```python
retry_model(handler)
```

可以决定：

```text
调用 handler 一次
调用 handler 多次
完全不调用 handler
```

这种能力适合：

```text
模型重试
模型回退
缓存命中直接返回
动态调整模型请求
统计模型调用耗时
```

所以 LangChain 需要专门提供模型调用包装 Hook。

---

## 十一、真实源码：wrap_model_call

打开：

`langchain/agents/middleware/types.py`

找到：

```python
def wrap_model_call(
    self,
    request: ModelRequest[ContextT],
    handler: Callable[
        [ModelRequest[ContextT]],
        ModelResponse[ResponseT],
    ],
) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
    ...
```

先不要被这里的类型吓住。

我们已经知道它需要解决什么问题：

> 让外部逻辑控制一次模型调用。

所以只需要关注两个参数。

### 第一个参数：request

```python
request
```

表示本次模型调用所需要的信息。

例如：

```text
本轮 messages
使用哪个 model
可用 tools
system_message
state
runtime
```

### 第二个参数：handler

```python
handler
```

表示：

**真正执行后续模型调用的函数。**

因此，最简单的 Wrapper 是：

```python
def wrap_model_call(self, request, handler):

    print("模型调用前")

    response = handler(request)

    print("模型调用后")

    return response
```

这里：

```python
handler(request)
```

才是真正继续执行模型调用的位置。

### 11.1 为什么要把 request 也传给 handler？

因为 Wrapper 可能希望修改本轮请求。

例如，我们想增加一条临时系统指令。

概念上可以写：

```python
def wrap_model_call(self, request, handler):

    modified_request = request.override(
        system_message=...
    )

    return handler(modified_request)
```

这里的关键不是修改系统提示词的具体语法。

而是：

**Wrapper 能在请求实际交给模型之前改变它。**

这与：

```python
before_model(state, runtime)
```

不同。

后者主要在 Graph 生命周期层面操作 State。

前者直接处理本次：

```python
ModelRequest
```

因此我们现在可以清楚地区分：

```text
before_model：
    进入 Model Node 前执行一段逻辑

wrap_model_call：
    包裹 Model Node 内部的真实模型调用
```

这两个能力并不重复。

---

## 十二、如果工具执行也需要被拦截呢？

现在我们已经理解模型调用为什么需要 Wrapper。

接下来回到退款任务。

假设模型决定执行：

```python
run_refund_tests()
```

我们希望在测试前打印：

```text
准备执行退款测试
```

执行后打印：

```text
退款测试执行完成
```

最直接的办法是修改工具：

```python
def run_refund_tests():

    print("准备执行测试")

    result = execute_pytest()

    print("测试执行完成")

    return result
```

但现在有多个工具：

```text
read_file
edit_file
run_refund_tests
execute
```

如果希望对所有工具记录日志，难道要逐个修改？

如果希望禁止某些危险工具，是不是每个工具里都要增加权限检查？

这再次出现了前面遇到的问题：

**通用处理逻辑与具体业务操作混在一起。**

因此，我们可以采用与模型调用相同的包装方法。

```python
def audit_tool(request, handler):

    print("工具执行前")

    result = handler(request)

    print("工具执行后")

    return result
```

于是：

```text
               Tool Call
                   |
                   v
             audit_tool()
                   |
                   v
             handler(request)
                   |
                   v
              真实工具执行
                   |
                   v
                Tool Result
```

现在，工具本身不需要知道审计逻辑的存在。

这就是：

```python
wrap_tool_call()
```

的设计动机。

---

## 十三、真实源码：wrap_tool_call

同样在：

`langchain/agents/middleware/types.py`

找到：

```python
def wrap_tool_call(
    self,
    request: ToolCallRequest,
    handler: Callable[
        [ToolCallRequest],
        ToolMessage | Command[Any],
    ],
) -> ToolMessage | Command[Any]:
    ...
```

这里只关注两个参数。

### request

表示当前工具调用请求。

包括：

```text
Tool Call 名称
工具参数
工具对象
当前 State
Runtime
```

### handler

表示：

**继续执行该工具调用的函数。**

例如：

```python
def wrap_tool_call(self, request, handler):

    name = request.tool_call["name"]

    print("开始执行：", name)

    result = handler(request)

    print("执行结束：", name)

    return result
```

到这里，Tool Wrapper 的作用就很清楚了。

### 13.1 为什么 Wrapper 可以拦截危险操作？

因为它可以选择不调用：

```python
handler(request)
```

例如：

```python
def wrap_tool_call(self, request, handler):

    name = request.tool_call["name"]

    if name == "dangerous_operation":

        return ToolMessage(
            content="This operation is not allowed.",
            tool_call_id=request.tool_call["id"],
            name=name,
            status="error",
        )

    return handler(request)
```

这是教学示例，不是 DeepAgents 默认的权限实现。

但它体现了一个非常重要的设计能力：

**模型提出工具请求，并不代表系统一定要执行。**

真正的执行仍然可以在 Wrapper 层被控制。

这也是后面 HITL、工具权限检查等机制的基础。

---

## 十四、现在我们终于能够给 Middleware 一个完整定义

我们不是一开始就背：

```text
Middleware 是一种用于扩展 Agent 的机制。
```

而是经过了几个实际问题。

第一，模型调用前需要记录消息长度。

所以引入：

```python
before_model()
```

第二，本次 Agent 开始时需要进行初始化。

所以引入：

```python
before_agent()
```

第三，需要观察模型响应。

所以引入：

```python
after_model()
```

第四，需要在 Agent 结束时进行收尾处理。

所以引入：

```python
after_agent()
```

第五，需要控制真实模型调用，例如重试。

所以引入：

```python
wrap_model_call()
```

第六，需要控制真实工具执行，例如权限检查。

所以引入：

```python
wrap_tool_call()
```

这六种 Hook 并不是随意设计出来的。

**它们分别对应 Agent Loop 中不同生命周期与执行控制需求。**

现在可以把它们统一称为：

**Agent Middleware 的扩展接口。**

---

## 十五、把 Middleware 放回 Agent Loop

我们现在重新画一张图。

```text
                     START
                       |
                       v
                  before_agent
                       |
                       v
             +---- before_model <--------+
             |           |               |
             |           v               |
             |     wrap_model_call       |
             |           |               |
             |           v               |
             |         Model             |
             |           |               |
             |           v               |
             |     wrap_model_call       |
             |       返回结果            |
             |           |               |
             |           v               |
             |       after_model         |
             |           |               |
             |      Has Tool Call?       |
             |           |               |
             |     +-----+-----+         |
             |     |           |         |
             |     v           v         |
             |    No          Yes        |
             |     |           |         |
             |     v           v         |
             | after_agent  wrap_tool_call|
             |     |           |         |
             |     v           v         |
             |    END        ToolNode     |
             |                 |          |
             |                 v          |
             |           wrap_tool_call   |
             |             返回结果       |
             |                 |          |
             +-----------------+----------+
```

这是一张概念图。

其中需要特别注意：

**before_agent、before_model、after_model、after_agent 属于生命周期处理位置。**

**wrap_model_call 和 wrap_tool_call 属于具体调用的包装机制。**

它们在源码中的实现方式也不同。

但这不是本章需要立刻展开的内容。

我们下一章会专门阅读 `factory.py`，研究：

> 为什么某些 Hook 会编译成 Graph Node，而另一些 Hook 会变成函数包装链？

本章先理解为什么要区分这些扩展位置。

---

## 十六、实验：给真实 Agent Loop 增加 Middleware

现在我们已经知道 Middleware 应该做什么。

接下来使用 LangChain 的真实接口验证：

1. `before_agent` 是否在本次 Agent 开始时执行。
2. `before_model` 是否在每次模型调用前执行。
3. `wrap_model_call` 是否真正包裹模型调用。
4. `after_model` 是否在每轮模型输出后执行。
5. `wrap_tool_call` 是否在工具执行前后运行。
6. `after_agent` 是否在 Agent 正常结束时执行。

实验仍然使用退款 Coding Agent。

为了避免 API Key，我们使用 Fake Model。

但 Agent Loop、Middleware 和 ToolNode 都由 LangChain / LangGraph 运行。

### 16.1 安装依赖

建议使用与 DeepAgents 0.7.32 兼容的环境：

```bash
pip install "deepagents==0.7.32"
```

这也会安装所需的 LangChain 和 LangGraph 依赖。

### 16.2 完整代码

创建：

`test_agent_middleware.py`

```python
from typing import Any

from langchain.agents import create_agent

from langchain.agents.middleware import (
    AgentMiddleware,
)

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)

from langchain_core.messages import (
    AIMessage,
    ToolMessage,
)

from langchain_core.tools import tool


# =========================================
# 1. 模拟退款项目
# =========================================

project = {
    "/refund.py": (
        "def calculate_refund(amount):\n"
        "    return round(amount)\n"
    )
}

events = []


# =========================================
# 2. 定义三个工具
# =========================================

@tool
def read_refund() -> str:
    """Read the refund.py source code."""

    events.append("tool:read_refund")

    return project["/refund.py"]


@tool
def patch_refund() -> str:
    """Apply a demonstration precision patch."""

    events.append("tool:patch_refund")

    project["/refund.py"] = (
        "from decimal import Decimal\n"
        "\n"
        "def calculate_refund(amount):\n"
        "    return Decimal(str(amount))\n"
    )

    return "refund.py modified"


@tool
def run_refund_tests() -> str:
    """Perform a simulated refund check."""

    events.append("tool:run_refund_tests")

    if "Decimal" in project["/refund.py"]:
        return "PASS: demonstration check"

    return "FAIL: demonstration check"


# =========================================
# 3. 模拟模型输出
# =========================================

class ScriptedModel(FakeMessagesListChatModel):

    def bind_tools(
        self,
        tools: Any,
        **kwargs: Any,
    ):
        # Fake Model 的响应已预先安排。
        # 不需要真正向 Provider 注册工具。
        return self


def tool_call(name, call_id):

    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": {},
                "id": call_id,
            }
        ],
    )


model = ScriptedModel(
    responses=[
        tool_call(
            "read_refund",
            "call-1",
        ),
        tool_call(
            "patch_refund",
            "call-2",
        ),
        tool_call(
            "run_refund_tests",
            "call-3",
        ),
        AIMessage(
            content=(
                "已完成演示代码修改和模拟检查。"
            )
        ),
    ]
)


# =========================================
# 4. 定义 Middleware
# =========================================

class AuditMiddleware(AgentMiddleware):

    def before_agent(self, state, runtime):

        events.append("before_agent")

        return None

    def before_model(self, state, runtime):

        events.append("before_model")

        return None

    def wrap_model_call(self, request, handler):

        events.append("wrap_model_call:before")

        response = handler(request)

        events.append("wrap_model_call:after")

        return response

    def after_model(self, state, runtime):

        events.append("after_model")

        return None

    def wrap_tool_call(self, request, handler):

        name = request.tool_call["name"]

        events.append(
            f"wrap_tool_call:before:{name}"
        )

        result = handler(request)

        events.append(
            f"wrap_tool_call:after:{name}"
        )

        return result

    def after_agent(self, state, runtime):

        events.append("after_agent")

        return None


# =========================================
# 5. 创建真正的 Agent
# =========================================

agent = create_agent(
    model=model,
    tools=[
        read_refund,
        patch_refund,
        run_refund_tests,
    ],
    middleware=[
        AuditMiddleware(),
    ],
)


# =========================================
# 6. 运行 Agent
# =========================================

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": (
                    "修复退款精度问题，"
                    "并执行检查。"
                ),
            }
        ]
    },
    config={
        "recursion_limit": 50,
    },
)


# =========================================
# 7. 打印真实执行顺序
# =========================================

print("\n=== Middleware Events ===")

for index, event in enumerate(events, 1):

    print(
        f"{index:02d}. {event}"
    )


# =========================================
# 8. 验证调用次数
# =========================================

assert events.count("before_agent") == 1

assert events.count("before_model") == 4

assert events.count("after_model") == 4

assert events.count(
    "wrap_model_call:before"
) == 4

assert events.count(
    "wrap_model_call:after"
) == 4

assert events.count("after_agent") == 1


# =========================================
# 9. 验证工具执行顺序
# =========================================

tool_events = [
    event
    for event in events
    if event.startswith("tool:")
]

assert tool_events == [
    "tool:read_refund",
    "tool:patch_refund",
    "tool:run_refund_tests",
]


# =========================================
# 10. 验证 Wrapper 真正包住了工具
# =========================================

for name in [
    "read_refund",
    "patch_refund",
    "run_refund_tests",
]:

    before = events.index(
        f"wrap_tool_call:before:{name}"
    )

    execute = events.index(
        f"tool:{name}"
    )

    after = events.index(
        f"wrap_tool_call:after:{name}"
    )

    assert before < execute < after


# =========================================
# 11. 验证消息历史
# =========================================

tool_messages = [
    message
    for message in result["messages"]
    if isinstance(message, ToolMessage)
]

assert len(tool_messages) == 3

assert isinstance(
    result["messages"][-1],
    AIMessage,
)

assert not result["messages"][-1].tool_calls

print("\nMiddleware lifecycle verified.")
```

### 16.3 预期观察到的执行顺序

核心输出应当类似：

```text
01. before_agent

02. before_model
03. wrap_model_call:before
04. wrap_model_call:after
05. after_model

06. wrap_tool_call:before:read_refund
07. tool:read_refund
08. wrap_tool_call:after:read_refund

09. before_model
10. wrap_model_call:before
11. wrap_model_call:after
12. after_model

13. wrap_tool_call:before:patch_refund
14. tool:patch_refund
15. wrap_tool_call:after:patch_refund

16. before_model
17. wrap_model_call:before
18. wrap_model_call:after
19. after_model

20. wrap_tool_call:before:run_refund_tests
21. tool:run_refund_tests
22. wrap_tool_call:after:run_refund_tests

23. before_model
24. wrap_model_call:before
25. wrap_model_call:after
26. after_model

27. after_agent

Middleware lifecycle verified.
```

代码使用真实框架接口，但当前环境未安装 LangChain / LangGraph，因此这里提供的是根据源码推导的预期结果，不是已执行日志。

### 16.4 从结果可以发现什么？

最重要的是：

```text
before_agent
```

只执行一次。

但是：

```text
before_model
wrap_model_call
after_model
```

会随着模型调用反复执行。

因为每次工具结果返回后，Agent Loop 都会再次进入模型调用阶段。

### 为什么 wrap_model_call 会有两个日志？

因为我们显式记录了：

```python
response = handler(request)
```

前后的行为。

```text
wrap_model_call:before
    |
    v
真实模型调用
    |
    v
wrap_model_call:after
```

这与：

```text
before_model
after_model
```

不同。

前者是一个函数包裹整个调用。

后者是运行在 Graph 生命周期中不同位置的处理器。

### 为什么工具 Wrapper 只执行三次？

因为本次执行有三个工具调用：

```text
read_refund
patch_refund
run_refund_tests
```

最终模型回复没有 Tool Call。

所以第四次模型调用后，Agent 直接走结束路径。

没有第四次工具执行。

这就说明：

**Middleware 的执行次数由它接入的生命周期位置决定。**

---

## 十七、把这一实验放回真实源码

我们现在已经通过接口层理解不同 Hook 的作用。

接下来快速查看真实框架怎样接入它们。

注意，本节只定位关键断点。

具体编译和包装链的实现，会留到下一章逐行研究。

### 17.1 生命周期 Hook 会被注册成 Graph Node

打开：

`langchain/agents/factory.py`

可以找到：

```python
graph.add_node(
    f"{m.name}.before_model",
    before_node,
    ...
)
```

类似地，还有：

```text
<middleware>.before_agent
<middleware>.after_model
<middleware>.after_agent
```

这些都是可以被 Graph Runtime 调度的节点。

这解释了：

**为什么 before_model 能够在每轮模型执行前运行。**

### 17.2 wrap_model_call 进入 Model Node 内部

同一个文件中：

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

这里最关键的区别是：

```python
wrap_model_call_handler
```

不是一个独立 Graph Node。

它包裹的是：

```python
_execute_model_sync
```

即真正执行模型调用的函数。

### 17.3 wrap_tool_call 交给 ToolNode

Factory 创建 ToolNode 时：

```python
ToolNode(
    tools=available_tools,
    wrap_tool_call=wrap_tool_call_wrapper,
    awrap_tool_call=awrap_tool_call_wrapper,
)
```

因此，工具 Wrapper 不需要自己重新实现一个工具执行器。

它复用真实的：

```python
ToolNode
```

并在工具调用的执行过程中介入。

我们已经能看到：

**Middleware 的不同方法虽然定义在同一个类中，但真正接入 Agent Loop 的位置并不相同。**

这正是下一章源码分析的核心。

---

## 十八、为什么要将这些 Hook 放在同一个 AgentMiddleware 中？

现在我们可以再思考一个设计问题。

为什么 LangChain 不分别定义：

```python
BeforeAgentHandler
BeforeModelHandler
AfterModelHandler
ModelCallWrapper
ToolCallWrapper
```

然后让用户单独传入？

实际上，这也是一种可能的设计。

但对于一个完整功能来说，它可能需要在多个位置介入。

例如：

**模型调用审计功能**可能需要：

```text
before_agent：
    初始化审计上下文

wrap_model_call：
    记录实际请求与返回

after_agent：
    汇总结果
```

如果这些逻辑分别散落在多个无关函数中，就不容易把它们当成一个完整组件管理。

所以 LangChain 提供：

```python
class AgentMiddleware:
    ...
```

让同一个 Middleware 可以实现自己需要的 Hook。

例如：

```python
class AuditMiddleware(AgentMiddleware):

    def before_agent(self, state, runtime):
        ...

    def wrap_model_call(self, request, handler):
        ...

    def after_agent(self, state, runtime):
        ...
```

其他没有实现的 Hook，则无需由开发者全部填写。

这使 Middleware 成为一个可以整体注册、组合和复用的扩展组件。

---

## 十九、为什么不能把所有能力都理解为 Middleware？

现在我们已经掌握了 Middleware 的设计动机。

但有一个边界必须明确。

假设模型调用：

```python
read_file("/refund.py")
```

Middleware 可以负责：

```text
注册 read_file 工具
检查权限
处理工具结果
```

但真正读取文件的能力来自：

```text
Backend
```

例如：

```python
backend.read(...)
```

同样，Middleware 可以提交状态更新。

但这些更新最终由：

```text
Graph State
Channel
Reducer
```

负责管理。

如果 Agent 中断后需要恢复：

```text
Checkpointer
LangGraph Runtime
```

又承担其他职责。

因此：

**Middleware 是 Agent Loop 的扩展机制，但不是执行系统的全部。**

比较准确的架构关系是：

```text
                  Agent Loop
                      |
          +-----------+-----------+
          |                       |
          v                       v
      Model Node               ToolNode
          |                       |
          v                       v
   Model Middleware        Tool Middleware
          |                       |
          +-----------+-----------+
                      |
                      v
              Runtime Infrastructure
                      |
          +-----------+-----------+
          |           |           |
          v           v           v
        State       Backend   Checkpointer
```

这个关系我们会在后面继续深入。

现在只需要记住：

**Middleware 负责在合适的位置增加行为，而具体能力可能由 Tool、Backend 或 Runtime 实现。**

---

## 二十、回到 DeepAgents：它为什么特别依赖 Middleware？

现在再来看：

```python
create_deep_agent(...)
```

我们就能理解它为什么要创建：

```python
FilesystemMiddleware(...)
```

以及：

```python
create_summarization_middleware(...)
```

因为它并不希望重写整个 Agent Loop。

它希望复用 LangChain 的基础循环，同时增加：

```text
文件操作
上下文管理
Skills
Memory
Subagents
工具结果处理
HITL
```

这些能力。

所以其基本结构是：

```text
                 DeepAgents
                     |
                     v
              create_deep_agent()
                     |
                     v
               创建 Middleware
                     |
                     v
               组合 Middleware
                     |
                     v
                create_agent()
                     |
                     v
                 Agent Loop
```

这是一个非常重要的架构认识。

我们可以说：

> **DeepAgents 的很多核心功能，并不是通过重写基础 Agent Loop 实现的，而是通过构建、组合不同的 Middleware，再交给 LangChain 的 Agent Factory 完成装配。**

不过，也不能进一步推导成：

> DeepAgents 的所有能力都是 Middleware。

因为：

```text
Backend
State
Checkpointer
Store
```

仍然各自承担独立的基础设施职责。

---

## 二十一、本章的设计收益与代价

现在可以评价 Middleware 是否值得引入。

### 收益一：降低核心 Loop 的复杂度

无需将所有扩展逻辑写进：

```python
model_node()
```

和：

```python
tool_node()
```

核心职责相对稳定。

### 收益二：扩展能力可以独立组合

不同 Agent 可以使用不同 Middleware。

例如：

```python
middleware=[
    AuditMiddleware(),
    ContextMiddleware(),
]
```

这比为每种组合重新写一套 Agent Loop 更灵活。

### 收益三：处理位置明确

例如：

```text
运行开始：
    before_agent

每轮模型调用前：
    before_model

包裹实际模型调用：
    wrap_model_call

模型响应后：
    after_model

包裹工具执行：
    wrap_tool_call

正常结束阶段：
    after_agent
```

这让功能具有清晰的接入点。

### 代价一：执行顺序更复杂

假设注册：

```python
middleware=[
    A(),
    B(),
    C(),
]
```

它们都实现：

```python
wrap_model_call()
```

那么实际模型调用可能被多层 Wrapper 包住。

这就产生：

```text
谁先执行？
谁后执行？
谁能修改谁的请求？
谁能阻止后续调用？
```

这些问题。

### 代价二：调试难度增加

当一次模型调用的输入发生变化时，需要判断：

```text
是 Model Node 自己构造的输入？
还是某个 Middleware 修改了输入？
还是多个 Middleware 共同修改？
```

因此需要更好的可观测性。

### 代价三：Middleware 可能产生相互影响

例如：

```text
Middleware A：
    修改 messages

Middleware B：
    根据 messages 计算 Token

Middleware C：
    决定模型选择
```

那么它们的顺序就可能影响最终行为。

所以：

**Middleware 让扩展变得容易，但不会自动消除扩展之间的耦合。**

这也是为什么下一章必须深入研究实际的编译和组合顺序。

---

## 二十二、现在重新理解 Agent Harness

到这里，我们已经从最简单的模型调用逐步发展出：

```text
一次 LLM 调用
      |
      v
工具调用
      |
      v
Agent Loop
      |
      v
Graph 调度
      |
      v
Middleware 扩展
```

我们可以得到一个越来越清晰的认识。

Agent 的基础机制是：

```text
Model → Tools → Model
```

但是实际工程需要：

```text
上下文处理
权限控制
日志记录
任务规划
错误恢复
```

为了避免这些能力不断侵入核心循环，框架提供了明确的生命周期 Hook 与调用包装 Hook。

因此可以把整体结构理解为：

```text
                    Agent Loop
                        |
            +-----------+-----------+
            |                       |
            v                       v
         Model                    Tools
            |                       |
            v                       v
      Model Hooks              Tool Hooks
            |                       |
            +-----------+-----------+
                        |
                        v
                 Middleware Stack
                        |
                        v
                 Agent Harness
```

这里的 Harness 还依赖 State、Backend 和 Runtime 等基础设施。

所以，更完整的表述是：

> **Agent Harness 以 Agent Loop 为核心，通过 Middleware 等扩展机制在特定执行位置增加行为，并由状态与运行时基础设施提供支撑。**

---

## 二十三、本章总结

我们从一个很小的工程需求开始：

> 每轮模型调用前，记录消息长度。

最初，我们直接修改：

```python
model_node()
```

这种方案能够工作。

但随着需求增加：

```text
上下文压缩
模型重试
工具权限
调用日志
执行统计
```

Model Node 和 ToolNode 开始承担大量不属于其核心职责的逻辑。

于是我们需要外部扩展点。

最初只是：

```python
before_model()
```

随后发现：

```text
有些逻辑属于 Agent 开始阶段
有些逻辑属于每次模型调用
有些逻辑属于模型调用之后
有些逻辑必须包裹真实执行
有些逻辑属于工具执行
有些逻辑属于 Agent 结束阶段
```

最终自然推导出了：

```text
before_agent
before_model
wrap_model_call
after_model
wrap_tool_call
after_agent
```

这些扩展点组合成：

```python
AgentMiddleware
```

因此，本章最重要的结论是：

> **Middleware 的价值不是给 Agent Loop 增加一组回调函数，而是将容易变化的执行策略从核心循环中分离出来，并明确规定它们可以在哪个生命周期位置影响 Agent 的行为。**

---

# 下一章：Middleware 怎样真正接入 Agent Loop？

现在我们已经知道：

```python
class AgentMiddleware:

    def before_model(...):
        ...

    def wrap_model_call(...):
        ...

    def wrap_tool_call(...):
        ...
```

这些方法各自解决什么问题。

但还有一个更深入的问题：

> **为什么我们只需继承 AgentMiddleware、重写一个方法，LangChain 就知道应该在哪里执行它？**

下一章将进入：

```text
langchain/agents/factory.py
```

研究真实装配过程：

```text
create_agent(middleware=[...])
          |
          v
识别 Middleware 实现了哪些 Hook
          |
          v
区分生命周期 Hook 与 Wrapper
          |
          v
为生命周期 Hook 创建 Graph Node
          |
          v
将 Wrapper 组成调用链
          |
          v
连接 Graph 的入口、循环和出口
          |
          v
compile()
          |
          v
实际执行
```

重点回答：

1. Factory 怎样知道某个 Middleware 重写了 `before_model`？
2. 为什么 `before_model` 是 Graph Node？
3. 为什么 `wrap_model_call` 不是独立 Graph Node？
4. 多个 Middleware 如何确定顺序？
5. 为什么 `before_agent` 不会在每轮 Tool 调用结束后重复执行？
6. Middleware 如何声明工具和 State？
7. 如果 Middleware 想改变正常路由，会发生什么？

这将是我们真正理解：

**Agent Loop 如何成为可扩展 Agent Harness**

的关键一章。

---

## 本章源码导航

### LangChain Middleware 协议

文件：

`libs/langchain_v1/langchain/agents/middleware/types.py`

建议阅读：

```text
AgentMiddleware
    |
    +-- before_agent
    |
    +-- before_model
    |
    +-- wrap_model_call
    |
    +-- after_model
    |
    +-- wrap_tool_call
    |
    +-- after_agent
```

源码：

https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/types.py

### LangChain Agent Factory

文件：

`libs/langchain_v1/langchain/agents/factory.py`

本章只需定位：

```text
middleware_w_before_agent
middleware_w_before_model
middleware_w_wrap_model_call
middleware_w_after_model
middleware_w_wrap_tool_call
middleware_w_after_agent
```

以及：

```python
graph.add_node(...)
```

```python
wrap_model_call_handler(...)
```

```python
ToolNode(
    wrap_tool_call=...
)
```

源码：

https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py

### DeepAgents 真实使用

固定版本：

`0.7.32 / e85c6b9`

文件：

`libs/deepagents/deepagents/graph.py`

重点：

```python
create_deep_agent(...)
```

如何构建 Middleware 列表并传给：

```python
create_agent(
    middleware=deepagent_middleware,
    ...
)
```

源码：

https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py

注意：DeepAgents 使用固定 Commit；LangChain 源码链接指向当前主分支。正式逐行调试时，应核对实际安装依赖的版本。
