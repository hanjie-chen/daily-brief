# Daily Brief

个人使用的资讯简报生成器，生成简短的中文简报。

目前数据源来自 Hacker News，筛选 7 条可能值得我关注的内容，代替我通过微信公众号获取资讯的习惯。

简报每天发布到 [hanjie-chen.com](https://hanjie-chen.com/)。项目动机、成功标准与产品方向见 [`docs/product.md`](./docs/product.md)。

## Output

每天生成一份中文简报，分为两个栏目：

- 最多 5 条技术精选：覆盖 AI、软件开发、编程语言、数据库、计算机系统、互联网技术、开源项目和开发工具等计算与软件领域；
- 最多 2 条圈外热门内容：从 HN 全站热门候选中读取原文，只保留正文明确显示主要主题位于计算与软件领域之外的条目。

每条内容包含：原标题、中文摘要、推荐理由、原文链接、HN 讨论链接、points 和评论数。

有内容时每次运行写出三类文件：

- `briefs/YYYY-MM-DD.md` — 用于阅读的 Markdown；
- `briefs/YYYY-MM-DD.json` — 用于网站发布的 schema 结构化数据；
- `data/YYYY-MM-DD-hn-candidates.json` — 全部候选及入选/落选原因、原文 transport、正文 extractor 与错误、摘要依据，以及包含 interaction 状态和 token usage 的摘要生成诊断，用于复盘和 debug。

## How It Works

1. 收集候选：从 Hacker News 收集近期内容，并补充热门候选
2. 筛选条目：结合主题和社区热度，选出最多 5 条技术精选和最多 2 条圈外探索。项目分享、工具推荐或实践经验征集帖先读取 HN 评论；
3. 生成摘要：获取入选内容的原文，并结合网页提供的介绍生成中文摘要；原文抓取不到时补充 HN 讨论；
4. 输出与发布：生成用于阅读、发布和复盘的文件；发布作为独立步骤执行

每个步骤的 details 详见 [`src/daily_brief/README.md`](./src/daily_brief/README.md)。

## Config

首次配置时，将 [`.env.example`](./.env.example) 复制为 `.env`，再填写所需的凭据。模板中列出了全部配置项及默认值；已有 `.env` 时直接编辑，避免覆盖现有配置。

### 必填配置

- 生成简报：填写 `GEMINI_API_KEY`。
- 发布简报：填写 `DAILY_BRIEF_PUBLISH_URL` 和 `DAILY_BRIEF_PUBLISH_TOKEN`。只生成、不发布时无需配置。

### 可选配置

以下服务用于补充原文获取能力，不配置也可以生成简报：

- `JINA_API_KEY`：在 Jina Reader 匿名访问受限时，使用 API key 重试。
- `TAVILY_API_KEY`：原文抓取受阻时，搜索同稿页面或相关报道，补充摘要材料。
- `PDF_SERVICES_CLIENT_ID`、`PDF_SERVICES_CLIENT_SECRET`：同时填写后启用 Adobe PDF-to-Markdown；未配置时使用本地 PDF 提取。

Gemini 模型、备用模型和请求间隔已有默认配置，通常无需修改。如需调整，见 [`.env.example`](./.env.example)；额度与限流说明见 [API 额度文档](./docs/api-quotas.md)。

### 让配置生效

Daily Brief 只读取进程环境变量，不会自动加载 `.env`。手动运行前，在项目目录的同一个终端中执行：

```sh
set -a
. ./.env
set +a
```

修改 `.env` 后，手动运行需要重新执行上述命令。当前部署的每日 cron 任务会在每次运行前加载 `.env`，因此修改会在下一次任务中生效，无需重启 cron；已经运行中的任务不受影响。
