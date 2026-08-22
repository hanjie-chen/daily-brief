# Daily Brief

个人使用的资讯简报生成器，生成简短的中文简报。

目前数据源来自 Hacker News，筛选少量可能值得我关注的内容，代替我通过微信公众号获取资讯的习惯。

简报每天发布到 [hanjie-chen.com](https://hanjie-chen.com/)。项目动机、成功标准与产品方向见 [`docs/product.md`](./docs/product.md)。

## Output

每天生成一份中文简报，分为两个栏目：

- 最多 5 条 AI 相关内容；
- 最多 2 条 全站热门的 non-AI 内容，提供少量核心兴趣之外的探索。

每条内容包含：原标题、中文摘要、推荐理由、原文链接、HN 讨论链接、points 和评论数。

若入选条目的原文抓取失败，简报会明确显示错误并跳过模型摘要；网页直连发生网络超时时，会在等待 1 秒后重试一次，仍然超时则尝试一次 Jina Reader；识别到来源网站的浏览器验证或自动抓取阻止时，会显示更具体的失败说明。Reuters 原文及 Jina 均因 DataDome 失败时，系统可通过 Tavily 在显式 allowlist 中发现同稿转载，再由本地抓取和确定性验证取得摘要材料；搜索结果摘要本身不会交给模型。

每次运行写出三类文件：

- `briefs/YYYY-MM-DD.md` — 用于阅读的 Markdown；
- `briefs/YYYY-MM-DD.json` — 用于网站发布的 schema 结构化数据；
- `data/YYYY-MM-DD-hn-candidates.json` — 全部候选及入选/落选原因、原文 transport、正文 extractor 与错误、摘要依据，用于复盘和 debug。

## How It Works

1. 收集候选:双来源(Algolia 搜 AI + HN 官方热榜),得到一批候选条目
2. 筛选条目:层层过滤,得到今日至多 5(AI)+ 2(non-AI)条
3. 抓取材料:只抓入选条目（网页正文、PDF、YouTube 字幕），尽力而为，允许失败
4. 生成摘要:根据抓到的材料生成中文摘要,材料不足则逐级降级
5. 输出与发布:生成三份简报文件;发布是独立命令,推送到网站

每个步骤的 details 详见 [`src/daily_brief/README.md`](./src/daily_brief/README.md)。

## Config

`.env` 中需要的参数可以参考 `.env.example`。`TAVILY_API_KEY` 为可选项；未配置时 Reuters 同稿转载 fallback 会安全跳过，不影响其余简报生成。

## Publish

```bash
daily-brief publish
```

`publish` 发送当天（或 `--date` 明确指定日期）尚未成功发布或内容已变化的 JSON，成功内容的 SHA-256 记录在 `data/publish-state.json`；

失败内容不会被标记成功。修正某天的简报后可强制重发：

```bash
daily-brief publish --date YYYY-MM-DD --force
```

自动流程每天 08:00（Asia/Singapore）依次执行 `daily-brief generate && daily-brief publish`。
