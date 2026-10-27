---
title: agent 的 trace 长什么样：OpenTelemetry GenAI 语义约定
description: 从一次 45 秒的回答讲起，拆开 invoke_agent / chat / execute_tool 的 span 结构、核心属性与指标、默认不捕获内容的取舍，以及为什么这套标准对企业比任何私有 SDK 都重要
slug: otel-genai
authors: [hwt]
tags: [可观测性, OpenTelemetry, Agent, 企业架构]
---

# agent 的 trace 长什么样：OpenTelemetry GenAI 语义约定

用户问了一句话，agent 用了 45 秒才回答。

这 45 秒花在哪？可能是模型推理慢，可能是某个工具调用卡住，可能是 agent 在循环里重试了五次，也可能是上下文太长导致每次调用都变慢。

如果只有一句「耗时 45 秒」的日志，这些你都不知道。

OpenTelemetry 在 2026 年推进的 GenAI 语义约定，解决的正是这件事：**给 agent 的每一次模型调用、每一次工具调用，定一套标准的记录方式。**

这篇文章讲这套标准具体长什么样，以及它在企业落地时的几个关键取舍。

<!-- truncate -->

> 本文的 span 名、属性名、指标名均来自 OpenTelemetry 官方文档与官方博客，已在各处标注。属性名可能随版本变化，接入时以官方文档当前版本为准。

## 一、先理解 OpenTelemetry 的三个概念

不熟悉这套体系的话，先花一分钟把名词对齐。

**OpenTelemetry**（简称 OTel）是一个开源的观测数据标准与工具集，最初由 CNCF 托管（来源：OpenTelemetry 官方文档）。它定义了三类数据：

| 类型 | 是什么 | 典型用途 |
| --- | --- | --- |
| Trace | 一次请求的完整调用链 | 定位慢在哪一步 |
| Metric | 聚合的数值指标 | 看趋势、做告警 |
| Log | 离散的事件记录 | 看细节、查异常 |

**Span** 是 trace 的基本单位。一个 span 表示「一段有开始和结束的操作」，它记录：操作名、开始时间、耗时、状态、一组属性（key-value）。

Span 之间可以嵌套，形成树。父 span 的耗时包含子 span 的耗时，这样你就能一层层往下找瓶颈。

**语义约定**（Semantic Conventions）是 OTel 的一部分，它规定「某类操作的 span 应该叫什么名字、应该带哪些属性」。

这很关键。如果没有约定，每家 SDK 自己起名：有的叫 `llm_call`，有的叫 `model_invoke`，有的把模型名放在 `model`，有的放在 `llm.model_name`。数据进了后端，就没法统一查询。

GenAI 语义约定就是给 AI 相关操作定这套名字。

## 二、标准的 span 树

OTel 的 GenAI 约定给出一个三层结构（来源：OpenTelemetry 官方博客《Inside the LLM Call: GenAI Observability with OpenTelemetry》，2026；span 名以官方 GenAI 语义约定文档为准）：

```text
invoke_agent                    ← 一次 agent 任务的完整执行
├── chat                        ← 每次 LLM 调用
├── execute_tool                ← 每次工具调用
├── chat                        ← 又一次 LLM 调用
├── execute_tool
└── chat
```

三个 span 名的含义：

**`invoke_agent`** 表示一次 agent 的完整运行。它是根 span，包含了整个任务的耗时。

**`chat`** 表示一次模型调用。注意它叫 `chat` 而不是 `llm_call`——约定用的是「对话式调用」这个语义。

**`execute_tool`** 表示一次工具执行。参数、返回值、耗时都记在这里。

这个结构的好处是：**它天然对应 agent 的心智模型**。

一个 agent 就是「想一步、做一步、再想一步」的循环。span 树把这个循环画了出来，每一步都可见。

### 2.1 多 agent 怎么办

多 agent 场景下，每个 agent 有自己的 `invoke_agent` span，它们通过父子关系或者链接（span link）关联。

```text
invoke_agent (coordinator)
├── chat
├── invoke_agent (researcher)     ← 子 agent
│   ├── chat
│   └── execute_tool
└── invoke_agent (writer)
    └── chat
```

