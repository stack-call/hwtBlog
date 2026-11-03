---
title: 2026 AI 开发 Skills 检索与推荐：8 个开源项目、阶段对照表与三种组合
description: 检索 GitHub、skills.sh 排行榜与多个官方仓库，按开发阶段梳理适用于 Claude Code、Codex、Cursor、OpenCode 的 Agent Skills，附安装命令、三种组合方案与选型注意事项。
date: 2026-11-03
slug: ai-dev-skills
tags:
  - ai-coding
  - agentic-engineering
  - claude-code
  - codex
  - skills
keywords:
  - Agent Skills
  - Skills.sh
  - Superpowers
  - Matt Pocock Skills
  - Vercel Agent Skills
  - Anthropic Skills
  - Claude Code
  - Codex
---

# 2026 AI 开发 Skills 检索与推荐：8 个开源项目、阶段对照表与三种组合

这一年 AI Coding Agent 的竞争，已经从「模型有多强」转向「你给它装了什么」。同一个 Claude Code 或 Codex，配上不同的 Skills，产出的代码质量、流程纪律和可维护性可以差得很远。

本文是一次检索的整理结果：从 GitHub、[skills.sh](https://skills.sh/) 排行榜以及多个项目的官方文档出发，筛出**适用于 Claude Code、Codex、Cursor、OpenCode 等 AI Coding Agent 的 Skills**，按「生态 → 开发阶段 → 热度 → 组合 → 安装」的顺序组织，方便按需取用而不是一次性全装。

先说结论：如果只是对 `grill-me` 这类需求追问 Skill 感兴趣，建议重点研究 **Matt Pocock Skills、Superpowers、Vercel Agent Skills、Anthropic Skills** 这四个生态。它们分别覆盖需求决策、软件开发流程、前端工程和通用 Agent 能力。

<!-- truncate -->

> [!NOTE] 阅读说明
> 下文的 Skill 名称来自各项目官方仓库的文档与目录结构，安装量来自 skills.sh 榜单在某次检索时的展示值，**均为近似值**，会随时间变化，不代表独立用户数或实际使用效果。个别项目名称与知名开发者同名但并非其官方项目，文中已单独标注。

## 先搞清楚 Skill 是什么

Agent Skills 目前的事实标准形态很简单：**一个文件夹加一个 `SKILL.md`**。`SKILL.md` 里是给 Agent 的指令，可以带脚本、模板、参考资料等同目录文件。

它和另外几种常见扩展方式的区别：

| 机制 | 加载时机 | 典型用途 |
| --- | --- | --- |
| System Prompt | 每次请求都在上下文里 | 全局人设、硬约束 |
| MCP Server | 常驻工具，随时可调 | 访问外部系统 |
| Subagent | 主 Agent 主动派活 | 隔离上下文的子任务 |
| **Skill** | 按需读取（描述命中才展开） | 把一套工作方法写成可复用指令 |

关键在最后一行：Skill 是**渐进披露**的，平时只占一行描述，被判定相关时才把正文读进上下文。所以装二三十个 Skill 不会像塞二三十段 Prompt 那样立刻把窗口撑爆——但装的越多，命中判断越容易出错，指令之间也越容易互相打架。这个矛盾后面还会提到。

## 一、最值得关注的 8 个开源项目

| 项目 | 定位 | 代表能力 |
| --- | --- | --- |
| [Superpowers](https://github.com/obra/superpowers) | 完整软件工程工作流 | `brainstorming`、`writing-plans`、`tdd`、`systematic-debugging`、`code-review` |
| [Matt Pocock Skills](https://github.com/mattpocock/skills) | 需求澄清、架构设计、工程质量 | `grill-me`、`grill-with-docs`、`tdd`、`codebase-design`、`improve-codebase-architecture` |
| [Vercel Agent Skills](https://github.com/vercel-labs/agent-skills) | React、Next.js、Web 性能与设计规范 | `vercel-react-best-practices`、`web-design-guidelines` |
| [Anthropic Skills](https://github.com/anthropics/skills) | Agent Skills 官方参考实现 | `frontend-design`、Web 应用测试、MCP 开发、`skill-creator` |
| [UI UX Pro Max](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) | UI/UX 设计与设计系统 | 风格、配色、字体、组件与 UX 规范的可搜索知识 |
| [Sentry Skills](https://github.com/getsentry/skills) | 代码审查、代码简化、工程规范 | `code-review`、`code-simplifier`、`agents-md`、Django 专项审查 |
| [Vercel Skills CLI](https://github.com/vercel-labs/skills) | Skills 搜索、安装、更新 | `find-skills` |
| [Karpathy Guidelines](https://github.com/multica-ai/andrej-karpathy-skills) | 代码生成行为规范 | 先思考、保持简单、小范围修改、目标驱动执行 |

逐个说下取舍：

**1. Superpowers —— 把开发流程写进 Agent**

包含 `brainstorming`、`writing-plans`、`test-driven-development`、`systematic-debugging`、`code-review` 等能力，本质是把「先想清楚再动手」固化成 Agent 必须走的路径。适合希望 Agent 严格遵循开发流程、宁可慢一点也不要跳步的场景。

**2. Matt Pocock Skills —— 小型、可组合的工程能力**

`grill-me`、`grill-with-docs`、`tdd`、`codebase-design`、`improve-codebase-architecture`。和 Superpowers 的「大流程」相反，它强调单个 Skill 足够小、可自由拼装。`grill-me` 的流行也说明一件事：**大多数人缺的不是写代码能力，而是把需求问清楚的那一步**。

**3. Vercel Agent Skills —— 前端工程规范**

`vercel-react-best-practices`、`web-design-guidelines` 和部署优化相关能力。如果你的技术栈是 Next.js，这套基本是刚需——它把 Vercel 官方那套性能与可访问性经验直接变成了 Agent 的检查项。

**4. Anthropic Skills —— 学怎么写 Skill，而不只是用**

包含 `frontend-design`、Web 应用测试、MCP 开发和 Skill 创建等示例，也包含文档处理能力。它的价值更多在**参考实现**：想自己写 Skill 的话，这是最权威的样板。

**5. UI UX Pro Max —— 让界面不那么「AI 味」**

提供风格、配色、字体、组件和 UX 规范等可搜索的设计知识。适合让 AI 生成的界面摆脱默认的紫蓝渐变 + 圆角卡片。

**6. Sentry Skills —— 生产团队的工程纪律**

由 Sentry 团队维护，包含 `code-review`、`code-simplifier`、`agents-md` 等工程技能，以及 Django 专项审查。`code-simplifier` 的思路值得单独学：**在保留行为的前提下简化代码**，而不是顺手加功能。

**7. Vercel Skills CLI —— 统一安装入口**

本身不是能力集合，而是 Skill 的管理工具，包含 `find-skills`。很适合作为其他项目的统一安装与更新入口，避免每个生态各装一套。

**8. Karpathy Guidelines —— 改善默认编程习惯**

强调先思考、保持简单、小范围修改和目标驱动执行。

> [!WARNING] 关于这个名字
> 这个仓库（`multica-ai/andrej-karpathy-skills`）**并非 Andrej Karpathy 本人维护的官方项目**，只是把他的公开言论与风格整理成 Skill 指令。引用时别写成「Karpathy 官方 Skills」。

## 二、按开发阶段检索的常见 Skills

比看热门榜更实用的方式，是直接按「我现在在做哪一步」来选。

| 开发阶段 | 常见 Skill | 来源 | 主要用途 |
| --- | --- | --- | --- |
| 需求澄清 | `grill-me` | Matt Pocock | 连续追问需求 |
| 需求分析 | `brainstorming` | Superpowers | 讨论并明确设计方案 |
| 领域建模 | `domain-modeling` | Matt Pocock | 统一业务术语和模型 |
| 技术规划 | `writing-plans` | Superpowers | 编写实施计划 |
| 规格设计 | `to-spec` | Matt Pocock | 将讨论转为规格文档 |
| 任务拆分 | `to-tickets` | Matt Pocock | 拆解任务与依赖 |
| 编码实现 | `implement` | Matt Pocock | 基于规格实施 |
| 测试驱动 | `tdd` | Matt Pocock | Red-Green-Refactor |
| 系统调试 | `systematic-debugging` | Superpowers | 根因调查 |
| Bug 诊断 | `diagnosing-bugs` | Matt Pocock | 复现、定位、回归验证 |
| 代码评审 | `code-review` | Sentry / Matt Pocock | 检查代码质量和规范 |
| 代码简化 | `code-simplifier` | Sentry | 在保留行为的前提下简化代码 |
| 架构优化 | `improve-codebase-architecture` | Matt Pocock | 寻找模块设计问题 |
| React 性能 | `vercel-react-best-practices` | Vercel | 前端性能优化 |
| UI 设计 | `frontend-design` | Anthropic | 生成高质量界面 |
| UI 审核 | `web-design-guidelines` | Vercel | 审核可访问性与交互 |
| Git 协作 | `using-git-worktrees` | Superpowers | 并行开发隔离 |
| 任务交接 | `handoff` | Matt Pocock | 为下一轮 Agent 保存上下文 |
| Skill 开发 | `skill-creator` | Anthropic | 创建和改进 Skills |
| Skill 搜索 | `find-skills` | Vercel | 发现适用 Skills |

这些名称来自所检索项目的官方文档，部分仓库同时包含相近能力。

有两个位置值得特别注意：

- **`using-git-worktrees` + `handoff`**：这两个组合起来才构成并行 Agent 开发的基础。前者让多个 Agent 在不同 worktree 里互不干扰，后者让上下文能在会话之间传递。只装其中一个，收益有限。
- **`code-review` 有两个来源**：Sentry 和 Matt Pocock 都有实现，侧重点不同（前者偏工程规范与框架专项，后者偏通用质量）。同时装会出现两份相似的审查指令，建议只留一个。

## 三、当前热门的开发 Skills

根据 [skills.sh 排行榜](https://skills.sh/) 的检索结果，以下是安装量较高、且与软件开发直接相关的 Skills：

| 排名 | Skill | 累计安装量（近似） |
| --- | --- | --- |
| 1 | `find-skills` | 4.2M |
| 2 | `grill-me` | 1.5M |
| 3 | `agent-browser` | 1.3M |
| 4 | `grill-with-docs` | 1.3M |
| 5 | `improve-codebase-architecture` | 1.2M |
| 6 | `tdd` | 1.2M |
| 7 | `frontend-design` | 1.1M |
| 8 | `grilling` | 1.0M |

这张表最值得注意的是**第一名不是任何能力型 Skill，而是 `find-skills`**。说明大家实际遇到的问题不是「没有好 Skill」，而是「不知道有什么、装哪个」。排行榜本身也就成了这个问题的答案。

另外 `grill-me`、`grill-with-docs`、`grilling` 三个都挤进前八，说明「让 Agent 先把需求问清楚」确实是当前最普遍的需求。

> [!CAUTION] 安装量不等于质量
> 榜单统计的是安装次数，同一个用户可以重复安装、可以 CI 里批量拉取，也无法反映 Skill 是否真的被调用、效果如何。把它当作**关注度**指标可以，当作**质量**指标会踩坑。

## 四、推荐的三种组合

### 组合 A：日常全栈开发（优先推荐）

`grill-me` + `writing-plans` + `tdd` + `code-review` + `systematic-debugging`

需求、计划、编码质量和问题定位都有覆盖，而且不需要完全引入一个复杂框架。这五个几乎覆盖了日常开发里最容易出问题的地方：**需求没对齐、计划没写、测试没做、代码没审、bug 没找到根因**。

### 组合 B：前端 / React / Next.js

`frontend-design` + `ui-ux-pro-max` + `vercel-react-best-practices` + `web-design-guidelines`

覆盖视觉设计、页面实现、性能和可访问性。两个设计类 Skill（`frontend-design` 与 `ui-ux-pro-max`）的指令可能重叠，建议按任务分别调用，不要在同一轮里同时启用。

### 组合 C：大型项目与长期维护

`grill-with-docs` + `domain-modeling` + `to-spec` + `to-tickets` + `improve-codebase-architecture` + `handoff`

更适合业务复杂、需要跨多轮 Agent 会话持续开发的项目。这条链路的重点是**把知识落盘**：领域模型进文档，讨论变规格，规格拆 ticket，会话结束前用 `handoff` 把上下文交接出去。

## 五、如何安装和搜索

可以使用 Vercel 的 Skills CLI。以下命令是独立示例，按需要选择安装，不必全部执行。

```bash
# 搜索开发 Skills
npx skills find testing
npx skills find code-review
npx skills find architecture

# 安装 Superpowers
npx skills add obra/superpowers

# 安装 Matt Pocock Skills
npx skills add mattpocock/skills

# 安装 Vercel Agent Skills
npx skills add vercel-labs/agent-skills

# 安装 Anthropic Skills
npx skills add anthropics/skills

# 检查已安装 Skills 的更新
npx skills update
```

安装器通常支持选择具体的 Skill 和目标 Agent，也就是可以只从某个仓库里挑几个装，而不是整包全收。

> [!IMPORTANT] 别重复安装同一套指令
> 不同 Agent 的原生插件安装与 Skills CLI 安装可能使用不同的更新机制。同一个 Skill 若既通过 Agent 插件装了、又通过 CLI 装了一遍，会出现两份内容不同步的副本——更新时只更新了一份，Agent 可能读到旧版本。选一种方式并保持统一。

## 六、三个持续找新 Skills 的入口

- [skills.sh](https://skills.sh/) —— 安装量排行榜及搜索
- [Anthropic Skills](https://github.com/anthropics/skills) —— Skill 编写范例，学格式看这里
- [Superpowers](https://github.com/obra/superpowers) —— 工程工作流参考，学流程设计看这里

## 七、我的建议

先从 `grill-me`、`systematic-debugging`、`tdd`、`code-review`、`find-skills` 这五种能力开始。

理由不是它们最热门，而是它们**覆盖了开发中比较常见的痛点，又不会一开始就让 Agent 的指令体系过于复杂**：

| Skill | 解决的痛点 |
| --- | --- |
| `grill-me` | 需求没问清楚就开写 |
| `systematic-debugging` | 瞎改一通碰运气修 bug |
| `tdd` | 写完没有验证手段 |
| `code-review` | 没人（包括 Agent 自己）回头看代码 |
| `find-skills` | 不知道该装什么 |

等这五个用顺了，再按项目类型补前端或架构类的 Skill。反过来，一上手就把 Superpowers 整个装进去、再叠几个重叠的代码审查 Skill，很容易出现指令互相覆盖、Agent 行为飘忽的情况——那时你很难判断到底是哪个 Skill 起了反作用。

## 准确性说明

这份整理有几处已知的局限，使用时请留意：

1. **安装量是榜单近似值**。skills.sh 上展示的数字会变化，本文记录的是检索时的展示值，语义上也只代表安装次数，不代表独立用户数或效果。
2. **`Karpathy Guidelines` 不是 Karpathy 官方项目**。仓库位于 `multica-ai` 组织下，内容是对其公开观点的整理。
3. **Skill 名称可能随仓库演进变化**。开源项目的目录结构和 Skill 命名调整很频繁，安装前建议直接看仓库 README 确认当前名称。
4. **不同 Agent 的支持程度不同**。Claude Code、Codex、Cursor、OpenCode 对 Skills 的加载机制、触发条件和目录约定并不完全一致，同一个 Skill 在不同 Agent 上的实际表现可能有差异。
5. **本文是检索整理，不是评测**。文中没有对各 Skill 做横向对照实验，排列顺序反映的是关注度与生态完整度，而非实测效果排序。

## 相关阅读

- [Vibe Coding 工程化实践：七大挑战、系统质量属性与可靠交付](https://stack-call.github.io/hwtBlog/blog/ai-engineering/vibe-coding-engineering-v3)：Skills 解决的是「Agent 该怎么做」，那篇解决的是「整个交付链路怎么组织」。
- [Vibe Coding 该怎么做：让 AI 的产出速度，匹配你的理解速度](https://stack-call.github.io/hwtBlog/blog/how-to-vibe-code-well)：`grill-me`、`handoff` 这类 Skill 想解决的问题，本质上是理解速度跟不上生成速度。

## 参考来源

### 项目仓库

1. [obra/superpowers](https://github.com/obra/superpowers)
2. [mattpocock/skills](https://github.com/mattpocock/skills)
3. [vercel-labs/agent-skills](https://github.com/vercel-labs/agent-skills)
4. [anthropics/skills](https://github.com/anthropics/skills)
5. [nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill)
6. [getsentry/skills](https://github.com/getsentry/skills)
7. [vercel-labs/skills](https://github.com/vercel-labs/skills)
8. [multica-ai/andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills)

### 排行榜与入口

9. [skills.sh：Agent Skills 安装量排行榜与搜索](https://skills.sh/)
