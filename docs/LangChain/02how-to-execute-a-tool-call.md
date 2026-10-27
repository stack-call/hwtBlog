---
title: "01｜模型提出工具调用，究竟是谁执行了它？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-execute-a-tool-call
description: "从 refund.py 的失败测试出发，用真实 pytest 实验区分模型的工具请求与程序的实际执行，再追踪 AIMessage、ToolNode、BaseTool 与 ToolMessage 的固定版本源码。"
tags:
  - LangChain
  - LangGraph
  - Agent
  - 源码精读
  - Python
---

> 本文是「LangChain × LangGraph 源码精读」的第 01 章。[先读系列引言](/langchain-langgraph-source/introduction)。文件名采用博客发布序号 `02`，所以是 `02how-to-execute-a-tool-call.md`。
>
> **本章只回答一个问题：当模型说“运行测试”时，究竟是谁让 `pytest` 真正运行起来？** 我们暂时不研究模型为什么决定运行测试，也不研究测试结束后怎样自动进入下一轮；那是下一章的任务。

我们已经知道 `refund.py` 有一个精度问题。测试也已经写好。现在，假设模型返回一句话：

> 我需要执行 `test_refund.py`，看看现在是什么状态。

如果它只是说出了这句话，电脑上的测试会开始运行吗？当然不会。

但即使模型给出的不是一句普通文字，而是一份看上去符合规范的工具调用数据，情况也没有改变。**模型能提出请求，不等于请求已经被执行。**

<!-- truncate -->

## 1. 先看一个不会执行任何操作的“工具调用”

延续引言中的例子，项目目录只有两个业务文件。

`refund.py`：

```python
# 教学化简：只保留能够暴露浮点精度问题的计算。
def refund_amount(item_price: float, allocated_coupon: float) -> float:
    return item_price - allocated_coupon
```

`test_refund.py`：

```python
from refund import refund_amount


def test_partial_refund_exact_money():
    assert refund_amount(0.3, 0.2) == 0.1
```

现在我们把“模型准备运行测试”的输出写成如下 Python 数据：

```python
# 概念数据：不是一次真实模型 API 调用的输出。
request = {
    "name": "run_tests",
    "args": {"target": "test_refund.py"},
    "id": "call_001",
    "type": "tool_call",
}
```

这份数据包含要调用的工具名、参数，以及本次请求的标识符。把它赋值给变量，Python 只是在内存中创建了一个字典。没有 `pytest` 进程被创建，磁盘上的文件也没有变化。

这里容易产生第一个误解：有些框架会把这类数据放在“AI 消息”里，让它看起来像是一个已经被系统接受的命令。实际上，它首先只是**待处理的请求**。

换句话说，此刻系统中还缺少一个动作：

```text
工具请求（数据） ──？──> 真正执行 pytest（副作用）
```

把问号补上的，不可能是字典本身；必须有一段我们实际运行的程序。

## 2. 先不用框架：把“请求”变成真实测试

最简单的办法，是写一个分派函数：收到 `run_tests`，查找对应的 Python 函数，然后调用它。

在这里我们不允许模型任意拼接 Shell 命令。只提供一个预先登记的工具，它只能运行当前项目里的 `test_refund.py`。这并不是完整的安全隔离，却能让读者观察到“模型参数”与“程序允许的操作”之间的边界。

在上述两个文件旁边新建 `demo_dispatch.py`：

