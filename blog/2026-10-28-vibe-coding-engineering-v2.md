---
title: Vibe Coding 工程化实践：七大核心挑战、底层逻辑与落地指南
sidebar_label: Vibe Coding 工程化实践
sidebar_position: 1
deprecated: true
supersededBy: /blog/ai-engineering/vibe-coding-engineering-v3
supersededByLabel: Vibe Coding 工程化实践：七大挑战、系统质量属性与可靠交付
slug: /ai-engineering/vibe-coding-engineering
description: 从目标对齐、上下文、架构、执行控制、结果验证、生产可靠性与长期可持续性七个问题，系统推导 Vibe Coding 的工程实践，并提供订单系统案例、风险分级和可复用模板。
keywords:
  - Vibe Coding
  - Agentic Engineering
  - AI Coding
  - Context Engineering
  - Software Architecture
  - Software Reliability
  - SRE
  - AI Agent
  - Docusaurus
---

# Vibe Coding 工程化实践：从七大核心挑战到可靠交付

> **核心观点：Vibe Coding 的真正挑战，不是让 AI 更快地生成代码，而是如何在具有不确定性的 AI 执行过程中，持续交付设计合理、功能正确、生产可靠且能够长期演进的软件。**

过去，程序员往往同时承担需求理解、技术设计、编码、调试与验证。如今，Claude Code、Codex、Cursor 等 Coding Agent 能阅读仓库、修改文件、运行命令、编写测试，并持续迭代。编码的执行主体正在变化，但软件工程的责任并没有消失。

在国内外开发者社区中，一些人通过 Plan、Spec、Rules、Skills 和多 Agent 显著提高了交付速度；另一些人则遭遇了需求跑偏、代码库失控、测试不足和上线故障。它们并非彼此无关的零散问题，而是同一个根本变化的不同后果：**人通过目标和反馈间接控制一个非完全确定的执行者。**

本文从 **Why（问题根源）→ Principle（工程原则）→ How（具体实践）→ Evidence（如何验证）** 四个层次组织这些经验。既可以用作个人工作流指南，也适合团队建立 AI Coding 工程规范。

:::info 阅读范围
本文的七个维度是用于分析和治理的**概念框架**，不是业界统一标准，也不是每个任务必须机械遵循的七步流程。社区案例属于个人经验；可执行检查、测试数据与生产指标应以真实项目为准。
:::

## 一、为什么要重新思考 AI Coding 的工程方法？

传统开发的许多反馈发生在程序员编码的过程中：人理解代码，发现设计不合适，调整实现，再逐步收敛。

Agent Coding 把一部分探索、设计与执行交给 AI。这样可以显著提高实现吞吐量，但也可能让错误假设更快变成大量变更，或让人来不及理解生成结果。

```text
人的业务意图 / 质量要求
          │ 目标与约束
          ▼
     AI Coding Agent ◄──── 项目知识 / 历史决策 / 工具
          │
          ▼
  技术方案 → 代码修改 → 系统行为
          │
          ▼
  测试 / 审查 / 运行观测
          │
          └────────────── 反馈、纠偏与知识沉淀
```

因此，成熟工作流必须同时回答七个问题：

| 核心挑战 | 要回答的 Why | 对应原则 | 典型实践 |
| --- | --- | --- | --- |
| **1. 目标对齐** | AI 怎么知道真正要做什么？ | 明确意图 | Spec First、验收标准 |
| **2. 知识与上下文** | AI 凭什么做出符合项目的判断？ | 提供可靠知识 | Context Engineering、`AGENTS.md`、模块文档 |
| **3. 架构合理性** | 功能正确，为什么系统仍可能设计得不合理？ | 显式权衡设计 | Architecture Review、ADR、架构测试 |
| **4. 执行可控性** | 如何限制 Agent 错误操作的影响？ | 控制自主权和变更边界 | Small Batch、Worktree、权限、Hooks |
| **5. 结果可信性** | AI 说完成了，凭什么相信它？ | 独立验证 | TDD、CI、E2E、Code Review |
| **6. 生产运行可靠性** | 测试通过，为什么上线后仍可能崩溃？ | 在真实负载和故障下验证 | 并发测试、压测、限流、SLO、可观测性 |
| **7. 长期可持续性** | 短期提效，如何避免长期成本反噬？ | 持续改进 | Skills、Harness、技术债治理、效能度量 |

