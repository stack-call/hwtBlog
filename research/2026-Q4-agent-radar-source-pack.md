# 企业级 Agent 技术雷达 · 素材包（2026-10-27）

> 用途：为后续博客/文档提供可核对的原始素材。本文件不是成稿，是**素材库**。
> 检索日期：2026-10-27
> 标注规则：`[一手]` 官方文档/规范/官网/新闻稿；`[机构]` 分析机构、政府、标准组织；`[媒体]` 新闻报道；`[转述]` 博客/公众号对原始材料的二次加工（数字需回查）。

---

## 零、一句话结论

2026 年企业级 agent 的竞争重心，已经从「模型多聪明」转到「**行为可治理**」。
四个可验证的信号：

1. 监管层面，中国 5 月出台首份智能体专项文件，第一次把**决策权限**写进国家层面的规范。
2. 采购层面，IDC 调研显示 58.7% 的企业把「治理与合规」列为引入智能体平台的首要目标。
3. 架构层面，AI Gateway 正在从「模型代理」演进为「agent 交互治理层」。
4. 商业层面，Gartner 预测到 2027 年底超过 40% 的 agentic AI 项目会被取消，原因不是模型不行，是成本、价值不清、风控不足。

---

## 一、市场大盘（数字优先，注意口径差异）

| 指标 | 数值 | 来源 | 等级 |
| --- | --- | --- | --- |
| 2026 年底内置任务型 agent 的企业应用占比 | 40%（2025 年 <5%） | Gartner 2025-08-26 | [机构] |
| 已部署 agent 的组织占比 | 17%；60%+ 计划部署 | Gartner 2026 CIO 调查 | [机构] |
| 成功把 AI 扩展到多个业务单元的组织 | 22% | Gartner 2026-09-01 | [机构] |
| 至少在做 agent 实验的企业 | 62%；23% 已在某处规模化 | McKinsey 2025 | [机构] |
| 有成熟 agentic AI 治理模型的企业 | 21% | Deloitte 2026 | [机构] |
| 从未进入生产的 genAI PoC 占比 | 95% | MIT Technology Review | [机构] |
| 到 2027 年底将被取消的 agentic AI 项目 | >40% | Gartner 2025-06-25 | [机构] |
| 2028 年内置 agentic AI 能力的企业软件占比 | 33%（2024 年 <1%） | Gartner | [机构] |
| 2028 年由 AI 自主完成的日常工作决策占比 | ≥15% | Gartner | [机构] |
| 积极使用 AI 治理的企业，进入生产的 AI 项目数 | 12 倍 | Databricks | [机构] |

> [!NOTE]
> 口径冲突要留意：Gartner 的 40%（2026 年底）是**预测**，17%（已部署）是**调查**。两者不矛盾，但经常被媒体混着引用，制造「已经普及」的错觉。

### 中国市场

| 指标 | 数值 | 来源 | 等级 |
| --- | --- | --- | --- |
| 中国企业活跃智能体数量（2031） | 3.5 亿，CAGR >135% | IDC 2026-03-04 | [机构] |
| 2026–2027 年活跃智能体同比增速 | 单年 >200% | IDC | [机构] |
| 智能体 Token 消耗年均增速 | >30 倍 | IDC | [机构] |
| 低代码/无代码智能体（2026 → 2031） | 约 300 万 → 近 2 亿 | IDC | [机构] |
| 独立智能体活跃占比（2026 → 2031） | 7.5% → 20.1% | IDC | [机构] |
| 中国企业级 AI 智能体市场（2025） | 约 190 亿元，2025–2028 CAGR >110% | IDC | [机构] |
| 中国智能体市场总规模（2025 → 2030） | 256.8 亿 → 4925.2 亿元 | 爱分析 | [机构] |
| 全球活跃 Agent（2025 → 2030） | 2860 万 → 22.16 亿（约 80 倍） | IDC | [机构] |
| 智能体应用普及率目标（2027 / 2030） | >70% / >90% | 国务院 国发〔2025〕11 号 | [一手] |

### IDC 的四类智能体结构（值得单独记）

