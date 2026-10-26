---
title: 第十六章：Model Adapter——更换模型后，Agent 为什么坏了？
sidebar_label: 第十六章：Model Adapter
description: 从多模型输出差异推导统一消息、工具事件、流式解析与能力约束
sidebar_position: 17
tags: [Agent, Harness, Model-Adapter, OpenCode, Codex]
---



# 第十六章：Model Adapter——为什么不能直接调用模型 API？

前面，我们已经实现了一个功能比较完整的 Coding Agent。

它能够：

- 通过 Agent Loop 连续执行任务。
- 通过 Tool System 调用文件、Shell 等工具。
- 通过 Context Manager 管理模型上下文。
- 通过 Permission System 控制危险操作。
- 在需要时暂停执行，等待用户输入。

但我们的实现有一个隐含前提：

**整个 Agent Harness 一直使用同一种模型 API。**

直到有一天，团队要求同时支持多个模型。

## 一、第一次事故：换了模型，Agent Loop 为什么就不能运行了？

我们的 Coding Agent 正在修复优惠券退款 Bug。

最初，它使用某个模型提供商的工具调用接口。

模型返回：

```json
{
  "tool_calls": [
    {
      "id": "call_001",
      "type": "function",
      "function": {
        "name": "read_file",
        "arguments": "{\"path\":\"refund.py\"}"
      }
    }
  ]
}
```

我们的 Agent Loop 根据这个格式编写：

```python
response = await client.chat(messages)

for call in response.tool_calls:
    name = call.function.name
    args = json.loads(call.function.arguments)

    result = await execute_tool(name, args)
```

这段代码工作得很好。

后来，小李想换另一家 Provider。

但新 Provider 使用不同的返回结构，工具调用可能表示为：

```json
{
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_001",
      "name": "read_file",
      "input": {
        "path": "refund.py"
      }
    }
  ]
}
```

这里是不同 API 风格的简化示例，省略了其他响应字段。

注意区别：

原来的格式通过：

```text
response.tool_calls
```

访问工具调用。

新格式则通过：

```text
response.content
```

中的 `tool_use` 块表示工具调用。

而且原来 `arguments` 是 JSON 字符串，新格式中的 `input` 是结构化对象。

结果，我们之前写的：

```python
response.tool_calls
```

直接失效了。

小李发现：

**虽然两个模型都支持 Tool Calling，但它们对同一种能力的表示方式并不相同。**

难道每增加一家模型 Provider，都要修改 Agent Loop？

显然不应该。

## 二、第一次改进：让 Agent Loop 不认识任何 Provider 的格式

小李重新思考：

Agent Loop 真正关心的是什么？

它其实不关心模型使用 JSON 字符串，还是结构化 `tool_use` 对象。

Agent Loop 只关心：

```text
模型想调用哪个工具？
传入什么参数？
这次调用的 ID 是什么？
```

那么我们能不能在模型响应与 Agent Loop 之间加一层转换？

```text
Provider A Response
         |
         v
    Model Adapter
         |
         v
    Unified Response
         |
         v
      Agent Loop
```

另一家 Provider 也是一样：

```text
Provider B Response
         |
         v
    Model Adapter
         |
         v
    Unified Response
         |
         v
      Agent Loop
```

这样，Agent Loop 只需要理解一种内部结构。

例如，我们自己定义：

```python
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall]
```

这是教学设计，不是某个真实框架的完整类型。

对于 Provider A：

```python
def normalize_provider_a(response):
    ...
```

对于 Provider B：

```python
def normalize_provider_b(response):
    ...
```

它们最终都返回：

```python
ModelResponse(
    text="",
    tool_calls=[
        ToolCall(
            id="call_001",
            name="read_file",
            arguments={"path": "refund.py"}
        )
    ]
)
```

现在，Agent Loop 不需要修改：

```python
response = await model.generate(messages)

for call in response.tool_calls:
    await execute_tool(call)
```

我们直到这里，才引入第一个核心概念：

**Model Adapter（模型适配层）**。

它最基本的职责，就是屏蔽不同模型 API 的差异。

但事情还没有结束。

因为我们刚才只考虑了模型输出，没有考虑模型输入。

---

## 三、第二次事故：为什么输出统一了，历史消息却发不出去？