这七项还可以从更高层归纳为三种责任：

1. **构建正确的系统（Build the Right System）**：目标、知识、架构。
2. **正确地构建与交付系统（Build and Deliver It Right）**：执行控制、结果验证。
3. **让系统在真实世界持续工作（Keep It Working）**：生产可靠性、长期演进。

---

## 二、目标对齐：为什么需要 Spec First？

### Why：自然语言的模糊性与软件行为的精确性冲突

假设任务是“给订单查询增加缓存，提高性能”。这句话没有回答：缓存哪些字段？失效条件是什么？允许多长时间的数据不一致？Redis 故障时是否降级？怎样判定性能提升？

AI 可以迅速补全这些空白并开始编码；**问题是它补全的假设未必等于真实业务要求**。实现越快，错误目标被放大的速度也越快。

### Principle：先让意图成为可确认的契约

对于有明显歧义或较高风险的任务，建议在实现前确认：

- **目标（Goal）**：最终希望用户或系统获得什么结果。
- **范围与非目标（Scope / Non-goals）**：本次做什么、明确不做什么。
- **业务规则（Invariants）**：任何情况下都必须成立的约束。
- **验收条件（Acceptance Criteria）**：以什么可观察证据证明完成。
- **质量属性（Quality Attributes）**：性能、容量、安全、一致性等要求。

### How：需求澄清 → 简短 Spec → 方案确认

```markdown
## Feature: Order Query Cache

### Goal
降低重复订单查询对数据库的压力。

### Scope
仅缓存订单详情查询；不调整订单写入 API。

### Invariants
- 用户只能读取有权限访问的订单。
- 订单状态变更后不得无限期返回旧状态。

### Acceptance
- 缓存命中、失效、Redis 故障均有测试。
- 无权限用户无法通过缓存 Key 绕过授权。
- 在约定负载下，性能达到双方确认的指标。

### Open questions
- 可接受的最大陈旧时间是多少？
- 是否需要主动失效还是仅靠 TTL？
```

### Evidence：如何知道目标已对齐？

- Agent 能清楚复述目标、非目标与待确认假设。
- 验收标准可由测试或人工操作检查，而不是“看起来完成”。
- 重要设计决策有记录，后续变更能够追溯。

**适用边界：** 文案修改、小型明确 Bug 不必强制写长 Spec。前置澄清的投入应与任务风险、歧义程度和潜在返工成本相匹配。

