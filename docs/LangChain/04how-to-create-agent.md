---
title: "03｜手写 Agent Loop 已经能工作，为什么还需要 create_agent？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-create-agent
description: "以修复 refund.py 的同一条工具反馈链为例，从手写循环中拆出节点与路由，沿固定版本的 create_agent 源码追踪 ToolNode、model 节点、条件边与编译入口，理解它究竟构建了什么。"
tags:
  - LangChain
  - LangGraph
  - Agent
  - create_agent
  - 源码精读
  - Python
---

> 本文是「LangChain × LangGraph 源码精读」的**第 03 章**，文件名 `04how-to-create-agent.md`。建议先读[第 02 章：一次工具调用结束后，模型为什么还能继续？](/langchain-langgraph-source/how-to-build-agent-loop)。
>
> **本章只回答一个问题：我们已经能用 `while` 把模型与工具连成循环，LangChain 的 `create_agent()` 究竟替我们组织了什么？** 我们会走到「生成可执行图」为止；图内部怎样逐步调度、合并状态，是后面的章节。

上一章，我们让退款案例完成了一条真实执行链：

```text
模型请求 run_tests → pytest 失败
       ↓
模型请求 apply_fix → 文件被修改
       ↓
模型请求 run_tests → pytest 通过
       ↓
模型不再请求工具 → 程序结束
```

这里面，工具是真实运行的，选择下一步的“模型”则是一个**脚本化假模型**。这让我们验证了反馈循环，却没有宣称测试过大模型的修复能力。

而且，从程序员的角度看，那个手写循环已经足够好了：少量 Python、顺序清晰、能放断点、也不需要额外框架。**既然如此，为什么还有 `create_agent()`？**

先明确一点：我们不是要证明 `while` 天生落后，也不是说任何 Agent 都必须画成图。恰恰相反，只有先承认 `while` 能解决这个问题，才看得出框架真正承担了哪部分工作。

<!-- truncate -->

## 1. 先保留最简单的办法：一段没有问题的 while

上一章的核心结构可以压缩成这样：

```python
# 教学化简：model、dispatch 和 messages 已经初始化；不是 LangChain 源码。
for _ in range(MAX_ROUNDS):
    ai_message = model(messages)
    messages.append(ai_message)

    if not ai_message.tool_calls:
        break

    for call in ai_message.tool_calls:
        tool_reply = dispatch(call)
        messages.append(tool_reply)
```

对于我们的退款任务，这段代码具备三个必需的动作：

1. **调用模型**：让模型依据当前消息决定下一步。
2. **执行工具**：模型如果请求了 `run_tests` 或 `apply_fix`，让 Python 真正执行。
3. **决定下一步**：没有工具请求就结束，有请求就把结果交回模型。

这正是一个最小 Agent Loop。前两章已经分别追踪过「执行工具」和「消息反馈」，不需要在这里重新讲一遍。

现在增加一个仍然很小的需求：我们希望不同 Agent 都可以复用这套执行结构，但每个 Agent 的工具和模型可能不同。若继续将一切写死在同一段循环里，**具体做什么**与**下一步去哪儿**总是纠缠在一起。

例如，下面两行回答的是不同问题：

```python
ai_message = model(messages)      # 这一步做什么？
if not ai_message.tool_calls:    # 做完以后去哪儿？
```

`dispatch(call)` 负责执行；外层 `if` 和 `for` 负责让执行继续。它们当前写在一起并没有错。只是当我们想把这个结构作为框架复用时，有必要让执行单元和连接关系各自拥有明确的位置。

于是这一章只引入一个新想法：**把「做什么」表示为节点，把「接着做什么」表示为连接或路由。** 暂时无需讨论并行或恢复，也不需要先理解整个 LangGraph。

## 2. 先用一张非常小的图表达原来的循环

回忆我们的两种动作：`model` 和 `tools`。

```text
          ┌──────────┐
    START →   model  │ ◄───────────┐
          └────┬─────┘             │
               │                   │
         有工具请求吗？             │
            /      \              │
           否       是             │
           │        │             │
           ▼        ▼             │
          END   ┌──────────┐       │
                │  tools   │───────┘
                └──────────┘
```

这不是让模型“变聪明”的架构图，只是把上一章已经存在的控制条件画了出来。

还可以把路由规则写成一份最小的 Python 函数：

