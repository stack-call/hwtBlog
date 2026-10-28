---
title: 2026 Vibe Coding 实践调研：国内外程序员到底怎么用 AI 写代码？
description: 调研 V2EX、掘金、LINUX DO、Reddit、Hacker News 与 GitHub 上的真实开发经验，比较 Plan First、上下文工程、测试驱动、Multi-Agent 与 Agent Harness 的实践及争议。
date: 2026-10-28
tags:
  - vibe-coding
  - ai-coding
  - agentic-engineering
  - claude-code
  - codex
keywords:
  - Vibe Coding
  - Agentic Engineering
  - AI 编程
  - Claude Code
  - Codex
  - Context Engineering
---

# 2026 Vibe Coding 实践调研：国内外程序员到底怎么用 AI 写代码？

AI 编程工具已经不只是“帮我补全几行代码”。越来越多程序员开始让 Claude Code、Codex、Cursor 等编码 Agent 阅读项目、规划任务、修改多个文件、运行测试，甚至互相审查代码。

但新的问题也随之出现：**生成代码越来越快，需求对齐、测试、代码审查和后期维护是否跟得上？**

本文整理了截至 **2026 年 10 月 28 日**，国内外开发者社区公开分享的 Vibe Coding 实践。关注的不是工具排行榜，而是三件事：**大家实际上怎么做、怎样评价效果，以及有哪些分歧。**

<!-- truncate -->

> **阅读说明**：下文会区分“开发者的个人自述”“公开仓库中可观察的实践”和“研究论文的归纳”。社区帖子中的效率数据、项目规模及主观体验通常没有经过独立核验，不能当作行业平均值。

## 一、先区分两个容易混淆的概念

**狭义 Vibe Coding** 更接近“用自然语言指挥 AI 生成代码，主要通过运行效果判断结果”，开发者可能不细读每一行实现。

**Agentic Engineering** 则是让 AI Agent 承担大量实现工作，但开发者仍通过架构约束、明确验收条件、版本控制、自动化测试和 Review 管理交付质量。

现实社区里，这两个概念经常混用。对于有经验的程序员，值得研究的往往是后者：**把 AI 编程纳入成熟的软件工程流程，而不是单纯追求生成速度。**

## 二、国内社区：从“让 AI 写”到“建立可控工作流”

### 1. V2EX：半年实践后，工作流反而变简单

