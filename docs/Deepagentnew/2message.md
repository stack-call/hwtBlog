---
title: 第二章：Agent Loop 的通信协议
description: 从最小 Agent Loop 的普通字典出发，理解 LangChain 消息体系、ToolCall 请求、ToolMessage 反馈、工具调用关联及 DeepAgents 的实际协议使用
tags: [DeepAgents, LangChain, Agent Loop, Tool Calling, Messages, 源码精读]
sidebar_position: 2
---

# 第二章：Agent Loop 的通信协议

## 一、从上一章的最小实现开始

上一章，我们已经完成一个最小 Agent Loop。

核心代码是：

```python
while True:

    response = model(messages)

    messages.append(response)

    if not response["tool_calls"]:
        break

    for call in response["tool_calls"]:

        tool = tools[call["name"]]

        result = tool(**call["args"])

        messages.append({
            "role": "tool",
            "name": call["name"],
            "tool_call_id": call["id"],
            "content": result,
        })
```

它能够完成：

```text
读取 refund.py
    ↓
模型生成第一次修复
    ↓
修改 refund.py
    ↓
运行测试
    ↓
测试失败
    ↓
模型生成第二次修复
    ↓
再次运行测试
    ↓
测试通过
```

这个实验说明：

**只要工具结果能够进入下一次模型决策，一个简单的循环就可以完成多轮任务。**

但是，我们的实现有一个明显问题。

所有消息都是自己约定的普通字典：

```python
{
    "role": "tool",
    "name": "run_tests",
    "tool_call_id": "call-001",
    "content": "FAIL",
}
```

这套格式只有我们自己的代码知道怎样解释。

当我们接入不同模型、不同工具或更复杂的执行器时，就需要一套更加明确的通信规则。

这一章要解决的问题就是：

> **模型怎样表达行动意图？工具怎样表达执行结果？运行时怎样确定两者之间的关系？**

---

## 二、为什么不能只传递字符串？

假设模型读取了退款代码。

它返回：

```text
我需要读取 test_refund.py。
```

我们的程序可以尝试识别：

```python
if "读取" in response:
    ...
```

但这个方案不可靠。

模型还可能返回：

```text
接下来应该查看退款测试。
```

或者：

```text
建议先检查相关单元测试。
```

这些表达的含义相似，但字符串形式不同。

如果执行器需要解析自由文本，问题就会出现：

- 工具名称不明确。
- 参数可能缺失。
- 参数类型不确定。
- 模型的解释性文字可能被误判为执行指令。

所以我们需要把模型输出分成两类。

### 第一类：模型向用户表达的内容

例如：

```text
退款金额分摊存在一分钱误差。
```

这是普通消息内容。

### 第二类：模型要求系统执行的操作

例如：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "/test_refund.py"
    }
}
```

这是行动请求。

**普通回答与行动请求不能仅靠一段自然语言来区分。**

因此，LangChain 为模型输出设计了专门的消息类型。

---

## 三、先理解 Message：为什么不同来源的信息需要不同类型？

在上一章，我们使用：

```python
messages = []
```

记录所有历史。

但历史中其实存在不同来源的信息。

例如：

```text
用户：
    修复退款精度问题

模型：
    我需要查看 refund.py

工具：
    refund.py 的实际代码内容

模型：
    我发现了舍入问题
```

虽然它们最终都被模型读取，但它们的含义不同。

用户消息表示任务要求。

模型消息表示模型输出。

工具消息表示外部系统的执行反馈。

因此，LangChain 使用不同的 Message 类型。

### 3.1 真实源码：BaseMessage

文件：

`langchain_core/messages/base.py`

所有常见消息类型都继承自：

```python
BaseMessage
```

它提供一些通用字段，例如：

```python
content
id
name
additional_kwargs
response_metadata
```

其中：

```python
content
```

表示消息内容。

```python
id
```

表示这条消息本身的标识。

其他字段用于承载附加信息和元数据。

为什么需要统一的 BaseMessage？

因为 Agent Loop 每一轮都要处理一组历史消息。

如果不同消息没有统一接口，那么模型调用前就要编写大量类型转换代码。

通过统一消息类型，框架可以将它们作为一个消息序列管理。

### 3.2 常见消息类型

| 类型 | 代表什么 |
|---|---|
| `SystemMessage` | 系统级指令 |
| `HumanMessage` | 用户输入 |
| `AIMessage` | 模型输出 |
| `ToolMessage` | 工具执行结果 |

例如：

```python
from langchain_core.messages import (
    SystemMessage,
    HumanMessage,
    AIMessage,
    ToolMessage,
)

messages = [
    SystemMessage(
        content="你是一个 Coding Agent。"
    ),
    HumanMessage(
        content="修复退款精度问题。"
    ),
]
```

但请注意：

**这些类型不是四个不同的执行节点。**

它们是通信协议中的不同消息。

它们将通过 Agent Loop 在模型和工具之间传递。

---

## 四、为什么 AIMessage 不能只有 content？

现在研究最关键的模型输出。

假设模型返回：

```python
AIMessage(
    content="需要读取 refund.py"
)
```

这是普通文本。

但我们真正希望模型请求：

```python
read_file("/refund.py")
```

因此，AIMessage 必须能够携带结构化行动请求。

### 4.1 真实源码：AIMessage

打开：

`langchain_core/messages/ai.py`

找到：

```python
class AIMessage(BaseMessage):
    ...
```

其中的重要字段为：

```python
tool_calls: list[ToolCall] = Field(
    default_factory=list
)

invalid_tool_calls: list[InvalidToolCall] = Field(
    default_factory=list
)

usage_metadata: UsageMetadata | None = None
```

这是实际源码中的核心字段。

先只关注：

```python
tool_calls
```

它表示：

**模型希望调用哪些工具。**

例如：

```python
from langchain_core.messages import AIMessage