我们的 Agent 已经执行了很多轮。

内部 History 是这样的：

```text
User：
    修复退款 Bug

Assistant：
    调用 read_file("refund.py")

Tool：
    返回 refund.py 的源码

Assistant：
    调用 execute("pytest")

Tool：
    返回测试失败结果
```

现在，我们把这一段历史发送给另一个 Provider。

请求却被拒绝。

为什么？

因为模型之间不一定采用相同的消息协议。

例如，一种 API 允许：

```json
{
  "role": "tool",
  "tool_call_id": "call_001",
  "content": "文件内容"
}
```

另一种 API 则可能要求把工具结果放到特定的内容块中：

```json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_001",
      "content": "文件内容"
    }
  ]
}
```

它们在语义上都是：

> 之前的工具调用已经执行，现在返回工具结果。

但序列化格式不同。

于是我们发现：

**Model Adapter 不仅需要处理模型输出，还需要转换输入消息。**

小李重新设计架构：

```text
                Agent Harness
                     |
                     v
              Internal Messages
                     |
                     v
               Model Adapter
                     |
            +--------+--------+
            |                 |
            v                 v
       Provider A         Provider B
        Request            Request
            |                 |
            v                 v
        Model A             Model B
            |                 |
            v                 v
       Provider A         Provider B
        Response           Response
            |                 |
            +--------+--------+
                     |
                     v
               Model Adapter
                     |
                     v
              Internal Events
                     |
                     v
                 Agent Loop
```

从此，Harness 内部保存的是统一语义，而不是某个 Provider 的原始消息格式。

### 内部消息应该表达什么？

最少需要：

```text
System Message
User Message
Assistant Message
Tool Call
Tool Result
```

如果要支持复杂模型，还可能需要：

```text
Image / Audio / File Content
Reasoning Metadata
Provider-specific Metadata
Token Usage
Response ID
```

但这里有一个非常重要的原则：

**统一内部格式，不代表所有 Provider 的信息都能够无损互相转换。**

例如某些模型返回经过签名、加密或绑定上下文的推理数据。

这些数据可能只能原样传回原 Provider，不能直接转给其他模型。

因此成熟 Adapter 不只是转换，还必须知道哪些信息可以转换，哪些必须保留，哪些根本不能迁移。

---

## 四、源码对照：DeepAgents 为什么能够接受不同模型？

现在我们去看 DeepAgents。

源码：

[DeepAgents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)

`create_deep_agent()` 的 `model` 参数支持：

```python
model: str | BaseChatModel | None = None
```

注意这里最重要的类型：

```python
BaseChatModel
```

DeepAgents 没有要求调用者必须提供某一家 Provider 的 SDK Client。

它主要复用 LangChain 的模型抽象。

### 4.1 BaseChatModel 解决了什么？

LangChain 的核心源码：

[BaseChatModel](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/chat_models.py)

其中可以看到：

```python
class BaseChatModel(BaseLanguageModel[AIMessage], ABC):
    ...
```

它向上提供统一的模型操作，例如：

```python
model.invoke(messages)
model.stream(messages)
model.ainvoke(messages)
```

不同 Provider 通过具体的 ChatModel 实现来接入。

例如：

```python
from langchain.chat_models import init_chat_model

model = init_chat_model("openai:your-model-id")
```

也可以通过支持的其他 Provider 创建对应模型实例。

然后交给：

```python
from deepagents import create_deep_agent

agent = create_deep_agent(model=model)
```

这里的 `your-model-id` 是占位符，需要替换为实际可用模型名称。

### 4.2 为什么这能够减少 Harness 的复杂度？

如果没有统一模型接口：

```text
DeepAgents
    |
    +--> OpenAI API 特殊处理
    |
    +--> Anthropic API 特殊处理
    |
    +--> Google API 特殊处理
```

有了 LangChain：

```text
DeepAgents
    |
    v
BaseChatModel
    |
    +--> ChatOpenAI
    |
    +--> ChatAnthropic
    |
    +--> 其他 ChatModel
```

DeepAgents 主要依赖：

```python
BaseChatModel
```

而 Provider-specific 的消息序列化、响应转换和模型调用，由底层集成负责。

这个设计体现了依赖倒置：

**Agent Harness 依赖稳定的模型能力抽象，而不是依赖某一家模型 API 的具体实现。**