1. **应用内智能体**：嵌在 ERP/CRM/IM 里，零迁移、开箱即用，最快落地；2027 年后增速放缓。
2. **低代码/无代码智能体**：数量最大，长期占全部活跃智能体一半以上。
3. **独立智能体**：跨系统执行，当前最少但弹性最大，随 MCP 等互操作协议爆发。
4. **定制智能体**：数量最少、价值密度最高，主要服务央国企、政务等高安全场景。

来源：IDC《智能体Token消耗年均增超30倍》，2026-03-04，[机构]

### IDC 提出的三重压力（原文框架，可直接引用）

- **架构压力：系统必须对 AI 可读。** 企业软件要同时服务人类用户和 agent，通过 MCP 等标准协议被无缝调用。
- **治理压力：信任成为生产前提。** 全链路可观测、细粒度权限控制、可审计机制成为基础能力。核心数据不出域，端云协同与混合部署成为常态。
- **成本压力：Token 正在改变 IT 预算逻辑。** 成本可观测与效能监测成为商业可持续性的核心能力。

---

## 二、政策与合规（中国，2026 年最重要的变量）

### 《智能体规范应用与创新发展实施意见》

- 发布：2026-05-08，国家网信办 + 国家发改委 + 工信部 联合印发
- 地位：**国家层面首份智能体专项规范性文件**，首次把 agent 作为独立监管对象
- 来源：https://www.cac.gov.cn/2026-05/08/c_1779979789523320.htm `[一手]`

关键条款（原文摘录）：

> **6. 明确决策权限。** 在遵守法律法规、尊重社会公德和伦理规范前提下，厘清**仅限用户本人决策、需由用户授权决策和智能体自主决策**等各种决策方式的合理边界及所需权限。确保用户对智能体自主决策享有**知情权和最终决策权**，智能体执行操作不得超出用户授权范围。

> **7. 加强行为管控。** 发展**规则内嵌、行为围栏**等技术……探索利用区块链等技术，建立重要应用场景智能体行为可验证、可追溯机制。

> **3. 建立智能体标准体系。** ……加强**智能体互联协议（AIP）**等智能体互联关键国家标准、行业标准的推广应用。

> **4. 布局发展智能互联网。** 研究建立智能互联网体系架构，探索建立**智能体注册平台**，提供智能体**数字身份管理**、检索发现、能力声明等服务……研究智能体身份标识、可信互联、合规支付、安全防护、冲突解决等基础技术。

> **11. 构建分类分级治理框架。** 对敏感领域及重点行业，由网信部门联合行业主管部门确定开放场景，实行**备案、检测、问题产品召回**等管理措施。

> [!TIP]
> 三个可直接落进技术方案的词：**决策权限三级**（本人 / 授权 / 自主）、**行为围栏**、**AIP**。
> 注意 AIP 是中国主导的智能体互联协议国家标准，和 A2A/MCP 不是同一套东西——这是一个中文语境独有的技术点，英文材料里基本没有。

### 配套研究与行业实践

- 信通院 + 三六零《企业级智能体技术与应用研究报告（2026 年）》，2026-06
  https://www.caict.ac.cn/kxyj/qwfb/ztbg/202606/t20260625_728115.htm `[一手]`
  要点：需建立全链路审计追踪，实现执行路径「白盒化」溯源；配备动态沙箱隔离与异常熔断，高风险操作自动拦截并触发人工接管。

- 中移智库《智能体行为围栏：Agent 治理框架与实践路径》，2026-09-20
  https://www.secrss.com/articles/94121 `[转述]`（引用 IDC 数据，观点可参考）
  引用 IDC 2026 调研：**62% 的企业把数据权限与安全合规列为智能体跨系统执行的首要障碍；58.7% 把治理与合规放在智能体平台引入目标的首位。**
  提出三层治理能力：行为可观测 / 操作可审计 / 风险可干预。
  提出「事前设防—事中管控—事后追溯」全链路，以及「预执行—确认—提交」三段式跨系统流程。
  真实案例（转述，未独立核实）：某中型电商用 agent 做 K8s 告警响应，误判目标 Pod 导致服务中断 15 分钟。

