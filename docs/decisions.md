# 2026-07-31: 暂缓迁移到 GCP VM,继续在树莓派上迭代

决定: daily-brief 暂时留在树莓派运行,不迁移到 GCP VM。

理由:当前处于变更密集期 (Gemini 切换刚落地、prompt 待真实数据检验、反馈校准尚未开始),树莓派环境 debug 摩擦最小。断网断电的实际后果(网站挂昨天的简报一天,恢复后 publish 自动补发)可接受。

考虑迁移的触发条件:连续两周没有修改任何代码和 prompt,即第一轮反馈校准完成、系统进入稳定运行状态。

# 2026-08-05: TLS issuer 不可用时使用一次 Jina fallback

决定:当直接 HTTPS 请求抛出结构化 `SSLCertVerificationError`,且 OpenSSL verify code 为 20 (`unable to get local issuer certificate`)时,允许通过 Jina Reader 做一次有界 retrieval fallback,并记录 `fallback_reason=tls_issuer_unavailable`。过期、hostname 不匹配、自签名和其他 TLS 错误继续直接失败。

理由:部分公开网站只发送 leaf certificate,Chrome 等浏览器能够通过 AIA 补齐并严格验证证书链,而 Python/OpenSSL 默认不会,导致浏览器可正常阅读但 Daily Brief 无法抓取。项目不自行实现浏览器级 PKI path building;对于这个低风险的个人新闻摘要场景,复用现有 Jina retrieval 是更合适的可用性折中。

信任边界:verify code 20 只表示本地无法取得 issuer,本身不证明源站证书一定有效。这条 fallback 明确把恢复性抓取委托给 Jina,不代表 Daily Brief 独立验证了源站证书链。Jina 响应仍须通过既有的 envelope、provider status、origin status、origin URL、正文非空和大小限制校验;所有网页正文继续作为不可信模型输入处理。

# 2026-08-16: 官方来源使用确定性 hostname allowlist

决定:网站根据 schema v2 已有的 `source_url` 解析并规范化 hostname,只对 allowlist 中精确匹配的站点显示官方来源名;首个映射为 `claude.com` -> `Claude 官方`。未命中的站点继续显示普通 hostname,不新增 payload 字段或升级 schema。域名级标签只声明来源官方身份,不把该域名下的非博客页面误称为官方博客。

理由:官方身份是产品 metadata,不应由摘要模型根据 URL、页面 `sitename`、作者署名或常识推断。精确 allowlist 能让来源身份可审计,并避免 substring 匹配把相似域名或未审核的子域误标为官方来源。

边界:`www.` 和 hostname 大小写按网站现有规则规范化;其他子域只有在显式加入 allowlist 后才能显示官方标签。来源标签不构成摘要事实依据,网页正文仍作为不可信外部内容处理。

# 2026-08-16: 先提高通用摘要信息量,不新增 guide mode

决定:通用摘要默认使用一至两句话,直接陈述最有区分度的事实;正文包含多个会改变理解的机制、结果、限制或行动建议时,至少保留两个具体事实。暂不新增 `technical_article`、`explainer_guide` 或其他 summary mode。

理由:当前明确失败样本是 Claude Code 指南摘要只列主题,证据不足以支持新的高置信路由。共同的 base prompt 缺少信息量底线,先修正这条共同约束能以更小范围覆盖指南、公告、观点和项目介绍,同时保留现有 research 与 memorial module 的专项要求。

验证:使用 6 篇真实材料组成微型固定对照组,覆盖指南、发布公告、观点、项目介绍、研究报告和悼念文章。第三方文章全文及模型输出保存在 Git 忽略的 `data/`,已跟踪的 `tests/fixtures/model_evaluation/phase_a_manifest.json` 只记录样本 metadata、输入哈希和单句 must-retain 条件,避免在仓库中转载完整原文。

# 2026-08-22: 原文网络超时先重试一次,再使用 Jina fallback

决定:原文 direct transport 遇到网络 timeout 时,固定等待 1 秒并重试一次;第二次仍 timeout 时,通过既有的 Jina Reader 路径尝试一次。候选审计记录总 transport attempts。其他已有的 challenge、empty content 和 TLS issuer fallback 保持不变,确定性的 URL 校验、HTTP、证书、content type、大小和 extraction 失败不进入这条 retry 路径。

理由:当天一个入选条目在同批其他六条成功时发生 TLS handshake timeout,稍后 direct 和 Jina 均可成功抓取。现有证据只能确定这是 host-specific 的 timeout,不能区分临时路由、DDoS protection edge 故障或 anti-bot 处置。一次 direct retry 覆盖普通瞬时故障,随后切换 Jina 提供不同 retrieval path,同时保持尝试次数有界。

暂不抽象共享 retry executor。HN、Gemini、publisher 和任意网页 retrieval 的可重试条件、退避和 provider 行为不同;当前真实缺口只在 article fetch。先让这套 article-specific policy 经受生产样本,以后出现明确的重复实现成本时再评估共享边界。

# 2026-08-22: Reuters DataDome 失败后只恢复经验证的同稿转载

决定:仅当原始 `reuters.com` URL 被高置信度 DataDome challenge 阻止、且既有 Jina fallback 也失败时,通过 Tavily Search 在显式 allowlist 中发现最多三个同稿转载候选。首期 allowlist 只有 `finance.yahoo.com`。Tavily 只提供候选标题和 URL;候选正文必须由现有本地 article fetcher 取得,并通过 Reuters marker、原文 URL 日期、story anchors、正文长度和 teaser 排除等确定性验证,才能进入现有 grounded summarizer。

理由:目标是取得足以支持可靠摘要、且可审计的材料,而不是绕过 Reuters 的站点防护。搜索 snippet 或供应商生成的 answer 无法提供同等的材料 provenance;任意搜索结果又存在误匹配风险。域名 allowlist、本地抓取与多信号验证把首期范围限制在已真实验证的 Reuters 通讯社转载模式。

边界:discovery 位于 `cli.run_generate(...)` 的原始 Reuters failure 路径,不进入通用 `article_fetcher.py`,因此转载候选失败不会递归触发搜索。公共 source URL 继续指向 Reuters,public schema 不变。candidate audit 记录实际材料 URL、最终 transport/extractor/attempts、原始 Reuters/Jina 失败链和有界 recovery 结果。缺少 `TAVILY_API_KEY`、供应商错误、候选抓取或验证失败都 fail closed,保留原始阻止状态且不影响整份简报。
