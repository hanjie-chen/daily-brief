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

默认输出：

- `briefs/YYYY-MM-DD.md`：每日可读简报。
- `data/YYYY-MM-DD-hn-candidates.json`：当天候选 story 快照，用于之后复盘关键词、分数和拒绝原因。

只检查 CLI 参数、不写入文件：

```bash
daily-brief generate --dry-run
```

## 定时运行

当前机器使用 `+08` 时区时，可以用 cron 每天 08:00 自动生成：

```cron
0 8 * * * cd /home/plain/projects/daily-brief && mkdir -p briefs data logs && /home/plain/.venv/website/bin/python -m daily_brief generate >> logs/daily-brief.log 2>&1
```

`logs/` 不是内容数据目录。它只保存 cron 运行时的 stdout/stderr，方便排查网络失败、认证失败、`codex exec` 失败等问题。真正用于复盘筛选规则的原始候选数据在 `data/`。

## 生成文件

`briefs/`、`data/`、`logs/` 默认不提交到 Git：

- `briefs/` 每天都会变，适合作为本地阅读产物。
- `data/` 可能较大，而且是本地调参用的原始快照。
- `logs/` 是机器运行日志，不应该进入版本库。

## 测试

运行 Task 8 的 CLI 测试：

```bash
pytest tests/test_cli.py -q
```

运行完整测试：

```bash
pytest -q
```