message = AIMessage(
    content="",
    tool_calls=[
        {
            "name": "read_file",
            "args": {
                "file_path": "/refund.py"
            },
            "id": "call-001",
        }
    ],
)
```

此时：

```python
message.content
```

可以是空字符串。

但是：

```python
message.tool_calls
```

包含了模型的行动请求。

### 4.2 为什么需要两个独立字段？

因为模型的输出可能同时包含：

```text
向用户解释当前操作
```

以及：

```text
要求执行某个工具
```

例如：

```python
AIMessage(
    content="我先检查退款计算代码。",
    tool_calls=[
        {
            "name": "read_file",
            "args": {
                "file_path": "/refund.py"
            },
            "id": "call-001",
        }
    ],
)
```

也可能不包含解释性文字，只包含工具调用。

因此：

```text
content
```

与：

```text
tool_calls
```

承担不同职责。

前者是消息内容。

后者是结构化的行动请求。

### 设计结论

> Agent Loop 不应该通过解析 AIMessage.content 来判断是否执行工具，而应该检查 AIMessage.tool_calls。

这也是 LangChain 构建 Agent Loop 时采用的基本原则。

---

## 五、现在进一步研究 ToolCall：为什么需要 name、args 和 id？

我们已经知道：

```python
AIMessage.tool_calls
```

保存行动请求。

但是一个请求究竟需要哪些信息？

先想最简单的情况。

模型要读取一个文件：

```python
read_file("/refund.py")
```

至少需要：

```text
工具名称
工具参数
```

因此我们可以设计：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "/refund.py"
    }
}
```

但 LangChain 的 ToolCall 还有一个字段：

```python
id
```

为什么？

先不急着回答，看看真实源码。

### 5.1 真实源码：ToolCall

文件：

`langchain_core/messages/tool.py`

核心定义：

```python
class ToolCall(TypedDict):

    name: str

    args: dict[str, Any]

    id: str | None

    type: NotRequired[Literal["tool_call"]]
```

以及构造函数：

```python
def tool_call(
    *,
    name: str,
    args: dict[str, Any],
    id: str | None,
) -> ToolCall:

    return ToolCall(
        name=name,
        args=args,
        id=id,
        type="tool_call",
    )
```

这里有三个关键字段。

### name：调用哪个工具

例如：

```python
"name": "read_file"
```

它是工具标识。

之后执行系统会使用这个名称寻找对应工具。

### args：调用工具需要什么参数

例如：

```python
"args": {
    "file_path": "/refund.py"
}
```

这些参数来自模型产生的行动请求。

注意：

```python
args
```

是一个结构化字典。

它不是原始的一段随意文本。

### id：标识哪一次调用

例如：

```python
"id": "call-001"
```

它用于关联后续的执行结果。

为什么必须能够关联？

因为模型不一定每轮只调用一个工具。

这就是下一个问题。

---

## 六、一个模型响应可能同时请求多个工具

假设模型要同时检查：

```text
refund.py
test_refund.py
```

它可能一次产生两个 Tool Call：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "read_file",
            "args": {
                "file_path": "/refund.py"
            },
            "id": "call-source",
        },
        {
            "name": "read_file",
            "args": {
                "file_path": "/test_refund.py"
            },
            "id": "call-test",
        },
    ],
)
```

现在执行器收到两个请求。

```text
                 AIMessage
                     |
            +--------+--------+
            |                 |
            v                 v
     call-source           call-test
            |                 |
            v                 v
     read refund.py    read test_refund.py
```

如果系统允许并行处理，它们的完成时间可能不同。

例如：

```text
先完成：
    test_refund.py

后完成：
    refund.py
```

因此，不能简单假设：

> 第一个返回结果一定对应第一个工具调用。

我们需要更可靠的匹配方式。

这就是：

```python
ToolCall.id
```

存在的直接原因。

### 6.1 为什么不能仅靠工具名称匹配？

因为两个调用使用的是同一个工具：

```python
read_file
```

但是参数不同：

```text
/refund.py
/test_refund.py
```

所以：

```text
工具名称
```

只能说明执行了什么类型的操作。

不能说明：

```text
这是哪一次操作？
```

### 6.2 为什么不直接用参数匹配？

因为模型可能多次使用同一组参数。

例如：

```python
read_file("/refund.py")
```

在修改前和修改后各调用一次。

两次请求的参数相同，但对应不同的执行时刻和结果。

所以还需要独立的调用标识。

---

## 七、工具结果为什么需要 ToolMessage？

现在模型已经生成：

```python
ToolCall(
    name="read_file",
    args={"file_path": "/refund.py"},
    id="call-source",
)
```

执行器真正读取文件以后得到：

```text
def calculate_refund(amount):
    return round(amount)
```

下一步需要将结果反馈给模型。

最简单的方案是添加一个 HumanMessage：

```python
HumanMessage(
    content="文件内容是 ..."
)
```

但这样会丢失一个重要事实：

**这段内容不是用户提出的新要求，而是某次工具执行的结果。**

更重要的是，它无法明确说明：

```text
对应哪个 Tool Call？
```

因此，LangChain 使用：

```python
ToolMessage
```

### 7.1 真实源码：ToolMessage

打开：

`langchain_core/messages/tool.py`

找到：

```python
class ToolMessage(
    BaseMessage,
    ToolOutputMixin,
):
    ...
```

重要字段包括：

```python
tool_call_id: str

type: Literal["tool"] = "tool"

artifact: Any = None

status: Literal["success", "error"] = "success"
```

我们先只关注：

```python
tool_call_id
```

它表示：

> 这条工具结果对应的是哪一次模型发起的 Tool Call。

例如：

```python
from langchain_core.messages import ToolMessage

