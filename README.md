# Daily Brief

个人使用的资讯简报生成器，生成简短的中文简报。

目前数据源来自 Hacker News，筛选少量可能值得我关注的内容，代替我通过微信公众号获取资讯的习惯。

简报每天发布到 [hanjie-chen.com](https://hanjie-chen.com/)。项目动机、成功标准与产品方向见 [`docs/product.md`](./docs/product.md)。

## Output

每天生成一份中文简报，分为两个栏目：

- 最多 5 条 AI 相关内容；
- 最多 2 条圈外热门内容：从 HN 全站热门候选中读取原文，只保留正文明确显示主要主题位于计算与软件领域之外的条目。

每条内容包含：原标题、中文摘要、推荐理由、原文链接、HN 讨论链接、points 和评论数。

若入选条目的原文抓取失败，简报会明确显示错误并跳过模型摘要；

有内容时每次运行写出三类文件：

- `briefs/YYYY-MM-DD.md` — 用于阅读的 Markdown；
- `briefs/YYYY-MM-DD.json` — 用于网站发布的 schema 结构化数据；
- `data/YYYY-MM-DD-hn-candidates.json` — 全部候选及入选/落选原因、原文 transport、正文 extractor 与错误、摘要依据，用于复盘和 debug。

如果当天没有任何可发布条目，仍写 Markdown 和 candidate audit，但不写无效的 public JSON；改为写出 `briefs/YYYY-MM-DD.no-content`。之后运行 `publish` 会把该 marker 视为正常的 no-content 状态并幂等跳过。

## How It Works

1. 收集候选: 双来源(Algolia 搜 AI + HN 官方热榜),得到一批候选条目
2. 选择 AI: 明确关键词命中的候选按现有 score 选出至多 5 条
3. 筛选圈外探索: 沿 HN 热度顺序检查至多 5 条候选，抓取网页正文、PDF 或 YouTube 字幕，并分类为 AI、核心领域非 AI、圈外或不确定；只有明确圈外的条目可占至多 2 个名额
4. 生成摘要: 圈外入选条目复用筛选阶段取得的正文；AI 入选条目按需抓取正文，再生成中文摘要
5. 输出与发布: 写出阅读、发布和审计产物；发布是独立命令，无内容日期会正常跳过

每个步骤的 details 详见 [`src/daily_brief/README.md`](./src/daily_brief/README.md)。

## Config

请参考 `.env.example` 设置变量和 key：

- `GEMINI_API_KEY`：生成简报时必填；
- `DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL`、`DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL`：可选的固定模型 ID；
- `DAILY_BRIEF_GEMINI_MIN_REQUEST_INTERVAL_SECONDS`：Gemini 请求（含内部重试）之间的最小间隔，默认 6 秒；覆盖模型时需按对应 RPM 配额同步调整；
- `TAVILY_API_KEY`：可选；未配置时 Reuters 同稿转载 fallback 会安全跳过；
- `PDF_SERVICES_CLIENT_ID`、`PDF_SERVICES_CLIENT_SECRET`：可选，但必须同时配置，启用 Adobe PDF-to-Markdown；
- `DAILY_BRIEF_PUBLISH_URL`、`DAILY_BRIEF_PUBLISH_TOKEN`：仅发布时需要，必须同时配置。

应用只读取进程环境变量，不会自动加载 .env