不过这不意味着切换模型一定无缝。

不同模型是否支持 Tool Calling、结构化输出、图像和特定推理参数，仍然需要在运行时处理。

---

## 五、第三次事故：为什么统一了响应格式，流式输出仍然出问题？

现在小李的 Coding Agent 已经能够切换模型。

但产品要求：

> 模型生成内容时，前端必须能够实时显示，同时工具调用也要能够流式处理。

于是小李开始使用 Stream API。

模型可能依次返回：

```text
Event 1：Text Delta
Event 2：Text Delta
Event 3：Tool Call Start
Event 4：Tool Arguments Delta
Event 5：Tool Arguments Delta
Event 6：Tool Call Complete
Event 7：Response Complete
```

假设模型正在调用：

```json
{
  "name": "read_file",
  "arguments": {
    "path": "refund.py"
  }
}
```

但参数并不是一次性返回的。

实际事件可能依次包含：

```text
Chunk 1: {"path":
Chunk 2: "refund
Chunk 3: .py"}
```

如果我们的 Harness 一收到第一个片段就调用：

```python
json.loads(chunk)
```

就会解析失败。

更严重的是，如果 Harness 在参数尚未完整、尚未校验时就执行工具，可能执行错误操作。

### 第三次改进：区分 Stream Delta 与完整消息

小李意识到：

**流式事件不是最终模型响应。**

一次响应可以不断追加增量。

但只有当某个输出项真正完成，才可以按照完整结构处理它。

于是我们引入一个 Stream Assembler：

```text
Provider Stream
      |
      v
Stream Normalizer
      |
      v
Partial Events
      |
      v
Stream Assembler
      |
      +--> 更新 UI
      |
      +--> 组装 Tool Call
      |
      v
Completed Tool Call
      |
      v
Validation
      |
      v
Tool Runtime
```

这里有两个很容易混淆的概念。

**Text Delta**

例如：

```text
"我发现"
"退款公式"
"存在问题"
```

可以逐步展示给用户。

**Tool Call Delta**

例如：

```text
{"path":
"refund.py"}
```

需要先组装成合法、完整的工具参数。

不能把这两者都当作普通文字处理。

### 我们需要哪些统一事件？

可以设计：

```text
TextDelta
ToolCallStarted
ToolCallCompleted
UsageReported
ResponseCompleted
ModelError
```

内部还可能保留工具参数增量、推理事件等。

这套事件类型是教学抽象，实际框架会有更丰富的状态。

其目的不是把所有 Provider 的事件名称变得一样，而是让上层 Agent Loop 和 UI 能够处理稳定的业务语义。

---

## 六、源码对照：Codex 为什么需要 ResponseEvent？

现在看 Codex。

源码：

[Codex client.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/client.rs)

Codex 的核心模型客户端称为：

```rust
ModelClient
```

它还会为 Turn 创建：

```rust
ModelClientSession
```

当前源码中，`ModelClient` 持有与 Session 相关的配置状态，例如模型 Provider、认证和部分传输状态。

`ModelClientSession` 则处理当前 Turn 的模型请求和连接复用。

为什么要这样划分？

因为不是所有模型调用状态都具有相同的生命周期。

例如：

```text
Session 生命周期：
    Provider
    Authentication
    Conversation State

Turn 生命周期：
    Model Settings
    Streaming Request
    Transport State
```

这些状态不应该全部塞进每次模型 API 请求参数中。

### 6.1 Codex 的统一响应事件

Codex 的模型响应流使用 `ResponseEvent`。

在 `client.rs` 中，可以看到处理：

```rust
ResponseEvent::OutputItemDone(item)
```

以及：

```rust
ResponseEvent::Completed {
    response_id,
    token_usage,
    usage_metadata,
    end_turn,
}
```

这两种事件非常重要。

**OutputItemDone**

表示模型的某一个输出项已经完成。

该输出项可能是文本或工具调用等结构化内容。

**Completed**

表示本次模型响应流完成，并携带响应 ID、Token 使用情况等信息。

注意：

**一个模型请求完成，不等于整个 Agent Turn 完成。**

如果输出项中包含需要执行的工具调用，Agent Loop 仍然可能继续下一次模型请求。