message = ToolMessage(
    content=(
        "def calculate_refund(amount):\n"
        "    return round(amount)\n"
    ),
    name="read_file",
    tool_call_id="call-source",
    status="success",
)
```

这里：

```python
tool_call_id="call-source"
```

与之前的：

```python
ToolCall.id == "call-source"
```

对应。

于是形成完整关系：

```text
                AIMessage
                    |
                    v
                 ToolCall
                    |
                    | id = call-source
                    v
              Tool Execution
                    |
                    v
                ToolMessage
                    |
                    | tool_call_id = call-source
                    v
             加入消息历史
                    |
                    v
                  Model
```

### 7.2 这个协议真正解决了什么？

它让系统能够判断：

- 这次 Tool Call 是否已经有结果。
- 结果与哪个请求关联。
- 是否存在尚未完成的请求。
- 工具执行失败时，错误属于哪个请求。

这不仅是消息展示问题。

它还是 Agent Runtime 进行执行跟踪和路由所需要的数据。

---

## 八、为什么 AIMessage 和 ToolMessage 不能互相替代？

现在我们有两种消息。

```python
AIMessage(
    tool_calls=[...]
)
```

和：

```python
ToolMessage(
    tool_call_id=...,
    content=...,
)
```

它们看起来都与工具有关。

但一定要明确：

### AIMessage 表示行动意图

例如：

```text
我希望读取 refund.py。
```

它来自模型。

它不保证工具已经执行。

### ToolMessage 表示行动反馈

例如：

```text
refund.py 已读取，内容如下……
```

它来自工具执行层。

它不表示模型做出了新的决策。

因此：

```text
                  AIMessage
                      |
                      v
                   ToolCall
                      |
                      v
                 Tool Executor
                      |
                      v
                  ToolMessage
```

恰好对应上一章的：

```text
Decision
    |
    v
Action
    |
    v
Observation
```

在现代 Tool Calling Agent 中：

```text
AIMessage.tool_calls
```

承载决策产生的行动请求。

```text
ToolMessage
```

承载外部行动的观察结果。

这就是消息协议与 Agent Loop 理论之间的关系。

---

## 九、但 ToolMessage 还有两个字段：status 和 artifact

现在我们继续考虑真实 Coding Agent 中的情况。

### 9.1 如果工具执行失败了怎么办？

假设模型请求读取：

```python
read_file("/missing.py")
```

工具发现文件不存在。

那么系统可以返回：

```python
ToolMessage(
    content="Error: file not found",
    name="read_file",
    tool_call_id="call-002",
    status="error",
)
```

这里：

```python
status="error"
```

明确表示这次工具执行出现错误。

为什么需要独立的状态字段？

因为不能只靠：

```python
"Error:"
```

这样的字符串判断是否失败。

工具输出可能包含一段源码：

```python
raise RuntimeError("Error: ...")
```

这并不代表读取文件的操作失败。

所以：

```python
content
```

和：

```python
status
```

需要分开。

**content 描述工具返回了什么。**

**status 描述工具执行结果的状态。**

注意：不是所有 Python 异常都会自动转成错误 ToolMessage。是否捕获并反馈，取决于工具和执行器的错误处理配置。

### 9.2 为什么还需要 artifact？

现在假设 Agent 执行：

```python
run_tests()
```

产生了一份很大的测试报告。

工具可能得到：

```python
{
    "summary": "1 failed, 128 passed",
    "full_log": "上万行测试日志……",
}
```

模型当前只需要知道：

```text
1 failed, 128 passed
```

但其他程序逻辑可能还需要完整日志。

因此可以创建：

```python
ToolMessage(
    content="1 failed, 128 passed",
    artifact={
        "full_log": "上万行测试日志……"
    },
    tool_call_id="call-tests",
    name="run_tests",
)
```

这里：

```python
content
```

是面向模型的主要消息内容。

```python
artifact
```

用于携带不准备直接作为普通模型文本发送的附加结果。

这提供了一种将：

```text
模型需要的反馈
```

与：

```text
工具产生的完整数据
```

分开的方式。

为什么要分开？

因为真实工具结果可能非常大。

如果全部塞进模型上下文，就会产生额外 Token 消耗。

这与后续 Context Management 要研究的问题直接相关。

但 ToolMessage 本身并不会自动将大数据写入磁盘；真正的卸载与持久化仍需要相应机制。

---

## 十、工具的参数格式又是怎么告诉模型的？

到这里我们已经知道模型能输出：

```python
AIMessage.tool_calls
```

但是还有一个问题：

**模型怎么知道 `read_file` 的名称、用途和参数？**

它不应该凭空猜测。

因此，调用模型之前，需要向模型声明工具。

例如：

```text
工具名称：
    read_file

用途：
    读取文件内容

参数：
    file_path: str
```

LangChain 将这些信息组织为 Tool Schema，并通过模型的工具绑定接口提供给模型。

例如：

```python
@tool
def read_file(file_path: str) -> str:
    """Read a source file."""
    ...
```

再调用：

```python
model_with_tools = model.bind_tools(
    [read_file]
)
```

这里必须强调：

```python
bind_tools(...)
```

**不是执行工具。**

它主要是在模型调用阶段声明工具能力。

然后模型才可能生成：

```python
AIMessage(
    tool_calls=[...]
)
```

系统收到 Tool Call 后，再交给执行器。

所以完整过程是：

```text
                   Tool Definition
                         |
                         v
                      Schema
                         |
                         v
                  model.bind_tools()
                         |
                         v
                    model.invoke()
                         |
                         v
                      AIMessage
                         |
                         v
                     Tool Calls
                         |
                         v
                     Tool Executor
                         |
                         v
                      ToolMessage
```

这里又出现一个重要的职责分离：

**Tool Schema 描述可以怎样调用工具。**

**Tool Executor 负责真正执行工具。**

即使模型正确生成了参数，也不意味着它有权执行这项操作。

执行权限还需要由系统控制。

---

## 十一、真实源码：ToolCall 怎样变成 ToolMessage？

我们现在已经理解整个消息协议。

接下来需要在源码中验证：

> LangChain 是怎样将一个 ToolCall 转换成真正的 ToolMessage 的？

打开：

`langchain_core/tools/base.py`

找到：

```python
class BaseTool(...):
    ...