这样你既能看到整体，也能下钻到某个子 agent。

### 2.2 一个实际的排查过程

回到开头那个 45 秒的问题。有了 span 树，排查是这样走的：

1. 打开 `invoke_agent` 的 trace，看总耗时 45.2 秒。
2. 展开子 span，发现 8 个 `chat` 加起来 12 秒，3 个 `execute_tool` 加起来 31 秒。
3. 三个工具里有一个 `execute_tool` 单独占了 28 秒。
4. 看它的属性，发现是一个外部 API 调用，返回了 429（限流），agent 重试了三次。

结论很清楚：不是模型慢，是工具限流加重试。

没有这套结构，你只能看到 45 秒。

## 三、核心属性

Span 上挂的属性，决定了你能查到什么。GenAI 约定里最常用的几个（来源：OpenTelemetry GenAI 语义约定文档）：

| 属性 | 含义 |
| --- | --- |
| `gen_ai.request.model` | 本次调用使用的模型 |
| `gen_ai.usage.input_tokens` | 输入 token 数 |
| `gen_ai.usage.output_tokens` | 输出 token 数 |
| `gen_ai.response.finish_reasons` | 停止原因（`stop` / `tool_calls` 等） |
| `gen_ai.system_instructions` | 系统提示词（需显式开启内容捕获） |
| `gen_ai.input.messages` | 输入消息（同上） |
| `gen_ai.output.messages` | 输出消息（同上） |

前四个是**默认采集**的，后三个要显式开启（来源：OpenTelemetry GenAI 语义约定文档）。

### 3.1 为什么 finish_reasons 值得单独说

`gen_ai.response.finish_reasons` 记录的是模型为什么停下来。

常见取值（来源：OpenTelemetry GenAI 语义约定文档；具体取值以文档当前版本为准）：

- `stop`：正常说完
- `tool_calls`：要调工具
- `length`：达到 max_tokens 被截断
- `content_filter`：被内容过滤拦下

这个字段的价值在于**区分「正常的工具调用循环」和「异常循环」**。

一个 agent 如果连续十次 `finish_reasons` 都是 `tool_calls`，但工具返回的都是同一个错误，那它就在死循环里。

而如果大量出现 `length`，说明你的 max_tokens 设小了，或者上下文太长了——模型话没说完就被截断，输出质量会明显下降。

这两个问题在只有耗时的日志里完全看不出来。

### 3.2 token 计数的两种用法

`gen_ai.usage.input_tokens` 和 `output_tokens` 分开记，是因为它们的成本结构不同。

大多数模型的输入和输出定价不一样，输出通常更贵。如果只记总数，算成本会算错。

更重要的是，这两个数字能暴露两类问题：

- **输入 token 持续增长**：上下文没做压缩，历史越堆越长。
- **输出 token 异常高**：模型在啰嗦，或者陷入了重复生成。

这两类问题都会直接反映在账单上。

## 四、核心指标

Trace 记录单次请求，Metric 记录聚合趋势。

GenAI 约定定义了两个核心指标（来源：OpenTelemetry GenAI 语义约定文档）：

| 指标 | 类型 | 含义 |
| --- | --- | --- |
| `gen_ai.client.operation.duration` | 直方图 | LLM 调用延迟，可按模型、操作类型过滤 |
| `gen_ai.client.token.usage` | 直方图 | token 消耗，可按 input / output 分离 |

两个都是**直方图**，不是简单的平均值。

这个选择很重要。延迟的分布通常是长尾的：中位数 800ms，P99 可能是 8 秒。如果只看平均值，你会得到一个「还行」的数字，然后对 P99 的用户体验一无所知。

直方图能算出分位数，才能看到长尾。

### 4.1 怎么用这两个指标

| 问题 | 用什么 |
| --- | --- |
| 哪个模型最慢 | `operation.duration` 按 model 分组 |
| 哪个模型最贵 | `token.usage` 按 model 分组，乘单价 |
| 上下文是不是越来越长 | `token.usage` 的 input 维度趋势 |
| 换模型有没有变快 | 按时间对比 model 维度的 P95 |

