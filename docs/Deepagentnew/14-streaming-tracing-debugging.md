---
title: 第十四章：Agent 执行过程怎样被观察？——Streaming、Tracing 与 Debugging
description: 从优惠券部分退款 Bug 的一次看不见中间过程的修复任务出发，沿 DeepAgents 0.7.32 的装配入口、LangChain Agent Graph 和 LangGraph Stream API，逐步理解节点更新、模型 Token、工具执行、自定义进度、子 Agent、Trace 与 Debug 的区别和实际使用边界。
tags: [DeepAgents, LangChain, LangGraph, Streaming, Tracing, Debugging, ToolNode, Subagents, 源码精读]
sidebar_position: 19
---

# 第十四章：Agent 执行过程怎样被观察？——Streaming、Tracing 与 Debugging

> **本章核心问题：** 第十三章解决了 Agent 的状态怎样被保存与恢复；但是当它反复读取代码、修改 `refund.py`、运行测试甚至委派子 Agent 时，用户如何知道它**正在做什么**？开发者又怎样判断它**为什么**做错了？

## 一、接着第十三章：状态保存下来了，但运行过程仍然不可见

前面几章中，我们一直使用同一个 Coding Agent 任务：

```text
用户：修复优惠券部分退款的金额精度 Bug。
      请检查 shop/refund.py，补充回归测试，运行 pytest。
```

经过前面十三章，Agent 已经能够：

- 在 Model → Tool → Model 循环中持续推进任务。
- 通过 `read_file`、`edit_file` 等工具与项目交互。
- 利用 Middleware 处理上下文、规划、权限和子任务。
- 通过 State 与 Checkpointer 保存执行状态，并在适当条件下恢复。

于是，一个最自然的调用方式是：

```python
# 框架级接口示意：需要已初始化的 model 和适配的 backend
from deepagents import create_deep_agent

agent = create_deep_agent(model=model, backend=backend)

result = agent.invoke({
    "messages": [
        {"role": "user", "content": "修复 shop/refund.py 中优惠券部分退款精度问题，并运行测试。"}
    ]
})

print(result["messages"][-1])
```

它有一个优点：调用方只关心最终状态。

但它也留下一个很实际的问题。

如果此次任务历经十几轮循环：

```text
读取 refund.py
  ↓
读取 coupon_service.py
  ↓
生成补丁
  ↓
运行 pytest
  ↓
失败：Decimal 比较结果不正确
  ↓
再次读取测试文件
  ↓
修改尾差归集逻辑
  ↓
再次运行 pytest
  ↓
最终回复
```

那么在普通的 `invoke()` 调用尚未返回时，外部界面看不到这些过程。即使最后收到一个完整的 `State`，也仍然不容易回答：

- `pytest` 是什么时候开始执行的？
- 第一次测试失败之后，模型是否真的看到了错误输出？
- 哪个节点写入了 `ToolMessage`？
- “测试通过”是模型的口头声明，还是有真实工具结果支撑？
- 是模型请求慢，还是工具执行慢？

于是，我们真正缺少的并不是又一个状态字段。

**我们缺少一条能在执行发生时，把可观察事件送出来的通道。**

这时才有必要引入本章第一个概念：**Streaming（流式输出）**。

### 1.1 固定源码基线

继续沿用本系列的阅读版本：

```text
DeepAgents Python：0.7.32
DeepAgents Commit：e85c6b9
LangChain：以 1.4.7 为系列研究基线
LangGraph：采用与上述依赖兼容的版本；不虚构唯一固定的 Commit
```

主要源码入口：