这正好联系了第十章研究的 Agent Loop。

### 6.2 为什么需要中间的 ResponseEvent？

因为如果 Agent Loop 直接依赖底层 HTTP 或 WebSocket 消息，就必须理解：

```text
HTTP Response
SSE Event
WebSocket Event
Provider Output Item
```

之间的细节差异。

通过内部的响应事件，Agent Loop 不必直接处理每个传输事件的原始格式。

所以这里存在两个不同层次：

```text
Transport Event
       |
       v
Model Response Event
       |
       v
Agent Loop Decision
```

Codex 的 ModelClient 比最简单的 Provider Adapter 更宽一些，还管理认证恢复、HTTP/WebSocket 切换和请求路由等运行时细节。

因此不能简单认为它只是一个 JSON 转换器。

---

## 七、第四次事故：为什么某些模型支持工具，某些模型不支持？

现在 Coding Agent 支持多个模型。

用户选择了一个价格较低的模型。

但运行时发现：

它不支持项目需要的某些 Tool Calling 能力。

如果 Harness 仍然把完整工具列表发过去，就可能请求失败。

甚至有的模型虽然支持工具调用，但不支持并行工具调用，或对 Tool Schema 的约束不同。

小李意识到：

**统一 API，并不意味着底层模型具有完全一致的能力。**

于是需要为模型维护能力信息。

例如：

```python
@dataclass
class ModelCapabilities:
    tool_calling: bool
    parallel_tool_calls: bool
    structured_output: bool
    vision: bool
    context_window: int | None
```

这是教学设计，真实能力信息可能更复杂，也可能依赖 Provider 配置与当前 API 模式。

### 运行前为什么需要检查能力？

假设当前任务要求：

```text
读取代码
修改文件
执行测试
```

Agent Loop 依赖模型能够可靠地请求工具。

如果用户选择的模型不支持 Tool Calling，那么需要：

- 拒绝以当前配置启动任务。
- 选择另一种兼容的模型。
- 或显式使用其他受支持的调用机制。

而不是假装所有模型都能完成相同操作。

这就是 **Capability Negotiation（能力协商）**。

它决定：

> 当前模型的能力是否满足本次 Agent 执行的需求？

### 能力信息也会影响 Context Manager

假设：

```text
Model A Context Window = 128k
Model B Context Window = 32k
```

同一个 Agent Session 从 A 切换到 B。

此前能够完整发送的 History，现在可能超出模型窗口。

因此必须重新计算上下文预算，必要时执行 Compaction。

这说明 Model Adapter 不只是影响模型请求格式，还会影响：

- Context Budget。
- Tool Exposure。
- Output Token Reservation。
- 多模态输入。
- Reasoning 配置。

但能力适配与内容压缩仍然应该分开：

**Model Adapter 提供模型能力与格式信息，Context Manager 根据这些信息决定如何构建输入。**

---

## 八、源码对照：OpenCode 如何处理多 Provider？

OpenCode 比较适合研究多模型支持，因为它明确支持多种模型 Provider。

当前仓库 V2 的重要入口是：

- [packages/core/src/provider.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/provider.ts)
- [packages/core/src/session.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session.ts)
- [packages/core/src/session/model-transport.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/model-transport.ts)

注意：OpenCode 仓库同时存在不同代际的实现。V1 的 `packages/opencode/src/provider/transform.ts` 也很值得读，但不要把 V1 和 V2 的类与函数误认为同一个调用链。

### 8.1 Provider 层负责选择模型

OpenCode 的 Provider 层会管理 Provider 配置、模型发现和模型解析。

例如模型可以通过：

```text
providerID
modelID
```

来定位。

为什么不能只使用一个模型字符串？

因为相同的模型系列可能通过不同的 Provider、Gateway 或部署渠道访问。

例如：

```text
Provider A / Model X
Provider B / Model X
```

虽然逻辑模型名称可能接近，但认证、Endpoint、支持的参数、服务能力却可能不同。

因此：

**Model Identity 与 Provider Identity 应该分开管理。**

### 8.2 Session 为什么不应该直接构造 Provider 请求？

在当前 OpenCode V2 的 `session.ts` 中，可以看到：

```typescript
const llm = yield* LLMClient.Service
const transport = yield* SessionModelTransport.Service
```