- 绿盟科技《从内容治理到行为治理：中国智能体治理框架深度解析》
  https://blog.nsfocus.net/ `[转述]`
  提法：治理对象从「静态模型」转向「动态执行流程」。

---

## 三、技术方向清单

### 3.1 决策模型（Decision Model）— 最被低估的一层

**是什么**：不生成文本，只返回「类型化结果 + 校准概率」的专用模型。开发者预先声明封闭选项空间，模型直接给答案。

**代表**：TypeSafe AI 的 Jev（2026-09-15 发布）

| 项 | 内容 | 来源 |
| --- | --- | --- |
| 定位 | System One Model，借卡尼曼快慢思维框架 | https://typesafe.ai `[一手]` |
| 训练算法 | RLCD（Reinforcement Learning for Calibrated Decisions），优化概率校准而非人类偏好 | 同上 |
| 性能对比 | 0.114 秒 / $0.000081 vs 大模型 8.566 秒 / $0.013880 | 同上 |
| 折算倍数 | 快 193.6 倍，便宜 444.6 倍（官方口径） | 同上 |
| 定价 | $42 / 十亿输入 token，输出不计费 | 同上 |
| 输入价对比 | 比「the model 5.1」低 238 倍 | 同上 |
| 采纳速度 | 24 小时内 13% Vercel 付费团队接入；一周内 Cloudflare / LangChain / Langfuse 集成 | 博客园 `[媒体]` |
| 部署 | 闭源托管 API；据央广网报道未向中国大陆开放 | 同上 |
| 客户案例 | Jack & Jill 候选人初筛成本降 88%，速度 2 倍，10 天上线 | typesafe.ai `[一手]` |

**开源复现**：SemIf（原 OpenJev，作者 Theo Lee）

- 仓库：https://github.com/TheoLeeCJ/SemIf `[一手]`
- 关键对照（同一模型、同一问题、同一组封闭选项）：
  - 直接读 logits：中位 1.023 秒
  - 自回归生成 JSON 再解析：中位 5.332 秒
  - 差 5.21 倍
- 温度校准：校准误差 0.208 → 0.069
- 诚实声明：只复现接口模式，未复现 Jev 未公开的模型与训练过程；对比覆盖可对齐的 102 行公开子集
- 限制：读出的概率是「给定选项上的归一化偏好排序」，不是校准过的置信度；量化构建影响精度

**适用四条件**（来自博客园实践总结，`[转述]`）：选项封闭可枚举、调用高频、延迟敏感、有兜底。

**落地三坑**：概率 ≠ 置信度（需设阈值 + 温度校准）；公开跑分不能直接搬（要用自己数据重测）；选项设计就是提示词工程（「其他」会吞概率质量）。

> [!NOTE]
> 为什么这条重要：它把「高频小判断」从大模型账单里剥出来，是企业最容易算清 ROI 的一层。
> 但它也是**最容易被误当成「又一个模型」**的一层——它的价值在于接口形状（类型化 + 概率），不在参数量。

---

### 3.2 多 Agent 编排框架 — 今年定下了两件事

#### Microsoft Agent Framework 1.0

- 发布：2026-04-03，.NET + Python 同时 GA，MIT 协议
- 来历：Semantic Kernel（企业工程能力）+ AutoGen（编排模式）合并
- 来源：https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/ `[一手]`

稳定能力清单：

| 能力 | 说明 |
| --- | --- |
| 服务连接器 | Foundry、Azure OpenAI、OpenAI、Anthropic、Bedrock、Gemini、Ollama |
| Middleware 钩子 | 内容安全、日志、合规策略，不改 prompt 即可插入执行链 |
| 记忆与上下文 | 会话历史、持久 KV、向量检索；后端可选 Mem0、Redis、Neo4j、自建 |
| 图工作流引擎 | agent 推理 + 业务逻辑编排，支持检查点与中断恢复 |
| 多 agent 编排 | sequential / concurrent / handoff / group chat / Magentic-One，全部支持流式、检查点、人工审批、暂停恢复 |
| 声明式 YAML | 指令、工具、记忆、拓扑写进版本控制 |
| A2A + MCP | 跨运行时协作、动态发现外部工具 |

