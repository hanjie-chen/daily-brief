# Daily Brief

个人每日信息简报生成器，生成简短的中文 Markdown 简报

数据源: 

- Hacker News: AI 和开发工具相关话题 + 少量全站热门内容.

## Motivation

我希望每天得到一份简报：以我关心的内容为主，同时保留少量热门话题，减少信息噪声，也不会错过最近发生的重要事情。

Daily Brief 目前仅仅是一个起点。长期来看，我希望这个项目成长为一套属于自己的信息聚合与情报整理系统作为我们每日的优质上下文，而不是去一堆垃圾中手动收集和过滤好的内容。

如何从中判断一条信息对于我来说是否足够优质：

- 是否点开原文或讨论区了吗？（点了 = 选题至少勾住了你）
- 读完后是否知道了一件之前不知道、且我在乎的事吗？（是 = 这条有效）
- 如果这条没出现在简报里，是否会觉得可惜吗?（会 = 真正的优质）

## Daily Output

Daily Brief 目前生成一份 Markdown 简报，内容分为两部分：

- 最多 5 条 AI 和开发工具相关内容；
- 最多 2 条 Hacker News 全站热门内容，帮助我关注兴趣范围之外的重要话题。

每条内容包含中文摘要、推荐理由、原文链接、Hacker News 讨论链接以及 points 和 comments。

## How It Works

1. 从 Hacker News 收集过去一天的新内容和当前热门内容；
2. 根据关键词、points 和 comments 对内容进行筛选和排序；
3. 对重复内容去重，选出 AI 相关内容和少量全站热门内容；
4. 使用本地 Codex 生成中文摘要，并输出为 Markdown 简报。

## Run

需要 Python 3.12 或更高版本，并确保本地已经可以使用 `codex` 命令。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

daily-brief generate
```

生成结果保存在：

- `briefs/YYYY-MM-DD.md`：每天阅读的 Markdown 简报；
- `data/YYYY-MM-DD-hn-candidates.json`：用于复盘筛选结果的候选数据。

当前通过 cron 在每天 08:00（Asia/Singapore）自动生成。