这是实际存在的服务依赖获取方式。

它表明 Session 层不是简单地直接写死某个模型 SDK，而是从运行时获得模型客户端与传输服务。

可以把逻辑关系理解成：

```text
Session Runtime
       |
       v
Model Selection
       |
       v
Provider Configuration
       |
       v
LLM Client / Transport
       |
       v
Model Response
```

其中一部分配置和 Provider 信息由独立服务管理。

不同版本的实现细节可能变化，但责任分离的意义不变：

**Session 管理任务和会话，Provider 层管理如何定位和使用模型。**

### 8.3 OpenCode 如何处理 Provider-specific 差异？

OpenCode V1 中的：

[provider/transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts)

提供了很直观的案例。

它会针对具体模型和 Provider 处理消息转换，例如：

- 特定模型的 Tool Call ID 约束。
- Reasoning 数据的表示差异。
- Provider-specific 消息顺序要求。
- Prompt Cache 相关配置。
- Temperature、Top P、Reasoning Effort 差异。

举个例子：

某些 Provider 对 Tool Call ID 的字符集和长度有特殊要求。

如果内部 Tool Call ID 是：

```text
call:refund/read-file/001
```

目标 Provider 可能不接受这个格式。

Adapter 就需要按照目标 API 的规则转换标识。

但这里不能随便丢掉关联关系。

必须保证：

```text
Tool Call ID
     |
     v
Tool Result ID
```

仍然能够正确配对。

如果只修改 Tool Call 的 ID，而没有同步修改对应 Tool Result，就可能破坏整个消息序列。

这正是 Provider Adapter 中一种典型的复杂性：

**不仅要让消息格式合法，还要保持消息之间的语义关系。**

---

## 九、第五次事故：模型 API 调用失败，要不要自动重试？

现在 Coding Agent 已经可以调用多个 Provider。

某次请求返回：

```text
HTTP 429
Rate Limit Exceeded
```

小李想：

“既然这只是暂时的限制，等一下再调用不就好了？”

于是他增加重试。

但是很快遇到另一种情况：

```text
HTTP 400
Invalid Tool Schema
```

如果我们仍然自动重试十次，显然不会解决问题。

于是我们必须区分错误类型。

| 错误 | 典型处理策略 |
|---|---|
| Rate Limit | 尊重 Retry-After，有限退避重试 |
| 临时网络错误 | 有条件重试 |
| 服务端临时异常 | 有界重试或故障转移 |
| 参数格式错误 | 修正请求，不盲目重试 |
| 模型不支持某能力 | 调整模型或功能配置 |
| 认证失败 | 按认证机制刷新或重新授权 |
| 用户取消 | 停止执行并传播取消 |

这里引出了：

**Model Error Normalization（模型错误归一化）**。

Harness 不应该对所有异常都执行同一种操作。

更合理的是让 Model Adapter 把 Provider 错误映射成系统内部可判断的类型。

例如：

```text
RateLimitError
AuthenticationError
InvalidRequestError
UnsupportedCapabilityError
TransportError
```

这是一套建议的内部分类，不是统一行业标准。

### 为什么重试还需要区分模型调用和工具执行？

模型请求重试与工具执行重试的风险不同。

重复一次模型请求可能增加费用，或产生不同输出。

而重复执行：

```text
delete_order(...)
```

可能产生真实业务副作用。

因此 Model Adapter 的重试策略不能代替 Tool Runtime 的幂等策略。

尤其是流式响应中断时，要谨慎处理。

假设模型已经流式返回了一个不完整的工具调用：

```text
execute("python ...
```

这时网络连接断开。

Harness 不应该把这个未完成工具请求当成合法 Tool Call 执行。

更安全的策略是：

**只有当模型输出项满足当前协议的完整性与验证要求时，才能进入工具执行阶段。**

---

## 十、第六次问题：模型切换后，之前的 Reasoning 和 KV Cache 怎么办？

小李又遇到一个场景。

同一个 Coding Agent Session：

```text
前 20 轮：Model A
后 20 轮：Model B
```

用户希望能够在不中断任务的情况下切换模型。

那么此前的 History 能不能直接继续使用？

答案是：

**语义上可以尝试延续，但不能假设所有底层状态都能迁移。**

### 10.1 普通对话历史