预览能力里有一条特别值得记：

> **GitHub Copilot SDK 与 Claude Code SDK** —— 把成熟的 coding agent harness 当作组件直接嵌进编排，与其他 agent 组成同一条工作流。
> 另有 **Agent Harness**：可定制的本地运行时，给 agent 提供 shell、文件系统、消息循环。

#### AgentScope 2.0（阿里通义）

- 来源：https://java.agentscope.io/v2/zh/blogs/agentscope-v2-release `[一手]`
- 主线：从「透明开发」走向「系统工程」

升级要点：

| 模块 | 变化 |
| --- | --- |
| 模型接入 | 统一重试 + 备用模型机制，主模型失败自动切换 |
| 消息与事件 | 统一 Content Block；一次回复变成可订阅事件流（模型调用、文本增量、工具调用、工具结果、用户确认、外部执行） |
| **权限系统** | 按静态规则 + 工具类型 + 输入内容，判定 允许 / 用户审批 / 拒绝；文件工具查危险目录，命令工具查高风险命令与动态 shell |
| 上下文管理 | 结构化保留任务目标、当前状态、关键发现、下一步计划；工具结果截断；文件读写缓存 |
| Middleware | 模型调用前后、工具执行前、reasoning/acting 流程、system prompt 构造阶段均可插入 |
| **Workspace** | 把「做什么」和「在哪执行」解耦；本地 FS / Docker / E2B 云沙箱统一接口；内置预热池 |
| Agent Service | 原独立运行时合并进主库，支持 Session 日志恢复、长耗时工具后台运行 |

多语言：Python 2.0、TypeScript 版本已推出、Java 版本同步中。
（Java 1.0 的发布时间有冲突：一说 2025-12 开源，一说 2026-05 发布 1.0 —— **待核实**）

#### 判断（主观）

两家的升级清单里，**没有一条是「让 agent 更聪明」**。全是可运维、可审计、可替换。
验收一个框架是否企业级，看三条：能否中断恢复、能否插审计钩子、能否换执行环境。

---

### 3.3 协议层 — MCP 管工具，A2A 管 agent，AIP 是中国路线

#### MCP 2026-07-28 规范

来源：https://blog.modelcontextprotocol.io/posts/2026-07-28/ `[一手]`

| 项 | 旧 | 新 |
| --- | --- | --- |
| 会话 | initialize 握手 + Mcp-Session-Id | 废弃，改无状态核心 |
| 多轮交互 | 服务端反向请求 | MRTR，返回 `resultType: "input_required"` |
| 路由 | 解析 body | `Mcp-Method` / `Mcp-Name` 头部 |
| 列表结果 | 每次重算 | `ttlMs` + `cacheScope` 可缓存 |
| 授权 | — | RFC 9207 `iss` 校验 |
| 客户端注册 | DCR | CIMD |
| Tasks | 核心 | 移入扩展 |
| Roots / Sampling / Logging | 核心 | 弃用，12 个月窗口 |

#### A2A

- 2025-06，Google 把 A2A 捐给 Linux 基金会（Open Source Summit Denver 宣布）
- 来源：https://thenewstack.io/google-donates-the-agent2agent-protocol-to-the-linux-foundation/ `[媒体]`
- 意义：中立治理 → 降低「换供应商」成本

#### AIP（中国）

- 见《实施意见》第 3、4 条：智能体互联协议国家标准，配套智能体注册平台、数字身份管理
- 与 A2A/MCP 不同源，是国内独立路线 `[一手]`

#### 判断（主观）

协议是唯一不随供应商变化而贬值的资产。但**协议范围 ≠ 治理范围**（见 3.5 的 MCP gateway 讨论）。

---

### 3.4 Agent 身份与授权链（NHI）

**是什么**：NHI = Non-Human Identity，非人类身份，含服务账号、API key、OAuth token、工作负载身份、AI agent。