```python
# 教学化简：描述最小正常路径，不是 LangGraph 的内部路由函数。
def next_step(just_ran: str, *, has_tool_calls: bool = False) -> str:
    if just_ran == "model":
        return "tools" if has_tool_calls else "END"
    if just_ran == "tools":
        return "model"
    raise ValueError(just_ran)

assert next_step("model", has_tool_calls=True) == "tools"
assert next_step("tools") == "model"
assert next_step("model", has_tool_calls=False) == "END"
```

这个小实验不需要第三方库。我在本地实际执行了三个断言，均通过。它**只验证我们提炼出的最小路由规则**，既没有运行 LangGraph，也没有验证 Agent 智能。

由此可以清楚地区分两种变化：

- **执行没有变**：`run_tests` 还是运行 `pytest`，`apply_fix` 还是写文件。
- **表达方式变了**：原来散落在循环里的执行与跳转，现在可以分别注册、连接，并交给同一个执行入口驱动。

下面我们不再猜想框架“应该”怎样做，而是直接去读实际源码。

## 3. 真实入口：`create_agent()` 接受什么，返回什么？

固定代码基线：

- **LangChain Agent**：`langchain 1.4.7`，Commit [`fbb8a35da7023ac0382c3b22f047337861bd3486`](https://github.com/langchain-ai/langchain/commit/fbb8a35da7023ac0382c3b22f047337861bd3486)。
- **LangChain Core 消息接口**：`langchain-core 1.7.0`，Commit [`f4e6cde98d85910cef5d1f43e26e7a497ae861f3`](https://github.com/langchain-ai/langchain/commit/f4e6cde98d85910cef5d1f43e26e7a497ae861f3)。
- **LangGraph 图构建 / 执行框架**：`langgraph 1.2.17`，Commit [`4c52e12`](https://github.com/langchain-ai/langgraph/commit/4c52e12)。
- **LangGraph ToolNode**：`langgraph-prebuilt 1.1.1`，Commit [`5eb60aef6f3837a1fb632a8abc6cf8e8da81785b`](https://github.com/langchain-ai/langgraph/commit/5eb60aef6f3837a1fb632a8abc6cf8e8da81785b)。

注意：这些是**分别固定的源码阅读坐标**，并不是已经实际安装测试过的依赖锁文件；后面会明确交代实验验证范围。

我们先打开：

[`libs/langchain_v1/langchain/agents/factory.py` — `create_agent()`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L819-L839)。

这个函数最重要的两个普通参数是 `model` 与 `tools`。**真实源码节选**（为聚焦本章，省略了其他可选参数）：

```python
def create_agent(
    model: str | BaseChatModel,
    tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None = None,
    *,
    # ……其他可选配置省略……
) -> CompiledStateGraph[
    AgentState[ResponseT], ContextT, InputAgentState, OutputAgentState[ResponseT]
]:
```

上面保留了源码的参数类型与返回类型，`# ……` 是**省略标记，不是源码原文**。这一个签名已经给出关键信息：**它返回的不是 `AIMessage`、不是 `ToolMessage`，甚至不是一次执行结果；它返回一张已经编译过、可以调用的图。**

谁调用它？是我们编写应用时调用，例如：

```python
from langchain.agents import create_agent

# model 和工具已准备好；当前是创建阶段，而不是执行任务。
agent = create_agent(model=model, tools=[run_tests, apply_fix])
```

在这个阶段，我们只是配置了**以后如何执行**。直到后面调用 `agent.invoke(...)`，才提交一次具体的 Agent 运行。把“创建”和“执行”区分开，是理解下面源码的第一把钥匙。

## 4. `tools=[...]` 传进来后，为什么先产生一个 `ToolNode`？

上一章手写循环中的 `dispatch(call)` 必须能根据工具名找到对应函数。现在调用者只传一份工具列表，就希望框架替我们组织分派入口。

回到同一文件中 [`create_agent()` 的工具初始化位置](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1033-L1059)。

该版本有一个值得注意的细节：工厂先区分由模型提供商管理的工具声明（`dict`）与需要在本地执行的普通工具；再对需要在客户端执行的工具构建 `ToolNode`。关键真实源码仅摘两行：

```python
available_tools = middleware_tools + regular_tools
```

以及：

```python
tool_node: ToolNode | None = None
```

`ToolNode(...)` 的实际创建在这两行之间相邻的代码块中，并接收 `tools=available_tools` 等参数。**我们当前正常案例没有中间件，且传入的是普通 Python 工具**，所以可以把这一阶段理解成：

```python
# 教学化简：只表达本例，不是 create_agent 原样实现。
tool_node = ToolNode([run_tests, apply_fix])
```

这里产生了新的区分：

- 提供给模型的**工具描述**，告诉模型它可以请求哪种操作。
- `ToolNode` 维护的**本地执行入口**，将匹配的请求交给工具实现。

二者相关，但不相等。`bind_tools()` 不是执行工具；真实执行仍然要等到相应工具节点收到请求。

这一节还没启动任何图执行，工具函数也还没有被调用。`create_agent()` 目前只是把未来需要的执行者准备好。

## 5. 那么，模型调用放在哪里？

我们的 `while` 里有一行 `ai_message = model(messages)`，要将它和工具执行分开注册，模型也需要自己的执行位置。

在 [`factory.py` 的 `model_node()` 和 `_execute_model_sync()`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1358-L1419) 中，可以看到这样的真实源码节选：

```python
output = model_.invoke(messages)
```

真实实现并非直接使用外层输入变量 `messages`：`model_node` 从状态中取出消息，构造 `ModelRequest`，再根据本次请求获取正确绑定工具的模型，准备系统提示，才走到这行 `invoke()`。当前我们只需看清因果关系：

**节点被执行 → 读取这次运行的消息 → 调用一次模型 → 产生一条新的模型响应。**

特别要注意“**一次**”两个字。不是 `model_node` 自己实现了无限 `while`；如果需要第二次模型调用，仍然必须有外层执行控制把流程再次带回它。

在这个节点定义完成后，工厂把它注册到图中。同一文件的[节点注册位置](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1475-L1480)有下面的真实源码节选：

```python
graph.add_node("model", RunnableCallable(model_node, amodel_node, trace=False))
if tool_node is not None:
    graph.add_node("tools", tool_node)
```

`RunnableCallable` 在这里适配同步/异步节点调用。我们暂时不展开它的所有接口，只需要知道：**把 `model_node` 交给 `add_node`，不等于立即调用了模型。**

现在，节点已经齐了：一个节点负责模型的一次决策，另一个负责执行模型提出的工具。可是，这两个节点只靠注册仍然不会循环。还差我们在第 2 节画出的那几条连接。

## 6. 从 `if` 到条件路由：是谁决定“下一个节点”？

先看最熟悉的情况。模型返回 `AIMessage`，里面没有 `tool_calls`，上一章的代码会执行 `break`。如果有工具请求，就执行工具。

在 [`factory.py` 的 `_make_model_to_tools_edge()`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1829-L1876)，我们能够定位到同一个决定。

**真实源码节选：**

```python
if len(last_ai_message.tool_calls) == 0:
    return end_destination
```

这就是原来 `if not ai_message.tool_calls: break` 的可追踪对应点。区别只是，前者返回一个**下一步目的地**，而不是直接控制 Python 循环退出。

有了待执行工具请求时，这版代码还会构造调度目标：

```python
return [Send("tools", [tool_call]) for tool_call in pending_tool_calls]
```

这里先不展开 `Send` 的运行时语义。它在本章的必要意义只有一个：**路由结果不再是“请在当前位置调用所有工具”，而是“把这些待执行的调用交给 `tools` 节点”。** 这里的 `pending_tool_calls` 还排除了已经存在匹配工具回复的请求，说明真实实现比我们的最小 `if` 多处理了某些情况；我们将在后续执行与状态章节再讨论它们。

工具执行之后，为什么还会回到模型？在 [`_make_tools_to_model_edge()`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1903-L1939) 的**普通正常路径**中，最终返回的是 `model_destination`，把工具结果交给下一轮模型决策。

注意这句话的限制：它是**普通路径**，而不是永远如此。这一版还有 `return_direct` 和结构化响应等提前结束分支。本章先跟随没有这些选项的 `run_tests / apply_fix` 案例，不把额外机制一次塞进来。

最后，谁把这些“路由函数”装进图？回到 [`create_agent()` 添加条件边的代码](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1594-L1640)。工厂对 `tools` 执行后和 `model` 执行后分别注册 `graph.add_conditional_edges(...)`，在运行时根据状态计算下一步，而不是在创建图时就确定要不要调用工具。

现在，我们终于可以把源码与手写循环一一对应起来：

| 手写 Agent Loop | `create_agent()` 构造出的机制 | 决定了什么 |
| --- | --- | --- |
| `ai_message = model(messages)` | `model` 节点 | 这一步谁调用模型 |
| `dispatch(call)` | `tools` 节点 / `ToolNode` | 这一步谁执行工具 |
| `if not ai_message.tool_calls: break` | 模型后的条件路由 | 什么时候结束 |
| `messages.append(tool_reply)` 后再循环 | 工具节点输出 + 返回模型的路由 | 工具反馈如何进入下一轮（状态更新细节后讲） |

最后一行需要格外谨慎：表里表示的是**职责对应**，不是说 `create_agent` 内部也在做同样的 `list.append`。它产生的是节点输出，实际消息字段怎样合并，要到后面的状态章节才能解释完整。

## 7. 为什么还需要 `StateGraph` 和 `compile()`？

现在我们拥有了三种信息：

- 哪些执行单元存在：`model` 与 `tools`。
- 谁先开始：`START` 指向首个节点。
- 什么时候结束或回环：模型后的条件路由、工具后的条件路由。

这些信息可以成为一份**待执行的程序描述**，而不是立刻开始运行。于是 `create_agent()` 使用了 `StateGraph`。

[`factory.py` 中的图构建入口](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1124-L1141)先解决了输入输出和状态的结构，再创建 `StateGraph`。其中**真实源码节选**是：

```python
graph: StateGraph[
    AgentState[ResponseT], ContextT, InputAgentState, OutputAgentState[ResponseT]
] = StateGraph(
    state_schema=resolved_state_schema,
    input_schema=input_schema,
    output_schema=output_schema,
    context_schema=context_schema,
)
```

可以先将 `state_schema` 理解为“**各步骤之间共享的数据应该长什么样**”。在本例里，最重要的字段是 `messages`；其他细节暂时不影响我们弄懂图从哪里来。

注意，`StateGraph` 此时仍然是**用来描述节点和连接关系的构建器**。等到节点、条件边准备好，`create_agent()` 最终会调用 `graph.compile(...)`，并将配置附在编译结果上。

在这个固定 Commit 的[函数尾部](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1744-L1770)，返回表达式的**教学化简**是：

```python
# 教学化简：省略了源码中的 checkpointer、store、interrupt 等编译选项。
return graph.compile(...).with_config(config)
```

这里的 `...` 只是省略占位符，**不能原样复制运行**。最关键的事实是：`create_agent()` 的输出已经具备统一的执行入口，可以对它调用 `invoke()` 或 `stream()`。但这仍然不表示创建时就执行过 `run_tests`。

至于 `compile()` 具体建立了怎样的运行结构、一次 `invoke()` 如何推动节点执行，它们都是合理的问题，却不是本章该抢先讲完的内容。下一章会先研究图的构造接口，然后才有足够铺垫继续追踪内部调度。

## 8. 将退款案例交给 `create_agent()`：一个无需 API Key 的框架实验

下面给出可以**在安装对应依赖后尝试运行**的完整框架示例，它复用前两章相同的场景。关键不是使用一个真正会修 Bug 的模型，而是查看：当我们不再写 `for/while`，`agent.invoke(...)` 会不会沿同一条工具链产生消息。

这个示例的“模型”仍然是**脚本化假的 ChatModel**，不会联网，也没有推理能力。它通过最后一条 `ToolMessage` 决定下一步。我们只更换外层执行方式：让 `create_agent()` 建立节点与路由。

> **实验状态**：本章写作环境没有安装 LangChain / LangGraph 对应包，尝试从 PyPI 安装时受到网络 DNS 解析失败阻断。因此，以下框架脚本已按固定源码接口编写并做静态检查，但**没有在本环境成功执行**。不要把本节后面的“预期轨迹”当成已经运行得到的真实输出。

建议新建虚拟环境，安装与这次阅读相对应的主包版本，并通过 `pip freeze` 记录实际解析到的 Core / Prebuilt 依赖版本。下述版本组合的端到端安装兼容性尚未在本文环境中验证。

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install "langchain==1.4.7" "langgraph==1.2.17" pytest
python -m pip freeze > requirements-resolved.txt
```

保存为 `demo_create_agent.py`：

```python
"""框架 API 实验：脚本化假模型 + 真正的 pytest；无需 API Key。

本脚本按固定版本源码接口编写，当前写作环境未安装框架，因此尚未执行。
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import subprocess
import sys

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

BROKEN = '''def refund_amount(item_price: float, allocated_coupon: float) -> float:
    return item_price - allocated_coupon
'''
TEST = '''from refund import refund_amount


def test_partial_refund_exact_money():
    assert refund_amount(0.3, 0.2) == 0.1
'''
FIXED = '''from decimal import Decimal


def refund_amount(item_price: float, allocated_coupon: float) -> float:
    return float(Decimal(str(item_price)) - Decimal(str(allocated_coupon)))
'''


class ScriptedRefundModel(BaseChatModel):
    """只复现预定的请求次序；绝不模拟或声称模型智能。"""

    @property
    def _llm_type(self) -> str:
        return "scripted-refund-demo"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        # 教学适配：让 create_agent 的工具绑定步骤能接受这个假模型。
        # 这不是工具执行；真正执行交给 tools 节点。
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last = messages[-1]
        if isinstance(last, HumanMessage):
            response = self._call("call_001", "run_tests", "test_refund.py")
        elif isinstance(last, ToolMessage):
            content = str(last.content)
            if last.name == "run_tests" and "exit_code=1" in content:
                response = self._call("call_002", "apply_fix", "refund.py")
            elif last.name == "apply_fix" and content == "patch_applied":
                response = self._call("call_003", "run_tests", "test_refund.py")
            elif last.name == "run_tests" and "exit_code=0" in content:
                response = AIMessage(content="这个退款测试已通过。")
            else:
                response = AIMessage(content="尚不能确认测试通过。")
        else:
            raise ValueError(f"Unexpected message: {type(last).__name__}")
        return ChatResult(generations=[ChatGeneration(message=response)])

    @staticmethod
    def _call(call_id: str, name: str, target: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"id": call_id, "name": name, "args": {"target": target}}],
        )


def main() -> None:
    with TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "refund.py").write_text(BROKEN, encoding="utf-8")
        (root / "test_refund.py").write_text(TEST, encoding="utf-8")

        @tool
        def run_tests(target: str) -> str:
            """Run the allowlisted refund pytest file."""
            if target != "test_refund.py":
                raise ValueError("Only test_refund.py is allowed")
            p = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return f"exit_code={p.returncode}\n{p.stdout}"

        @tool
        def apply_fix(target: str) -> str:
            """Apply the single allowlisted teaching patch to refund.py."""
            if target != "refund.py":
                raise ValueError("Only refund.py is allowed")
            (root / "refund.py").write_text(FIXED, encoding="utf-8")
            return "patch_applied"

        agent = create_agent(
            model=ScriptedRefundModel(),
            tools=[run_tests, apply_fix],
        )
        # 这里没有显式 for/while：循环控制由已编译图处理。
        result = agent.invoke(
            {"messages": [HumanMessage(content="修复 refund.py，并运行 pytest 验证")]},
            config={"recursion_limit": 20},
        )

        tool_messages = [
            message for message in result["messages"]
            if isinstance(message, ToolMessage)
        ]
        assert [m.name for m in tool_messages] == [
            "run_tests", "apply_fix", "run_tests"
        ]
        assert "exit_code=1" in str(tool_messages[0].content)
        assert tool_messages[1].content == "patch_applied"
        assert "exit_code=0" in str(tool_messages[2].content)
        assert "1 passed" in str(tool_messages[2].content)
        assert isinstance(result["messages"][-1], AIMessage)
        assert not result["messages"][-1].tool_calls
        print("expected: pytest failure -> patch -> pytest success -> final answer")


if __name__ == "__main__":
    main()
```

执行命令（在安装好依赖的环境中）：

```bash
python demo_create_agent.py
```

**预期执行轨迹**（推导自脚本中的假模型决策逻辑，非本环境真实运行输出）：

```text
model: 请求 run_tests(test_refund.py)
tools: 返回 exit_code=1
model: 请求 apply_fix(refund.py)
tools: 返回 patch_applied
model: 请求 run_tests(test_refund.py)
tools: 返回 exit_code=0
model: 返回最终回答（无 tool_calls）
```

这段实验与上一章最大的不同，不是“框架替我们修好了退款计算”，因为修复仍然是事先写好的 `FIXED`；而是：**我们不再手工维护外层循环。调用者准备模型、工具和初始消息，后续节点切换由框架完成。**

另外，虽然这里在临时目录运行 `pytest`，它**并不是安全沙箱**。`apply_fix` 只因为我们在代码里设置了白名单和目标目录，才限制为这次教学用修改；不能把本地文件路径与真正的隔离执行环境混为一谈。

最后再提醒一次：本例 `FIXED` 为了兼容之前的 `float` 接口，将十进制计算结果转换回 `float`，只服务于当前测试，不能视为生产级金额精度策略。

## 9. `create_agent()` 已经替我们做了什么，又没有做什么？

回看最开始的 `while`，我们很容易将封装的收益说成“减少了几行代码”。但通过源码已经看到，**减少代码不是设计的根本**。

真正重要的转变是：我们把原本写在一块的执行任务，分成可以由构建器登记、连接和编译的几个明确部分。

- `create_agent()` 的**创建阶段**负责组织模型节点、工具节点、必要的路由关系，以及最终的编译配置。
- `agent.invoke(...)` 的**执行阶段**接收一次任务的输入，让编译后的执行图开始运行。
- 模型返回的工具请求仍然只是请求；工具是否真的能运行、能访问什么，取决于注册的实现与执行环境。
- 图负责执行路径的组织，**不自动赋予模型更强的代码理解或推理能力**。

这也解释了为什么我们不应该神化 `create_agent()`。如果应用只有一个确定步骤、没有继续决策的需求，直接调用模型或手写小循环都可能是更好的选择。图带来了清晰的结构与扩展入口，也带来了状态、调度、编译等新的理解成本。

还有一个暂时没有回答完的问题：`model_node` 产生的输出怎样进入后继节点读取的 `messages`？这件事不能简单解释为“框架替我们调用了 `messages.append`”。源码中已经出现 `AgentState`、节点更新与状态合并的痕迹，但要准确讨论它们，先得知道 `StateGraph` 如何把一组节点和边整理成可运行的对象。

## 10. 回看今天的推导，留下下一层问题

到此为止，我们所需的架构图仍然只有两种执行动作：

```text
START → model ─── 有 tool_calls ──→ tools ──→ model
           │
           └── 无 tool_calls ───────→ END
```

前两章负责弄清**模型请求和工具反馈是什么**；本章第一次在真实源码中看见，这条循环被拆成了节点和路由，并交给 `StateGraph` 构建、编译。

我们已经能回答：

> **`create_agent()` 不是替模型思考，也不是一调用就替我们执行任务；它把模型调用、工具执行、条件路由和状态结构组织成可执行图，返回一个之后可以 `invoke()` 的对象。**

但是现在自然出现一个新问题：**`StateGraph` 只是我们注册节点和边的地方吗？它为什么不能在调用 `add_node()` 时就立刻执行节点，而要单独调用 `compile()`？**

这就是下一章的起点：**第 04 章《一段 Agent Loop，怎样变成 StateGraph 的节点与边？》**。我们会继续沿同一条退款任务路径，用最小节点和最小路由深入图的构建入口，而不是突然抛出整个 Runtime 架构。

---

## 本章源码索引与验证边界

| 固定源码入口 | 本章对它的结论 | 下一层暂不展开的内容 |
| --- | --- | --- |
| [`create_agent()` — `factory.py:819+`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L819-L839) | 构造 Agent，返回已编译图 | 全部配置选项 |
| [`ToolNode` 创建 — `factory.py:1033+`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1033-L1059) | 注册客户端工具执行入口 | 动态工具与 Middleware |
| [`StateGraph` 创建 — `factory.py:1124+`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1124-L1141) | 组织图和状态结构 | Channel / Reducer |
| [`model_node()` — `factory.py:1368+`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1368-L1419) | 执行一次模型调用 | 中间件和结构化输出 |
| [`add_node` / `add_conditional_edges` — `factory.py:1475+`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1475-L1640) | 描述节点与执行路由 | Graph Runtime 调度 |
| [`_make_model_to_tools_edge()` — `factory.py:1829+`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1829-L1876) | 决定模型输出之后去哪里 | `Send` 详细语义 |
| [`_make_tools_to_model_edge()` — `factory.py:1903+`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1903-L1939) | 普通工具执行之后回模型 | 提前返回的分支 |
| [`compile()` — `factory.py:1754+`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1754-L1770) | 返回可执行图 | 编译内部实现 |
| [`AgentState` — `types.py:320+`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/middleware/types.py#L320-L336) | 消息属于图运行过程的数据 | 消息如何合并 |

**已实际执行**：本节的纯 Python 三个路由断言；前一章独立脚本的真实 `pytest` 失败、教学修复、再次 `pytest` 通过链路已重新执行。

**仅做源码核对与静态检查、尚未执行**：`demo_create_agent.py` 中的自定义 ChatModel、`create_agent()`、`agent.invoke()`、ToolNode 的整条框架运行链。文中的框架轨迹是预期结果，不能冒充已观察到的运行输出。
