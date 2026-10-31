# 内容去重评测

一组需要一起判断的文章存为一个 JSON，按“主题-日期”命名，如 `php-2026-10-22.json`。

每个文件包含两部分：

- `input`：文章列表，每篇包含 `item_id`、`title`、`summary`。
- `expected`：预期结果，每篇包含 `item_id`、`duplicate_of`。`null` 表示必须保留；编号列表表示必须删除，重复对象为列表中任意一篇即可。

模型只接收 `input`，`expected` 用于核对结果。

在项目根目录[加载配置](../../README.md#load-config)后运行：

```sh
python evals/content_deduplication/run.py evals/content_deduplication/php-2026-10-22.json
```

七条输入共测七次：原始顺序跑三次；前五条按 `23451`、`34512`、`45123`、`51234` 各跑一次，后两条固定。
原始顺序检查正确性和稳定性，轮转检查位置影响。输入不足五条时，只轮转已有条目。
直接使用生产提示词、模型、请求间隔和重试配置。
结果逐次保存到 `data/content-dedup-evaluations/`，包含判断、提示词、配置和用量。