这几条是最基础的成本与性能监控，也是最容易被忽略的——很多团队上 agent 之后，只知道月度账单，不知道钱花在哪个环节。

## 五、默认不捕获内容：一个关键的默认值

这是整套约定里最重要的设计决策。

**默认情况下，GenAI span 只记录元数据：模型名、token 数、耗时、停止原因。**

它**不记录**：

- 提示词内容
- 模型输出内容
- 工具调用的参数
- 工具返回的结果

要记录这些，必须显式开启内容捕获（不同实现里叫 `captureContent` 或类似名字）（来源：OpenTelemetry 官方博客 2026 年 GenAI 可观测性一文，明确说明内容捕获需 opt-in）。

### 5.1 为什么这么设计

因为 prompt 和工具参数里，装的都是业务数据。

一个客服 agent 的 prompt 里有客户姓名、订单号、地址。一个财务 agent 的工具参数里有账号、金额。一个 HR agent 的上下文里有薪资、绩效。

这些数据进了日志系统，就等于**复制了一份到另一个地方**，而日志系统通常：

- 保留期更长（几个月到几年）
- 访问控制更宽（运维、开发都能看）
- 备份更多（多地多副本）

这直接违反了数据最小化原则。

### 5.2 开启内容捕获时的取舍

有些场景确实需要看内容——调试提示词、复现错误、评估输出质量。

这时候的取舍是（下表为本文建议，非官方规范）：

| 需求 | 建议做法 |
| --- | --- |
| 调试提示词 | 只在开发/预发环境开启 |
| 复现生产问题 | 开启但脱敏，或只捕获哈希 |
| 评估输出质量 | 抽样开启，采样率 1% 以下 |
| 审计合规 | 不开在 trace 里，走专门的审计通道 |

最后一条值得展开：**审计和可观测性是两个不同的东西。**

可观测性是为了「让工程师看懂系统在干什么」，审计是为了「证明某个动作被授权过」。

前者可以采样、可以丢弃、可以脱敏。后者不能采样、不能丢、必须完整。

把它们混在一起，要么审计数据不完整，要么日志系统里塞满敏感数据。

### 5.3 一个实操建议

如果要在生产环境开内容捕获，至少做三件事：

1. **脱敏**：用正则或专用模型识别并替换 PII（身份证、手机号、卡号）。
2. **采样**：不要 100% 捕获，1% 已经足够定位问题。
3. **隔离**：内容捕获写到独立的存储，和普通 trace 分开，设置更短的保留期。

## 六、现状：还在演进

必须说清楚这套标准的状态。

截至 2026 年 5 月，多家材料指出 GenAI 与 MCP 的语义约定仍处于 **Development** 状态，尚未稳定（来源：Greptime 对 OTel 语义约定状态的观察，2026-05-09）。OTel 官方在 2026 年的说明里也把它描述为「已在用，且仍在活跃开发中」（来源：OpenTelemetry 官方博客 2026 年 GenAI 可观测性一文）。

这意味着：

- 属性名还可能变（比如从 `gen_ai.usage.prompt_tokens` 改成 `gen_ai.usage.input_tokens` 这种事发生过）
- 新的 span 类型可能加入
- 不同 SDK 的实现可能有差异

所以工程上的建议是：**不要把属性名硬编码进业务逻辑。**

具体做法是在中间加一层适配：

```python
# 概念伪代码：隔离语义约定的变化
ATTR = {
    "model": "gen_ai.request.model",
    "in_tokens": "gen_ai.usage.input_tokens",
    "out_tokens": "gen_ai.usage.output_tokens",
}

def record_call(span, model, usage):
    span.set_attribute(ATTR["model"], model)
    span.set_attribute(ATTR["in_tokens"], usage.input)
    span.set_attribute(ATTR["out_tokens"], usage.output)
```

属性名变了，只改这张表。查询端也做同样的事：用视图或映射表把标准名对上，而不是在每个仪表盘里写死。

## 七、生态：谁在输出这套数据

