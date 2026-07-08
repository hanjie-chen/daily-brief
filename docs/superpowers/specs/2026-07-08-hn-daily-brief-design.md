# HN Daily Brief v1 Design

## Background

Daily Brief 是一个个人信息推送聚合项目。第一版先聚焦 Hacker News，输出简单可读的 Markdown 文件。Web 面板、主动推送、更多信息源、长期存储和复杂推荐系统暂不进入 v1。

v1 的核心目标是：以 AI 相关内容为主线，同时保留少量非 AI 高热度内容，避免信息茧房。

## Goals

- 每天生成一份 Hacker News brief。
- 默认每天 08:00 Asia/Singapore 生成，24 hours 窗口为前一天 08:00 到当天 08:00。
- AI 内容是主内容区，最近 24 hours 内最多 5 条。
- 非 AI 内容是补充区，每天最多 2 条。
- 如果没有非 AI 内容达到高热度阈值，也展示当天非 AI 最热的 1 条。
- 每条内容包含标题、中文摘要、推荐理由、原文链接、HN discussion 链接、points 和 comments。
- 每天保留一份原始候选数据 JSON，便于后续调整关键词和权重。
- 中文摘要由本地 `codex exec` 生成，暂不直接接 OpenAI API。

## Non-Goals

- 不做 Web UI。
- 不做 Telegram、Slack、Email 或其他推送。
- 不接入第二个信息源。
- 不做用户行为反馈、收藏、已读状态、数据库或长期推荐模型。
- 不把输出数量做成复杂个性化系统；v1 先用清晰规则。

## Data Sources

### AI Content

AI 内容使用 Algolia HN Search API 召回。v1 先按时间窗口分页拉取最近 24 hours 的 stories，再在本地做关键词匹配、打分、筛选和 JSON 快照保存。这样关键词调整不需要改变远端查询逻辑，也能保留完整候选数据。

单纯从 HN `topstories` 中筛选容易漏掉最近 24 hours 内未进入热门榜但仍有价值的 AI 内容，因此不作为 AI section 的主召回方式。

### Non-AI Hot Content

非 AI 高热度补充使用 HN official API 的 `topstories` 和可选 `beststories`。原因是这部分的目标不是找个人兴趣内容，而是保留 Hacker News 全站视野。

## Content Rules

每日窗口和输出文件名使用 Asia/Singapore 日期。默认运行时间为每天 08:00 Asia/Singapore，窗口为前一天 08:00 到当天 08:00。相同 HN story 可能从 Algolia 和 HN official API 同时出现，v1 按 HN item id 去重；如果缺少 item id，则按 source URL 去重。同一条 story 同时符合 AI 和 Non-AI Hot 时，归入 AI section。

### AI Main Section

- Time window: 最近 24 hours。
- Max items: 5。
- Candidate source: Algolia HN Search API。
- Ranking rule: 热度打底，AI 相关性加权。
- Minimum score: `score >= 6` 才能进入 AI section。
- 允许内容少于 5 条；如果当天 AI 内容不足或分数低于最低入选线，不强行补满。

### Non-AI Hot Section

- Candidate source: HN official `topstories`，必要时补充 `beststories`。
- 排除命中 AI 规则的 stories。
- 高热度阈值：`points >= 300` 或 `comments >= 150`。
- 达到阈值时最多输出 2 条。
- 如果没有任何非 AI story 达到阈值，输出非 AI 最热的 1 条作为兜底。

## AI Keyword Strategy

关键词分为权重层级。高权重关键词可以直接把 story 拉入 AI 候选；中低权重关键词需要结合 points、comments 或多个关键词命中。裸词和组合词分开处理：例如 `AI agent` 和 `coding agent` 是强信号，但裸 `agent` 只是上下文弱信号。

### Matching Semantics

- 普通关键词匹配使用 Unicode-aware case-insensitive 单词边界或短语边界匹配，不能使用简单子串匹配。例如 `eval` 不能命中 `medieval`，`RAG` 不能命中 `storage` 或 `average`。
- 缩写词 `AI`、`LLM`、`RAG`、`MCP`、`GPU` 默认要求以独立 token 命中，并要求原文中是全大写形式，避免把普通英文词或路径片段误判为 AI 信号。
- 匹配范围包括 title、story text、source URL hostname/path 中可读 token，以及抓取到的正文片段；ranking 的主要依据仍以 title 和 story text 为先。

### High Weight

- AI coding
- coding agent
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
- AI developer tools