如果历史已经被规范化为：

```text
User Message
Assistant Tool Call
Tool Result
Assistant Message
```

可以尝试转换成 Model B 支持的格式。

但需要验证：

- Tool Call 配对是否合法。
- 是否存在 Model B 不支持的内容。
- 上下文是否超限。
- 是否存在必须保留的 Provider Metadata。

### 10.2 Reasoning Data

有些 Provider 会返回不透明的推理数据或带签名的推理块。

这些数据不一定能够转换到其他 Provider。

不能把一个 Provider 的内部推理状态直接当作另一个模型的可复用状态。

通常需要依赖普通历史、结构化摘要和任务状态完成交接。

### 10.3 KV Cache

我们此前学过：

KV Cache 保存的是特定模型对 Token 前缀计算产生的 Key/Value 张量。

不同模型的参数、Tokenizer、Attention 结构可能不同。

因此不能把 Model A 的 KV Cache 直接拿给 Model B 使用。

切换模型后，即使逻辑 History 不变，也应按目标推理服务的缓存条件重新判断命中，而不能假设跨模型复用。

这说明：

**Session History 的可迁移性，与底层推理状态的可迁移性，是两回事。**

---

## 十一、最终设计：Model Adapter 应该有哪些职责？

经过这些故障，我们可以推导出如下结构：

```text
                   Agent Loop
                       |
                       v
                 Model Request
                       |
                       v
                 Model Adapter
                       |
          +------------+------------+
          |            |            |
          v            v            v
      Capability    Message      Provider
       Checking     Encoding     Selection
          |            |            |
          +------------+------------+
                       |
                       v
                 Provider Client
                       |
                       v
                 Model Response
                       |
                       v
               Stream Normalizer
                       |
                       v
               Unified Model Events
                       |
                       v
                   Agent Loop
```

模型调用的关键职责可以分成：

| 组件 | 职责 |
|---|---|
| Model Registry | 维护模型身份和元数据 |
| Capability Resolver | 判断当前模型支持哪些能力 |
| Request Encoder | 转换输入消息、工具定义和参数 |
| Provider Client | 发起实际请求 |
| Response Normalizer | 转换输出和流式事件 |
| Error Mapper | 将 Provider 错误转换成内部错误 |
| Usage Collector | 记录 Token、缓存使用量、延迟等 |

这是一种便于理解的逻辑拆分，不要求每个真实框架都有这些同名类。

### 一个最小的 Model Adapter 接口

我们可以从以下抽象开始：

```python
from typing import Protocol, AsyncIterator

class ModelAdapter(Protocol):
    def capabilities(self) -> dict:
        ...

    async def stream(
        self,
        messages: list,
        tools: list,
    ) -> AsyncIterator["ModelEvent"]:
        ...
```

真实实现还需要：

- 模型标识。
- 调用参数。
- 取消信号。
- 请求追踪 ID。
- 错误处理。
- Token Usage。
- Provider-specific Options。

但设计的重点已经非常清楚：

**Agent Loop 接收的是内部 ModelEvent，而不是某个 Provider 原始的网络响应。**

例如：

```text
TextDelta("我发现退款逻辑...")
ToolCallCompleted(read_file, {"path": "refund.py"})
ResponseCompleted(...)
```

这样 Agent Loop 可以专注于控制执行过程，不必理解所有厂商的协议细节。

---

## 十二、三个 Harness 为什么采用不同的模型适配架构？

现在将三者放到一起。

| 关注点 | DeepAgents / LangChain | OpenCode | Codex |
|---|---|---|---|
| 模型抽象 | `BaseChatModel` | Provider / Model / LLM Client | `ModelClient` |
| 模型选择 | `init_chat_model`、模型实例 | Provider ID + Model ID | Model Info + Provider |
| 输入消息 | LangChain Messages | 内部 Session Messages 与 Provider 转换 | Prompt / Response Items |
| 工具调用转换 | ChatModel 集成层 | Provider/LLM 适配层 | Responses 输出项及内部处理 |
| 流式输出 | `stream` / `astream` | LLM Stream 与事件转换 | `ResponseEvent` |
| 模型能力 | Model Profile 等 | Provider Model Metadata | ModelInfo / Provider 配置 |
| 传输管理 | 由模型集成承担 | LLM Client / Transport | HTTP、WebSocket、认证和重试 |

