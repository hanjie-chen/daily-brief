# 2026-07-31: 暂缓迁移到 GCP VM,继续在树莓派上迭代

决定: daily-brief 暂时留在树莓派运行,不迁移到 GCP VM。

理由:当前处于变更密集期 (Gemini 切换刚落地、prompt 待真实数据检验、反馈校准尚未开始),树莓派环境 debug 摩擦最小。断网断电的实际后果(网站挂昨天的简报一天,恢复后 publish 自动补发)可接受。

考虑迁移的触发条件:连续两周没有修改任何代码和 prompt,即第一轮反馈校准完成、系统进入稳定运行状态。

# 2026-08-05: TLS issuer 不可用时使用一次 Jina fallback

决定:当直接 HTTPS 请求抛出结构化 `SSLCertVerificationError`,且 OpenSSL verify code 为 20 (`unable to get local issuer certificate`)时,允许通过 Jina Reader 做一次有界 retrieval fallback,并记录 `fallback_reason=tls_issuer_unavailable`。过期、hostname 不匹配、自签名和其他 TLS 错误继续直接失败。

理由:部分公开网站只发送 leaf certificate,Chrome 等浏览器能够通过 AIA 补齐并严格验证证书链,而 Python/OpenSSL 默认不会,导致浏览器可正常阅读但 Daily Brief 无法抓取。项目不自行实现浏览器级 PKI path building;对于这个低风险的个人新闻摘要场景,复用现有 Jina retrieval 是更合适的可用性折中。

信任边界:verify code 20 只表示本地无法取得 issuer,本身不证明源站证书一定有效。这条 fallback 明确把恢复性抓取委托给 Jina,不代表 Daily Brief 独立验证了源站证书链。Jina 响应仍须通过既有的 envelope、provider status、origin status、origin URL、正文非空和大小限制校验;所有网页正文继续作为不可信模型输入处理。

# 2026-08-16: 官方来源使用确定性 hostname allowlist

决定:网站根据 schema v2 已有的 `source_url` 解析并规范化 hostname,只对 allowlist 中精确匹配的站点显示官方来源名;首个映射为 `claude.com` -> `Claude 官方博客`。未命中的站点继续显示普通 hostname,不新增 payload 字段或升级 schema。

理由:官方身份是产品 metadata,不应由摘要模型根据 URL、页面 `sitename`、作者署名或常识推断。精确 allowlist 能让来源身份可审计,并避免 substring 匹配把相似域名或未审核的子域误标为官方来源。

边界:`www.` 和 hostname 大小写按网站现有规则规范化;其他子域只有在显式加入 allowlist 后才能显示官方标签。来源标签不构成摘要事实依据,网页正文仍作为不可信外部内容处理。

# 2026-08-16: 先提高通用摘要信息量,不新增 guide mode

决定:通用摘要默认使用一至两句话,直接陈述最有区分度的事实;正文包含多个会改变理解的机制、结果、限制或行动建议时,至少保留两个具体事实。暂不新增 `technical_article`、`explainer_guide` 或其他 summary mode。

理由:当前明确失败样本是 Claude Code 指南摘要只列主题,证据不足以支持新的高置信路由。共同的 base prompt 缺少信息量底线,先修正这条共同约束能以更小范围覆盖指南、公告、观点和项目介绍,同时保留现有 research 与 memorial module 的专项要求。

验证:使用 6 篇真实材料组成微型固定对照组,覆盖指南、发布公告、观点、项目介绍、研究报告和悼念文章。第三方文章全文及模型输出保存在 Git 忽略的 `data/`,已跟踪的 `tests/fixtures/model_evaluation/phase_a_manifest.json` 只记录样本 metadata、输入哈希和单句 must-retain 条件,避免在仓库中转载完整原文。
