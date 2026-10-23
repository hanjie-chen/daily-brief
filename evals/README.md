# 模型评测

这里保存历史案例和独立编写的合成案例，用于比较分类、中文摘要和当天内容去重的表现。

- [分类测试集](./classification/README.md)：50 条案例，每类 10 条，检查分类是否符合预期。
- [摘要测试集](./summary/README.md)：12 条案例，检查摘要是否准确、具体、好读；逐条评阅要点见 [review.md](./summary/review.md)。
- [当天内容去重测试集](./content_deduplication/README.md)：PHP 开发回归案例和 6 个独立合成案例，按正序、倒序重复检查信息判重及代表文章选择，提供可直接调用生产模型的运行脚本。

先阅读对应目录的 README，了解材料、使用方式和目前的不足。分类和摘要目录保存测试数据和评阅说明；内容去重评测通过 `scripts/evaluate_content_deduplication.py` 运行，结果及私有材料保存在忽略的 `data/` 下，独立于确定性程序测试。