```python
# 教学化简：纯 Python 工具分派实验，不是 LangChain 源码。
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
executions = 0


def run_tests(target: str) -> str:
    global executions
    if target != "test_refund.py":
        raise ValueError("Only test_refund.py is allowed")

    executions += 1
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return f"exit_code={completed.returncode}\n{completed.stdout}"


TOOLS = {"run_tests": run_tests}


def dispatch(call: dict) -> dict:
    tool = TOOLS.get(call["name"])
    if tool is None:
        raise ValueError(f"Unregistered tool: {call['name']}")

    result = tool(**call["args"])
    return {
        "type": "tool",
        "tool_call_id": call["id"],
        "content": result,
    }


request = {
    "name": "run_tests",
    "args": {"target": "test_refund.py"},
    "id": "call_001",
    "type": "tool_call",
}

print("请求生成后，pytest 执行次数:", executions)
assert executions == 0

reply = dispatch(request)

print("分派完成后，pytest 执行次数:", executions)
print("响应关联 ID:", reply["tool_call_id"])
print("测试退出码为 1:", "exit_code=1" in reply["content"])
print("真实测试失败:", "FAILED test_refund.py" in reply["content"])

assert executions == 1
assert reply["tool_call_id"] == request["id"]
assert "exit_code=1" in reply["content"]
assert "FAILED test_refund.py" in reply["content"]
```

这个实验有意保留了两个不同的时刻。

第一个时刻，是 `request` 创建完成，`dispatch()` 尚未调用。此时 `executions == 0`。这个断言证明：**即使已经有了结构完整的工具请求，也没有任何执行发生。**

第二个时刻，是 `dispatch(request)` 运行。分派函数先用 `name` 查询工具登记表，再将 `args` 传给 `run_tests()`。只有运行到 `subprocess.run(...)`，才真正启动了一个 Python 进程去执行 `pytest`。

在同一目录执行：

```bash
python -m pip install pytest
python demo_dispatch.py
```

本地实际运行结果（Python 环境中使用 `pytest 9.0.2`）：

```text
请求生成后，pytest 执行次数: 0
分派完成后，pytest 执行次数: 1
响应关联 ID: call_001
测试退出码为 1: True
真实测试失败: True
```

**验证范围：** 这组输出已通过上述三个文件在本地真实执行并通过断言，调用的是实际 `pytest`，不是模拟测试。我们尚未调用任何 LLM，也尚未执行 LangChain / LangGraph 的框架代码。

现在请回头看 `dispatch()`。它其实只解决了三个问题：

1. **找谁执行：** `TOOLS.get(call["name"])` 选择允许的工具。
2. **用什么输入执行：** `tool(**call["args"])` 将请求参数交给 Python 函数。
3. **怎样记录结果属于哪次请求：** 返回数据保留 `tool_call_id`。

`request` 是模型侧可能产生的数据；`TOOLS`、`run_tests()` 和 `subprocess.run()` 则属于**我们控制的运行程序**。这个分界线，就是本章要在框架源码中找到的东西。

## 3. 第一个源码入口：为什么 AIMessage 里保存的是 tool_calls？

如果要让上述过程接入不同的模型，我们首先需要稳定的表达方式，让程序能够读懂“模型请求调用了什么工具”。于是出现了 LangChain Core 的 `AIMessage`。

在固定版本 **`langchain-core 1.7.0`，Commit `f4e6cde98d85910cef5d1f43e26e7a497ae861f3`** 中，打开：

[`libs/core/langchain_core/messages/ai.py` — `AIMessage`](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/messages/ai.py)

关键字段是一行真实源码节选：

```python
tool_calls: list[ToolCall] = Field(default_factory=list)
```

这里先不要研究 `AIMessage` 的全部字段、流式消息、兼容解析等细节。只需要读懂这一行：模型消息可以带着一组结构化的工具请求。

它回答的是**“模型提出了什么请求”**，没有负责找到工具、执行 Python 函数或启动操作系统进程。因此，以下代码仅创建消息，并不会运行测试：

```python
# 框架 API 使用示例：本章环境未安装对应包，未执行。
from langchain_core.messages import AIMessage

model_message = AIMessage(
    content="",
    tool_calls=[
        {
            "name": "run_tests",
            "args": {"target": "test_refund.py"},
            "id": "call_001",
        }
    ],
)

assert model_message.tool_calls[0]["name"] == "run_tests"
```