1. [DeepAgents `graph.py`（固定 Commit）](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)：`create_deep_agent()` 的 `debug` 参数和 `create_agent()` 交接点。
2. [LangChain `agents/factory.py`](https://github.com/langchain-ai/langchain/blob/main/libs/langchain/langchain/agents/factory.py)：通用 Agent Graph 的 Model Node、ToolNode 与 Middleware 执行边界。此为实现机制参考；阅读时应另行切换至安装版本源码。
3. [LangGraph `pregel/main.py`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/main.py)：编译后 Graph 的 `stream()` / `astream()` 执行入口。此为实现机制参考，并非固定版本快照。
4. [LangGraph `types.py`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py)：`StreamMode` 与流式事件数据类型。
5. [LangChain Streaming 文档](https://docs.langchain.com/oss/python/langchain/streaming)：Agent 级别流式输出用法。
6. [LangGraph Streaming 文档](https://docs.langchain.com/oss/python/langgraph/streaming)：Graph 级别的流模式与子图事件。

这里必须明确一条版本纪律：**DeepAgents 的 Commit 可以固定，但这不意味着依赖包的全部内部代码也被同一个 Commit 固定。** 尤其是 LangGraph 在较新版本提供了 `version="v2"` 的统一 `StreamPart` 格式。本章会标明这一新格式的版本要求，避免把较新文档的写法误认为所有兼容版本的默认输出。

本章代码分别标记为**真实源码节选**、**教学化简**、**框架级示例**或**纯 Python 实验**。真实模型调用和在线 Trace 不会在未执行时被说成已经验证通过。

---

## 二、第一步：不想等到结束，最简单的方案是什么？

先不研究任何框架。

假设我们亲自编写一个最小退款修复流程：

```python
# 教学化简：纯 Python 顺序任务

def fix_refund():
    source = read_file("shop/refund.py")
    patch = plan_patch(source)
    edit_file("shop/refund.py", patch)
    test_result = run_pytest()
    return test_result
```

如果希望前端知道进度，我们很可能想到 `print()`：

```python
# 教学化简

def fix_refund():
    print("开始读取 refund.py")
    source = read_file("shop/refund.py")

    print("开始生成补丁")
    patch = plan_patch(source)

    print("开始修改文件")
    edit_file("shop/refund.py", patch)

    print("开始执行 pytest")
    return run_pytest()
```

这个版本确实解决了部分问题。对于在终端里运行的单个脚本，直接打印日志非常实用。

但现在换一个场景：Agent 运行在服务端，调用方是浏览器页面。浏览器需要展示：

```text
正在读取：shop/refund.py
正在测试：shop/tests/test_refund.py
第一次测试失败：断言金额差异为 0.01
正在尝试第二次修复……
```

`print()` 把文本写到服务端标准输出，并不会自动变成调用方可以消费的结构化数据。

于是，我们可以把“打印”换成“产出一个事件”：

```python
# 概念伪代码：仅用于建立生成器直觉

def fix_refund_stream():
    yield {"stage": "reading", "path": "shop/refund.py"}
    source = read_file("shop/refund.py")

    yield {"stage": "editing", "path": "shop/refund.py"}
    patch = plan_patch(source)
    edit_file("shop/refund.py", patch)

    yield {"stage": "testing", "command": "pytest"}
    result = run_pytest()

    yield {"stage": "finished", "result": result}
```

`yield` 的意义是：**一次函数调用可以逐步交出多个值，而不是只在结束时返回一个值。**

调用者就能消费：

```python
for event in fix_refund_stream():
    render_progress(event)
```

但这个实现仍然依赖我们事先手写固定步骤。

真实 Agent Loop 的下一步是由模型决定的：这次可能 `read_file`，下次可能 `grep`，再次可能调用 `task`，还有可能直接结束。我们不知道应该在哪些业务代码行插入 `yield`。

因此，需要由 **Graph Runtime 在节点运行期间自动发出事件**。

这就是 `agent.stream()` 要解决的问题。

---

## 三、第二步：先只观察“哪个节点完成了什么”

先考虑退款任务里最基本的三个动作：

```text
Model：决定读取 refund.py
  ↓
Tools：执行 read_file
  ↓
Model：根据代码决定下一步
```

如果只观察最终 `State`，这三个动作可能挤在同一个最终结果里，不容易看出先后。

我们现在的最小需求只有一个：

> **每个节点执行完成后，告诉我它提交了什么更新。**

这正是 `stream_mode="updates"`。

### 3.1 从 `invoke()` 换成 `stream()`

```python
# 框架级示例：model、backend 需先自行配置
from deepagents import create_deep_agent

agent = create_deep_agent(
    model=model,
    backend=backend,
)

inputs = {
    "messages": [
        {"role": "user", "content": "检查 shop/refund.py 的部分退款精度问题。"}
    ]
}

for update in agent.stream(inputs, stream_mode="updates"):
    print(update)
```

这个示例调用的是 `create_deep_agent()` **返回的编译后 Agent Graph** 的 `stream()`，不是另一个 DeepAgents 专门实现的循环。

为便于理解，假设模型恰好选择 `read_file`，一次输出可以抽象为：

```text
# 教学化输出示意：不是当前环境真实运行日志
{
  "model": {
    "messages": [AIMessage(tool_calls=[...])]
  }
}

{
  "tools": {
    "messages": [ToolMessage(content="...")]
  }
}

{
  "model": {
    "messages": [AIMessage(content="已定位精度问题...")]
  }
}
```

重点不在输出里具体有哪些中文内容，而在事件的**结构**：

```text
{"model": ...}
{"tools": ...}
{"model": ...}
```

这是节点提交更新的时间序列。

现在我们能知道：

1. `model` 节点产生了一次工具调用意图。
2. `tools` 节点实际返回了工具结果。
3. 模型在得到工具反馈后又执行了一轮。

**这并不意味着 `updates` 会报告工具内部的每一行进度。** 如果 `pytest` 运行了二十秒，普通的节点更新通常要等相应执行阶段产生结果后才出现。我们先只解决“节点完成了什么”这一层。

### 3.2 源码层面：为什么 DeepAgents 可以直接 `stream()`？

打开固定版本：

[DeepAgents `graph.py`：`create_deep_agent()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)

函数入口接受 `debug`、`checkpointer`、`name` 等参数；末尾把 Harness 配置交给 LangChain 的 `create_agent()`。

下面仅保留本章相关的真实调用参数，省略其他参数和中间装配步骤：

```python
# 真实源码关键调用的节选：省略无关参数与装配逻辑
return create_agent(
    model,
    # ...
    middleware=deepagent_middleware,
    # ...
    checkpointer=checkpointer,
    store=store,
    debug=debug,
    name=name,
    cache=cache,
    # ...
)
```

这一段本身**没有执行任何 Streaming**。它说明的是：DeepAgents 把 Agent Graph 的构建委托给 LangChain。

上一章已经确认：LangChain 用 LangGraph 构建并编译执行图。因而调用方拿到的是可以 `invoke()`、`stream()`、`ainvoke()`、`astream()` 的 Graph 对象。

调用链因此变成：

```text
create_deep_agent()
    |
    | 装配 Model、Middleware、Backend、State 等
    v
LangChain create_agent()
    |
    | 构建 / 编译 Agent Graph
    v
Compiled Graph
    |          |
    |          +---- invoke()：等待返回结果
    |
    +--------------- stream()：逐步给出事件
```

真正产生并传播 Stream 事件的执行能力，属于**底层 Graph Runtime**，不是 `graph.py` 里又定义了一套 `while + yield` 循环。

### 3.3 这里的“更新”不是“完整状态”

这一点会直接影响调试方式。

假设退款任务的完整状态是：

```python
# 教学化简：不冒充实际 State 完整字段
state = {
    "messages": ["用户任务", "模型请求", "文件内容", "模型结论"],
    "files": {"/shop/refund.py": "..."},
    "todos": ["检查代码", "添加测试", "运行 pytest"],
}
```

某个节点只新增一条工具结果：

```python
update = {
    "messages": ["pytest 失败：预期 19.99，得到 20.00"]
}
```

在支持对应 Reducer 的 State 中，这个更新可能合并进已有消息历史。

所以 `updates` 更接近“**谁写了什么**”，而不是“当前状态包含所有什么”。

当你只想定位哪一次写入造成了问题时，`updates` 往往比连续打印完整 State 更清晰。

---

## 四、第三步：如果要看每一轮后的完整 State 呢？

现在我们有一个新的问题。

第一次 `pytest` 失败后，模型又做了一次文件修改。开发者想知道：**此时 Agent 的文件状态、Todo 和消息历史分别是什么？**

仅看一个节点返回的更新，可能还需要自己把所有增量重新合并一次。

于是我们需要另一种视角：

> **在每个执行阶段后，给我当前完整的 State 视图。**

LangGraph 提供 `stream_mode="values"`：

```python
# 框架级示例
for state in agent.stream(inputs, stream_mode="values"):
    print("消息数量：", len(state.get("messages", [])))
    print("当前 Todo：", state.get("todos"))
```

它与上一节的对比如下。

| 想知道什么 | 适合的模式 |
|---|---|
| 哪个节点刚写入了新的工具结果？ | `updates` |
| 这一时刻累计的消息和任务状态是什么？ | `values` |

这两种输出不是重复功能。

**`updates` 面向“变化”，`values` 面向“当前值”。**

第十三章讲过 Reducer、DeltaChannel 和 Checkpoint。现在可以把两章联系起来：

```text
Node 返回更新
    |
    v
State Channel 应用更新规则
    |
    +------------------> updates：关注节点本次写入
    |
    +------------------> values：关注此时可见的整体 State
    |
    +------------------> Checkpointer：按执行机制保存可恢复状态
```

注意：这是**概念数据流图**，不是说 Streaming、Reducer 和 Checkpointer 彼此按图中顺序直接调用对方的同名函数。它强调三者观察或处理的是同一次 Graph 执行的不同侧面。

### 4.1 为什么不建议一直把 `values` 打印出来？

我们的退款 Agent 可能保存：

- 较长的源代码内容；
- 多轮模型消息；
- 完整 `pytest` 失败日志；
- 文件状态和 Todo；
- Middleware 自己的状态字段。

因此，持续输出完整 `values` 既可能增加日志体积，也可能暴露代码或敏感数据。

调试“何时多了一条错误消息”时，先用 `updates`。只有需要还原某个阶段的 State 视图时，再考虑 `values` 或 `get_state()`。

**Streaming 不会自动帮你过滤敏感信息，也不等于日志留存策略。**

---

## 五、第四步：节点完成才有更新，但模型生成文字时想实时显示怎么办？

到这里，我们可以看到每一轮 Model 和 Tool 的更新。

可是用户仍然会有一个体验问题：

模型生成最终说明可能需要一段时间：

```text
“我定位到 refund.py 中的优惠券摊销逻辑……”
```

如果必须等整个 `model` 节点完成才看到回复，用户仍然会觉得界面卡住了。

于是，现在才需要区分两种完全不同的“流”：

```text
节点执行流：   Model 完成 → Tools 完成 → Model 完成

模型 Token 流：我 → 定位 → 到 → refund.py → 中的 → 问题……
```

它们并不是同一种粒度。

### 5.1 使用 `stream_mode="messages"`

```python
# 框架级示例：模型提供者需支持对应的流式行为
for message_chunk, metadata in agent.stream(
    inputs,
    stream_mode="messages",
):
    if message_chunk.content:
        print(message_chunk.content, end="", flush=True)
```

这里的 `message_chunk` 通常是模型产生的增量消息，例如 `AIMessageChunk`；`metadata` 可以包含当前来源节点等信息。

需要特别强调：

1. `messages` 模式主要观察**模型输出的增量**，不等于 ToolNode 已经提交的 `ToolMessage` 更新。
2. `message_chunk.content` 不一定永远是一个普通字符串。不同模型及多模态格式可能使用更复杂的内容块。
3. 流式模型可能先生成工具调用参数的片段。这并不等于工具已经执行；工具执行仍需等待工具调用被正确组装并交由 ToolNode 处理。

这与第二章讨论的消息协议呼应：**模型说“我要调用工具”，与工具已经执行并返回结果，是两件事。**

### 5.2 为什么返回一个 Token 的同时，还要附带 Metadata？

我们的 Agent 并不只有一处模型调用。

例如：

```text
主 Agent：读取 refund.py 后决定调用测试分析子 Agent
    |
    v
子 Agent：分析测试失败根因
    |
    v
主 Agent：修改文件并总结
```

如果 Streaming 只提供文字：

```text
“正在分析……”
“发现尾差……”
“建议修改……”
```

开发者很难判断它们究竟来自哪一次模型调用。

因此事件还需要描述自己的来源。

在 Graph 的模型事件 Metadata 中，可以使用 `langgraph_node` 等字段辅助定位：

```python
# 框架级示例：仅展示过滤方法
for chunk, metadata in agent.stream(inputs, stream_mode="messages"):
    node_name = metadata.get("langgraph_node", "unknown")

    if node_name == "model":
        print(chunk.content, end="", flush=True)
```

这里的 `"model"` 只是常见主模型节点名。**如果实际图中存在不同的节点名或嵌套图，应以观察到的 Metadata 为准，而不是假设所有模型输出都来自同一个节点。**

至此，我们把第一个“Streaming”概念拆成了两层：

- `updates`：执行结构的进度。
- `messages`：模型内容的生成进度。

下一个问题自然出现：工具自己执行很久怎么办？

---

## 六、第五步：`pytest` 运行二十秒，怎样报告工具内部的进度？

假设测试工具开始执行：

```text
execute("pytest shop/tests/test_refund.py -q")
```

它还没有返回，因此 `tools` 节点的最终更新也可能还没有出现。

用户想看到：

```text
开始运行退款测试
已完成 12 / 48 个用例
已完成 36 / 48 个用例
测试结束：1 failed, 47 passed
```

但这些阶段并不是 LangGraph 事先知道的节点。

最直接的方案是让工具或节点自己汇报：“我现在进展到哪里了”。

这一层叫 **Custom Streaming**。

### 6.1 从工具内部主动发送事件

LangGraph 提供 Stream Writer 机制。下面先用独立节点展示，因为我们暂时不需要引入 DeepAgents Backend 的命令流实现：

```python
# 框架级示例：LangGraph custom streaming
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.config import get_stream_writer


class DemoState(TypedDict):
    passed: int


def run_tests(state: DemoState):
    writer = get_stream_writer()

    writer({"stage": "testing", "detail": "开始运行退款测试"})

    # 这是示意任务，不是真实 pytest。
    # 真正的命令进度必须由后端实际获取并主动写入。
    writer({"stage": "testing", "detail": "模拟完成 2 个测试"})

    return {"passed": 2}


builder = StateGraph(DemoState)
builder.add_node("run_tests", run_tests)
builder.add_edge(START, "run_tests")
builder.add_edge("run_tests", END)

graph = builder.compile()

for event in graph.stream({"passed": 0}, stream_mode="custom"):
    print(event)
```

这里不是 Graph 自动解析了 `pytest` 的百分比。

真正发生的是：

```text
run_tests() 自己调用 writer(...)
    |
    v
LangGraph Runtime 接收自定义事件
    |
    v
调用方通过 stream_mode="custom" 收到事件
```

如果我们没有在工具或节点里提供实际进度，**仅设置 `stream_mode="custom"` 不会凭空生成进度数据**。

### 6.2 为什么 Custom Event 不应该被当作业务最终结果？

假设工具发出：

```python
writer({"stage": "testing", "detail": "正在运行测试"})
```

随后进程崩溃。

调用方确实观察到它曾经开始测试，但这不能推出：

```text
测试通过
```

同样，自定义进度事件默认也不应被误认为已经提交给 Reducer 的 State 更新。

概念上应区分：

```text
writer(...)           → 发送观察信息
return {"passed": 2} → 提交节点的 State 更新
```

这条边界在我们的退款场景尤其重要：**“正在测试”是一条进度事件；“测试通过”应由实际测试工具结果证明。**

---

## 七、第六步：如果想同时看到节点进度和模型文字怎么办？

上一节我们逐层得到了三种需要：

```text
知道 Graph 哪个步骤结束了     → updates
知道模型正在生成什么内容       → messages
知道长时间工具的内部进度       → custom
```

如果前端需要同时显示它们，难道必须启动三个 Agent？

不必。

Graph 允许在一次执行中订阅多个 Stream Mode。

### 7.1 多模式输出

```python
# 框架级示例：使用默认 v1 形式的分派思路
for mode, payload in agent.stream(
    inputs,
    stream_mode=["updates", "messages", "custom"],
):
    if mode == "updates":
        print("[NODE UPDATE]", payload)

    elif mode == "messages":
        message_chunk, metadata = payload
        if isinstance(message_chunk.content, str) and message_chunk.content:
            print(message_chunk.content, end="", flush=True)

    elif mode == "custom":
        print("[PROGRESS]", payload)
```

这里要注意一个**具体版本差异**。

### 7.2 不是所有 LangGraph 版本都返回相同的事件外壳

在 LangGraph 的传统 `stream()` 格式（v1/default）中：

- 单一 `stream_mode` 常直接返回该模式的数据。
- 多个模式时通常返回 `(mode, data)`。
- `subgraphs=True` 时可能再包含命名空间，外层元组会发生变化。

因此，上面的示例只用于**未同时开启 `subgraphs=True` 的 v1 多模式分派**。

而较新的 LangGraph 文档明确说明：**从 LangGraph 1.1 开始，使用 `version="v2"` 可获得统一的 `StreamPart` 字典结构**：

```python
# 较新 LangGraph 的框架级示例：要求版本支持 StreamPart v2
for event in agent.stream(
    inputs,
    stream_mode=["updates", "messages", "custom"],
    version="v2",
):
    kind = event["type"]
    namespace = event["ns"]
    data = event["data"]

    if kind == "updates":
        print("[UPDATE]", namespace, data)
    elif kind == "messages":
        message_chunk, metadata = data
        print("[MESSAGE]", metadata.get("langgraph_node"))
    elif kind == "custom":
        print("[CUSTOM]", data)
```

这一新 API 来自 [LangGraph Streaming 文档](https://docs.langchain.com/oss/python/langgraph/streaming)，不能据此断言所有与 DeepAgents `0.7.32` 兼容的 LangGraph 安装版本都已经支持。

因此，阅读源码或编写生产代码时，应先确认安装版本：

```bash
python -m pip show deepagents langchain langgraph
```

并查看实际 `stream()` 签名及相应版本文档。

**对照事件格式时，不能把 v1 的元组示例与 v2 的字典示例拼接成一套代码。**

---

## 八、第七步：Streaming 能告诉我过程，但为什么一次调用耗时很久？

到这里，前端已经可以显示：

```text
07:20:10 model 提出 read_file 调用
07:20:10 tools 读取 refund.py 完成
07:20:11 model 提出 execute 调用
07:20:35 tools 返回 pytest 结果
07:20:36 model 输出修复结论
```

这比最终只返回一句“已修复”好得多。

但开发者会继续问：

> **到底慢在哪里？是谁调用了谁？同一次任务里各个子步骤的输入输出能否关联起来？**

仅靠一串分散的 Stream Event，并不容易还原调用关系。

这时才有必要引入本章第二个概念：**Tracing（调用追踪）**。

### 8.1 最简单的调用日志为什么不够？

我们可能给所有函数加上时间戳：

```python
# 教学化简：普通日志
print("model start")
print("tool start")
print("tool end")
print("model end")
```

如果只有一条顺序执行链，日志还能看懂。

但当主 Agent 调用子 Agent 时：

```text
主 Agent model
    |
    v
主 Agent tools: task
    |
    +--> 子 Agent model
    |       |
    |       v
    |    子 Agent tools: read_file
    |       |
    |       v
    |    子 Agent model
    |
    +--> 子 Agent 返回
    |
    v
主 Agent model
```

日志可能交织：

```text
model start
model start
tool start
tool end
model end
model end
```

看到这些行，我们并不知道哪一个 `model end` 对应哪一次 `model start`，更不知道主子 Agent 的父子关系。

于是，调用追踪至少还需要：

1. 当前步骤的标识（Run ID）。
2. 触发它的上一级步骤（Parent Run ID 或等价父子关系）。
3. 开始与结束时间。
4. 步骤类型，例如 Model、Tool、Graph Node。
5. 用于排错的输入、输出与错误信息（在安全允许范围内）。

我们可以将一次执行表示为：

```text
Trace：refund-fix-run
  |
  +-- Agent Graph
      |
      +-- model (1)
      |    +-- ChatModel Call
      |
      +-- tools (1)
      |    +-- read_file
      |
      +-- model (2)
      |    +-- ChatModel Call
      |
      +-- tools (2)
      |    +-- task
      |         +-- SubAgent Graph
      |              +-- model
      |              +-- tools
      |
      +-- model (3)
           +-- ChatModel Call
```

这样才能看出调用层级。

### 8.2 Streaming 与 Tracing 的目的不同

| 机制 | 首要问题 | 典型用途 |
|---|---|---|
| Streaming | **正在发生什么？** | 前端进度条、增量回答、实时提示 |
| Tracing | **哪一步导致了结果？** | 调用树、错误归因、延迟与成本分析 |
| Checkpoint | **执行后怎样恢复状态？** | 线程继续执行、中断恢复 |

注意，Tracing 与 Checkpoint 不能互相代替。

一次 Trace 可以告诉你某个工具曾执行失败，但不会因此自动使外部副作用可回滚；Checkpoint 可以保留可恢复状态，却不一定保存一棵便于观察调用耗时的诊断树。

---

## 九、第八步：真实框架如何接入 Tracing？

LangChain 与 LangGraph 使用 Runnable / Callback 体系串起调用过程。对于使用 LangChain 生态组件构建的 Agent，可使用 LangSmith 追踪执行。

### 9.1 最少的追踪配置

开发环境中，可以通过环境变量开启：

```bash
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY="你的 LangSmith API Key"
export LANGSMITH_PROJECT="refund-agent-study"
```

然后照常执行 Agent：

```python
# 框架级示例：需要模型凭证、LangSmith 凭证和网络环境
agent = create_deep_agent(model=model, backend=backend)

result = agent.invoke({
    "messages": [
        {"role": "user", "content": "检查 shop/refund.py 并修复优惠券部分退款问题。"}
    ]
})
```

Tracing 开启后，LangSmith 可以根据实际执行时生成的记录展示调用关系。具体可见的字段取决于组件、配置、模型服务以及 Trace 的采集策略。

这里的职责分界是：

```text
DeepAgents create_deep_agent()
    |
    v
LangChain create_agent()
    |
    v
LangGraph 执行 Graph
    |
    +--> Runnable / Callback 事件
                |
                v
           Tracing 系统
```

**DeepAgents 本身不需要再手写一个外部监控平台才能使用这些已有的追踪接口。**

但应注意：使用外部 Trace 服务往往意味着将部分输入、输出或元数据发送给对应服务。源码、测试日志和工具参数中可能包含机密数据，因此必须根据项目的数据安全要求配置脱敏、采样、访问控制或禁用追踪。不要在教学示例里把 API Key 写入仓库。

### 9.2 如果不接外部平台，能不能先观察更细粒度的事件？

可以考虑 Runnable 事件接口。

例如 `astream_events()` 可以在异步执行期间观察模型、工具等内部调用的生命周期事件。下面只是典型过滤方式：

```python
# 框架级示例：事件名称取决于实际 Runnable 组件
import asyncio


async def main():
    async for event in agent.astream_events(
        inputs,
        version="v2",
    ):
        kind = event["event"]
        name = event.get("name")

        if kind in {
            "on_chat_model_start",
            "on_chat_model_end",
            "on_tool_start",
            "on_tool_end",
        }:
            print(kind, name)


asyncio.run(main())
```

这里要区分两个很容易混淆的 `v2`：

- `agent.astream_events(..., version="v2")`：**Runnable 事件接口**的事件 Schema 版本。
- `agent.stream(..., version="v2")`：较新 **LangGraph StreamPart 接口**的统一输出格式。

它们不是同一个参数语义，也不能根据前者存在就推断后者在任意 LangGraph 版本中可用。

在接口选型时可以先问：

```text
我只是想在界面显示模型输出？
  → 优先 messages / updates

我想知道工具开始、结束，以及嵌套调用？
  → 考虑 Runnable Event / Tracing

我想分析完整调用树、延迟与错误？
  → 需要 Trace 系统和相应观测策略
```

不要因为三者都叫“事件”就把它们当成同一层协议。

---

## 十、第九步：`debug=True` 究竟打开了什么？

经过前面的步骤，现在我们已经有了运行过程和调用层级的观察手段。

但修复退款 Bug 时仍然可能出现这种情况：

```text
Agent 卡在某个节点附近；
不知道哪个节点提交了异常更新；
想临时打印更多 Runtime 内部信息。
```

这时开发者常会尝试：

```python
agent = create_deep_agent(
    model=model,
    backend=backend,
    debug=True,
)
```

它确实是有效的公开配置参数，但必须搞清楚它被谁消费。

### 10.1 源码证据：DeepAgents 只把 `debug` 继续交给 Agent Factory

在固定版本 `deepagents/graph.py` 的函数声明中有：

```python
# 真实源码节选

debug: bool = False,
```

在调用 LangChain `create_agent()` 的尾部，可以看到：

```python
# 真实调用参数节选

debug=debug,
```

这两行说明：

**DeepAgents 接收并透传 Debug 开关，底层 Graph 的构建与执行代码负责具体的调试输出行为。**

因此不能根据 `create_deep_agent(debug=True)` 就得出：

- 所有模型 Token 都会自动出现在你的业务前端。
- 所有工具错误都能被自动解释。
- Agent 的内部推理一定被完整记录。
- 程序自动接入了某个外部 Tracing 服务。

这些都不是该参数能够单独保证的事情。

### 10.2 `debug=True` 与 `stream_mode="debug"` 是不是一回事？

也不是。

`debug=True` 是**构建 / 执行配置的调试开关**。

`stream_mode="debug"` 是**指定流中输出哪一类调试事件**。

较新的 LangGraph 文档定义了 `debug` Stream Mode，用来报告任务、Checkpoint 及相关调试信息：

```python
# 框架级示例：打印调试事件，注意日志可能非常大
for event in agent.stream(inputs, stream_mode="debug"):
    print(event)
```

与之相关但更有针对性的模式还有：

```text
checkpoints：Checkpoint 事件（需要 Checkpointer）
tasks：任务启动、完成、错误等事件（需要 Checkpointer）
debug：更完整的调试事件
```

不同版本的具体载荷和要求，以已安装 LangGraph 的类型定义及文档为准。

我们在实际排错时应采取一个简单原则：

> **先用最小范围的事件回答当前问题，不要一开始就把所有模型消息、完整 State 和 Debug 事件全部记录下来。**

---

## 十一、第十步：Subagent 的事件为什么容易看混？

第十二章里我们已经讨论，普通 `task` 工具会调用子 Agent，并把子任务结果返回主 Agent。

现在设想：

```text
主 Agent：调查 coupon_service.py 中的优惠券分摊规则
    |
    v
主 Agent 调用 task
    |
    v
子 Agent：read_file
    |
    v
子 Agent：grep
    |
    v
子 Agent：总结规则
    |
    v
主 Agent：继续修改 refund.py
```

如果前端仅展示流式文本，可能把子 Agent 的分析和主 Agent 的回答混在一起。

这时，除了事件类型以外，还需要标识**事件所属的 Graph 层级**。

### 11.1 先看一个普通子图例子

LangGraph 的 `subgraphs=True` 用于包含子图事件。

下面展示较新版本统一格式下的思想：

```python
# 框架级示例：LangGraph 1.1+ 的 v2 格式
for event in graph.stream(
    inputs,
    stream_mode="updates",
    subgraphs=True,
    version="v2",
):
    print("事件类型：", event["type"])
    print("来源命名空间：", event["ns"])
    print("数据：", event["data"])
```

主图事件的 `ns` 可能为空元组，子图事件则带有执行路径。

但**这里不能机械地推断：DeepAgents `task` 工具的所有子 Agent 必然都以某个固定 `ns` 结构对外展示。**

因为子 Agent 可以通过工具内部调用独立 Runnable，流式事件如何转发与当前版本、配置、调用方式有关。必须观察实际事件来确认，而不是根据普通 Graph 子图例子的路径去硬编码。

### 11.2 为什么 `task` 返回结果不等于子 Agent 全部过程？

同步委派的最外层结构是：

```text
主 Agent Model
    |
    v
主 Agent ToolNode
    |
    v
执行 task 工具
    |
    v
内部运行子 Agent
    |
    v
生成主 Agent 可消费的 ToolMessage
```

因此仅订阅主 Agent 的普通 `updates`，你可能首先看到的是主 Agent 的 `tools` 更新，也就是委派完成的结果。

想观察内部每次模型调用、文件读取或测试操作，必须使用实际能传递那些内部事件的流或 Trace 方案。

这一点尤其需要和第十二章区分：

- 普通同步 `task`：主 Agent 等待子 Agent 产生结果。
- 远程异步委派：具有另外的任务生命周期和状态查询接口。
- `subgraphs=True`：是 LangGraph 对嵌套图事件的观察选项，本身不是启动后台子 Agent 的指令。

**观察方式不会改变委派的执行语义。**

---

## 十二、第十一步：一次失败的 `pytest`，应该怎样逐层排查？

现在把学到的机制重新放回退款案例。

模型最终回答：

```text
“我已经修复了优惠券部分退款精度问题，所有测试通过。”
```

但实际代码审核发现：最后一次退款还会出现 `0.01` 的差异。

此时如果只是重新调用一次 Agent，希望它“更仔细”，很可能不知道之前究竟错在哪里。

我们需要一条有证据的排查链。

### 12.1 第一个问题：是否真的调用了测试工具？

观察 `updates`：

```text
model → tools → model
```

单纯看到 `tools` 并不能证明其中一定是 `pytest`，因此还要检查模型 Tool Call 的名称与参数，以及相应的工具结果。

需要建立完整关联：

```text
AIMessage.tool_calls
    tool name: execute
    arguments: pytest ...
    tool_call_id: X
             |
             v
ToolMessage
    tool_call_id: X
    content: 测试输出或错误
```

这个 `tool_call_id` 的关联来自第二章的消息协议。

**最终的模型声明，不应代替 `ToolMessage` 里的真实工具结果。**

### 12.2 第二个问题：工具运行了吗，还是直接报错？

检查 `ToolMessage` 的内容，以及可能的 Tool Runnable 事件：

```text
ToolNode 开始执行
    |
    +-- 命令支持吗？
    |
    +-- Backend 能执行 shell 吗？
    |
    +-- 发生权限拒绝或审批暂停吗？
    |
    +-- pytest 进程有没有被真正启动？
    |
    +-- 返回码与输出是什么？
```

第九章已经明确：并不是所有 Backend 都支持 `execute`。默认 StateBackend 的文件存储能力不能被误认为它也具有真实 Shell 执行能力。

因此，看到 `execute` 工具名，不等于断言测试已经在真实项目目录运行。

### 12.3 第三个问题：模型是否看到了失败反馈？

如果 `pytest` 返回：

```text
FAILED test_partial_coupon_refund
Expected: Decimal('19.99')
Actual:   Decimal('20.00')
```

接下来应该检查：

1. `ToolMessage` 是否加入了后续消息状态。
2. 下一次 Model 的输入是否包含这条失败信息，或者其有效摘要。
3. Summarization 是否改变了本轮模型看到的上下文。

第八章已经指出：保存的 `State["messages"]` 与下一次模型实际消费的消息可能并不完全相同。因此不能只看 State 里的历史就断定模型“已经看见并理解”每条测试日志。

要定位问题，可能需要模型调用级 Trace 或 Middleware 的审计信息。对于生产系统，这类详细观察必须考虑敏感数据保护。

### 12.4 第四个问题：Agent 为什么没有继续修复？

如果模型在收到失败输出后直接结束：

```text
Model
  |
  +-- tool_calls = []
          |
          v
         END
```

那是 Agent Loop 的正常停止条件之一，但**业务任务未必成功**。

这时应从：

- 模型输出；
- 当前任务指令；
- Todo 状态；
- Context 压缩后的有效内容；
- 是否发生人工拒绝或工具错误；

共同判断原因。

Graph 正常结束，仅表示当前 Graph 执行走到了终止路径，不等于测试通过。

### 12.5 第五个问题：如果卡住，先看哪一层？

下面是一份本章逐步推导出来的排查顺序：

```text
界面没有文字？
    |
    +-- 是否只订阅 updates，未订阅 messages？
    |
    +-- 模型是否支持/提供流式输出？
    |
    +-- 模型当前是否正在等待工具返回？

很久没有节点更新？
    |
    +-- 看是否进入 execute、task 或其他长耗时工具
    |
    +-- 检查工具级事件 / Trace / 超时

看到“已完成”却不可信？
    |
    +-- 按 tool_call_id 核对真实 ToolMessage
    |
    +-- 检查 pytest 返回码和断言输出

子 Agent 过程看不到？
    |
    +-- 检查子图/子 Runnable 事件是否被转发
    |
    +-- 必要时观察嵌套 Trace

中断后似乎停止？
    |
    +-- 检查是否有 HITL interrupt
    |
    +-- 检查 Checkpointer 和 thread_id 配置
```

这里的每一步都由前面章节的机制自然导出，没有引入新的 Agent Loop。

---

## 十三、一个无需 API Key 的最小实验：同一次执行产生三类观察信息

前面有很多框架级示例。但要确认自己真的理解 `updates`、`messages` 与 `custom` 的区别，不需要先申请模型 API Key。

我们先用一个纯 Python 生成器模拟同一条执行链。

注意：**这个实验验证的是事件分类与消费逻辑，不验证 LangGraph 源码自身的 Streaming 实现，也不验证真实模型能正确修复退款代码。**

### 13.1 完整可运行代码

将下面内容保存为 `chapter14_minimal.py`：

```python
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class Event:
    mode: str
    node: str
    data: object


def fake_refund_agent() -> Iterator[Event]:
    """模拟一轮 read_file -> 模型思考 -> pytest 失败 -> 修复的观察事件。"""

    yield Event(
        mode="updates",
        node="model",
        data={"tool_calls": [{"name": "read_file", "path": "shop/refund.py"}]},
    )

    yield Event(
        mode="updates",
        node="tools",
        data={"tool_result": "读取 refund.py 成功"},
    )

    yield Event(mode="messages", node="model", data="发现")
    yield Event(mode="messages", node="model", data="尾差")
    yield Event(mode="messages", node="model", data="风险")

    yield Event(
        mode="custom",
        node="tools",
        data={"stage": "pytest", "detail": "开始执行退款测试"},
    )

    yield Event(
        mode="updates",
        node="tools",
        data={"tool_result": "FAILED: 19.99 != 20.00"},
    )

    yield Event(
        mode="updates",
        node="model",
        data={"tool_calls": [{"name": "edit_file", "path": "shop/refund.py"}]},
    )


def main() -> None:
    events = list(fake_refund_agent())
    updates = [e for e in events if e.mode == "updates"]
    messages = [e for e in events if e.mode == "messages"]
    custom = [e for e in events if e.mode == "custom"]

    assert len(updates) == 4
    assert len(messages) == 3
    assert len(custom) == 1
    assert "FAILED" in str(updates[2].data)
    assert "".join(str(e.data) for e in messages) == "发现尾差风险"
    assert custom[0].data["stage"] == "pytest"

    print("updates:", len(updates))
    print("messages:", "".join(str(e.data) for e in messages))
    print("custom:", custom[0].data)
    print("PASS: 三种观察事件各自承担不同职责")


if __name__ == "__main__":
    main()
```

运行：

```bash
python chapter14_minimal.py
```

预期输出：

```text
updates: 4
messages: 发现尾差风险
custom: {'stage': 'pytest', 'detail': '开始执行退款测试'}
PASS: 三种观察事件各自承担不同职责
```

这个实验中有一个刻意保留的现象：

```text
custom: 开始执行退款测试
updates: FAILED: 19.99 != 20.00
```

`custom` 先发出“已开始”，而真正的失败结果在之后的 `updates` 出现。

因此，不能把“进度消息到了”解释成“业务步骤成功结束”。

这正是实际前端最容易犯的错误之一。

### 13.2 实验真正验证了什么？

它验证了三个设计认识：

1. 同一次执行可以包含不同粒度的事件。
2. 不同事件需要不同的消费规则。
3. 状态更新和进度提示不能互相冒充。

它**没有**验证：

- `create_deep_agent()` 的实时时间行为；
- 模型 Provider 的真实 Token Streaming；
- LangGraph 与 ToolNode 的具体事件顺序；
- 真实 `pytest` 执行；
- LangSmith 的远程 Trace 采集。

下一节把这些概念重新映射到框架入口。

---

## 十四、返回真实框架：一份可以逐步扩展的观察脚手架

现在我们已经知道，不需要一开始就开启所有功能。

建议在实践中分三次改造同一个 Agent 调用。

### 14.1 第一次：先看每轮节点更新

```python
# 框架级示例：需配置真实模型与 Backend
agent = create_deep_agent(model=model, backend=backend)

for event in agent.stream(inputs, stream_mode="updates"):
    print(event)
```

目标是回答：

> 任务究竟经过了哪些节点？每轮有什么工具结果？

### 14.2 第二次：需要显示模型文字，再加 messages

```python
# 框架级示例：v1 多模式形态；不启用 subgraphs=True
for mode, payload in agent.stream(
    inputs,
    stream_mode=["updates", "messages"],
):
    if mode == "updates":
        print("\n[STEP]", payload)
    elif mode == "messages":
        token, metadata = payload
        if isinstance(token.content, str):
            print(token.content, end="", flush=True)
```

目标是回答：

> Agent 哪一轮正在输出什么，同时又在哪个节点执行？

### 14.3 第三次：需要找性能瓶颈，再加事件追踪

此时不必继续给 `messages` 增加复杂条件，而应该观察工具与模型调用各自的生命周期：

```python
# 框架级示例：使用 Runnable 事件接口
async for event in agent.astream_events(inputs, version="v2"):
    if event["event"] in {"on_tool_start", "on_tool_end"}:
        print(event["event"], event.get("name"))
```

再结合 Trace 平台查看嵌套调用树，必要时记录耗时与错误。

### 14.4 为什么不推荐“一键记录所有事情”？

因为在 Coding Agent 场景中，过多日志可能包含：

- 原始项目代码；
- 测试环境变量或密钥；
- 工具输入参数；
- 用户数据；
- 大量重复的完整 State；
- 长时间运行的冗余 Token 片段。

观测能力越强，采集、存储和权限管理的成本也越高。

因此最好先定义可观察问题，再选择最小数据集合：

```text
前端即时反馈 → updates + messages
工具内部进度 → custom（工具主动上报）
错误归因 → tool events + Trace
状态快照 → values / Checkpoint
深入 Runtime 排错 → tasks / debug（注意体积）
```

这是设计取舍，不是某个 Stream Mode 天生优于其他模式。

---

## 十五、把第十四章的架构从头重新拼起来

本章开始时，我们只有：

```text
agent.invoke(inputs)
    |
    v
最终结果
```

它已经可以正确执行，但我们无法观察中间过程。

于是，为了看见每次节点执行结果，我们引入：

```text
Agent Graph
    |
    +--> updates：节点写了什么
```

接着，发现模型文字需要实时显示，增加：

```text
Agent Graph
    |
    +--> updates：节点写了什么
    |
    +--> messages：模型生成了什么
```

再发现工具内部可能长期运行，增加：

```text
Agent Graph
    |
    +--> updates：节点更新
    |
    +--> messages：模型 Token
    |
    +--> custom：工具 / 节点主动上报
```

这些能满足实时界面的需要，却还不容易还原嵌套调用的因果关系，于是再引入：

```text
Agent Graph
    |
    +--> Streaming
    |      +-- updates
    |      +-- values
    |      +-- messages
    |      +-- custom
    |
    +--> Tracing
    |      +-- Run / Parent Run 关系
    |      +-- Model / Tool 调用树
    |      +-- 耗时 / 错误 / 元数据
    |
    +--> Debug
           +-- 内部任务事件
           +-- Checkpoint 事件
           +-- 更详细的运行诊断
```

这里还有一条与本章主题相邻、但职责独立的路径：

```text
Agent Graph
    |
    +--> State Channels / Reducers
    |
    +--> Checkpointer
           +-- Thread
           +-- 持久化 / 恢复
```

它是第十三章讲的**状态与恢复机制**，不是我们为了实时显示文字才额外发明的东西。

现在再看整体关系，就能准确说出：

- DeepAgents 装配完整 Harness。
- LangChain 构建包含 Middleware 的 Agent Graph。
- LangGraph 负责实际调度并提供流式执行接口。
- Trace 从运行过程收集调用关系。
- Checkpointer 为恢复提供状态依据。

这些机制解决的是**不同问题**，所以不能因为名字都与“运行过程”有关，就把它们归为一个万能的“监控模块”。

---

## 十六、本章最重要的七个结论

**第一，`stream()` 不会改变 Agent Loop 的决策能力。**

它改变的是调用方观察执行结果的方式。模型不会因为调用了 `stream()` 就天然更聪明，测试也不会因此更可靠。

**第二，`updates` 与 `values` 的区别是变化与全貌。**

前者适合定位哪个节点提交了什么，后者适合观察某一时刻的整体 State。

**第三，`messages` 关注模型生成片段，不等于工具已经执行。**

模型可能先输出工具调用参数；工具仍须由 ToolNode / Backend 执行并产生实际反馈。

**第四，`custom` 必须有主动上报者。**

只打开自定义流模式，不会自动获取真实 `pytest` 的执行百分比。

**第五，Streaming、Tracing、Debugging 是三种不同的观察粒度。**

Streaming 用于实时反馈，Tracing 用于调用关系与问题归因，Debug 用于更深入的 Runtime 诊断。

**第六，子 Agent 的事件来源不能靠文本猜测。**

需要结合命名空间、Runnable Metadata、实际事件转发方式或调用 Trace；`subgraphs=True` 的效果受执行结构与版本影响。

**第七，可观察不等于业务正确。**

看到模型宣称“pytest 通过”，并不等于有真实命令执行记录；看到 Graph 正常结束，也不等于优惠券尾差问题真的被修复。

---

## 十七、自然进入第十五章：把这些机制串成一次完整修复

到目前为止，我们已经从最小 `Agent Loop` 一步步得到：

```text
Model / Tool Loop
    ↓
消息协议
    ↓
LangGraph 状态调度
    ↓
Middleware 生命周期
    ↓
DeepAgents Harness
    ↓
Context / Skills / Memory
    ↓
Filesystem / Backend
    ↓
HITL / 权限
    ↓
Planning / Todo
    ↓
Subagents
    ↓
State / DeltaChannel / Checkpoint
    ↓
Streaming / Tracing / Debugging
```

现在理论链已经完整，真正需要做的是回到最初的退款 Bug：

> 从用户提出修复请求开始，沿一条真实执行路径，逐步指出每一次模型决策、文件操作、状态更新、审批、子任务以及测试结果究竟由哪个组件负责。

我们不再新增一批抽象，而是要把前十四章的机制合在一次可观察的 Coding Agent 执行中。

**下一章：第十五章——端到端走读：一个 DeepAgents Coding Agent 如何修复优惠券部分退款 Bug？**

---

## 附录 A：阅读源码的最短路线

建议按以下顺序回到实际代码，而不是一开始就阅读全部 `Pregel` Runtime：

1. [DeepAgents 固定版本 `graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)：确认 `create_deep_agent()` 最终返回 `create_agent()` 构建的 Graph，以及 `debug` 如何传递。
2. [LangChain Agent Factory](https://github.com/langchain-ai/langchain/blob/main/libs/langchain/langchain/agents/factory.py)：回顾 `model`、`tools` 节点为何存在；此处应结合本系列固定版本的源码阅读。
3. [LangGraph Streaming 文档](https://docs.langchain.com/oss/python/langgraph/streaming)：先只研究 `updates` 与 `values`。
4. [LangGraph `types.py`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py)：对照 `StreamMode`、`MessagesStreamPart`、`UpdatesStreamPart` 等概念；新旧版本结构需区别。
5. [LangChain Streaming 文档](https://docs.langchain.com/oss/python/langchain/streaming)：研究 Agent 的模型增量输出与自定义事件。
6. [LangGraph `pregel/main.py`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/main.py)：最后才深入 `stream()` / `astream()` 的 Runtime 执行入口。
7. [LangSmith Tracing 指南](https://docs.langchain.com/langsmith/trace-with-langgraph)：研究嵌套调用如何变成可分析的 Trace。

这条路线的原则是：**先知道当前想观察哪个事实，再去找那个事实从哪里产生、如何被转发。**

## 附录 B：自测题

不看正文，试着回答下面八个问题：

1. 为什么 `invoke()` 的最终 State 不能替代实时进度？
2. 如果你想知道哪个节点加入了一条失败的 `ToolMessage`，应该优先观察哪一种 Stream Mode？
3. `values` 与 Checkpoint 的职责是否相同？为什么？
4. 为什么 `messages` 中出现 `execute` 的工具调用片段，不意味着 `pytest` 已经运行？
5. 如果 `pytest` 执行二十秒，开启 `custom` 就能自动显示进度百分比吗？
6. `debug=True` 与 `stream_mode="debug"` 为什么不是同一个概念？
7. 怎样区分主 Agent 和子 Agent 的嵌套调用？
8. 如果 Trace 显示模型在收到失败日志后直接结束，应该沿着哪几个步骤继续排查？

如果能够用实际的输入、事件、工具结果和源码位置回答这些问题，就已经理解了本章所讨论的**可观察性怎样从 Agent Loop 中自然长出来**。
