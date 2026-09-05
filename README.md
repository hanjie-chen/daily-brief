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

如果当天没有任何可发布条目，仍写 Markdown 和 candidate audit，但不写无效的 public JSON；改为写出 `briefs/YYYY-MM-DD.no-content`。之后运行 `publish` 会把该 marker 视为正常的 no-content 状态并幂等跳过。

## How It Works

1. 收集候选：从 Hacker News 收集近期内容，并补充热门候选
2. 筛选条目：结合主题相关性、原文内容和社区热度，选出最多 5 条技术精选和最多 2 条圈外探索
3. 生成摘要：获取入选内容的原文并生成中文摘要；无法取得可靠材料时会明确标注，不根据标题或模型常识补写
4. 输出与发布：生成用于阅读、发布和复盘的文件；发布作为独立步骤执行

每个步骤的 details 详见 [`src/daily_brief/README.md`](./src/daily_brief/README.md)。

## Config

Daily Brief 只读取进程环境变量，不会自动加载 `.env`。完整的变量列表、默认值和说明见 [`.env.example`](./.env.example)。

本地运行时，可以复制配置模板，编辑后将其加载到当前 shell：

```sh
cp .env.example .env
# 编辑 .env 后执行
set -a
. ./.env
set +a
```

### 生成简报所需

- `GEMINI_API_KEY`：生成简报时必填。

### 可选功能

- `TAVILY_API_KEY`：用于在部分原文抓取受阻时寻找经过验证的恢复材料；未配置时会跳过这一恢复路径。
- `PDF_SERVICES_CLIENT_ID`、`PDF_SERVICES_CLIENT_SECRET`：同时配置后启用 Adobe PDF-to-Markdown；未配置时仍会使用本地 PDF 提取。
- Gemini 模型和请求间隔通常无需调整；如需覆盖默认配置，请参考 [`.env.example`](./.env.example)。

### 发布所需

- `DAILY_BRIEF_PUBLISH_URL`、`DAILY_BRIEF_PUBLISH_TOKEN`：只有运行 `publish` 时需要，必须同时配置。