### Medium-High Weight

- Gemini
- Google AI
- Meta AI
- xAI
- Mistral
- Perplexity
- AI workflow
- AI productivity
- assistant
- chatbot
- AI app
- AI tool
- AI automation

### Medium Weight

- AI
- inference
- fine-tuning
- training
- eval
- AI benchmark
- LLM benchmark
- GPU
- embedding
- vector database

### Contextual Weak Signals

- agent
- agents
- model
- workflow
- automation
- productivity
- benchmark
- developer tools

这些词单独出现时不能决定入选，需要与强 AI 关键词共同出现，或由 points、comments 证明热度足够。

### Low Weight

- funding
- acquisition
- regulation
- lawsuit

低权重的融资、诉讼、监管和行业新闻会进入候选，但除非热度较高或与高权重关键词共同命中，否则排序靠后。

## Ranking

v1 使用透明的规则打分，不引入复杂模型。初始公式：

```text
score = ln(points + 1)
      + 0.5 * ln(comments + 1)
      + keyword_bonus
      + topic_bonus
```

- `keyword_bonus`: high weight 关键词每个 +4，最多 +6；medium-high 每个 +2.5，最多 +5；medium 每个 +1.5，最多 +3；low weight 每个 +0.5，最多 +1。
- `topic_bonus`: AI coding、coding agent、AI agent、AI developer tools、AI workflow、AI productivity、AI automation 命中时额外 +2，最多 +4。裸 `developer tools` 只有在 story 已经有 high 或 medium-high AI 信号时才参与 topic bonus。
- Contextual weak signals: `agent`、`model`、`workflow`、`automation` 等泛词单独出现时不加分；只有与 high 或 medium-high 关键词共同出现时，每个 +0.5，最多 +1。
- Bonus caps are independent by layer. `keyword_bonus` 的总上限为 +10，`topic_bonus` 的总上限为 +4，避免一条 story 因为命中大量相近关键词而压过所有其他候选。
- 用 `ln` 是为了降低极高 points 或 comments 对排序的碾压，让关键词和个人兴趣仍能影响结果。

排序结果仍需满足 section 的数量限制：AI section 最多 5 条且 `score >= 6`，Non-AI Hot section 最多 2 条。

## Output Format

输出路径建议为：

```text
briefs/YYYY-MM-DD.md
```

原始候选数据路径建议为：

```text
data/YYYY-MM-DD-hn-candidates.json
```

JSON 需要包含所有候选 stories，包括最终未入选的条目。字段名使用 `snake_case`。保留字段包括 `source`、`hn_item_id`、`title`、`source_url`、`hn_discussion_url`、`created_at`、`points`、`comments`、`matched_keywords`、`score`、`selected`、`section` 和 `rejection_reason`。这样可以在 HN points/comments 变化后仍复盘当天快照，并用历史候选数据调试关键词和权重。

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

每条内容先整理成结构化 input，再逐条交给 `codex exec` 生成中文摘要。逐条调用可以让单条摘要失败时只降级该条，不影响其他入选内容。

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
- Keyword matching 使用单词边界或短语边界，不会让 `eval` 命中 `medieval`，也不会让 `RAG` 命中 `storage`。
- `agent`、`model` 等弱关键词不会单独造成明显误判。
- AI section 不输出 `score < 6` 的候选。
- AI section 最多输出 5 条。
- Non-AI Hot section 达阈值时最多输出 2 条。
- Non-AI Hot section 没有达阈值时输出最热 1 条。
- 输出 Markdown 包含标题、摘要、推荐理由、链接和 stats。
- 输出 Markdown 在中文摘要和英文标题混排时保持可读。
- 原始候选 JSON 包含入选和未入选 stories，并记录 `score`、`section` 和 `rejection_reason`。
- 数据源失败或摘要失败时，脚本仍能生成可读 Markdown。

## Deferred Decisions

- 是否改用 48 hours 窗口：v1 先用 24 hours；如果发现经常漏掉第二天才发酵的 AI 内容，再考虑 48 hours 加去重。
- 是否跳过周末推送：v1 不做主动推送，脚本可以每天生成 Markdown；将来加推送时再区分“每天采集”和“仅工作日推送”。
- 是否接入更多信息源：HN v1 稳定后再讨论。
- 是否做 Web UI 或推送：Markdown 输出稳定后再讨论。
- 是否改用 OpenAI API：本地 `codex exec` 方案跑通后再评估。