```

其中有：

```python
def invoke(
    self,
    input: str | dict[str, Any] | ToolCall,
    config: RunnableConfig | None = None,
    **kwargs: Any,
) -> Any:

    tool_input, kwargs = _prep_run_args(
        input,
        config,
        **kwargs,
    )

    return self.run(
        tool_input,
        **kwargs,
    )
```

注意这里：

```python
ToolCall
```

是 `invoke()` 接受的一种输入类型。

这意味着我们可以将完整的工具调用请求交给 LangChain Tool。

### 11.1 为什么传入完整 ToolCall，而不是只传 args？

比较两种写法。

第一种：

```python
read_file.invoke({
    "file_path": "/refund.py"
})
```

它只有工具参数。

第二种：

```python
read_file.invoke({
    "type": "tool_call",
    "name": "read_file",
    "args": {
        "file_path": "/refund.py"
    },
    "id": "call-001",
})
```

第二种同时携带：

```python
id
```

这让工具执行层有机会生成带有关联 ID 的 ToolMessage。

### 11.2 真实源码：_prep_run_args()

`base.py` 中：

```python
def _prep_run_args(
    value,
    config,
    **kwargs,
):
    ...
```

关键代码：

```python
if _is_tool_call(value):

    tool_call_id = value["id"]

    tool_input = value["args"].copy()

else:

    tool_call_id = None

    tool_input = value
```

这就是两个输入方式的区别。

如果直接传入参数字典：

```python
{"file_path": "/refund.py"}
```

没有 Tool Call ID。

如果传入完整 ToolCall：

```python
{
    "type": "tool_call",
    "name": "read_file",
    "args": {...},
    "id": "call-001",
}
```

系统能够取得：

```python
tool_call_id = "call-001"
```

### 11.3 真实源码：_format_output()

继续找到：

```python
def _format_output(
    content,
    artifact,
    tool_call_id,
    name,
    status,
):
    ...
```

核心逻辑：

```python
if isinstance(
    content,
    ToolOutputMixin,
) or tool_call_id is None:

    return content
```

否则构造：

```python
return ToolMessage(
    content,
    artifact=artifact,
    tool_call_id=tool_call_id,
    name=name,
    status=status,
)
```

这是一处非常关键的源码。

它说明：

**对于普通函数结果，当工具执行上下文包含 Tool Call ID 时，LangChain 可以自动将结果包装为对应的 ToolMessage。**

也就是说：

```text
             完整 ToolCall
                   |
                   v
              BaseTool.invoke
                   |
                   v
              提取 args 和 id
                   |
                   v
                执行函数
                   |
                   v
                普通结果
                   |
                   v
               _format_output
                   |
                   v
                ToolMessage
```

当然，如果工具本身已经返回 ToolMessage 等特殊结果类型，则不会简单地再次包装。

这为后续的 State 更新、Command 和其他高级工具协议留下了扩展空间。

---

## 十二、为什么还存在 invalid_tool_calls？

现在再考虑一种情况。

模型本来想生成：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "/refund.py"
    }
}
```

但 Provider 返回的原始工具参数不符合 JSON 语法。

例如：

```text
{"file_path": "/refund.py"
```

缺少结束的大括号。

这时框架不能把它当成一个正常 ToolCall 直接执行。

因此，`AIMessage` 还提供：

```python
invalid_tool_calls
```

用于记录无法正确解析的工具调用。

### 12.1 真实源码：AIMessage 的兼容解析

在：

`langchain_core/messages/ai.py`

找到：

```python
@model_validator(mode="before")
@classmethod
def _backwards_compat_tool_calls(cls, values):
    ...
```

其处理过程包含：

```python
parsed_tool_calls, parsed_invalid_tool_calls = (
    default_tool_parser(raw_tool_calls)
)
```

随后赋值：

```python
values["tool_calls"] = parsed_tool_calls

values["invalid_tool_calls"] = (
    parsed_invalid_tool_calls
)
```

这说明 LangChain 尝试将 Provider 返回的工具请求标准化。

解析成功的请求进入：

```python
tool_calls
```

解析失败的请求进入：

```python
invalid_tool_calls
```

### 为什么要区分两者？

因为：

```text
生成了合法结构
```

与：

```text
生成了无法解析的结构
```

是两种不同情况。

前者可以继续交给执行系统处理。

后者通常需要纠正、重试或专门的错误处理。

但还要区分另外一种错误：

```python
{
    "name": "read_file",
    "args": {
        "file_path": 123
    }
}
```

这在 JSON 结构上是合法的。

因此它未必属于：

```python
invalid_tool_calls
```

但其参数值可能不符合工具要求。

这类错误需要进一步的 Schema 校验。

因此我们有三个不同层次：

```text
模型输出能否解析？
        |
        v
ToolCall 参数是否满足工具 Schema？
        |
        v
系统是否允许真正执行？
```

这三个问题不能混为一谈。

---

# 十三、实验：使用真实 LangChain 消息与工具协议

现在我们不再自己定义 ToolCall 字典协议。

直接使用 LangChain Core 中的真实类：

```python
HumanMessage
AIMessage
ToolMessage
```

以及真实的：

```python
@tool
BaseTool.invoke()
```

本实验重点验证三个设计结论：

1. `AIMessage` 可以一次请求两个工具调用。
2. 每个结果通过 `tool_call_id` 与对应请求关联。
3. LangChain Tool 可以直接接收完整 ToolCall，返回对应的 ToolMessage。

与上一章不同，这次不再测试整个 Agent Loop。

我们只研究 Loop 内部的**通信协议**。

