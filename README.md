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

Daily Brief 只读取进程环境变量，不会自动加载 `.env`。完整的变量列表、默认值和说明见 [`.env.example`](./.env.example)。

各 API 的免费额度、限流及使用注意事项见 [`docs/api-quotas.md`](./docs/api-quotas.md)。

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

- `JINA_API_KEY`：Jina Reader 优先匿名请求；遇到 HTTP 401/429 时使用此 key 重试一次。未配置则不重试，继续既有恢复流程。原站验证页、无效正文不会触发带 key 重试。
- `TAVILY_API_KEY`：用于在原文抓取受阻时寻找同稿页面及既有 Reuters 恢复材料；同稿搜索只排除 HN，最多发现 10 个候选、实际抓取 3 个，YouTube 候选走字幕提取。未配置时跳过搜索恢复。
- `PDF_SERVICES_CLIENT_ID`、`PDF_SERVICES_CLIENT_SECRET`：同时配置后启用 Adobe PDF-to-Markdown；未配置时仍会使用本地 PDF 提取。
- Gemini 模型和请求间隔通常无需调整；如需覆盖默认配置，请参考 [`.env.example`](./.env.example)。

摘要默认依次使用 **3.6 Flash → 3.7 Flash → 3.8 Flash**。明确遇到每日额度耗尽时跳过该模型；服务繁忙、超时或网络错误经过有限重试后尝试下一个。分钟限流或原因不明的 429 只等待并有限重试，不自动切换；材料不足则继续原有的补充材料流程。各模型使用相同材料和摘要要求，切换前也保留请求间隔。复盘数据记录实际最后调用的模型及整个切换过程的请求总次数。

每日额度耗尽的记录保存在当前进程内，在美国太平洋时间午夜恢复尝试；重启进程后会重新检查。可将 `DAILY_BRIEF_GEMINI_SUMMARIZER_FALLBACK_MODELS` 设为空来禁用备用模型。`evaluate-model` 始终只测试指定的单个模型，不启用此备用链。

### 发布所需

- `DAILY_BRIEF_PUBLISH_URL`、`DAILY_BRIEF_PUBLISH_TOKEN`：只有运行 `publish` 时需要，必须同时配置。
