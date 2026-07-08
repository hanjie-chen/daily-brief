# Daily Brief

个人信息推送聚合项目。

## 当前目标

先从少量信息源开始，抓取并整理内容，输出为简单可读的 Markdown 文件。展示、推送和 Web 面板之后再讨论。

## 信息源

- Hacker News

## 本地运行

建议先创建虚拟环境并安装本项目：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

生成 Hacker News Daily Brief：

```bash
daily-brief generate --output-dir briefs --data-dir data
```

也可以直接用模块入口运行：

```bash
python -m daily_brief generate --output-dir briefs --data-dir data
```

只检查 CLI 参数、不写入文件：

```bash
daily-brief generate --dry-run
```

## 测试

运行 Task 8 的 CLI 测试：

```bash
pytest tests/test_cli.py -q
```

运行完整测试：

```bash
pytest -q
```