在 V2EX 的[《vibe coding 半年感受》](https://v2ex.com/t/1219408)中，作者回顾了使用 Codex、Claude 以及 Rules、Skills、Plan Mode、Spec 等功能的经历。

相比不断叠加复杂工具，作者最后更常采用的方式是：

1. 先通过对话澄清需求。
2. 让 AI 总结修改点和实施方案。
3. 人确认后，再让 AI 执行。

作者认为效率有所提高，但也提到新的成本：短时间内需要处理更多技术决策，注意力和认知负担不一定下降。评论中还有开发者表示，冗长的规划文档反而影响节奏，更希望 AI 提供精炼的方案或 API 契约。

**可观察的分歧**：Plan 很有价值，但 Plan 是否需要写得很长，并没有统一答案。

### 2. V2EX：CLAUDE.md、Hooks 和 Subagents 成为高频用法

在[《Claude Code 三个月实战：7 个真正提效的技巧》](https://global.v2ex.com/t/1245559)中，作者分享了自己逐步固定下来的使用方式：

- 用 `CLAUDE.md` 保存目录结构、项目规则及测试命令；
- 对复杂功能先进入 Plan Mode；
- 用自定义命令封装常用操作；
- 用 Hooks 约束高风险行为、自动运行检查；
- 利用会话恢复和 Subagents 处理长任务或独立子任务。

评论区也有人提醒：**不是 Skill 装得越多越好**。一些问题直接给 Agent 截图、日志和真实运行结果，比堆叠规则更有效。

这类分享显示，使用重点正在从“提示词怎么写”转向“Agent 的工作环境怎么组织”。

### 3. 掘金：个人小项目和企业存量项目不能一套打法

[《Agent Coding 的个人最佳实践》](https://juejin.cn/post/7615039996734701606)提出了一个重要区别：

- **个人小项目**：技术栈明确、功能简单时，先给规则再让 AI 快速实现，往往比较顺畅。
- **大型公司项目**：存在大量隐含约束、历史逻辑和跨模块依赖时，同样的做法可能导致理解偏差及大规模返工。

作者分享了在复杂项目中直接套用小项目工作流而失败的经历，随后更加重视结构化需求、渐进实施和代码审查。

**值得记住**：Agent 能从零构建功能，不代表它能安全地修改一个它尚未理解的复杂系统。

### 4. V2EX：把需求澄清和验收“左移”

另一篇[《Vibe coding 了半年，最后发现最有效的手法是朴实无华的左移》](https://v2ex.com/t/1218949)分享了一个名为 Chorus 的工作流思路：

```text
需求细化（Elaboration）
        ↓
技术提案（Proposal）
        ↓
实现（Implementation）
        ↓
任务验收（Task Review）
```

作者把重点放在编码前的需求对齐，以及编码后的独立验收。方案可以交给 Reviewer Agent 检查，实现完成后再按验收清单复核。

支持者认为这能减少返工；反对者担心前置流程过重，会让探索性开发变成另一种瀑布模式。

**这场争论真正的问题是**：面对不同不确定性和风险等级，应该投入多少规划成本？

### 5. LINUX DO 与 V2EX：不同模型分工协作

在[LINUX DO 的 Vibe Coding 讨论](https://linux.do/t/topic/2101386)和[V2EX 工具组合交流](https://v2ex.com/t/1194068)中，一些开发者分享了混合工作流，例如：

- Claude Code 负责实现，Codex 负责独立审查；
- Codex 先规划，Claude Code 再实现；
- 通过 OpenSpec 管理需求和任务，再交给编码 Agent；
- 使用 Git Worktree 隔离多个开发任务。

这不是严格的工具性能对比，也不能说明某种组合必然更强。它反映的是一个实践趋势：**有人开始把规划、实现与验证视为不同角色，而不是全部交给一个长会话。**

## 三、国外社区：测试、并行与可维护性成为焦点

### 1. Reddit：Plan → 实现 → 测试 → Git 回滚点

一位有多年经验的程序员在 [r/ClaudeAI 分享个人工作流](https://www.reddit.com/r/ClaudeAI/comments/1tj2i90/)。他会让 Claude Code 承担大量编码任务，有时不逐行阅读代码，但并不意味着完全放弃工程控制。

他的实践包括：

1. 先要求 Agent 给出能理解的方案；若方案过大，则进一步拆分。
2. 按阶段实现，每一步留下 Git 提交或回滚点。
3. 运行测试，并对 Web 应用做真实浏览器交互验证。

这提供了一个有意思的反例：**即便不逐行阅读每一行生成代码，仍然可以通过可观察行为、自动化测试与版本控制增加可控性**。但对于安全敏感、关键业务逻辑或高风险改动，这些手段仍不能完全替代人工代码审查。

### 2. Simon Willison：第一件事是运行现有测试

开发者 [Simon Willison 的 Agentic Engineering Patterns](https://simonwillison.net/guides/agentic-engineering-patterns/)强调，一个编码 Agent 的核心优势，不只是生成代码，而是能够运行代码、观察失败并继续修正。

其方法中值得借鉴的几个要点：

- 接手已有仓库时，先让 Agent **运行当前测试**，确认基线状态；
- 对适合的任务采用 **Red/Green TDD**：先出现合理失败，再实现到测试通过；
- 对 Web 功能进行真实端到端验证，而不只相信 Agent 声称的“已完成”。

如果没有独立验证，Agent 写得越快，潜在问题也可能积累得越快。

### 3. Hacker News：多 Agent 并行可能放大注意力成本

在 [Ask HN: How do you maintain flow when vibe coding?](https://news.ycombinator.com/item?id=47797632) 的讨论中，有开发者提到：同时管理多个 Agent，自己需要频繁切换上下文；如果放任 Agent 长时间独立运行，又可能产生难以发现的细微问题。

参与者提出的应对办法包括：

- 让人专注一个复杂任务，其他 Agent 只处理低依赖的简单工作；
- 在任务开始时集中确认约束，减少中途打断；
- 每次人工接手时，要求 Agent 汇总已完成内容、未解决问题及下一步；
- 以集成测试、模拟依赖和审查结果为验收依据。

**启示**：并行 Agent 数量不是生产力指标；真正要优化的是单位注意力能够管理的有效交付量。

### 4. Reddit：最难的是从 70% 走到可发布

在 [How do you go from 70% to finished?](https://www.reddit.com/r/ClaudeAI/comments/1v9rsei/) 的讨论中，有独立开发者表示，AI 很快完成了功能雏形，但项目临近完成时，遗留 Bug、未连接的模块、UI 问题与反复返工越来越多。

讨论中较有代表性的收尾方式是：

1. 暂停继续添加功能，整理完整的问题清单；
2. 按用户核心路径及问题严重程度排序；
3. 使用新的会话审查现有实现，避免沿用原会话的假设；
4. 小批量修复后，重新执行回归测试并保存版本。

这是 Vibe Coding 很容易忽视的阶段：**从“能展示的 Demo”到“可持续维护的产品”，工作重点会发生变化。**

### 5. GitHub：有人把工作流做成 Harness

一些公开仓库不只分享 Prompt，而是把角色分工、质量门禁和常用命令做成可复用工程设施。

**[gstack](https://github.com/garrytan/gstack)** 将需求审视、技术评审、调查 Bug、代码 Review、QA 和发布等环节封装为不同技能和流程。

**[AgentDeck 的 Harness 设计](https://github.com/puritysb/AgentDeck/blob/master/docs/agent-harness.md)** 展示了如何组织项目级规则、领域知识与多个编码工具之间的协作。

这些仓库体现出一种方向：**从优化单次对话，升级为持续维护 Agent 的开发环境和操作规范。**

### 6. 国外也存在明确的反对意见

在 Hacker News 的[“重新回到手写代码”讨论](https://news.ycombinator.com/item?id=46765460)中，一些程序员担心，长期依赖 AI 会削弱对系统细节的掌控；另一些人认为，只要坚持理解和验证生成代码，AI 可以释放时间去研究更重要的问题。

这种分歧不能简单归纳为“支持 AI”或“反对 AI”。更深层的问题是：**使用者是否仍拥有对代码、架构和生产系统的控制能力。**

## 四、把国内外实践放在一起比较

| 方式 | 社区常见用法 | 主要收益 | 典型风险 |
| --- | --- | --- | --- |
| Plan First | 先调查代码库、确认方案，再实现 | 减少需求误解 | 过度规划，降低探索效率 |
| `AGENTS.md` / `CLAUDE.md` | 固化项目规则、命令与边界 | 减少重复沟通 | 规则过多、过时或互相冲突 |
| TDD / 自动化验证 | Agent 先写测试、运行测试并修复 | 提高可验证性 | 测试本身可能有错误 |
| 多模型交叉审查 | 一个 Agent 实现，另一个 Review | 提供不同检查视角 | 两个模型可能共享相似盲区 |
| Git Worktree 并行 | 每个 Agent 在独立分支开发 | 任务隔离、可并行 | 冲突、集成成本和注意力负担 |
| Skills / Hooks / Harness | 将重复工作固定为流程 | 可复用、便于团队协作 | 小项目可能过度工程化 |
| 黑盒式开发 | 更多依据运行结果验收 | 原型速度快 | 维护性与安全性难以把握 |

表格是对公开案例的**定性归纳**，并不表示各实践在开发者群体中的使用比例或效果排名。

## 五、哪些结论有研究支持，哪些还只是经验？

2026 年 ICSE 一项关于 Vibe Coding 实践的[研究论文](https://doi.org/10.1145/3786583.3786866)，分析了 101 份实践者资料并提取 518 条一手记录。研究指出，快速获得原型和即时反馈是 Vibe Coding 的重要吸引力，但跳过测试、缺乏审查等行为也带来可靠性与维护性问题。

需要注意，这类研究分析的是公开经验资料，不能证明某个具体工具组合一定比另一种更高效，也不能把社区自报的节省时间比例直接当作客观指标。

就现有资料而言，可以做出相对稳妥的区分：

- **较一致的工程原则**：清晰上下文、明确验收标准、可执行测试、版本控制、人工负责高风险决策。
- **仍有明显分歧的选择**：Plan 应该多详细、同时运行几个 Agent、是否必须通读所有代码、要不要引入完整 Harness。

## 六、面向程序员：如何把这些经验变成自己的工作流？

结合上述案例，可以从一套轻量流程开始，而不是一开始就搭建“全自动软件工厂”。以下是**基于调研提出的建议**，不是某篇原帖的原样流程。

### 阶段一：调查，而不是立即修改

```text
请先不要修改代码。
阅读相关模块、调用链、现有测试及项目规范。
输出：
1. 已确认的现有行为（附文件路径）；
2. 需要修改的最小范围；
3. 可能的兼容性、安全及性能风险；
4. 尚未确认的假设。
```

### 阶段二：规划可独立验证的改动

要求 Agent 给出简短计划，每一步都明确：

- 修改哪些文件；
- 预期行为是什么；
- 如何测试；
- 失败时如何回滚。

复杂任务可以先审方案；小任务则不必制造冗长文档。

### 阶段三：让验证与实现一起推进

```text
按已确认计划实现一个最小任务。
优先补充或更新相关测试。
实际运行测试、类型检查和 Lint。
不要只报告“应该通过”；请列出执行命令和结果。
如果无法运行，请说明原因和未验证范围。
```

### 阶段四：Review、提交、再继续

关注最终 Diff 是否引入无关修改，检查异常路径、边界输入、权限与敏感信息处理。每完成一个可验收单元，就创建 Git 提交或 PR，避免将很多不相关修改混在一次交付里。

### 阶段五：只沉淀真正重复的规则

当某种错误反复发生时，再把约定写进 `AGENTS.md` 或 `CLAUDE.md`；当某个工作流需要反复手动提示时，再考虑 Skill 或 Hook。尽量让 Linter、测试和 CI 承担可以程序化执行的约束。

## 七、结论：从“更多代码”转向“更可靠的交付”

观察国内外实践，有三个方向值得关注：

1. **上下文工程**：让 Agent 更准确地理解项目现状、规则和历史决策。
2. **可执行验证**：让代码正确性更多通过测试和真实运行结果判断，而不是依赖 AI 的自我报告。
3. **可控的 Agent 协作**：在合适的地方引入独立审查、任务隔离和复用流程，但不过度追求 Agent 数量。

对程序员而言，Vibe Coding 的长期价值可能不是“终于不用写代码了”，而是：**把更多精力放在需求判断、系统设计和质量控制上，同时让 AI 承担可验证的实现工作。**

至于最适合你的 Plan 深度、Agent 数量和工具组合，仍需要在自己的真实代码库中通过交付时间、返工率、回归缺陷和审查成本来验证。

---

## 参考资料与延伸阅读

### 国内开发者社区

1. [V2EX：《vibe coding 半年感受》](https://v2ex.com/t/1219408)
2. [V2EX：《Claude Code 三个月实战：7 个真正提效的技巧》](https://global.v2ex.com/t/1245559)
3. [掘金：《Agent Coding 的个人最佳实践》](https://juejin.cn/post/7615039996734701606)
4. [V2EX：《Vibe coding 了半年，最后发现最有效的手法是朴实无华的左移》](https://v2ex.com/t/1218949)
5. [LINUX DO：Vibe Coding 工作流讨论](https://linux.do/t/topic/2101386)
6. [V2EX：AI 编码工具组合讨论](https://v2ex.com/t/1194068)

### 海外社区与开源项目

7. [Reddit：r/ClaudeAI 开发工作流分享](https://www.reddit.com/r/ClaudeAI/comments/1tj2i90/)
8. [Simon Willison：Agentic Engineering Patterns](https://simonwillison.net/guides/agentic-engineering-patterns/)
9. [Hacker News：How do you maintain flow when vibe coding?](https://news.ycombinator.com/item?id=47797632)
10. [Reddit：How do you go from 70% to finished?](https://www.reddit.com/r/ClaudeAI/comments/1v9rsei/)
11. [GitHub：garrytan/gstack](https://github.com/garrytan/gstack)
12. [GitHub：AgentDeck Harness 设计](https://github.com/puritysb/AgentDeck/blob/master/docs/agent-harness.md)
13. [Hacker News：After two years of vibecoding, I'm back to writing by hand](https://news.ycombinator.com/item?id=46765460)

### 学术研究

14. [ICSE 2026：Vibe Coding in Practice（DOI）](https://doi.org/10.1145/3786583.3786866)

> **方法说明**：文章是一份基于公开资料的定性调研，并非系统抽样调查；原帖内容与链接可能随时间变化。除明确注明的研究资料外，案例主要反映作者自述，不代表普遍效果。