## 13.1 安装

DeepAgents 0.7.32 的 Python 环境要求是 Python 3.11 及以上。

本实验只依赖 LangChain Core：

```bash
pip install "langchain-core>=1.7.0,<2.0.0"
```

如果已经安装了：

```bash
pip install "deepagents==0.7.32"
```

通常不需要单独安装 Core。

## 13.2 实验代码

创建：

`test_tool_protocol.py`

```python
from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
)

from langchain_core.tools import tool


# ==========================================
# 1. 模拟项目文件
# ==========================================

workspace = {
    "/refund.py": (
        "def allocate_coupon(total, count):\n"
        "    return [round(float(total) / count, 2)] * count\n"
    ),
    "/test_refund.py": (
        "def test_coupon_allocation():\n"
        "    assert True\n"
    ),
}


# ==========================================
# 2. 定义真实的 LangChain Tool
# ==========================================

@tool
def read_file(file_path: str) -> str:
    """Read a source file from the demo workspace."""

    if file_path not in workspace:
        return f"File not found: {file_path}"

    return workspace[file_path]


# ==========================================
# 3. 模拟用户发起任务
# ==========================================

history = [
    HumanMessage(
        content=(
            "修复优惠券部分退款精度问题，"
            "请先检查源码和测试。"
        )
    )
]


# ==========================================
# 4. 模拟模型一次请求两个工具调用
# ==========================================

ai_message = AIMessage(
    content="",
    tool_calls=[
        {
            "name": "read_file",
            "args": {
                "file_path": "/refund.py"
            },
            "id": "call-source",
        },
        {
            "name": "read_file",
            "args": {
                "file_path": "/test_refund.py"
            },
            "id": "call-test",
        },
    ],
)

history.append(ai_message)

assert len(ai_message.tool_calls) == 2


# ==========================================
# 5. 使用完整 ToolCall 执行工具
# ==========================================

results = []

for call in ai_message.tool_calls:

    # 这里调用的是真实 LangChain BaseTool。
    # 不需要手动创建 ToolMessage。
    result = read_file.invoke(call)

    assert isinstance(result, ToolMessage)

    results.append(result)


# ==========================================
# 6. 验证 Tool Call ID 关联
# ==========================================

assert results[0].tool_call_id == "call-source"
assert results[1].tool_call_id == "call-test"

assert results[0].name == "read_file"
assert results[1].name == "read_file"

assert "allocate_coupon" in results[0].content
assert "test_coupon_allocation" in results[1].content


# ==========================================
# 7. 不依赖结果顺序，用 ID 关联
# ==========================================

# 为了验证配对方式，
# 故意打乱本地结果收集顺序。
# 这不代表建议将乱序结果直接发送给任意 Provider。

unordered_results = list(reversed(results))

result_by_id = {
    result.tool_call_id: result
    for result in unordered_results
}

assert (
    result_by_id["call-source"].content
    == workspace["/refund.py"]
)

assert (
    result_by_id["call-test"].content
    == workspace["/test_refund.py"]
)


# ==========================================
# 8. 按原请求顺序重建消息历史
# ==========================================

ordered_results = [
    result_by_id[call["id"]]
    for call in ai_message.tool_calls
]

history.extend(ordered_results)


# ==========================================
# 9. 验证所有 ToolCall 都得到反馈
# ==========================================

requested_ids = {
    call["id"]
    for call in ai_message.tool_calls
}

returned_ids = {
    result.tool_call_id
    for result in ordered_results
}

assert requested_ids == returned_ids


# ==========================================
# 10. 模拟下一次模型输出
# ==========================================

history.append(
    AIMessage(
        content=(
            "我已读取源码与测试文件。"
            "接下来需要检查金额分摊守恒问题。"
        ),
        tool_calls=[],
    )
)


# ==========================================
# 11. 打印完整消息链
# ==========================================

print("=== Message History ===")

for message in history:

    print(
        type(message).__name__,
        str(message.content)[:100],
    )

    if isinstance(message, AIMessage):
        print(
            "  tool_calls:",
            [
                call["id"]
                for call in message.tool_calls
            ],
        )

    if isinstance(message, ToolMessage):
        print(
            "  tool_call_id:",
            message.tool_call_id,
        )


print("\nTool protocol verified.")
```

## 13.3 预期结果

程序应当打印类似：

```text
=== Message History ===

HumanMessage 修复优惠券部分退款精度问题，请先检查源码和测试。

AIMessage
  tool_calls: ['call-source', 'call-test']

ToolMessage def allocate_coupon(total, count):
  tool_call_id: call-source

ToolMessage def test_coupon_allocation():
  tool_call_id: call-test

AIMessage 我已读取源码与测试文件。接下来需要检查金额分摊守恒问题。
  tool_calls: []

Tool protocol verified.
```

### 13.4 这个实验真正验证了什么？

第一个关键行为：

```python
read_file.invoke(call)
```

其中 `call` 不是普通参数字典，而是完整 ToolCall。

它包含：

```python
{
    "name": "read_file",
    "args": {...},
    "id": "...",
    "type": "tool_call",
}
```

所以 BaseTool 能够取得 Tool Call ID。

第二个关键行为：

```python
result = read_file.invoke(call)
```

能够返回：

```python
ToolMessage
```

并保留：

```python
result.tool_call_id
```

第三个关键行为：

即使本地收集结果的顺序发生变化，我们仍然可以使用：

```python
tool_call_id
```

找到正确的结果。

这就证明：

**调用 ID 是一种关联机制，不应该仅凭工具名称或结果位置判断归属。**

不过，实际传递给模型时，还必须遵守模型提供商的消息格式与排序要求。实验中最终按原请求顺序组织了 ToolMessage。

### 13.5 实验边界

本例使用真实 LangChain Tool 和 Message 类。

但是：

```python
AIMessage(...)
```

