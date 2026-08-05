# 2026-07-31: 暂缓迁移到 GCP VM,继续在树莓派上迭代

决定: daily-brief 暂时留在树莓派运行,不迁移到 GCP VM。

理由:当前处于变更密集期 (Gemini 切换刚落地、prompt 待真实数据检验、反馈校准尚未开始),树莓派环境 debug 摩擦最小。断网断电的实际后果(网站挂昨天的简报一天,恢复后 publish 自动补发)可接受。

考虑迁移的触发条件:连续两周没有修改任何代码和 prompt,即第一轮反馈校准完成、系统进入稳定运行状态。

# 2026-08-05: TLS issuer 不可用时使用一次 Jina fallback

决定:当直接 HTTPS 请求抛出结构化 `SSLCertVerificationError`,且 OpenSSL verify code 为 20 (`unable to get local issuer certificate`)时,允许通过 Jina Reader 做一次有界 retrieval fallback,并记录 `fallback_reason=tls_issuer_unavailable`。过期、hostname 不匹配、自签名和其他 TLS 错误继续直接失败。

理由:部分公开网站只发送 leaf certificate,Chrome 等浏览器能够通过 AIA 补齐并严格验证证书链,而 Python/OpenSSL 默认不会,导致浏览器可正常阅读但 Daily Brief 无法抓取。项目不自行实现浏览器级 PKI path building;对于这个低风险的个人新闻摘要场景,复用现有 Jina retrieval 是更合适的可用性折中。

信任边界:verify code 20 只表示本地无法取得 issuer,本身不证明源站证书一定有效。这条 fallback 明确把恢复性抓取委托给 Jina,不代表 Daily Brief 独立验证了源站证书链。Jina 响应仍须通过既有的 envelope、provider status、origin status、origin URL、正文非空和大小限制校验;所有网页正文继续作为不可信模型输入处理。
