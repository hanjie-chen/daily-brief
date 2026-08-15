# Daily Brief

个人使用的资讯简报生成器，生成简短的中文简报。

目前数据源来自 Hacker News，筛选少量更可能值得我关注的内容，代替我通过微信公众号获取资讯的习惯。

简报每天发布到 [hanjie-chen.com](https://hanjie-chen.com/)。项目动机、成功标准与产品方向见 [`docs/product.md`](./docs/product.md)。

## Output

每天生成一份中文简报，分为两个栏目：

- 最多 5 条 AI 相关内容；
- 最多 2 条 Hacker News 全站热门的非 AI 内容，提供少量核心兴趣之外的探索。

每条内容包含：原标题、中文摘要、推荐理由、原文链接、HN 讨论链接、points 和评论数。实际条数可以少于上限。若入选条目的原文抓取失败，简报会明确显示错误并跳过模型摘要，不再生成一个看似正常的标题改写。

每次运行写出三类文件：

- `briefs/YYYY-MM-DD.md` — 用于阅读的 Markdown；
- `briefs/YYYY-MM-DD.json` — 用于网站发布的严格 schema v2 结构化数据；每条内容的 `content_status` 标明正文和摘要是否正常；
- `data/YYYY-MM-DD-hn-candidates.json` — 全部候选及入选/落选原因、原文 transport、正文 extractor 与错误、摘要依据，以及研究材料的 evidence 选择策略、原始/选中字符数和章节，用于复盘。

## How It Works

每天 08:00（Asia/Singapore）通过

- Algolia HN Search API 收集过去 24 小时的新 stories
- HN 官方 API 收集当前 top/best stories（不受时间窗口限制）

然后合并去重，并排除最近 7 天已推荐的内容。

候选先经过确定性的关键词匹配与热度打分；命中明确 AI 关键词的直接进入 AI 候选池，无明确信号的高分候选交给模型做主题分类。最终入选的内容才会补全原文：已有 HN story text 时直接使用；YouTube 视频通过 `yt-dlp` 只取得优先的人工字幕或原语言自动字幕，不下载音视频；标准 GitHub 仓库链接通过 GitHub 官方 API 获取 README；GitHub blob 链接转换为精确的 raw 文件 URL；普通 HTML 使用本地 `trafilatura` 提取正文。配置 Adobe PDF Services 凭据后，PDF 会优先转换为保留标题、段落、列表与表格结构的 Markdown；Adobe 未配置或鉴权、网络、配额、超时、输出校验失败时，自动退回受限 subprocess 中的本地 `pypdf` text-layer 提取。YouTube 没有可用字幕或字幕抓取失败时会明确失败，不会自动改用视频简介中的相关文章、音频 ASR 或多模态视频理解。直接请求明确遇到 Cloudflare Challenge、识别到返回 HTTP 200 的浏览器验证页、普通 HTML 下载成功但 `trafilatura` 提取为空，或 TLS 校验仅因本地无法取得 issuer（OpenSSL verify code 20）而失败时，会通过 Jina Reader 做至多一次有界 retrieval fallback，并校验其 JSON envelope 与正文；Jina 返回的浏览器验证页同样不会被视为原文。过期、hostname 不匹配、自签名等其他证书错误不会触发 fallback。系统分别记录正文 transport（如 direct、YouTube caption、GitHub raw、Jina）和 extractor（如 `trafilatura`、`yt-dlp`、`adobe_pdf_to_markdown`、`pypdf`），以及摘要实际依据的材料。取得正文后，纯代码路由会识别高置信度的悼念/讣告或研究论文/报告；所有不确定情况继续使用通用模式。研究模式不是“所有 PDF 模式”，而是要求正文具有完整且有序的研究章节结构；它同时识别纯文本和 Markdown 标题，优先把 `Abstract` 与 `Results` / `Findings` / 编号 `Facts` 到 `Conclusion` 的正文交给摘要模型，排除参考文献和附录等噪声，无法可靠选取时退回完整正文。随后模型生成接地的中文摘要；下载、类型验证或提取失败时直接显示错误并跳过摘要模型，最后渲染为 Markdown 与 JSON 后发布到网站。

打分权重、入选门槛等参数集中在 `src/daily_brief/config.py`。模块职责、生成链路与关键不变量见 [`src/daily_brief/README.md`](./src/daily_brief/README.md)。

## Design Principles

- 确定性优先：抓取、匹配、打分、去重、排序、渲染、发布都是确定性逻辑；模型只负责主题分类和摘要两处。
- 局部降级：数据源、分类、原文抓取、摘要中的单项失败都不会让整份简报生成失败。
- 摘要必须接地：摘要只陈述材料中明确存在的事实；无法生成可靠摘要时显示固定文案，而不是编造。
- 重要语境不能被压平：当材料明确提供悼念文章的公开反差、被追忆者的独立身份或双方关系时，摘要应保留这些事实；材料没有提供时不得由模式或常识补出。
- 研究摘要必须交付结论：不能只说“研究了什么”；材料支持时应保留研究范围、至少两项主要发现，以及最会改变解读的一项限制或因果边界。
- 反馈校准：通过真实阅读记录（`opened` / `useful` / `noisy` / `note`）定期复盘筛选规则，而不是预先假定什么值得注意力。
- 克制的范围：现阶段只使用 Hacker News 一个信息源，这是有意的选择，不是尚未完成的功能。

## Run

需要 Python 3.12+；安装时会一并安装本地 HTML 与 PDF 提取依赖：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

生产生成默认使用 Gemini（分类与摘要均为 `gemini-3.5-flash-lite`，可通过环境变量覆盖），需要提供 `GEMINI_API_KEY`：

```bash
set -a && source .env && set +a   # .env 权限应为 0600
daily-brief generate              # 不带子命令时默认即 generate
```

建议同时在 `.env` 中配置 Adobe PDF Services；两项都存在时启用 PDF to Markdown，缺失时继续使用本地 `pypdf`：

```dotenv
PDF_SERVICES_CLIENT_ID=<client-id>
PDF_SERVICES_CLIENT_SECRET=<client-secret>
```

只有最终入选并需要补全原文的公开 PDF 会被发送给 Adobe。凭据不得写入日志、生成文件或 Git。

## Publish

发布到网站需要提供发布地址和 shared secret：

```bash
export DAILY_BRIEF_PUBLISH_URL="https://hanjie-chen.com/internal/briefs"
export DAILY_BRIEF_PUBLISH_TOKEN="<shared-secret>"

daily-brief publish
```

`publish` 发送当天（或 `--date` 明确指定日期）尚未成功发布或内容已变化的 JSON，成功内容的 SHA-256 记录在 `data/publish-state.json`；失败内容不会被标记成功。修正某天的简报后可强制重发：

```bash
daily-brief publish --date YYYY-MM-DD --force
```

自动流程每天 08:00（Asia/Singapore）依次执行 `daily-brief generate && daily-brief publish`。

## Model Evaluation

在一次正常生成时捕获模型实际收到的输入，之后可离线重放，用于对比不同 Gemini 模型：

```bash
daily-brief generate --capture-model-inputs
daily-brief evaluate-model --date YYYY-MM-DD
```

捕获输入保存在 `data/model-eval-inputs/`，评测结果保存在 `data/model-evaluations/`。评测不访问网络、不生成或发布简报、不修改推荐历史和发布状态。评测其他 Gemini 模型可通过 `DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL` / `DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL` 覆盖。

## Docs

- [`docs/product.md`](./docs/product.md) — 动机、成功标准、日常使用体验与产品方向
- [`src/daily_brief/README.md`](./src/daily_brief/README.md) — 内部架构说明