### DeepAgents：优先复用成熟模型抽象

DeepAgents 不把所有 Provider 适配代码放进自己的 Harness。

它利用 LangChain 的 ChatModel 生态。

这样，DeepAgents 更专注于上下文管理、工具和 Agent 能力组合。

### OpenCode：强调多 Provider 的实际差异

OpenCode 的模型适配不仅是统一函数签名。

它还需要处理模型元数据、参数变体、特殊消息要求和不同 Provider 的能力差异。

对于需要支持大量模型的产品，这类转换尤其重要。

### Codex：模型客户端与运行时深度结合

Codex 的 ModelClient 除了请求和响应格式，还处理 Turn 级连接、请求传输、认证恢复和增量响应状态。

这是因为模型调用不仅是一条独立 HTTP 请求，还可能是长时间运行 Agent Session 中连续发生的多个推理请求。

三者没有绝对的优劣。

它们只是把模型适配职责放在不同的软件层次中。

---

## 十三、Model Adapter 与其他模块的关系

现在可以把第十六章放回我们已经学习的整个系统。

```text
                  User Task
                      |
                      v
                  Agent Loop
                      |
                      v
                Context Builder
                      |
                      v
                 Model Adapter
                      |
                      v
                 Provider API
                      |
                      v
               Normalized Events
                      |
                      v
                  Agent Loop
                      |
          +-----------+-----------+
          |                       |
          v                       v
      Tool System             Turn Completion
          |
          v
   Execution Environment
```

其中：

**Agent Loop**

决定何时调用模型、收到输出后是否继续。

**Context Builder**

决定当前请求包含哪些历史、记忆和工具定义。

**Model Adapter**

负责把内部请求转换为目标 Provider 支持的格式，并把响应转成统一事件。

**Tool System**

负责执行模型请求的工具操作。

这几个模块不能互相替代。

例如，Model Adapter 即使知道模型的上下文窗口大小，也不意味着它应该自行删除历史。

正确的职责分配应该是：

```text
Model Adapter 提供限制信息
             |
             v
Context Builder 决定如何压缩或裁剪
             |
             v
Model Adapter 编码并发送请求
```

---

## 十四、一个关键设计取舍：抽象层是不是越统一越好？

小李最终想把所有 Provider 的能力都转换为：

```python
model.generate(text) -> text
```

这样接口最简单。

但马上会出现问题。

因为 Tool Calling、图片、结构化输出、Reasoning、缓存和流式事件等能力都没有地方表达。

如果强行把它们全部包装成纯文本，就会丢失关键语义。

反过来，如果统一接口包含每一家 Provider 所有专属参数，那么这个接口又会变得极其庞大。

因此需要在两个方向之间取舍：

**Common Interface**

提供大多数 Agent 都需要的公共能力。

例如：

```text
Messages
Tool Calls
Streaming
Usage
Errors
```

**Provider Extensions**

保留特定 Provider 独有的能力。

例如：

```text
Reasoning Controls
Prompt Cache Options
Provider Metadata
Transport-specific Features
```

一种合理的架构是：

```text
Model Request
    |
    +-- Common Fields
    |
    +-- Provider Options
```

但 Provider Options 应受到 Schema 验证，不能成为绕过安全或能力检查的任意参数入口。

**好的 Model Adapter，不是隐藏所有模型差异，而是统一真正共有的语义，同时明确暴露无法统一的能力差异。**

---

## 十五、如何验证 Model Adapter 是否正确？

我们不应该只测试：

```text
模型能否返回 Hello World
```

而应该覆盖实际 Agent 行为。

| 测试场景 | 预期结果 |
|---|---|
| 不同 Provider 的普通文本输出 | 统一为 Text Event |
| 不同 Provider 的 Tool Call | 统一为合法工具调用 |
| 工具参数被拆成多个 Stream Chunk | 完成组装后才执行 |
| Tool Call ID 包含特殊字符 | 正确转换并保持结果关联 |
| 模型不支持某个工具能力 | 拒绝或调整配置 |
| 中途切换模型 | 重建兼容的请求上下文 |
| Stream 在中间断开 | 不执行未完成的工具请求 |
| Context Window 缩小 | 重新计算预算 |
| Provider 返回 429 | 有界退避重试 |
| 用户取消模型请求 | 传播取消并停止后续处理 |