请注意，它与之前的 `request` 本质上一样：都只是记录意图的数据。但它为下游执行程序提供了统一接口，不用每次都猜模型输出的自然语言是什么意思。

这就把原来的问号缩小了一半：

```text
AIMessage.tool_calls ──谁解析、分派？──> Python Tool
```

## 4. 第二个源码入口：ToolNode 才负责把请求交给工具

之前我们自己写的 `dispatch()` 很短，但已经能看出它是一个专门的职责。如果许多节点都要自己维护工具登记表、解析消息、处理错误和返回结果，分派逻辑就会重复。

LangGraph 提供了一个这样的工具执行入口：`ToolNode`。

源码基线为 **`langgraph-prebuilt 1.1.1`，Commit `5eb60aef6f3837a1fb632a8abc6cf8e8da81785b`**：

[`libs/prebuilt/langgraph/prebuilt/tool_node.py` — `ToolNode`](https://github.com/langchain-ai/langgraph/blob/5eb60aef6f3837a1fb632a8abc6cf8e8da81785b/libs/prebuilt/langgraph/prebuilt/tool_node.py)

我们不要一口气读完整个类。只读与当前这个问题相关的路径。

**先看工具是在哪里登记的。** 在 `ToolNode.__init__()` 中，它把收到的工具规范化后，存入按名称索引的字典。对应的真实源码关键行是：

```python
self._tools_by_name[tool_.name] = tool_
```

这正好对应我们自己写的 `TOOLS = {"run_tests": run_tests}`。为什么要有这个表？因为模型返回的是一个*名字*，而不是可以直接执行的 Python 函数对象。运行程序需要用这个名字找到事先登记、可以执行的实现。

**再看收到消息以后谁负责启动执行。** 在 `ToolNode._func()` 中，真实源码先解析输入中的调用请求：

```python
tool_calls, input_type = self._parse_input(input)
```

随后，它为每个请求准备运行所需的上下文，并交给执行器处理。关键分派操作出现在同一函数中：

```python
executor.map(self._run_one, tool_calls, input_types, tool_runtimes)
```

原始实现允许一条消息请求多个工具，因此这里不再是我们教学实验中的单次 `tool(**args)`，而是把请求逐项交给执行路径。**本章先只研究其中一项工具调用的正常路径。** 并发、状态注入、错误策略等分支不会妨碍我们先理解“谁真正执行”。

继续沿同步执行路径进入 `ToolNode._execute_tool_sync()`，可以看到真正的调用点：

```python
response = tool.invoke(call_args, config)
```

到这里，问题第一次得到源码级的回答：**`ToolNode` 不只是展示模型希望调用什么工具；它负责把请求匹配到已注册工具，并进入实际的 `tool.invoke()`。**

把我们的代码和真实源码放在一起，对应关系就很明确：

| 纯 Python 实验 | 固定版本源码中的对应职责 | 运行意义 |
| --- | --- | --- |
| `request` | `AIMessage.tool_calls` | 记录请求，尚无工具副作用 |
| `TOOLS` | `ToolNode` 的工具登记表 | 仅从已登记工具中选择实现 |
| `dispatch(request)` | `ToolNode._func()` 的分派路径 | 从请求进入执行过程 |
| `tool(**call["args"])` | `_execute_tool_sync()` 中的 `tool.invoke(...)` | 触发具体工具的执行 |

这张表不是说双方实现完全相同。真实 `ToolNode` 还处理错误、上下文注入、多个工具请求及其他返回类型。我们只用源码中已经核实的几行，证明这个最小执行闭环确实存在。

## 5. 为什么调用的是 tool.invoke，而不是直接调用工具函数？

在我们的实验里，登记表保存的是普通函数，因此查表后直接调用即可。但真实框架面对的是不同的工具实现。它不能假设每个工具都有同样的 Python 函数签名，也不能跳过输入整理过程。

这时才需要理解一层更小的接口：`BaseTool`。

在 **`langchain-core 1.7.0`** 的 [`libs/core/langchain_core/tools/base.py` — `BaseTool.invoke()`](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/tools/base.py) 中，可以看到以下真实源码节选：

```python
tool_input, kwargs = _prep_run_args(input, config, **kwargs)
return self.run(tool_input, **kwargs)
```

这两行没有“自动代理”的魔法。它做的事很具体：把收到的调用数据和运行配置整理为工具能够处理的输入，再进入工具自身的运行方法。实际业务动作最终仍然由工具实现完成。

在我们的例子中，业务动作就是 `run_tests()` 内部的 `subprocess.run(...)`；如果工具只返回一个固定字符串，那么框架最多只是执行了那个返回字符串的函数，不会因此自动运行测试。

这意味着框架帮我们统一了**调用入口**，但没有替我们决定：哪些操作能够执行、文件路径是否可信、进程应该拥有什么权限。

## 6. 工具执行完之后，为什么还要变成一条消息？

`pytest` 已经真实启动，返回码也是 1。最简单的做法是直接 `print()` 失败信息。但我们以后希望模型还能看到这次失败，并决定下一步操作；仅仅输出到终端，不一定能进入下一次模型输入。

所以工具执行完以后，我们至少需要保留两部分数据：工具给出的结果，以及它回答的是哪一个请求。

我们之前的 `dispatch()` 已经返回了：

```python
# 教学化简：普通字典，不是框架返回的 ToolMessage 实例。
{
    "type": "tool",
    "tool_call_id": "call_001",
    "content": "exit_code=1\n...FAILED test_refund.py...",
}
```

对应的真实消息类型是 `ToolMessage`。在同一版 LangChain Core 的 [`libs/core/langchain_core/messages/tool.py`](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/messages/tool.py) 中，最值得留意的字段是：

```python
tool_call_id: str
```

它不是工具的名称，而是**这次请求的标识符**。模型先提出 `call_001`，程序执行之后再用同一标识符把结果送回来；这样，即便以后同一轮存在多个工具请求，也不会只凭消息顺序猜测“哪个结果对应哪个调用”。

还要特别区分两类成功：如果 `run_tests()` 正常返回字符串，但字符串表示 `pytest` 的退出码是 `1`，那么**工具函数完成调用**与**业务测试通过**是两回事。不要仅看到工具调用成功，就宣布 Bug 已修复。

对于以字典状态输入的普通 `ToolNode` 调用，源码会把工具输出包装为消息更新（例如 `{"messages": [...]}`）；后面由谁将这个更新合并进状态、又如何交回模型，是我们在下一章乃至状态章节需要继续研究的地方。本章仅确认工具结果能够以关联消息的形式离开执行入口。

## 7. 用框架替换我们手写的 dispatch（可选实验）

现在，我们可以把手写的工具登记与分派换成 `ToolNode`，而不引入真实大模型。下面给出与前面三个文件配套的最小框架实验，仍然运行同一个 `test_refund.py`。

安装与引言源码基线对应的依赖：

```bash
python -m pip install \
  "langchain-core==1.7.0" \
  "langgraph==1.2.17" \
  "langgraph-prebuilt==1.1.1" \
  "pytest==9.0.2"
```

创建 `demo_toolnode.py`：

```python
# 框架 API 示例：接口已按固定源码核对，但未在本次环境中运行。
from pathlib import Path
import subprocess
import sys

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

ROOT = Path(__file__).resolve().parent


@tool
def run_tests(target: str) -> str:
    """运行本教学项目中固定的退款测试文件。"""
    if target != "test_refund.py":
        raise ValueError("Only test_refund.py is allowed")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return f"exit_code={completed.returncode}\n{completed.stdout}"


model_message = AIMessage(
    content="",
    tool_calls=[
        {"name": "run_tests", "args": {"target": "test_refund.py"}, "id": "call_001"}
    ],
)

node = ToolNode([run_tests])
result = node.invoke({"messages": [model_message]})
reply = result["messages"][0]

assert reply.tool_call_id == "call_001"
assert "exit_code=1" in reply.content
assert "FAILED test_refund.py" in reply.content
print(reply.content)
```

这个示例用于验证一个特定机制：不用 `create_agent()`，不连接任何 LLM，也可以把一条**人为构造**的 `AIMessage.tool_calls` 交给 `ToolNode`，通过已登记工具触发实际操作。

**验证边界：** 代码中的类、函数和调用方式已与指定版本的仓库实现及参考文档对照，但本次运行环境没有安装对应的 LangChain / LangGraph 依赖，因此**没有执行 `demo_toolnode.py`，也不声称上述断言已经运行通过**。如果依赖安装出现解析冲突，应以锁文件和实际安装结果为准，不能把固定源码基线直接等同于已经验证过的依赖兼容矩阵。

即使将来这段实验全部通过，它也只能证明工具分派与消息构造可以工作，**不能证明任何模型具备修复退款 Bug 的能力**：这里根本没有模型作出决策，工具请求是我们手动写好的。

## 8. 回看：这次执行到底发生了什么？

现在，我们终于可以把开头那张只有一个问号的小图补完整：

```text
AIMessage.tool_calls
      │
      │ 只是请求：run_tests(test_refund.py)
      ▼
ToolNode
      │
      │ 解析请求、匹配已登记工具
      ▼
BaseTool.invoke(...)
      │
      │ 进入工具自己的运行逻辑
      ▼
run_tests() → subprocess.run(...) → pytest
      │
      │ 真实退出码 + 测试失败信息
      ▼
ToolMessage(tool_call_id="call_001", content="...")
```

这里的箭头描述的是**一次同步工具执行的数据与调用路径**，不是完整的图调度或状态流。当前没有任何机制保证工具执行完后模型必然再次被调用，更没有机制保证失败后自动修复代码。

而这个未解决的问题，正好会成为下一章的起点。

假设我们已经通过 `ToolMessage` 知道 `pytest` 失败。此时有两种完全不同的程序行为：一种是在工具返回后直接结束；另一种是将工具结果送回模型，让模型重新决定下一步。

**谁负责把工具执行结果再次交给模型？又是谁决定循环继续还是结束？**

下一章，我们会先用最简单的 `while` 循环回答，再把这段循环映射到真实框架中的执行路径。

---

## 本章源码索引与实验说明

| 阅读对象 | 固定基线 | 本章关注点 |
| --- | --- | --- |
| [AIMessage](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/messages/ai.py) | langchain-core 1.7.0，`f4e6cde` | `tool_calls` 如何保存请求 |
| [ToolNode](https://github.com/langchain-ai/langgraph/blob/5eb60aef6f3837a1fb632a8abc6cf8e8da81785b/libs/prebuilt/langgraph/prebuilt/tool_node.py) | langgraph-prebuilt 1.1.1，`5eb60ae` | 登记、解析和执行工具请求 |
| [BaseTool](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/tools/base.py) | langchain-core 1.7.0，`f4e6cde` | `invoke()` 如何进入工具实际运行 |
| [ToolMessage](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/messages/tool.py) | langchain-core 1.7.0，`f4e6cde` | `tool_call_id` 如何关联结果 |
| [Agent Factory](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py) | langchain 1.4.7，`fbb8a35` | 之后追踪 `create_agent()` 的入口 |

本文含有三类不同证据，请勿混淆：**真实源码节选**均对应上面锁定的文件；**教学化简**使用纯 Python 模拟请求分派，并已真实运行 `pytest`；**框架 API 示例**用于后续复现，本次未运行。固定版本链接用于可复核性，不表示这些包已在同一环境完成兼容性验证。
