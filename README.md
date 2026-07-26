# Daily Brief

个人每日信息简报生成器，生成简短的中文简报，并可自动发布到 `hanjie-chen.com`。

数据源: 

- Hacker News: AI 和开发工具相关话题 + 少量全站热门内容.

## Motivation

我希望每天得到一份简报：以我关心的内容为主，同时保留少量热门话题，减少信息噪声，也不会错过最近发生的重要事情。

Daily Brief 目前仅仅是一个起点。长期来看，我希望这个项目成长为一套属于自己的信息聚合与情报整理系统作为我的每日的优质上下文，而不是去一堆垃圾中手动收集和过滤好的内容。

如何从中判断一条信息对于我来说是否足够优质：

- 是否点开原文或讨论区了吗？（点了 = 选题至少勾住了你）
- 读完后是否知道了一件之前不知道、且我在乎的事吗？（是 = 这条有效）
- 如果这条没出现在简报里，是否会觉得可惜吗?（会 = 真正的优质）

## Daily Output

Daily Brief 目前生成一份 Markdown 简报，内容分为两部分：

- 最多 5 条 AI 和开发工具相关内容；
- 最多 2 条 Hacker News 全站热门内容，帮助我关注兴趣范围之外的重要话题。

每条内容包含中文摘要、推荐理由、原文链接、Hacker News 讨论链接以及 points 和 comments。
摘要在写入输出前会统一规范中文与英文、数字交界处的空格，避免不同模型带来排版差异。

## How It Works

1. 从 Hacker News 收集过去一天的新内容和当前热门内容；
2. 根据关键词、points 和 comments 对内容进行筛选和排序；
3. 对重复内容去重，选出 AI 相关内容和少量全站热门内容；
4. 通过统一的模型 backend 调用本地 Codex 完成主题分类和中文摘要，并输出为 Markdown 简报。

## Architecture

Python package 的生成链路、模块职责、关键不变量与常见改动入口见
[`src/daily_brief/README.md`](./src/daily_brief/README.md)。根 README 只保留项目层面的目标、使用方式和整体行为。

## Run

需要 Python 3.12 或更高版本，并确保本地已经可以使用 `codex` 命令。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

daily-brief generate
```

当前生产 backend 仍然是本地 Codex。为了在不影响每日生成、推荐历史和发布状态的
前提下比较 Gemini 等 provider，可以在某次正常生成时显式捕获模型的精确输入：

```bash
daily-brief generate --capture-model-inputs
```

捕获文件保存在 `data/model-eval-inputs/YYYY-MM-DD.json`，包括当天分类批次，以及完成
文章抓取后实际送入摘要模型的文本。之后可以反复离线重放同一输入：

```bash
daily-brief evaluate-model --date YYYY-MM-DD --backend codex
```

结果写入 `data/model-evaluations/YYYY-MM-DD-<backend>.json`，记录分类结果、每条摘要、耗时和
失败信息。`evaluate-model` 不抓取网络内容，不生成或发布简报，也不会修改
`recommendation-history.json` 或 `publish-state.json`。捕获输入和评测结果可能包含公开
文章正文，均位于被 Git 忽略的 `data/` 目录中，不应提交到仓库。

### Gemini evaluation

Gemini backend 使用官方 [Interactions REST API](https://ai.google.dev/api/interactions-api)
和 [structured output](https://ai.google.dev/gemini-api/docs/structured-output)，不增加 Python
runtime dependency。默认固定使用：

- `gemini-3.5-flash-lite`：主题分类；
- `gemini-3.6-flash`：中文摘要。

创建 Gemini auth API key 后，把 secret 放进仓库外、权限为 `0600` 的环境文件，不要粘贴到
聊天、提交到 Git 或写入命令行参数。加载 `GEMINI_API_KEY` 后运行：

```bash
daily-brief evaluate-model --date YYYY-MM-DD --backend gemini
```

如需评测其他固定模型，可通过 `DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL` 和
`DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL` 覆盖默认值；不要使用会自动切换的 `latest` alias。
API key 只通过 request header 发送，调用显式设置 `store: false`。客户端只重试网络错误、
408、429 和 5xx，且不会把 key 写入日志。`store: false` 不改变 Gemini Free Tier 的数据使用
[Free Tier 数据条款](https://ai.google.dev/gemini-api/docs/pricing)；评测输入应继续只包含
可接受发送给 provider 的公开内容。

`--backend gemini` 当前仅允许用于 `evaluate-model`。在评测质量通过并明确切换前，正式
`generate` 仍固定使用 Codex。

生成结果保存在：

- `briefs/YYYY-MM-DD.md`：每天阅读的 Markdown 简报；
- `briefs/YYYY-MM-DD.json`：用于网站发布的 schema-versioned 结构化简报；
- `data/YYYY-MM-DD-hn-candidates.json`：用于复盘筛选结果的候选数据。

网站发布需要配置：

```bash
export DAILY_BRIEF_PUBLISH_URL="https://hanjie-chen.com/internal/briefs"
export DAILY_BRIEF_PUBLISH_TOKEN="<shared-secret>"
daily-brief publish
```

`publish` 默认发送所有尚未成功发布或内容已经变化的 JSON，并在 `data/publish-state.json` 记录成功内容的 SHA-256。网络错误和 5xx 会有限重试；失败内容不会写入成功状态，因此下次运行会自动补发。修正某天内容后可使用：

```bash
daily-brief publish --date YYYY-MM-DD --force
```

当前 cron 在每天 08:00（Asia/Singapore）运行。完成 website 接口部署和 shared secret 配置后，定时流程应依次执行：

```bash
daily-brief generate && daily-brief publish
```

发布 secret 应保存在仓库外、权限为 `0600` 的环境配置文件中，不应写进 Git 或直接展开在 crontab 中。现有 Markdown 不回填；网站归档从结构化 JSON 发布启用之日开始。

首轮上线后的 1–2 周使用固定的 `Daily Brief Feedback` 对话记录 `opened`、`useful`、`noisy` 对应的 `hn_item_id` 和可选 `note`，每周汇总一次，用于后续校准筛选策略。