| 事实 | 来源 | 等级 |
| --- | --- | --- |
| 非人类身份数量已超过人类身份 | Cloud Security Alliance, Agent Identity Governance Framework | [一手] |
| 建议每个 agent 一个唯一、可加密验证的身份，绑定用途/范围/归属 | SecureAuth 2026 指南 | [转述] |
| 用 OAuth 2.1 client credentials 发放，而非共享长期 key | 同上 | [转述] |
| 需区分「以自己身份行动」与「代表用户行动」，后者权限委托 ≤ 用户本身 | Aembit / Ping Identity | [转述] |

**Aklivity 白皮书给出的授权链公式**（可直接引用）：

```
effective authority
  = user authority
  ∩ agent authority
  ∩ delegated task authority
  ∩ transaction policy
```

配套原则：**每次委托只能收窄权限，永不放大**；权限随事务结束而失效。

来源：https://www.aklivity.io/state-of-ai-gateways-2026 `[一手·厂商白皮书]`

> [!WARNING]
> 一个没有独立身份、共用一把长期 API key 的 agent，出事之后无法回答三个问题：
> 哪个 agent 做的、它当时代表谁、权限是谁批的。审计过不去。

---

### 3.5 AI Gateway — 从模型代理到治理运行时

来源：Aklivity《The State of AI Gateways in 2026》白皮书 `[一手·厂商]`
（注意：厂商白皮书有立场，但其中的架构演进与分类框架值得引用；引用的第三方数字需回查原始出处）

#### 四代演进

| 代 | 名称 | 治理边界 |
| --- | --- | --- |
| Gen 1 | LLM Proxy | 模型调用 |
| Gen 2 | Multi-Model Gateway | 多供应商、路由、token、成本 |
| Gen 3 | Enterprise AI Gateway | + 企业身份、策略、审计、DLP |
| Gen 4 | Agentic AI Gateway | + MCP 工具、API、其他 agent、数据 |
| 下一步 | Transaction Governance → AI Governance Runtime | 以「业务事务」为治理单位 |

#### 五类玩家（按起点分类）

1. LLM Gateway（起点：模型接入）— LiteLLM、Portkey、OpenRouter
2. API Gateway 扩展（起点：API 管理）— Microsoft、Google Apigee、Kong
3. 云与边缘 AI Gateway（起点：流量与边缘网络）— Cloudflare、Vercel AI Gateway
4. MCP & Agent Gateway（起点：agent 连通性）— Arcade、agentgateway
5. Unified AI Gateway / AI Data Plane（起点：上述收敛）— Redpanda Agentic Data Plane

#### 「三网关问题」（Three-Gateway Problem）

```
应用  → API Gateway → APIs
Agent → LLM Gateway → Models
Agent → MCP Gateway → Tools
Agent → Data Platform → 企业数据
```

四条边界，四个身份体系，四套策略语言，四条日志管道。单点都很强，端到端却断链。

具体例子：
```
Alice → 采购 Agent → 下单 Agent → SAP
```
SAP 可能看到的是 Alice、某个 agent，或一个共享服务身份。认证本身无法说明：委托了什么权限、怎么收窄的、什么时候过期。

#### 动作三级治理（Read / Write / Irreversible）

| 级别 | 例子 | 控制 |
| --- | --- | --- |
| READ | 查 CRM、查库、取文档 | 可自动执行 |
| WRITE | 改记录、建工单、改合同 | 强校验、审计、幂等 |
| IRREVERSIBLE | 转账、部署生产、删数据 | 提权、审批、检查点、回滚、熔断 |

对应 Gartner 2026-05 的建议：治理强度应与自治程度成正比。

#### 审批作为运行时状态

```
RUNNING → Action Proposed → Policy Evaluation → WAITING_FOR_APPROVAL
        → REVALIDATE → EXECUTE
```

关键点：**审批和执行之间要重新校验**。等待期间身份、价格、合同、凭据、预算都可能变，审批不应重放旧请求。

#### 与「事务」的关系

一个事务（如「续签合同、折扣不超过 7%、送签」）可能跨 Salesforce、模型、定价工具、法务工具、另一个 agent、高管审批、DocuSign、两天时间。
**没有任何一个 HTTP 请求、MCP 调用、模型 trace 能代表整个活动。**

---

### 3.6 可观测性 — OpenTelemetry GenAI 语义约定

来源：https://opentelemetry.io/blog/2026/genai-observability/ `[一手]`

