# ArenaHero 中文日志使用说明

本项目运行时将中文结构化 JSONL 日志写入运行目录的 `arena_core_agent.jsonl`，单文件 4 MiB，
最多保留 4 个轮转文件。日志查询只读取本地文件，不连接 Arena Hero 服务端。

## 1. 基本查询

在 `arena_core_agent.py`、`arena_log.py` 所在目录执行：

```bash
# 最近 50 条日志
python arena_log.py tail --limit 50

# 查询官方事件
python arena_log.py events --type UNIT_MOVE_FAILED --reason CELL_UNIT_LIMIT

# 查询 Tick 区间的完整统计
python arena_log.py stats --from-tick 10000 --to-tick 10583

# 输出机器可读 JSON
python arena_log.py stats --from-tick 10000 --to-tick 10583 --json

# 最近错误
python arena_log.py errors --limit 100
```

查询已部署目录时，`--log-dir` 放在子命令之前：

```bash
python arena_log.py --log-dir /path/to/WorkingDirectory stats --json
```

## 2. 兵种与资源统计

`stats` 的 JSON 输出包含以下字段：

| 字段 | 含义 |
|---|---|
| `兵种数量` | `Worker`、`Vanguard`、`Ranger` 在区间内的样本数、起点、终点、最小、最大和平均数量 |
| `人口统计` | 官方状态人口的区间摘要；人口只计算存活 Unit，不包含 Core |
| `资源统计.资源` | Core 资源数量的区间摘要 |
| `资源统计.容量` | Core 资源容量的区间摘要 |
| `资源统计.资源占用率` | 每个有效 Tick 的 `资源 / 容量`，容量为 0 或缺失时跳过 |
| `最新快照` | 区间最大 Tick 的兵种、人口、资源和容量快照 |
| `事件类别数量` | 按官方事件前缀分类的事件数量，未知事件归入 `官方事件` |

只查看最新 Tick 的各种兵种数量，不依赖额外安装的 `jq`，可使用 Python 标准库：

```bash
python arena_log.py stats --json \
  | python -c 'import json,sys; s=json.load(sys.stdin)["最新快照"]; print(json.dumps({k:s.get(k) for k in ("Worker数","Vanguard数","Ranger数","人口")}, ensure_ascii=False, indent=2))'
```

其中 `Worker数`、`Vanguard数`、`Ranger数` 是当前存活数量，三者之和应等于该快照的 `人口`。
查看一段 Tick 内的起点、终点、最小、最大和平均数量，则使用：

```bash
python arena_log.py stats --from-tick 10000 --to-tick 10583 --json \
  | python -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["兵种数量"], ensure_ascii=False, indent=2))'
```

如果系统已经安装 `jq`，也可以使用下面的等价写法：

```bash
python arena_log.py stats --from-tick 10000 --to-tick 10583 --json \
  | jq '{最新快照, 兵种数量, 人口统计, 资源统计}'
```

`jq` 不是项目运行依赖；Bohrium 等精简容器优先使用上面的 Python 命令即可。

PowerShell 可以直接读取 JSON：

```powershell
$summary = python arena_log.py stats --json | ConvertFrom-Json
$summary.最新快照 | Select-Object Worker数, Vanguard数, Ranger数, 人口
$summary.兵种数量
$summary.资源统计
```

只读取三个当前数量也可以直接执行：

```powershell
$summary.最新快照.Worker数
$summary.最新快照.Vanguard数
$summary.最新快照.Ranger数
```

如果终端中文显示异常，可先设置当前 PowerShell 会话的输出编码：

```powershell
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```

## 3. 区间语义

- 不指定 Tick 范围时，统计所有可读取的轮转日志；`--from-tick` 和 `--to-tick` 包含边界。
- 起点、终点、最小、最大和平均只使用字段存在且为数字的统计记录；缺失字段不当作 0。
- 没有有效样本时返回 `样本数=0`，其余摘要值为 `null`。
- `最新快照` 取筛选区间内最大 Tick 的最后一条统计记录。
- `资源起点`、`资源终点`、`资源变化`、事件数量、动作数量和规划耗时等旧字段仍保留。
- 兵种数量是当前存活快照，不是累计生产量；累计生产/损失应结合 `events` 的官方事件逐条分析。

## 4. 部署与安全

服务的 `WorkingDirectory` 决定默认日志目录。也可以在任意目录执行查询并显式传入
`--log-dir`。查询不会修改日志、`.env` 或 `.arena_core_state.json`。

日志只保存在本机，沿用服务的 `UMask=0077`；不会记录 API Key、完整环境变量、请求正文或完整
SDK 状态对象。轮转文件损坏的单行会被跳过并输出中文警告，不影响其它记录查询。

更多字段契约和官方边界见 [中文日志体系详细设计](logging-system-design.zh-CN.md) 与
[中文日志统计增强详细设计](logging-statistics-design.zh-CN.md)。
