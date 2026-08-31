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

# 2026-08-26: 圈外探索使用正文四态分类并 fail closed

决定:圈外探索候选在取得正文后分类为 `ai`、`core_non_ai`、`outside` 或 `uncertain`;正文抓取失败单独记为 `topic_unknown`,正文已取得但模型分类调用失败则记为 `classifier_failed`。后两种情况在产品选择上都 fail closed,不占探索名额。只有正文明确表明主要主题位于计算与软件领域之外时才允许 `outside` 占用探索名额。跨领域、主题模糊、材料不足或 excerpt 截断导致证据不完整时一律使用 `uncertain`,不能把“没有看到圈内证据”当作“确认圈外”。

分类边界:AI 是文章的主要对象、核心方法或关键因果因素,或者文章主要讨论 AI 的影响、安全或政策问题时,属于 `ai`;仅顺带提及 AI 或只使用 AI 润色文章不算。软件开发、编程语言、数据库、计算机系统与硬件、互联网技术、密码学、开源项目和开发工具等属于 `core_non_ai`;SQLite、分布式系统或密码学文章不能因为不是 AI 就进入圈外探索。

理由:触发案例 `Everything I own, owned` 的标题和 host 没有 Claude 证据,原来的 title-only 二元分类把证据不足错误地记录成确定的非 AI。公开栏目的产品语义是受控的圈外探索,需要正面圈外证据,而不是“未被识别成 AI”。四态分类把代码对齐到 `docs/product.md` 已有的 Content Scope,并用非对称的 `outside` 门槛避免同类错误的镜像版本。

# 2026-08-26: 正文分类只保证探索纯度,暂不改变 AI ranking

决定:删除最多 30 条候选的 title-only 批量分类。AI 栏继续只由明确关键词候选按现有 score 和最多五条的限额选定。圈外探索改为沿 HN 热度顺序有界游走:只检查达到热门门槛的未匹配候选,最多抓取和分类五条正文,取得两条 `outside` 后立即停止。正文阶段发现的 `ai` 不加入 AI pool,但写入 `topic_route=article_ai` 和日志,供以后评估 content-aware scoring。筛选阶段已经取得的正文在条目入选后直接复用于摘要。

理由:2026-08-17 至 2026-08-26 的历史回放中,原 `classifier_ai` 连续十天进入 AI 前五的次数为 0;该调用的实际作用只是从第二栏排除疑似 AI,没有提升 AI 召回。标题关键词候选带约 5 分 keyword bonus,正文确认 AI 的候选只有纯 heat score,两者在当前评分体系中不可直接公平竞争。此次改动因此只修复每天实际发生作用的探索栏目纯度,不暗中改变 AI scoring 或栏目限额。

边界:至多五个探索候选即使最终未入选,也可能在 candidate audit 中带有 retrieval provenance;其他未入选候选仍是 `not_attempted`。若真实日志反复出现值得保留的 `article_ai`,应单独设计 content-aware scoring,而不是给正文分类结果临时加 bonus。探索栏因热门候选全部属于核心领域而为空是预期结果;扩大探索来源属于另一项产品决策。

# 2026-08-26: 删除热门栏兜底并显式表示 no-content

决定:圈外探索不再在没有候选达到门槛时强行填一条。若 AI 与圈外两个栏目都为空,`generate` 仍写 Markdown 和 candidate audit,但不写 schema 不允许的空 public JSON;改为原子写入空文件 `briefs/YYYY-MM-DD.no-content`。有有效内容时先原子替换同日 public JSON,再清理 marker;无内容时先删除同日 JSON,再原子写 marker。

发布规则:同日 JSON 一旦存在就必须通过完整校验并发布,即使残留 marker 也不能掩盖损坏的 JSON。只有 JSON 不存在且 marker 存在时,`publish` 才以 info 日志正常、幂等地跳过;两者都不存在仍视为 generate 未运行、异常中断或产物丢失并报错。仅圈外栏目为空、AI 栏有内容时仍是正常有效简报。