**标准 span 结构**：

```
invoke_agent
├── chat            (每次 LLM 调用)
└── execute_tool    (每次工具调用)
```

**核心属性**：

| 属性 | 含义 |
| --- | --- |
| `gen_ai.request.model` | 使用的模型 |
| `gen_ai.usage.input_tokens` / `output_tokens` | token 计数 |
| `gen_ai.response.finish_reasons` | 停止原因（stop / tool_calls） |
| `gen_ai.system_instructions` | 系统提示词（需显式开启内容捕获） |
| `gen_ai.input.messages` / `output.messages` | 输入输出消息（同上） |

**核心指标**：

| 指标 | 含义 |
| --- | --- |
| `gen_ai.client.operation.duration` | LLM 调用延迟直方图，可按模型过滤 |
| `gen_ai.client.token.usage` | token 消耗直方图，可按 input/output 分离 |

**重要默认值**：默认**不捕获** prompt 内容与工具参数（敏感数据）。只含模型名、token 数、耗时。要显式开启 `captureContent`。

**生态现状**：VS Code Copilot、Codex、Claude Code 均已支持 OTel 输出（Claude Code 的 trace 支持还是 beta）。截至 2026-05，GenAI 与 MCP 语义约定仍处于 **Development** 状态，未稳定。

> [!TIP]
> 这一条对企业最实用：它意味着「agent 可观测性」不再是各家 SDK 私有格式，而是可以接进现有 OTel 体系（Aspire Dashboard、Jaeger、Grafana 等）。
> 但要注意 Development 状态 = 属性名还可能变。

---

### 3.7 沙箱执行环境

**为什么需要**：agent 要执行不可信代码（用户上传的脚本、模型生成的代码），容器隔离强度和启动成本都不对。

**技术底座**：Firecracker microVM（AWS 为 Lambda 开发）

**主要玩家**（2026 年格局，来自多家对比文章汇总 `[转述]`）：

| 平台 | 隔离方式 | 特点 |
| --- | --- | --- |
| E2B | Firecracker microVM | 每个 agent 一台隔离机器；可跑在自己的 AWS/GCP/Azure；支持暂停、分叉 |
| Daytona | 容器 | 持久工作区；2025 年初从开发环境管理转向 agent 基础设施 |
| Modal | 容器 + 自定义运行时 | 面向 coding agent 的基础设施 |
| Cloudflare | 边缘 | 边缘执行 |
| Vercel Sandbox | — | 与 Vercel 生态集成 |
| Northflank / Blaxel / Runloop / CodeSandbox / Upstash Box / Fly Sprites | 各异 | 细分场景 |

**性能数字**（`[转述]`，来自 CSDN 博客，需回查 Firecracker 官方 benchmark）：最小 MicroVM 启动约 124ms，内存占用约 5MB。

> [!NOTE]
> 「沙箱」这个词被四类不同东西共用：容器、microVM、语言级隔离、进程级隔离。安全边界完全不同，选型时必须问清是哪一种。

---

### 3.8 上下文与知识（RAG 的现状）

- **「长上下文会让 RAG 死掉」是误读。** 处理 100K 上下文的成本是 RAG 的 20–100 倍，且有 lost-in-the-middle 问题。两者互补。 `[转述]`
- **GraphRAG 索引成本已大幅下降**：从约 $33,000 降到接近向量 RAG 的水平（2026）。 `[转述]`，来源 cruxdigits.nl，**需回查原始测算**
- **Agentic RAG**：agent 读查询、拆子问题、选来源；能减少「取错块」的失败，但会放大方差，需要强评估闸门。
- **学术侧**：arXiv 2606.25656《Is GraphRAG Needed? From Basic RAG to Graph-/Agentic RAG》讨论何时该用哪种。 `[一手·预印本]`

**与 IDC「系统必须对 AI 可读」呼应**：知识工程的重点正从「检索精度」转向「知识资产化」——把隐性经验转成 agent 可调用的规则与知识。

---

### 3.9 成本工程（Token 经济）