由测试程序手动创建，并不是由真实模型生成。

最后一条 AIMessage 也只是为了展示完整消息历史。

所以本实验验证的是：

**消息结构、ToolCall 执行和 ToolMessage 关联。**

它不验证真实模型是否正确选择工具，也不验证 LangGraph 的节点调度。

这正是当前章节的研究范围。

代码已根据所核对的 LangChain Core 接口编写；当前执行环境没有安装 LangChain Core，且无法联网安装，因此未能在本环境实际运行。可以在安装依赖后执行上述断言验证。

---

## 十四、再做一个思维实验：缺少 ToolMessage 会发生什么？

假设模型一次请求两个文件：

```text
call-source
call-test
```

我们只返回：

```python
ToolMessage(
    content="refund.py 的源码",
    tool_call_id="call-source",
)
```

但没有返回：

```python
ToolMessage(
    content="test_refund.py 的源码",
    tool_call_id="call-test",
)
```

那么会出现什么问题？

系统知道模型已经提出：

```text
读取 refund.py
读取 test_refund.py
```

但只看到其中一次工具执行的反馈。

此时如果再次调用模型，一些模型提供商会认为消息历史中的工具调用请求没有完整对应结果，并拒绝该请求。

因此，在正常 Tool Calling 流程中，通常需要让每个需要客户端执行的 Tool Call 都得到对应的结果或错误反馈。

例如：

```text
                   AIMessage
                       |
            +----------+----------+
            |                     |
            v                     v
        call-source            call-test
            |                     |
            v                     v
      ToolMessage A         ToolMessage B
            |                     |
            +----------+----------+
                       |
                       v
                 Next Model Call
```

### 为什么这个问题很重要？

因为 Agent Loop 不只是：

```text
工具执行结束了
    ↓
再次调用模型
```

更准确地说，应该是：

```text
模型提出行动请求
    ↓
执行器处理请求
    ↓
生成与请求相匹配的反馈
    ↓
模型在一致的消息历史上继续决策
```

当然，中断、特殊 Provider 工具、结构化输出工具和某些 Runtime 恢复路径具有额外语义。

但对于我们当前研究的普通客户端工具调用，这个匹配关系是必须理解的基础。

---

## 十五、DeepAgents 怎样实际使用这套消息协议？

现在，我们已经理解 LangChain Core 的：

```text
AIMessage
ToolCall
ToolMessage
```

接下来回到真正的 DeepAgents 源码。

固定版本：

```text
DeepAgents 0.7.32
Commit e85c6b9
```

我们在第一章已经知道：

```python
create_deep_agent(...)
```

最终调用：

```python
create_agent(...)
```

从而进入 LangChain 的 Agent Loop。

但 DeepAgents 自身也大量依赖这套消息协议。

最直接的例子是：

```python
read_file
```

### 15.1 真实源码：创建 read_file 工具

打开：

`deepagents/middleware/filesystem.py`

找到：

```python
def _create_read_file_tool(
    self,
) -> BaseTool:
    ...
```

方法内部定义真实的同步读取函数：

```python
def sync_read_file(
    file_path: str,
    runtime: ToolRuntime[None, FilesystemState],
    offset: int = DEFAULT_READ_OFFSET,
    limit: int = DEFAULT_READ_LIMIT,
) -> ToolMessage | Command:
    ...
```

这里先关注返回类型：

```python
ToolMessage | Command
```

为什么？

因为读文件后的结果，不仅可能是一段普通文本，还可能涉及特殊的状态更新处理。

`Command` 属于后面研究 LangGraph State 时才需要深入的机制。

本章先只看普通 ToolMessage。

### 15.2 当文件读取失败

真实代码中：

```python
if read_result.error:

    return ToolMessage(
        content=f"Error: {read_result.error}",
        name="read_file",
        tool_call_id=tool_call_id,
        status="error",
    )
```

这里你会发现，它与我们刚才研究的协议完全一致。

```python
name="read_file"
```

说明来自哪个工具。

```python
tool_call_id=tool_call_id
```

关联模型提出的 Tool Call。

```python
status="error"
```

表示执行失败。

### 15.3 当文件读取成功

源码继续根据：

```text
文件类型
文件内容
分页范围
文本或多模态结果
```

决定如何构造消息内容。

然后返回 ToolMessage。

这里的实现可能很复杂，但本质职责没有改变：

**将 Backend 的文件操作结果，转换成 Agent Loop 能理解的工具反馈消息。**

所以：

```text
                    Model
                      |
                      v
                   AIMessage
                      |
                      v
                  ToolCall
                      |
                      v
                  read_file
                      |
                      v
                 backend.read()
                      |
                      v
                  ReadResult
                      |
                      v
                  ToolMessage
                      |
                      v
                    Model
```

这条关系不会因为 Backend 换成 StateBackend 或 FilesystemBackend 就改变。

因为：

**Backend 负责实际数据访问，ToolMessage 负责 Agent Loop 的通信协议。**

这就是 DeepAgents 为什么复用 LangChain Core Message 类型的一个重要原因。

---

## 十六、LangChain create_agent 又怎样依赖这套协议？

现在我们已经追踪到工具生成 ToolMessage。

但还有一个问题：

**LangChain 怎么知道某一次模型输出以后，应该执行工具还是结束循环？**

先不研究 Graph 具体怎样构建。

只看路由判断所依赖的数据。

打开：

`langchain/agents/factory.py`

找到：

```python
_make_model_to_tools_edge(...)
```

其中一个关键判断是：

```python
if len(last_ai_message.tool_calls) == 0:
    return end_destination
```

这说明：

**Agent Loop 的正常结束判断，依赖结构化的 tool_calls，而不是解析 AIMessage.content 的自然语言。**

如果存在 Tool Call，源码继续找出尚未收到结果的调用：

