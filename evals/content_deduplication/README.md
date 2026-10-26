# 当天内容去重评测

在项目根目录安装依赖，并按[根 README](../../README.md#load-config)加载 `.env` 后运行：

```sh
python scripts/evaluate_content_deduplication.py \
  --output data/content-dedup-evaluations/results.json
```

默认使用 `cases-v2.json`，11 个案例按正序、倒序各重复 3 次，共 66 次模型调用。可用 `--repeats` 调整次数；短重试默认 0 次，使用 `--max-retries 2 --max-requests 198` 可启用最多两次重试。OpenRouter 可用 `OPENROUTER_SUMMARIZER_INTERVAL_SECONDS=180` 将请求至少间隔 3 分钟。OpenRouter 预算用 `--budget-usd` 设置，默认 $0.25。

若需使用本地 PHP 正文，在命令中加 `--php-evidence-dir data/investigations/2026-10-22-content-dedup`；该目录须有 `50172041-article.txt`、`50180873-article.txt`、`50182987-article.txt`。未提供正文时，PHP 案例结果标为非权威。

评测结果只写入被 Git 忽略的 `data/` 目录，不提交进仓库。

离线测试：

```sh
pytest -q tests/test_content_deduplication_evaluation.py
```
