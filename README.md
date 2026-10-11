# Daily Brief

个人用资讯简报生成器。

当前数据源全部来自于 Hacker News，筛选 7 条可能值得我关注的内容，代替我通过微信公众号获取资讯的习惯。

简报每天发布到 [hanjie-chen.com](https://hanjie-chen.com/)。项目动机、成功标准与产品方向见 [`docs/product.md`](./docs/product.md)。

## Output

每天生成一份中文简报，分为两个栏目：

- 最多 5 条技术精选：覆盖 AI、软件开发、编程语言、数据库、计算机系统、互联网技术、开源项目和开发工具等计算机领域；
- 最多 2 条圈外热门内容：从 HN 全站热门候选中选择 2 条非计算机领域的新闻。

每个条目主要包括：标题、摘要、关于这一条摘要的生成详情。

有内容时每次运行写出三类文件：

- `briefs/YYYY-MM-DD.md` — 用于阅读的 Markdown；
- `briefs/YYYY-MM-DD.json` — 用于网站发布的 schema 结构化数据；
- `data/YYYY-MM-DD-hn-candidates.json` — 全部候选及入选/落选原因、原文 transport、正文 extractor 与错误、摘要依据，以及包含 interaction 状态和 token usage 的摘要生成诊断，用于复盘和 debug。

## How It Works

1. 收集候选：从 Hacker News 收集近期内容，并补充热门候选
2. 筛选条目：结合主题和社区热度，选出最多 5 条技术精选和最多 2 条圈外探索。
3. 生成摘要：获取入选内容的网页、帖子正文、以及评论区生成中文摘要；
4. 输出与发布：生成用于阅读、发布和复盘的文件；发布作为独立步骤执行

系统总览、原文获取流程与排查入口见 [`docs/architecture.md`](./docs/architecture.md)；模块职责与实现约束见 [`src/daily_brief/README.md`](./src/daily_brief/README.md)。

## Config

首次配置时，将 [`.env.example`](./.env.example) 复制为 `.env`，再填写所需的凭据。

模板按模型服务、网站发布、可选的材料获取服务和高级设置分区。高级设置已有默认值，通常无需修改。

### Model Service

通过 `LLM_PROVIDER` 选择模型服务：

- `openrouter`：填写 `OPENROUTER_API_KEY`。
- `gemini`：填写 `GEMINI_API_KEY`。

模板默认选择 OpenRouter，使用 Qwen3.8 Flash 分类、GPT-6 Luna 生成摘要。两份 API key 可以同时保留，程序只使用所选服务，不会在 OpenRouter 和 Gemini 之间自动切换。

模型和请求参数见 [`.env.example`](./.env.example)；OpenRouter 的费用控制与自定义模型配置见 [OpenRouter 接入与运行](./docs/openrouter.md)。

### Publishing

发布简报需要填写：

- `PUBLISH_URL`：网站接收简报的接口地址。
- `PUBLISH_TOKEN`：发布接口的认证凭据。

只生成本地简报时，可以留空。

### Optional Services

以下服务用于补充材料获取能力，不配置也可以生成简报：

- `JINA_API_KEY`：Jina Reader 优先匿名访问，遇到认证或限流错误时使用 key 重试。
- `TAVILY_API_KEY`：原文获取失败时，搜索并核验同稿页面或同事件报道，补充摘要材料。
- `PDF_SERVICES_CLIENT_ID`、`PDF_SERVICES_CLIENT_SECRET`：同时填写后启用 Adobe PDF-to-Markdown；未配置或转换失败时使用本地 PDF 提取。

各服务的额度说明见 [API 额度文档](./docs/api-quotas.md)。

### Load Config

Daily Brief 只读取进程环境变量，不会自动加载 `.env`。手动运行前，在项目目录的同一个终端中执行：

```sh
set -a
. ./.env
set +a
```

修改 `.env` 后，手动运行需要重新执行上述命令。当前部署的每日 cron 任务会在每次运行前加载 `.env`，因此修改会在下一次任务中生效，无需重启 cron；已经运行中的任务不受影响。

## Usage

### Install

需要 Python 3.12 及以上。在项目目录执行：

```sh
pip install -e .
```

运行以下命令前，先按 [Load Config](#load-config) 加载配置。

### Generate

```sh
daily-brief generate
```

生成当天的简报，输出文件见 [Output](#output)。发布需要单独执行。

### Retry

重新获取材料并生成摘要，保留原有的入选条目和顺序。

重试指定日期中摘要生成失败、材料不足或材料获取失败的条目：

```sh
daily-brief retry --date YYYY-MM-DD
```

指定某条摘要：

```sh
daily-brief retry --date YYYY-MM-DD --item HN_ID
```

`HN_ID` 是该条目的 Hacker News ID；多个条目可以重复添加 `--item`。

### Publish

```sh
daily-brief publish
```

把当天已生成的简报发布到网站，不会重新生成。发布其他日期时使用：

```sh
daily-brief publish --date YYYY-MM-DD
```

内容没有变化时会跳过；需要重新发送时加 `--force`。