| 事实 | 来源 |
| --- | --- |
| 中国企业智能体 Token 消耗年均增速 >30 倍 | IDC |
| 成本可观测与效能监测成为商业可持续性核心能力 | IDC |
| 开源 DeepInfer 把 KV Cache 对 HBM 需求降到 1/4、SSD 降到 1/8；相对初代缩小 437 倍 | DeepSeek `[一手]` |
| Agent 场景中缓存命中费用占比往往较高 | DeepSeek `[一手]` |

**优化手段清单**（来自 IDC 建议 + 厂商实践）：
token 缓存、上下文加载策略、agent 记忆管理、投机解码、模型分层（大模型管慢思考 / 决策模型管快判断）。

---

## 四、交叉观察（主观判断，写作时需标明是观点）

1. **监管与技术在同一年收敛到同一个词：权限。**
   中国政策讲「决策权限三级」，海外白皮书讲「授权链收窄」，AgentScope 讲「权限系统」，CSA 讲「NHI 治理」。四个来源、四种语言，指向同一件事。

2. **最贵的不是模型，是判断。**
   Jev 的经济学、IDC 的 token 压力、DeepSeek 的 KV Cache 优化，三条独立证据都指向「高频小判断」这层暗账。

3. **网关是这一年的架构焦点，但它可能只是过渡形态。**
   白皮书的四代演进暗示终点是「事务治理」，不是「网关」。这个判断属于厂商假设，引用时要说清。

4. **「可观测性」正在从工程话题变成合规话题。**
   OTel 给的是技术标准，信通院要的是「白盒化溯源」，网信办要的是「可验证、可追溯」。三者可以对接，但需要企业自己搭桥。

5. **框架竞争已经结束了一半。**
   Microsoft AF 和 AgentScope 的能力清单高度重合（中间件、检查点、权限、Workspace、协议）。差异不在功能，在生态绑定与语言栈。

6. **中文材料独有的两个点**：AIP 协议、决策权限三级。英文材料里基本找不到对应物，这是可以做出差异的地方。

---

## 五、来源清单

### 一手（官方/规范/官网）

| 材料 | URL | 日期 |
| --- | --- | --- |
| 《智能体规范应用与创新发展实施意见》 | https://www.cac.gov.cn/2026-05/08/c_1779979789523320.htm | 2026-05-08 |
| TypeSafe AI 官网 | https://typesafe.ai | — |
| SemIf 开源仓库 | https://github.com/TheoLeeCJ/SemIf | — |
| Microsoft Agent Framework 1.0 | https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/ | 2026-04-03 |
| AgentScope 2.0 发布说明 | https://java.agentscope.io/v2/zh/blogs/agentscope-v2-release | 2026 |
| MCP 2026-07-28 规范博客 | https://blog.modelcontextprotocol.io/posts/2026-07-28/ | 2026-07-28 |
| OpenTelemetry GenAI 可观测性 | https://opentelemetry.io/blog/2026/genai-observability/ | 2026 |
| DeepSeek V4.1 Flash 发布 | https://api-docs.deepseek.com/zh-cn/news/news260910/ | 2026-09-10 |
| DeepSeek V4.1 Flash 1017 | https://api-docs.deepseek.com/zh-cn/news/news261017 | 2026-10-17 |
| 信通院《企业级智能体技术与应用研究报告（2026）》 | https://www.caict.ac.cn/kxyj/qwfb/ztbg/202606/t20260625_728115.htm | 2026-06 |
| CSA Agent 身份治理框架 | https://labs.cloudsecurityalliance.org/agentic/agentic-identity-governance-framework-v1/ | 2026 |
| Aklivity《State of AI Gateways 2026》白皮书 | https://www.aklivity.io/state-of-ai-gateways-2026 | 2026 |
| E2B 官网 | https://e2b.dev/ | — |
| Amazon Bedrock AgentCore | https://aws.amazon.com/bedrock/agentcore/ | — |

### 机构（分析/研究）

