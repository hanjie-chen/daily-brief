# Daily Brief

个人使用的资讯简报生成器，生成简短的中文简报。

目前数据源来自 Hacker News，筛选少量可能值得我关注的内容，代替我通过微信公众号获取资讯的习惯。

简报每天发布到 [hanjie-chen.com](https://hanjie-chen.com/)。项目动机、成功标准与产品方向见 [`docs/product.md`](./docs/product.md)。

## Output

每天生成一份中文简报，分为两个栏目：

- 最多 5 条技术精选：覆盖 AI、软件开发、编程语言、数据库、计算机系统、互联网技术、开源项目和开发工具等计算与软件领域；
- 最多 2 条圈外热门内容：从 HN 全站热门候选中读取原文，只保留正文明确显示主要主题位于计算与软件领域之外的条目。

每条内容包含：原标题、中文摘要、推荐理由、原文链接、HN 讨论链接、points 和评论数。

若入选条目的原文抓取失败，简报会明确显示错误并跳过模型摘要；

有内容时每次运行写出三类文件：

- `briefs/YYYY-MM-DD.md` — 用于阅读的 Markdown；
- `briefs/YYYY-MM-DD.json` — 用于网站发布的 schema 结构化数据；
- `data/YYYY-MM-DD-hn-candidates.json` — 全部候选及入选/落选原因、原文 transport、正文 extractor 与错误、摘要依据，用于复盘和 debug。

如果当天没有任何可发布条目，仍写 Markdown 和 candidate audit，但不写无效的 public JSON；改为写出 `briefs/YYYY-MM-DD.no-content`。之后运行 `publish` 会把该 marker 视为正常的 no-content 状态并幂等跳过。

## How It Works

1. 收集候选：从 Algolia 拉取时间窗内的 HN stories，并从 HN 官方 top/best 榜补充热门候选
2. 形成技术精选：明确的非弱关键词命中直接进入核心池；其余候选按 score 排序后最多走查 25 条正文，分类为 AI、核心领域非 AI、圈外或不确定。正文确认属于核心范围的候选获得固定证据分，与关键词候选统一检查最低门槛和排名，最多选 5 条
3. 形成圈外探索：同一次有界走查中，只有正文明确属于圈外且独立达到圈外热度门槛的候选可以参选；走查结束后按 points 和评论数重排，最多选 2 条
4. 生成摘要：入选条目复用走查阶段已经取得的正文，其余条目按需抓取原文，再生成中文摘要
5. 输出与发布：写出阅读、发布和审计产物；发布是独立命令，无内容日期会正常跳过

每个步骤的 details 详见 [`src/daily_brief/README.md`](./src/daily_brief/README.md)。

## Config

请参考 `.env.example` 设置变量和 key：

- `GEMINI_API_KEY`：生成简报时必填；
- `DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL`、`DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL`：可选的固定模型 ID；默认分别使用 `gemini-3.5-flash-lite` 和 `gemini-3.6-flash`；
- `DAILY_BRIEF_GEMINI_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS`、`DAILY_BRIEF_GEMINI_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS`：分类与摘要请求（含内部重试）的独立最小间隔，默认分别为 6 秒和 20 秒；若两个角色使用同一模型，应用会采用两者中更保守的间隔；
- `TAVILY_API_KEY`：可选；未配置时 Reuters 同稿转载 fallback 会安全跳过；
- `PDF_SERVICES_CLIENT_ID`、`PDF_SERVICES_CLIENT_SECRET`：可选，但必须同时配置，启用 Adobe PDF-to-Markdown；
- `DAILY_BRIEF_PUBLISH_URL`、`DAILY_BRIEF_PUBLISH_TOKEN`：仅发布时需要，必须同时配置。

应用只读取进程环境变量，不会自动加载 .env