```python
pending_tool_calls = [
    c
    for c in last_ai_message.tool_calls
    if c["id"] not in tool_message_ids
    and c["name"] not in structured_output_tools
]
```

注意这里：

```python
c["id"] not in tool_message_ids
```

它刚好对应我们前面研究的：

```python
ToolCall.id
```

与：

```python
ToolMessage.tool_call_id
```

的关联。

为什么这样设计？

因为有些 Tool Call 可能已经有对应的 ToolMessage。

运行时需要判断：

```text
哪些调用仍然等待执行？
```

而不是简单地重新执行最新 AIMessage 中的所有工具请求。

于是整个关系变成：

```text
                  AIMessage
                      |
                      v
                tool_calls
                      |
                      v
            检查已存在的 ToolMessage
                      |
                      v
               pending_tool_calls
                      |
             +--------+--------+
             |                 |
             v                 v
          有待执行            无待执行
             |                 |
             v                 v
         进入 ToolNode       继续或结束
```

这里还是简化后的执行关系。

具体是否结束，还可能受到结构化输出和 Middleware 路由的影响。

但是核心结论已经足够清楚：

**消息协议不只是供模型阅读，它也直接参与 Agent Runtime 的控制流判断。**

这正是从普通消息系统发展到 Agent 通信协议的关键区别。

---

## 十七、需要特别区分的三个 ID

读源码时还有一个非常容易混淆的地方。

考虑：

```python
AIMessage(
    id="ai-message-001",
    tool_calls=[
        {
            "name": "read_file",
            "args": {
                "file_path": "/refund.py"
            },
            "id": "call-001",
        }
    ],
)
```

这里有两个 ID。

第一个：

```python
AIMessage.id
```

代表消息本身的 ID。

第二个：

```python
AIMessage.tool_calls[0]["id"]
```

代表这次 Tool Call 的 ID。

工具返回：

```python
ToolMessage(
    id="tool-message-001",
    content="文件内容",
    tool_call_id="call-001",
    name="read_file",
)
```

这里又有两个 ID。

```python
ToolMessage.id
```

是工具结果消息本身的 ID。

```python
ToolMessage.tool_call_id
```

是它所响应的工具请求 ID。

所以：

```text
AIMessage
    |
    +-- id = ai-message-001
    |
    +-- ToolCall
            |
            +-- id = call-001
                    |
                    v
                ToolMessage
                    |
                    +-- id = tool-message-001
                    |
                    +-- tool_call_id = call-001
```

它们承担不同职责。

**Message ID 用于标识和管理消息。**

**Tool Call ID 用于关联行动请求与执行反馈。**

这两个概念在后面研究 State Reducer 时都会用到，但不要现在就混为一谈。

---

## 十八、Tool Calling 协议能解决什么，不能解决什么？

现在我们已经完整研究了通信层。

但是要注意：

**结构化 Tool Calling 并不自动让 Agent 安全、正确或可靠。**

例如：

```python
AIMessage(
    tool_calls=[
        {
            "name": "execute",
            "args": {
                "command": "危险命令"
            },
            "id": "call-danger",
        }
    ],
)
```

从消息协议的角度看，这可能是一个结构合法的请求。

但它是否应该执行，是另外一个问题。

我们应该明确区分三层。

### 第一层：通信协议

回答：

> 模型请求了什么？工具返回了什么？两者如何匹配？

相关对象：

```text
AIMessage
ToolCall
ToolMessage
```

### 第二层：工具执行

回答：

> 哪个 Python 函数负责执行？参数是否合法？怎样处理失败？

相关机制：

```text
BaseTool
ToolNode
ToolRuntime
```

### 第三层：运行控制

回答：

> 这次操作是否允许？执行失败是否继续？下一步去哪个节点？

相关机制：

```text
Agent Loop
Middleware
Graph Routing
Backend
HITL
```

这三个层次彼此协作，但职责不同。

因此，不能因为理解了：

```python
AIMessage.tool_calls
```

就认为已经理解了 ToolNode 的执行机制。

也不能因为知道：

```python
ToolMessage.status
```

就认为 Agent 可以自动正确恢复所有错误。

这些问题需要后续章节继续解决。

---

## 十九、本章完整调用关系

现在我们已经从普通字典出发，推导出了真实通信协议。

可以把整个过程总结为：

```text
                      User Task
                          |
                          v
                    HumanMessage
                          |
                          v
                      Model
                          |
                          v
                     AIMessage
                          |
              +-----------+-----------+
              |                       |
              v                       v
           content                 tool_calls
              |                       |
              |                       v
              |                    ToolCall
              |                       |
              |                +------+------+
              |                |      |      |
              |                v      v      v
              |              name    args    id
              |                |      |      |
              |                +------+------+
              |                       |
              |                       v
              |                 Tool Executor
              |                       |
              |                       v
              |                    ToolMessage
              |                       |
              |             +---------+---------+
              |             |         |         |
              |             v         v         v
              |          content    status  tool_call_id
              |             |         |         |
              |             +---------+---------+
              |                       |
              +-----------+-----------+
                          |
                          v
                    Message History
                          |
                          v
                         Model
```

这张图对应的源码责任是：

```text
LangChain Core
    |
    +-- 定义 Message 类型
    |
    +-- 定义 ToolCall 结构
    |
    +-- 定义 BaseTool
    |
    +-- 管理 ToolMessage 的格式


LangChain Agent
    |
    +-- 读取模型消息
    |
    +-- 判断是否存在 Tool Call
    |
    +-- 组织工具路由


LangGraph
    |
    +-- 调度工具执行
    |
    +-- 处理工具返回结果
    |
    +-- 将结果交回 Agent Loop


DeepAgents
    |
    +-- 提供具体工具
    |
    +-- 将 Backend 结果转换成 ToolMessage
    |
    +-- 利用 Middleware 扩展行为
```

本章先把第一层研究清楚。