国内 V2EX 的 [Chorus 左移实践](https://v2ex.com/t/1218949) 将需求细化、技术提案和任务审查前置；另一篇 [半年使用感受](https://v2ex.com/t/1219408) 则体现了开发者倾向于“简短方案确认后再执行”的做法。两者在流程厚度上不同，但都在解决目标理解偏差。

---

## 三、知识与上下文：为什么需要 Context Engineering？

### Why：模型的通用能力不等于它掌握了项目事实

即使需求清晰，Agent 仍可能不知道现有架构、接口契约、数据库迁移历史、废弃方案以及团队约定，从而重复造轮子或破坏隐含约束。

更大的上下文窗口也不能自动保证正确：过时、矛盾和无关资料可能增加噪声。

### Principle：在需要时提供准确且足够的上下文

推荐建立三层信息结构：

| 层次 | 信息类型 | 推荐承载方式 |
| --- | --- | --- |
| 仓库级 | 项目目标、架构原则、测试命令、安全边界 | `AGENTS.md` / `CLAUDE.md` |
| 模块级 | 领域模型、接口契约、依赖限制、关键历史决策 | `README.md`、`docs/`、ADR |
| 任务级 | 当前目标、受影响文件、验收标准、明确假设 | Issue、Spec、Plan |

### How：先探索、再选择性注入上下文

```text
repo/
├── AGENTS.md                  # 简短的仓库级约定
├── docs/
│   ├── architecture.md        # 系统边界与依赖
│   ├── adr/                   # 重要设计决策
│   └── features/              # 当前功能的 Spec
├── services/
│   └── order/
│       └── README.md          # 订单领域知识
└── tests/
```

Agent 开始任务时应优先调查代码和现有测试，报告：相关调用链、可复用实现、可能影响的模块、尚不能确认的事实。

**不要强制它为一个拼写修复阅读整个仓库。** 规范文件应提供“去哪儿找信息”的索引，而非重复整套文档。

### Evidence

- 方案明确引用了真实的代码位置和现有契约。
- 未引入已有能力的重复实现。
- 重要假设和未知问题被显式标记。
- 仓库级规则经过定期清理，没有冲突、失效或无限膨胀。

---

## 四、架构合理性：为什么功能通过测试仍不足够？

### Why：局部实现正确，不保证全局设计合理

AI 经常围绕一个局部任务优化：为了快速满足当前需求，可能在多个 Service 中复制缓存逻辑、引入不必要的层级、增加双向依赖，或选用与业务规模不相称的分布式方案。

**功能正确性回答“有没有做对”，架构合理性回答“是不是用合适的结构完成”。** 架构合理性也不等于架构越复杂越好。

### Principle：关键设计必须显式比较权衡

对影响模块边界、公共 API、数据模型或基础设施的变更，要求方案回答：

1. 能否沿用现有架构，而不新增抽象或依赖？
2. 备选方案是什么，为什么选择当前方案？
3. 领域边界、模块耦合和依赖方向是否合理？
4. 并发一致性、安全、性能和故障隔离如何处理？
5. 后续迁移、扩容和回滚的成本是什么？
6. 哪些设计属性可以转为自动化规则？

### How：Architecture Review + ADR + Architecture Fitness

对重大决策记录简短 ADR：

```markdown
# ADR-007: Order cache invalidation

## Context
订单详情频繁查询，状态可能异步更新。

## Options
1. 仅靠短 TTL
2. 订单状态改变后主动失效 + TTL 兜底
3. 事件驱动失效

## Decision
[待评审方案]

## Consequences
- 一致性与性能的取舍
- 故障恢复与监控要求
- 对现有模块的影响
```

随后用架构适应性测试（Architecture Fitness Functions）持续守护可明确表达的规则：

| 架构约束 | 可能的验证手段 |
| --- | --- |
| Controller 不能直接访问 Repository | 静态依赖规则或架构测试 |
| 不允许领域模块循环依赖 | 依赖图检查 |
| 公共接口不能意外破坏兼容性 | 契约测试、API Diff |
| 新依赖需审批 | 依赖清单检查和 PR Gate |
| 关键路径性能不能显著退化 | 性能基准与趋势监控 |

自动化规则只能验证**明确且可测的性质**，不能替代对整体架构权衡的人工判断。

### Evidence

- 关键方案有对比理由与权衡记录。
- Diff 未引入无关抽象和架构漂移。
- 架构约束在 CI 中得到持续检查。
- 新架构的运行成本和故障模式被验证，而不只是画出设计图。

**适用边界：** 小功能不需要组织架构评审会；但数据库结构、核心事务、服务拆分和跨模块接口变更应显式审查。

---

## 五、执行可控性：为什么需要 Small Batch 与 Guardrails？

### Why：不能假设 Agent 的每一步执行都正确

Agent 可能修改超出需求的文件、意外执行危险命令、同时修改同一关键模块，或在修复中制造新的缺陷。

因此控制重点应从“保证每个动作都不会错”，转向：**缩小错误影响、提供停机边界、保留恢复能力**。

### Principle：自主权与任务风险相匹配

| 实践 | 实际解决的问题 |
| --- | --- |
| Small Batch | 缩小单次 Diff 和回归范围 |
| Git Commit / PR | 保存可审查、可回退的交付节点 |
| Git Worktree | 隔离多个任务的文件工作区 |
| Hooks / CI | 在规定时机自动执行检查 |
| 工具权限与沙箱 | 限制能访问和操作的资源 |
| 人工审批 | 防止高风险操作被自动执行 |

Git Worktree 是**代码工作区隔离**，不是安全沙箱。权限边界必须在操作系统、容器、平台权限和环境凭据层面实现。

### How：任务边界与最小权限

```text
任务 A：修复登录异常 → worktree A → 独立测试 → PR A
任务 B：重构日志组件 → worktree B → 独立测试 → PR B
                                             ↓
                                   审查后有序合并
```

不允许 Agent 在未经授权时接触真实生产凭据、删除生产数据、变更权限策略、部署或推送到受保护分支。

**Prompt 中写“禁止操作生产环境”不是安全保障。** 对重要资源，需要用真正的权限隔离与审批控制。

### Evidence

- 每次变更范围明确且可回滚。
- 敏感操作不可能仅凭 Agent 自己的判断绕过审批。
- 并行任务没有相互覆盖文件或重复修改关键契约。
- 出错时可以定位责任变更并恢复。

---

## 六、结果可信性：为什么需要 Verification First？

### Why：生成代码与生成正确代码是两回事

“AI 说已经完成”不是证据；“另一个 AI 说没有问题”也不是充分证据。甚至自动生成的测试，都可能只是在验证同一个错误的业务假设。

### Principle：用独立、可复现的结果判断是否交付

推荐先运行项目原有测试，建立基线；对风险较高的变更，用 Red/Green 测试循环：

1. 根据需求写测试，确认它能识别预期问题。
2. 让 Agent 实现最小修改。
3. 运行测试、类型检查、Lint、契约测试等。
4. 在真实运行场景中验证关键用户路径。
5. 人工审核 Diff、测试覆盖和未验证范围。

不同层次的验证回答不同问题：

| 检查 | 主要回答 |
| --- | --- |
| Unit / Integration | 局部逻辑和模块协作是否正确？ |
| Contract Test | 与上下游接口是否兼容？ |
| E2E / 浏览器操作 | 用户完整流程能否运行？ |
| 静态扫描 | 类型、依赖、部分安全规则是否违规？ |
| Code Review | 实现是否清晰、存在隐性风险或架构问题？ |
| 并发与负载测试 | 真实运行条件下是否仍满足关键性质？ |

### Evidence

Agent 交付时至少应附带：

```text
Files changed:
Commands run:
Observed results:
Acceptance criteria checked:
Not verified:
Known risks:
Rollback plan (if applicable):
```

**重要边界：** 通过单元测试并不能证明架构合理，也不能证明系统能承受生产流量；这需要下一节的运行可靠性验证。

Simon Willison 在 [Agentic Engineering Patterns](https://simonwillison.net/guides/agentic-engineering-patterns/) 中强调从现有测试开始，并将可执行验证作为 Coding Agent 工作的一部分。

---

## 七、生产运行可靠性：为什么测试通过，上线仍会出事？

### Why：生产环境不是单次成功的请求

真实系统面对同时到来的请求、数据竞争、非均匀流量、有限资源、第三方故障、网络抖动、重试以及长时间运行。

单次请求的正确性，不能直接推导出并发下的正确性；某一负载下的低延迟，也不能保证流量高峰时仍稳定。

**生产运行可靠性与架构合理性有关，但不能被架构评审替代。** 架构提供能力假设，负载测试和运行观测才提供现实证据。

### 7.1 并发正确性：业务不变量在竞争条件下能否保持？

看一个典型的错误下单逻辑：

```python
stock = get_stock(product_id)
if stock > 0:
    create_order(user_id, product_id)
    update_stock(product_id, stock - 1)
```

两个请求同时读到库存为 1 时，可能都创建订单，最终产生超卖。

正确的验收标准不是“每次请求都能成功下单”，而是：

- 库存不得为负，成功售出数量不能超过可售数量。
- 同一业务请求重复发送不能生成重复订单。
- 订单和库存的状态变化必须满足约定的事务一致性。

例如，数据库层面的**条件原子扣减**可以作为一种基本保护：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = :product_id AND stock > 0;
```

程序必须检查受影响行数，且将订单创建、去重约束与扣减操作纳入正确设计的事务流程。仅有这条 SQL **不是完整的订单一致性方案**；还需结合数据库隔离级别、唯一键、失败回滚，以及支付等外部系统的边界设计。

针对这种问题，应该写**并发正确性测试**，例如让多个请求竞争最后一件库存，并最终检查订单数与库存不变量。常规压测通过不等于不存在竞态条件。

### 7.2 容量与性能：能在约定负载下达到目标吗？

在开发前定义可衡量的质量目标，例如：

| 质量属性 | 应明确什么 |
| --- | --- |
| 流量模型 | 日常、峰值、突发请求分布以及读写比例 |
| 延迟 | p95 / p99 响应时间阈值 |
| 可用性 | 统计窗口和 SLO 定义 |
| 错误率 | 可接受的服务错误比例、超时比例 |
| 资源预算 | CPU、内存、连接池、消息队列、外部 API 配额 |
| 数据规模 | 总量、增长率、热点分布与查询模式 |

数字应来自业务需求、历史流量或容量测量，**不要让 Agent 任意编造“生产级性能指标”**。

不同测试关注不同情况：

| 测试类型 | 主要用途 |
| --- | --- |
| Smoke | 少量流量检查测试脚本和基础功能 |
| Average Load | 验证正常业务负载 |
| Stress | 验证高负载下的行为和容量边界 |
| Spike | 验证突发流量与恢复 |
| Soak | 观察长时间运行和资源泄漏 |
| Failure / Chaos | 检查依赖超时、服务重启、资源不足等故障模式 |

压测必须针对**专用或明确授权的环境**，控制流量上限，避免破坏业务数据或影响其他用户。

参考 [Grafana k6 的性能测试分类](https://grafana.com/docs/k6/latest/testing-guides/automated-performance-testing/)。

### 7.3 弹性、故障隔离与退化行为：不能处理所有请求时怎么办？

成熟系统必须回答：

- 超过承载能力后，是排队、拒绝还是降级？
- 下游超时是否有合理超时预算？
- 重试是否有上限、退避和抖动，避免放大故障？
- 哪些资源和业务链路需要隔离？
- 是否可以优先保护下单等核心操作，而牺牲非核心推荐或报表功能？

常见机制包括限流、背压、熔断、隔离、超时、重试预算、降级与有序扩缩容。不要不加区分地叠加所有机制；应针对实际故障模式设计。

Google SRE 在 [Handling Overload](https://sre.google/sre-book/handling-overload/) 和 [Addressing Cascading Failures](https://sre.google/sre-book/addressing-cascading-failures/) 中详细讨论了过载保护、级联故障与重试放大的问题。

### 7.4 可观测与发布：运行结果必须能被看见

上线前至少确认：

- **Metrics**：流量、延迟、错误、饱和度以及业务关键指标。
- **Logs / Traces**：关键请求可关联，敏感数据经过脱敏。
- **Alerts**：围绕用户影响或 SLO，而不只是 CPU 高低。
- **Rollout / Rollback**：支持分阶段发布，出现异常时有退回方案。
- **Runbook**：关键故障的初步排查和止损手册。

### Evidence：生产可靠性的交付证据

- 业务不变量经过并发场景检查。
- 在已声明负载模型下提供延迟、错误率和资源消耗结果。
- 关键依赖故障或过载时，系统表现与预期一致。
- 发布后有健康指标与回滚机制。
- 未测过的负载规模明确标注为未知，而不是推断“应该能扛住”。

---

## 八、长期可持续性：为什么需要 Skills、Harness 和持续治理？

### Why：写代码更快，不等于端到端交付成本更低

AI 能压缩实现成本，但可能增加需求澄清、代码审查、返工、安全修复和长期维护成本。

可以用一个简化模型表示：

```text
总交付成本 = 沟通与决策 + 实现 + 验证 + 返工 + 运行维护
```

真正目标是减少**完整生命周期中的总成本**，而不是追求生成代码行数或同时运行的 Agent 数量。

### Principle：把重复问题转化为可复用的工程能力

当 Agent 反复犯相同错误时，先判断原因：

- 需求表达不清楚？→ 更新 Spec 模板。
- 项目知识缺失？→ 更新项目文档与规则。
- 架构边界反复被突破？→ 增加架构约束与 CI 检查。
- 同类 Bug 不断出现？→ 补充回归测试。
- 审查动作重复且稳定？→ 封装 Skill / 命令 / Harness。
- 高风险操作无法阻止？→ 改进工具权限，而不是只写更强硬的 Prompt。

**能用确定性工具解决的事，优先使用确定性工具。** Skills 更适合封装复杂但重复出现的工作流程，而不是替代类型检查、权限隔离或 CI。

### How：建立复盘机制与指标

| 指标 | 意义 |
| --- | --- |
| Lead Time | 需求到可验收交付的耗时 |
| Review / Rework Time | 人工审核与返工投入 |
| Escaped Defects | 进入测试或生产后的回归问题 |
| Change Failure Rate | 发布引发问题的比例 |
| Recovery Time | 事故发生后恢复所需时间 |
| AI Cost | 模型调用与工具运行成本 |
| Maintainability | 依赖复杂度、模块边界、持续维护负担 |

同时保留人的关键理解能力：核心业务不变量、架构决策、数据一致性、权限与生产故障链路不能全部变成“AI 黑盒”。

参考 [gstack](https://github.com/garrytan/gstack) 这样将需求审查、实现、测试和发布流程分工的开源实践，但不必照搬整个工具链。

---

## 九、把七个挑战组织成一套工程闭环

理解完 Why 之后，才谈怎么组织开发。

```text
Explore → Specify → Architect → Plan → Implement
                         ↓               │
                       Review ← Verify ←─┘
                         │
                     Release → Observe → Evolve
                                     └────→ 下一轮 Explore
```

这不是严格的单向瀑布流程。Architect、Verify 与 Review 可在整个开发过程中反复发生。

| 阶段 | 关键问题 | 应留下的最小证据 |
| --- | --- | --- |
| Explore | 系统当前是什么样？ | 影响文件、调用链、现有测试、未知信息 |
| Specify | 目标和质量要求是什么？ | 验收条件、业务不变量、非目标 |
| Architect | 当前方案是否合理？ | 方案权衡、风险与必要 ADR |
| Plan | 如何分成可验证的增量？ | 任务边界、依赖与检查方式 |
| Implement | 如何受控执行？ | 小范围 Diff、提交记录、工具操作记录 |
| Verify | 怎么证明行为正确？ | 测试日志、并发/性能检查、未验证范围 |
| Review | 有哪些隐藏成本或风险？ | 人工审查结论、问题清单 |
| Release | 如何安全交付？ | 发布计划、回滚方式、监控与告警 |
| Observe / Evolve | 运行结果如何反哺工程？ | 事故复盘、规范更新、度量趋势 |

### 9.1 任务风险分级：不要每个 Bug 都走重型流程

| 级别 | 常见任务 | 建议实践 |
| --- | --- | --- |
| L1 低风险 | 文案、格式、明确的小脚本 | 精简上下文、直接实现、快速验证 |
| L2 中风险 | 局部功能与 Bug 修复 | 任务边界、回归测试、人工审查 |
| L3 高风险 | 跨模块接口、数据库迁移、关键链路重构 | Spec、架构审查、小步交付、CI、回滚方案 |
| L4 关键风险 | 支付、鉴权、生产数据、高并发交易、基础设施权限 | 人工主导设计、严格权限隔离、并发与故障验证、审批发布 |

多 Agent 并行只适合**职责清晰且文件、接口依赖较少**的任务。多个 Agent 的独立意见不能替代可执行验证和人工责任。

---

## 十、贯穿案例：让 AI 开发一个支持真实流量的下单接口

我们用“创建订单并扣减库存”把七个问题串起来。假设业务场景是库存有限的商品购买，系统可能遇到并发竞争和重复请求。这里不预设具体 QPS、数据库或部署规模；实际方案要依据业务条件确定。

### 10.1 目标对齐

先规定：成功售出数量不得超过库存；重复请求按幂等规则处理；用户只能操作自己的订单；提交失败不得留下不一致的中间状态。

### 10.2 上下文

Agent 先调查现有订单表、库存表、事务边界、唯一键约束、支付系统和消息队列，确认已有组件和调用链。

### 10.3 架构

讨论条件原子更新、唯一约束、数据库事务和跨服务一致性；明确是否需要异步下单、消息队列，以及复杂度是否与流量规模匹配。关键选择记录 ADR。

### 10.4 执行控制

将工作拆为“数据库约束与迁移”“下单事务”“幂等处理”“并发测试”“监控与发布”等可验证任务。迁移和生产配置需人工审批。

### 10.5 结果可信性

单元与集成测试覆盖正常下单、库存不足、重复提交、部分失败、回滚和权限校验。CI 应真实运行测试，而非仅生成测试文件。

### 10.6 生产可靠性

使用并发测试验证不变量；依据真实流量模型执行 Average / Stress / Spike 测试；观察数据库行锁、连接池、队列、p95/p99、超时与错误率；模拟关键依赖故障；确认限流、降级和回滚策略。

### 10.7 长期可持续性

将本次发现的共性问题沉淀为订单模块规范、事务测试模板、安全检查和持续监控，而不是下次从头解释。

最终交付不是“接口返回 200”，而是：**需求清楚、设计可解释、修改可追踪、行为有证据、生产有保障、后续可维护。**

---

## 十一、可以直接使用的工程模板

### 11.1 最小 `AGENTS.md`

```markdown
# Repository Guidance

## System
- Read README.md for setup and docs/architecture.md for service boundaries.
- Reuse existing patterns before adding new dependencies.

## Change policy
- Keep changes minimal and reversible.
- Do not modify unrelated modules or public APIs without approval.
- Schema migrations, production config changes and external writes need approval.

## Validation
- Run existing relevant tests before and after changes.
- Add regression tests for bug fixes.
- Report commands executed, actual results, unverified items and risks.

## Security
- Never print or commit secrets.
- Do not access or modify production data without explicit authorization.
- Use least privilege and approved environments for load testing.
```

### 11.2 复杂任务启动 Prompt

```text
你作为当前代码库的工程协作者，先调查，不要立即修改代码。

目标：[业务目标]
范围：[本次包含与排除的内容]
约束：[现有架构、技术栈、权限、禁止操作]

请按以下顺序推进：
1. Explore：列出相关文件、调用链、已有测试和不确定信息。
2. Specify：提出必须澄清的问题，整理功能与非功能性验收标准。
3. Architect：比较最小可行方案和备选方案，说明耦合、
   一致性、并发、容量、故障模式与回滚权衡。
4. Plan：将方案拆成可单独测试、可审查的小任务。
5. 等待我确认高影响方案和外部操作权限后再实现。
6. Implement & Verify：实现后执行测试、静态检查；
   高风险任务还需并发、性能或故障验证。
7. Review：报告实际改动、真实运行命令与结果、未验证内容、风险。

不要用“应该没问题”替代验证证据。
不得未经授权操作真实生产环境或敏感数据。
```

### 11.3 PR / 发布检查清单

**需求与设计**

- [ ] 目标、范围、非目标和验收条件清楚。
- [ ] 重大设计变化有权衡与审批记录。
- [ ] 领域边界、接口兼容与安全规则未被意外破坏。

**执行与验证**

- [ ] Diff 聚焦且可以回滚。
- [ ] 测试、类型检查、Lint 和适用的契约检查实际运行。
- [ ] 核心业务不变量和故障边界得到覆盖。
- [ ] 结果含明确的验证证据与未验证范围。

**生产准备（适用于相应风险等级）**

- [ ] 明确流量模型、性能目标及容量假设。
- [ ] 并发正确性和关键故障模式经过验证。
- [ ] 监控、告警、灰度和回滚方案已确认。
- [ ] 生产资源与凭据处于最小权限约束之下。

---

## 十二、如何衡量这套方法是否值得？

不要用生成代码行数、Token 总量或并发 Agent 数量代替结果。

建议选取一组真实且难度相近的任务，记录交付前后的**端到端时间、返工、缺陷、变更失败率、恢复时间、运行成本及代码可维护性**。对新加入的流程（Spec、额外 Review、Skills 等）也要观察它们自身引入的成本。

最终判断标准不是“工程流程是否最完整”，而是：

> 在可接受的风险、成本和人工投入下，是否更稳定地交付了正确、可靠、能够持续演进的软件？

## 十三、总结：从依赖模型能力，到管理系统的不确定性

七个挑战构成了一条完整的 Why → How 逻辑：

| 为什么会有问题（Why） | 因此需要的工程能力（How） |
| --- | --- |
| 人的意图并不天然精确 | **Spec First**，目标和验收契约 |
| 模型并不知道项目的全部事实 | **Context Engineering**，提供相关知识 |
| 局部正确不代表系统结构合理 | **Architecture Review**，权衡设计与守护边界 |
| Agent 不能保证每一步执行都安全正确 | **Small Batch + Guardrails**，限制影响与权限 |
| 生成代码不能证明功能正确 | **Verification First**，建立独立证据 |
| 开发环境不代表真实运行环境 | **Production Reliability**，验证并发、容量、韧性和恢复 |
| 一次交付成功不代表长期效率 | **Continuous Improvement**，把经验沉淀为制度与工具 |

**成熟的 Vibe Coding，不是放弃软件工程，而是更有意识地把软件工程的约束、验证和反馈交给人与工具共同执行。**

---

## 参考资料与延伸阅读

以下链接分为开发者实践、工程方法和生产可靠性资料。社区帖子反映作者个人经验，不等同于普遍适用的实证结论。

### 开发者社区与开源实践

- [V2EX：Vibe coding 了半年，最后发现最有效的手法是朴实无华的左移](https://v2ex.com/t/1218949) —— 需求澄清、提案与验收前置。
- [V2EX：vibe coding 半年感受](https://v2ex.com/t/1219408) —— 对话式需求整理、提效与认知负担。
- [gstack（GitHub）](https://github.com/garrytan/gstack) —— 工程角色与工作流技能化。

### Agentic Engineering 与工程控制

- [Simon Willison：Agentic Engineering Patterns](https://simonwillison.net/guides/agentic-engineering-patterns/) —— 测试、Git、人工验证和理解代码。
- [Claude Code：Best Practices](https://code.claude.com/docs/en/best-practices) —— 上下文、可执行检查、权限与工作流。
- [OpenAI：Iterating Development Workflows with Codex](https://developers.openai.com/cookbook/examples/codex/iterating-development-workflows-with-codex) —— 目标、计划、上下文和分阶段验收。
- [OpenAI：Rethinking Skills and Prompts](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra) —— 避免规则和上下文过度膨胀。

### 性能与生产可靠性

- [Google SRE：Handling Overload](https://sre.google/sre-book/handling-overload/) —— 过载保护与合理退化。
- [Google SRE：Addressing Cascading Failures](https://sre.google/sre-book/addressing-cascading-failures/) —— 级联故障和重试放大。
- [Grafana k6：Automated Performance Testing](https://grafana.com/docs/k6/latest/testing-guides/automated-performance-testing/) —— Smoke、Average、Stress、Spike、Soak 测试。

_更新日期：2026-10-28。本文为对公开实践的整理和工程化抽象，不构成对某个工具、模型或架构方案的性能保证。_