此外还应该比较：

- Task Success Rate。
- Token Usage。
- 首 Token 延迟。
- 工具调用成功率。
- 重试次数。
- Provider 切换后的任务延续能力。

模型适配成功，不只是 API 请求返回 200。

而是：

**更换 Provider 后，Agent 的关键行为仍然正确，或者系统能够明确报告目标模型不支持哪些必要能力。**

---

## 十六、本章的五个核心问题

### 1. Model Adapter 是什么？

Model Adapter 是连接 Agent Harness 内部执行协议与具体模型 Provider API 的适配层。

它处理消息编码、响应转换、能力差异以及调用错误。

### 2. 为什么需要？

因为不同模型服务对消息、工具调用、流式输出和参数的表示方式不一致。

Agent Loop 不应该依赖某一种 API 格式。

### 3. 没有它会怎样？

每接入一家 Provider，都需要修改 Agent Loop、Context Builder 和工具调用处理逻辑。

模型切换可能破坏已有消息与工具调用关系。

### 4. 实际怎么做？

定义内部模型请求与事件结构。

为不同 Provider 实现请求编码、响应归一化、能力检查和错误映射。

保留无法统一的 Provider-specific Metadata。

### 5. 在整个 Harness 中处于什么位置？

它位于 Agent Loop / Context Builder 与外部模型 API 之间。

向下屏蔽 Provider 差异，向上提供稳定的模型调用语义。

---

## 本章总结

我们最初只有：

```python
response = client.chat(messages)
```

后来需要支持不同 Provider，于是出现了一系列问题：

```text
工具调用格式不同
      |
      v
统一 Tool Call

输入消息协议不同
      |
      v
Message Conversion

流式输出不完整
      |
      v
Stream Normalization

模型能力不一致
      |
      v
Capability Negotiation

错误与重试行为不同
      |
      v
Error Normalization
```

最终，我们得到了一层独立的 Model Adapter。

它让 Agent Loop 不必知道当前模型是通过哪种 API、哪种传输方式返回响应。

但它也不能承诺所有模型具有相同的能力。

本章最重要的结论是：

> **Model Adapter 的价值，不是让所有模型看起来完全一样，而是把模型之间可统一的行为抽象出来，把无法统一的差异控制在明确的边界内。**

## 参考源码与阅读顺序

### 1. LangChain / DeepAgents：从统一模型接口理解抽象

- [BaseChatModel](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/chat_models.py)
  - 重点：`BaseChatModel`、`invoke()`、`stream()`。
- [init_chat_model](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/chat_models/base.py)
  - 重点：模型初始化和 Provider 选择。
- [DeepAgents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)
  - 重点：`create_deep_agent()` 如何接收模型实例。

### 2. OpenCode：从 Provider 差异理解模型适配

当前 V2：

- [Core Provider](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/provider.ts)
- [Core Session](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session.ts)
- [Session Model Transport](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/model-transport.ts)

另外，V1 的 [Provider Transform](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts) 很适合独立研究 Tool Call ID、Reasoning、缓存和模型参数转换，但不要与 V2 混成同一条调用链。

### 3. Codex：从流式请求理解模型客户端

- [Codex client.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/client.rs)
  - 重点：`ModelClient`、`ModelClientSession`、`ResponseEvent`。
- [Codex client_common.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/client_common.rs)
  - 重点：Prompt 与内部模型响应结构。
- [Codex Model Provider](https://github.com/openai/codex/tree/main/codex-rs/model-provider)
  - 重点：Provider 配置与客户端创建。

源码持续更新，正式研究时建议固定 Commit，避免将不同版本的接口混用。

---

**下一章：Hooks、MCP 与 Skills——为什么 Agent Harness 不应该把所有能力都写死在核心代码里？**

我们的 Coding Agent 已经支持不同模型和工具，但现在第三方团队希望：

- 在 Tool Call 前后运行自定义 Hook。
- 通过 MCP 接入外部数据库和服务。
- 通过 Skills 为 Agent 提供特定领域的操作知识。

这将引出三个不同层次的扩展机制：**生命周期扩展、工具能力扩展和知识能力扩展**。
