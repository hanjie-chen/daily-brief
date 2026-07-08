# HN Daily Brief v1 Design

## Background

Daily Brief 是一个个人信息推送聚合项目。第一版先聚焦 Hacker News，输出简单可读的 Markdown 文件。Web 面板、主动推送、更多信息源、长期存储和复杂推荐系统暂不进入 v1。

v1 的核心目标是：以 AI 相关内容为主线，同时保留少量非 AI 高热度内容，避免信息茧房。

## Goals

- 每天生成一份 Hacker News brief。
- AI 内容是主内容区，最近 24 hours 内最多 5 条。
- 非 AI 内容是补充区，每天最多 2 条。
- 如果没有非 AI 内容达到高热度阈值，也展示当天非 AI 最热的 1 条。
- 每条内容包含标题、中文摘要、推荐理由、原文链接、HN discussion 链接、points 和 comments。
- 中文摘要由本地 `codex exec` 生成，暂不直接接 OpenAI API。

## Non-Goals

- 不做 Web UI。
- 不做 Telegram、Slack、Email 或其他推送。
- 不接入第二个信息源。
- 不做用户行为反馈、收藏、已读状态或长期推荐模型。
- 不把输出数量做成复杂个性化系统；v1 先用清晰规则。

## Data Sources

### AI Content

AI 内容使用 Algolia HN Search API 召回。原因是 AI 内容需要按关键词和时间窗口搜索，单纯从 HN `topstories` 中筛选容易漏掉最近 24 hours 内未进入热门榜但仍有价值的 AI 内容。

### Non-AI Hot Content

非 AI 高热度补充使用 HN official API 的 `topstories` 和可选 `beststories`。原因是这部分的目标不是找个人兴趣内容，而是保留 Hacker News 全站视野。

## Content Rules

每日窗口和输出文件名使用 Asia/Singapore 日期。相同 HN story 可能从 Algolia 和 HN official API 同时出现，v1 按 HN item id 去重；如果缺少 item id，则按 source URL 去重。同一条 story 同时符合 AI 和 Non-AI Hot 时，归入 AI section。

### AI Main Section

- Time window: 最近 24 hours。
- Max items: 5。
- Candidate source: Algolia HN Search API。
- Ranking rule: 热度打底，AI 相关性加权。
- 允许内容少于 5 条；如果当天 AI 内容质量不足，不强行补满。

### Non-AI Hot Section

- Candidate source: HN official `topstories`，必要时补充 `beststories`。
- 排除命中 AI 规则的 stories。
- 高热度阈值：`points >= 300` 或 `comments >= 150`。
- 达到阈值时最多输出 2 条。
- 如果没有任何非 AI story 达到阈值，输出非 AI 最热的 1 条作为兜底。

## AI Keyword Strategy

关键词分为权重层级。高权重关键词可以直接把 story 拉入 AI 候选；中低权重关键词需要结合 points、comments 或多个关键词命中。

### High Weight

- AI coding
- coding agent
- agent
- agents
- AI agent
- LLM
- Claude
- OpenAI
- Anthropic
- ChatGPT
- Cursor
- Copilot
- MCP
- RAG
- developer tools

### Medium-High Weight

- workflow
- productivity
- assistant
- chatbot
- AI app
- AI tool
- automation

### Medium Weight

- inference
- fine-tuning
- training
- eval
- benchmark
- GPU
- embedding
- vector database

### Low Weight

- Gemini
- Google AI
- Meta AI
- xAI
- Mistral
- Perplexity
- funding
- acquisition
- regulation
- lawsuit

低权重的 AI 公司、融资、诉讼、监管和行业新闻会进入候选，但除非热度较高或与高权重关键词共同命中，否则排序靠后。

## Ranking

v1 使用透明的规则打分，不引入复杂模型。

- Base heat: points 和 comments 越高，基础热度越高。
- AI relevance: 高权重关键词加分最多，中低权重关键词加分较少。
- Topic preference: AI coding、agents、developer tools、workflow 和 productivity 方向优先。
- Weak keyword guard: `agent`、`model`、`workflow`、`automation` 等泛词不能单独决定入选，需要热度或其他关键词共同支持。

## Output Format

输出路径建议为：

```text
briefs/YYYY-MM-DD.md
```

Markdown 结构建议：

```markdown
# Daily Brief - YYYY-MM-DD

## Hacker News: AI

### Title

- Summary: 中文摘要
- Why: 推荐理由或命中关键词
- Source: 原文链接
- Discussion: HN discussion 链接
- Stats: points / comments

## Hacker News: Non-AI Hot

### Title

- Summary: 中文摘要
- Why: 全站高热度或今日非 AI 最热
- Source: 原文链接
- Discussion: HN discussion 链接
- Stats: points / comments
```

## Summary Generation

每条内容先整理成结构化 input，再交给 `codex exec` 生成中文摘要。

输入信息包括：

- title
- source URL
- HN discussion URL
- points
- comments
- matched keywords
- story text 或抓取到的正文片段，如果可用

如果原文正文无法抓取，摘要生成退化为基于 title、HN metadata 和可用 text 的短摘要。`codex exec` 适合本地个人 daily brief；如果未来部署到服务器或需要更稳定的自动化认证，再考虑改成直接使用 API。

## Error Handling

- Algolia 请求失败时，AI section 可以为空，并在输出中写明当天 AI 数据源失败。
- HN official API 请求失败时，Non-AI Hot section 可以为空，并写明当天 HN 热门数据源失败。
- 单条原文抓取失败时，不影响该 story 入选，只跳过正文片段。
- `codex exec` 摘要失败时，保留标题、链接、推荐理由和 stats，并把 Summary 标为摘要生成失败。

## Testing

v1 需要覆盖以下行为：

- AI keyword matching 能区分高权重、中权重和低权重关键词。
- `agent`、`model` 等弱关键词不会单独造成明显误判。
- AI section 最多输出 5 条。
- Non-AI Hot section 达阈值时最多输出 2 条。
- Non-AI Hot section 没有达阈值时输出最热 1 条。
- 输出 Markdown 包含标题、摘要、推荐理由、链接和 stats。
- 数据源失败或摘要失败时，脚本仍能生成可读 Markdown。

## Deferred Decisions

- 是否改用 48 hours 窗口：v1 先用 24 hours；如果发现经常漏掉第二天才发酵的 AI 内容，再考虑 48 hours 加去重。
- 是否接入更多信息源：HN v1 稳定后再讨论。
- 是否做 Web UI 或推送：Markdown 输出稳定后再讨论。
- 是否改用 OpenAI API：本地 `codex exec` 方案跑通后再评估。
