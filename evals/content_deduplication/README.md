# 内容去重评测

一组需要一起判断的文章存为一个 JSON，按“主题-日期”命名，如 `php-2026-10-22.json`。

每个文件包含两部分：

- `input`：文章列表，每篇包含 `item_id`、`title`、`summary`。
- `expected`：预期结果，每篇包含 `item_id`、`duplicate_of`。`null` 表示保留；文章编号表示与该篇重复，由该篇代表。

模型只接收 `input`，`expected` 用于核对结果。