2026 年的一个明显变化是，主流 coding agent 都开始支持 OTel 输出（来源：OpenTelemetry 官方博客 2026 年 GenAI 可观测性一文）：

- VS Code Copilot 支持导出 traces
- Codex 支持
- 另一个主流 coding agent 也支持（其 trace 支持标注为 beta）

这个变化的意义不在于「多了几个数据源」，而在于**agent 的可观测性不再绑死在某家 SDK 的私有格式上**。

一旦数据是标准 OTel 格式，你就可以：

- 接进现有的 Jaeger、Grafana Tempo、Datadog
- 和已有的微服务 trace 拼在一起
- 用同一套告警规则

对一个已经有可观测性体系的企业，这是从「再建一套」变成「接进去」的差别。

### 7.1 一个本地调试的办法

如果只是想看看 trace 长什么样，不需要部署一整套后端。OTel 的 Collector 加上 Aspire Dashboard 或者 Jaeger 的 all-in-one 镜像，本地就能跑起来。

流程是：

```text
应用 → OTel SDK → OTLP (gRPC/HTTP) → Collector → 后端（Jaeger / Tempo / ...）
```

Collector 这一步是可选的，但建议留着——它负责批处理、采样、脱敏，也让你以后换后端不用改应用。

## 八、用它回答三个问题

把上面这些收拢一下，这套标准最终是为了回答三个问题。

### 8.1 慢在哪

用 span 树逐层下钻。`invoke_agent` → `chat` / `execute_tool` → 具体的工具或模型。

关键是**每一层都有耗时**，所以你能定位到具体是哪一次调用慢，而不是只知道整体慢。

### 8.2 贵在哪

用 `gen_ai.client.token.usage` 按模型、按操作类型分组。

更进一步，可以在 span 上挂业务标签（部门、项目、任务类型），这样能算出「哪个业务线的 agent 最烧钱」。

这一步需要自定义属性，OTel 支持在标准属性之外加自己的。

### 8.3 错在哪

用 `gen_ai.response.finish_reasons` 加 span 的状态码。

异常模式通常是这几种（下表为本文归纳的排查经验，非官方定义）：

| 模式 | 含义 |
| --- | --- |
| 大量 `tool_calls` + 工具报错 | 死循环 |
| 大量 `length` | 上下文或输出被截断 |
| 大量 `content_filter` | 提示词触发了安全策略 |
| `chat` span 正常但 `execute_tool` 报错 | 工具侧问题 |

这四类模式，是 agent 上线后最常见的故障。有了标准属性，它们可以变成告警规则。

## 九、一个判断

可观测性在传统系统里是「有了更好」，在 agent 系统里是**「没有不行」**。

原因是 agent 的行为不确定。同一个输入，它可能走三步，也可能走三十步；可能调这个工具，也可能调那个。

传统系统里，你可以靠读代码知道它会做什么。agent 系统里，你只能靠看 trace 知道它做了什么。

这也是为什么 2026 年「可观测性」这个老话题会重新被拿出来讲——它不是新需求，是新对象。

而 OTel 这套约定的价值在于：它让这件事有了**不绑定供应商**的做法。

在一个 agent 框架三个月换一代的市场里，这一点比功能强弱重要得多。

---

## 参考来源

### 一手材料

- OpenTelemetry 官方博客《Inside the LLM Call: GenAI Observability with OpenTelemetry》：https://opentelemetry.io/blog/2026/genai-observability/
- OpenTelemetry GenAI 语义约定文档：https://opentelemetry.io/docs/specs/semconv/gen-ai/

### 转述材料

- Greptime：OpenTelemetry 如何追踪 LLM 调用与 agent 推理（语义约定状态说明）：https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions
- Dash0：OpenTelemetry GenAI Semantic Conventions Explained：https://www.dash0.com/knowledge/opentelemetry-genai-semantic-conventions-explained

> [!NOTE]
> 属性名与指标名以 OTel 官方文档当前版本为准。文中提到「截至 2026 年 5 月仍处于 Development 状态」来自第三方材料的观察，官方博客的表述是「已在用，且仍在活跃开发中」。两者不矛盾，但都说明属性名尚未冻结。