后面再逐层进入执行与调度。

---

## 二十、本章设计取舍

我们最后从架构角度看一下这套协议。

| 设计 | 解决的问题 | 代价 |
|---|---|---|
| 区分 HumanMessage / AIMessage / ToolMessage | 消息来源与语义不同 | 需要统一类型与序列化 |
| AIMessage.tool_calls | 不再从自由文本中猜测行动 | 模型必须支持或适配结构化工具调用 |
| ToolCall.name | 明确工具目标 | 需要维护工具注册关系 |
| ToolCall.args | 明确参数结构 | 需要额外验证参数 |
| ToolCall.id | 关联并发或重复请求 | 必须维护调用标识 |
| ToolMessage.tool_call_id | 关联执行结果与行动请求 | 必须处理缺失或重复反馈 |
| ToolMessage.status | 区分执行成功与失败 | 需要一致的错误处理约定 |
| ToolMessage.artifact | 分开模型可见内容与完整工件 | 需要管理附加数据的生命周期 |
| invalid_tool_calls | 区分解析失败与合法 Tool Call | 需要纠错或重试策略 |

这些结构并不是为了让消息类型更复杂。

它们是在解决 Agent Loop 必然遇到的几个问题：

**如何准确表达行动？**

**如何可靠关联反馈？**

**如何区分正常结果与失败？**

**如何让运行时基于消息做出明确的调度决策？**

---

## 二十一、本章总结

我们从上一章的最小代码出发：

```python
{
    "role": "tool",
    "name": "...",
    "tool_call_id": "...",
    "content": "...",
}
```

首先发现，纯字符串不能可靠地表达行动。

所以需要：

```python
AIMessage.tool_calls
```

然后发现，模型可能同时调用多个工具。

所以需要：

```python
ToolCall.id
```

接着发现，执行结果必须与请求关联。

所以需要：

```python
ToolMessage.tool_call_id
```

再进一步，工具可能失败，结果可能非常大。

所以需要：

```python
ToolMessage.status
ToolMessage.artifact
```

最后，我们发现这些消息不只是模型通信的数据，还会被 Agent Runtime 用于判断是否需要执行工具、哪些调用尚未完成、何时结束。

因此，本章最重要的结论是：

> **Agent Loop 的通信协议，将模型输出的行动意图与工具产生的执行反馈转换成结构化、可关联、可供 Runtime 判断的数据，从而支撑可靠的多轮决策与执行。**

---

# 下一章：为什么 Agent Loop 要从 while 循环演化为 StateGraph？

现在我们已经知道：

```text
Model
    |
    v
AIMessage.tool_calls
    |
    v
ToolMessage
    |
    v
Model
```

但上一章自己编写的 Loop 仍然使用：

```python
while True:
    ...
```

这就留下了下一章要解决的问题：

> **既然一个普通 while 循环已经能够运行 Agent，LangChain 为什么还要将 Model 和 Tools 拆成 Graph 节点，并交给 LangGraph Runtime 调度？**

我们将从最小 `while` 循环的工程限制出发，逐步推导：

```text
普通 while Loop
       |
       v
独立 Model / Tools 节点
       |
       v
条件路由
       |
       v
StateGraph
       |
       v
LangGraph Runtime
```

再研究：

```python
StateGraph(...)
```

```python
graph.add_node(...)
```

```python
graph.add_conditional_edges(...)
```

这些 API 为什么存在，以及它们如何真正实现一个 Agent Loop。

---

## 本章源码导航

### 1. LangChain Core：消息与 ToolCall

`libs/core/langchain_core/messages/`

重点：

```text
base.py
ai.py
tool.py
```

建议阅读：

```text
BaseMessage
    |
    v
AIMessage.tool_calls
    |
    v
ToolCall
    |
    v
ToolMessage
```

源码：

https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/messages/ai.py

https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/messages/tool.py

### 2. LangChain Core：Tool 的输出包装

`libs/core/langchain_core/tools/base.py`

重点：

```text
BaseTool.invoke()
    |
    v
_prep_run_args()
    |
    v
BaseTool.run()
    |
    v
_format_output()
    |
    v
ToolMessage
```

源码：

https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/tools/base.py

### 3. LangChain：消息如何参与 Agent Loop 路由

`libs/langchain_v1/langchain/agents/factory.py`

本章只需要定位：

```python
_make_model_to_tools_edge(...)
```

以及：

```python
last_ai_message.tool_calls
```

暂时不深入 Graph 编译。

源码：

https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py

### 4. DeepAgents：真实 ToolMessage

固定 DeepAgents 版本：

```text
0.7.32
Commit e85c6b9
```

文件：

```text
libs/deepagents/deepagents/middleware/filesystem.py
```

重点：

```text
_create_read_file_tool()
    |
    v
sync_read_file()
    |
    v
backend.read()
    |
    v
_handle_read_result()
    |
    v
ToolMessage
```

源码：

https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py

注意：DeepAgents 使用固定 Commit；此处 LangChain / LangChain Core 源码链接指向主分支。正式逐行调试时，应以实际安装的兼容依赖版本为准。

---

## 本章自检

读完本章，应当能够回答：

1. 为什么不能从 AIMessage.content 中直接解析工具调用？
2. AIMessage.tool_calls 的作用是什么？
3. ToolCall 为什么需要独立 ID？
4. ToolMessage 与普通 HumanMessage 有什么区别？
5. 为什么 ToolMessage 需要 tool_call_id？
6. 两个工具同时执行时，怎样对应执行结果？
7. ToolMessage.status 和 content 分别表达什么？
8. invalid_tool_calls 与工具参数校验失败有什么区别？
9. bind_tools 为什么不等于执行工具？
10. LangChain Agent 为什么要根据 tool_calls 进行路由？

这些问题全部清楚以后，就具备了继续阅读 LangGraph Agent Loop 的数据协议基础。
