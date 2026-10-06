# OpenRouter 接入与运行

`generate`、`retry`、`evaluate-model` 及未显式传入 backend 的库入口共用模型选择配置。为兼容既有安装，未设置 provider 时仍用 Gemini；生产切换需要明确配置：

```sh
DAILY_BRIEF_MODEL_BACKEND=openrouter
OPENROUTER_API_KEY=你的凭据
DAILY_BRIEF_OPENROUTER_CLASSIFIER_MODEL=qwen/qwen3.8-flash
DAILY_BRIEF_OPENROUTER_SUMMARIZER_MODEL=openai/gpt-6-luna
DAILY_BRIEF_OPENROUTER_MAX_RUN_COST_USD=0.25
```

凭据只写本机 `.env`，不要提交。CLI 不自动加载 `.env`，沿用现有环境加载方式。全部常用参数及默认值在 `.env.example`；模型参数分别为分类关闭思考、摘要 low，实际 reasoning tokens 以响应为准。

## 调用与费用

通过固定的 OpenRouter Chat Completions 端点提交严格 JSON schema，要求供应商支持参数，允许同型号的供应商回退。没有跨模型、免费模型或 Gemini 回退。选择模型后不自动跟随别名升级。

默认每个 backend 实例总费用预留上限 $0.25，包括分类、摘要和重试；收到 `usage.cost` 后替换该次预留，无法知道费用的失败不返还预留。输入以 UTF-8 字节数加开销保守估算，Luna 同时计入缓存写入费率。每次请求也有供应商单价上限，修改模型时须明确设置对应价格上限，并核查缓存价格。未来服务方价格或 token 计量变化仍需核对账单。

这是单次运行保护，不是每月或多个并发进程共用的硬上限。默认每天一次、全部用满预留时约 $7.50/30天；额外手动运行另计。正常用量预期远低于此，应按日志的 `component=openrouter` 和账户账单复核。不要把没有得到可用摘要视为没有费用。

429、5xx、超时和网络错误最多重试两次，遵循有界退避；401/402/403、无效输出及材料不足不重试。非 stop 完成（例如只思考耗尽输出额度）按失败处理并保留实际费用。原有抓取恢复策略没有改变。

协议依据：[请求格式](https://openrouter.ai/docs/api/reference/overview)、[结构化输出](https://openrouter.ai/docs/guides/features/structured-outputs)、[错误处理](https://openrouter.ai/docs/api/reference/errors-and-debugging)。

## 验证与切换

1. 在 feature 分支运行确定性测试；使用独立输出和 data 目录真实生成一份简报，验证公开 schema、各条摘要状态和费用。真实生成会收费，`--dry-run` 不会验证模型。
2. 网络必须把 `openrouter.ai` 送到实际通过模型调用的出口。已有 OpenAI 域名规则并不自动涵盖 OpenRouter。Mihomo 可用独立 OPENROUTER 组和精确域名规则，公共 `/models` 健康检查只能判断连通性。
3. 验证通过后合并 main，再修改日常 `.env` 的 provider；不要在旧 main 尚未支持 OpenRouter 时先切换配置。试运行不自动发布，生产 cron 的原定发布行为保持不变。
4. 回退可将 provider 改回 `gemini`（需要仍有效的 Gemini key）；日常任务下次启动读取配置。不要删除仍被其他服务使用的代理组或 watchdog。

## 维护文档同步建议

根 README 和 API 额度文档由项目所有者维护，本次没有直接改写。建议后续将 README 的“生成简报需 GEMINI_API_KEY”更新为“根据 DAILY_BRIEF_MODEL_BACKEND 配置 GEMINI_API_KEY 或 OPENROUTER_API_KEY”；在额度文档增加 OpenRouter 的按 token 计费、单次运行保护以及月度预算需要独立核对的说明。