理由:产品原则明确不应为填满数量而增加噪声,而 public schema v2 又有 `total_items > 0` 的合法性要求。显式 marker 能区分“当天确定无内容”和“生成没有完成”,并使独立运行的 publish 命令在预期 no-content 日期不制造假告警。空 marker 的日期由文件名承载,无需再维护第二套 schema。

# 2026-08-30: 正文证据可作为核心栏入场资格

决定:核心栏最多五条,统一覆盖计算与软件领域,不设置 AI 配额。明确 non-weak 关键词命中的候选直接进入核心池;未命中候选按现有 `score` 降序走查至多 25 条,正文分类为 `ai` 或 `core_non_ai` 时获得固定 `ARTICLE_EVIDENCE_BONUS = 4.0`,并与关键词候选一起检查核心最低门槛和统一排名。正文确认但低于门槛或排不进前五的候选明确落选,不得回流到圈外栏。

理由:关键词同时承担领域识别与兴趣加权时,会重复惩罚那些正文已经明确属于核心范围、但标题没有专名的文章。4.0 与一次 high-weight 关键词命中相同,使正文证据能够替代领域识别信号,但不等同于达到关键词总加分上限。`TOPIC_KEYWORDS` 的 +1.0 继续保留,因为 AI coding 和 AI agent 是核心范围内有意偏重的子方向。

走查队列不设热度或分数准入门槛。预算和 score 排序已经有界;取消门槛避免“先决定是否读正文、读完才取得资格加分”的循环定义。冷清日期可能因此发生少量注定落选的抓取,这是每天最多 25 条的可接受成本。圈外门槛仍在分类完成后的路由处独立检查;走查不会因凑满两个圈外名额而提前停止,全部合格 `outside` 候选最终按 points/comments 重排并取前二。

直接入口词形必须先通过可回放的 A.0 数据关卡,在 tracked manifest 中逐词获批,并保留确定性同形词负例。无法可靠消歧的裸词继续交给正文分类,不以直接入口换取有污染风险的召回。

# 2026-08-31: 高置信来源阻止后允许有标注的 Reuters 同事件报道

决定:仅在最终摘要模式中,非 Reuters 原始来源的 retrieval chain 已终止、确实尝试过 fallback,且原因属于`challenge_page`、`cloudflare_challenge`、`datadome_challenge`或`vercel_challenge`时,允许通过一次独立 Tavily 搜索发现 Reuters 或 Yahoo Finance 上的 Reuters-authored 同事件报道。候选必须由既有 article fetcher 完整抓取,初始 URL、redirect 和 effective URL 都限制在精确 allowlist 中,并独立验证 early Reuters marker、terminal `Reporting by`、非 teaser、至少 400 字符、接近的报道日期和足够的同事件信号。Yahoo 合格候选优先;同组多个合格候选取正文最长者,同长度按 URL 稳定排序,事件身份冲突时整体 fail closed。

理由:这条 fallback 的目标是在原文及精确副本均无法无人值守取得时,继续自动节省阅读时间,同时明确改变了摘要依据。Reuters 作者身份可以通过正文 marker 与署名尾确定性验证;项目也已有 Yahoo 精确 allowlist、SSRF/redirect/content-size 边界和 Reuters 同稿恢复经验。来源限制本身不能证明事件相同,因此日期与事件信号验证不可省略。Reuters 也发布分析和特稿,不能把来源身份写成“天然只报道单一事件”。

边界:新 finder、query 和 validator 位于独立的`alternate_reporting.py`,不复用或改变已有`syndicated_copy.py`的同稿语义、800 字符门槛和`exact_match=True`请求。搜索供应商的 answer、snippet、content 与 raw content 不进入验证、摘要或 audit。候选抓取失败不会递归发现。成功时 public source URL 仍指向原始来源,public schema v2 不新增字段,代码在模型摘要成功后固定添加`据 Reuters 对同一事件的报道：`前缀;模型失败时沿用原有 fallback summary 且不添加前缀。candidate audit 使用`material_origin=alternate_reporting`,记录实际材料 URL、原始失败链和独立 recovery 结果。任何 discovery、抓取、验证或摘要失败都沿用既有诚实失败状态。