| 材料 | URL | 日期 |
| --- | --- | --- |
| Gartner：40% 企业应用将内置任务型 agent | https://www.gartner.com/en/newsroom/press-releases/2025-08-26-gartner-predicts-40-percent-of-enterprise-apps-will-feature-task-specific-ai-agents-by-2026-up-from-less-than-5-percent-in-2025 | 2025-08-26 |
| Gartner：>40% agentic AI 项目将被取消 | https://www.gartner.com/en/newsroom/press-releases/2025-06-25-gartner-predicts-over-40-percent-of-agentic-ai-projects-will-be-canceled-by-end-of-2027 | 2025-06-25 |
| Gartner：仅 22% 组织成功规模化 AI | https://www.gartner.com/en/newsroom/press-releases/gartner-survey-finds-only-22-percent-of-organizations-have-successfully-scaled-ai-across-multiple-business-units | 2026-09-01 |
| Gartner：2026 Hype Cycle for Agentic AI | https://www.gartner.com/en/articles/hype-cycle-for-agentic-ai | 2026 |
| IDC：中国智能体 Token 消耗与规模 | https://www.idc.com/resource-center/blog/智能体token消耗年均增超30倍：中国企业智能体规模进/ | 2026-03-04 |
| IDC：企业级智能体迈入规模化治理新时代 | https://www.idc.com/resource-center/blog/idc洞察：政策与市场共振-《智能体实施意见》/ | 2026 |
| 爱分析：2026 中国企业智能体市场规模报告 | https://www.ifenxi.com/research/content/6781 | 2026 |

### 媒体 / 转述

| 材料 | URL | 日期 |
| --- | --- | --- |
| 葡萄城：Jev 爆火之后，给企业应用配一个 AI 决策模型 | https://www.cnblogs.com/powertoolsteam/p/23166078 | 2026-09-30 |
| CSDN：Jev 深度技术分析 | https://tianqi.csdn.net/6ab3cfca05257b085712977d.html | 2026-09-23 |
| 中移智库：智能体行为围栏 | https://www.secrss.com/articles/94121 | 2026-09-20 |
| The New Stack：Google 捐赠 A2A 给 Linux 基金会 | https://thenewstack.io/google-donates-the-agent2agent-protocol-to-the-linux-foundation/ | 2025-06-23 |
| Forbes：40% agentic AI 项目为何可能被取消 | https://www.forbes.com/sites/robertszczerba/2026/07/07/why-40-of-agentic-ai-projects-may-be-canceled-by-2027/ | 2026-07-07 |
| 腾讯云：AgentScope 深度实践 | https://www.cnblogs.com/BlogNetSpace/p/20362402 | 2026 |

---

## 六、待补 / 待核实

- [ ] AgentScope Java 1.0 发布时间（2025-12 vs 2026-05 两说）
- [ ] Firecracker microVM 启动/内存数字的官方 benchmark 出处
- [ ] GraphRAG 索引成本 $33,000 → 平价 的原始测算来源
- [ ] 爱分析市场规模口径与 IDC 口径差异原因
- [ ] AIP（智能体互联协议）国标的具体内容与进展——目前只有《实施意见》一句提及
- [ ] Gartner 2026-05「治理强度与自治程度成正比」原文
- [ ] 国内厂商（腾讯 WorkBuddy / 阿里百炼 / 字节）的企业级 agent 平台能力清单，尚未逐个核实
- [ ] 智能体备案制的具体落地流程

---

## 七、可写选题（按素材充分度排序）

| # | 选题 | 素材充分度 | 差异点 |
| --- | --- | --- | --- |
| 1 | 决策模型：把「判断」从大模型里剥出来 | 高 | 有官方数据 + 开源复现 + 落地三坑 |
| 2 | 决策权限三级：中国智能体监管给技术方案带来的具体约束 | 高 | 中文独有，英文无对应 |
| 3 | AI Gateway 四代演进与「三网关问题」 | 高 | 架构图清晰，可直接画 |
| 4 | Agent 身份与授权链：effective authority 公式 | 中高 | 有公式、有反面案例 |
| 5 | 可观测性：OTel GenAI 语义约定实战 | 中高 | 有标准、有代码、可跑 |
| 6 | 沙箱：四种「隔离」不是一回事 | 中 | 需补官方 benchmark |
| 7 | 市场数字背后的口径陷阱 | 中 | 40% vs 17% vs 22% 的辨析 |
