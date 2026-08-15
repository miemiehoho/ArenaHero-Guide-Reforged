# ArenaHero-Guide-Reforged 中文日志体系详细设计

> 设计日期：2026-08-16  
> 适用分支：`进攻才是最好的防守`  
> 官方基线：Arena Hero 规则 `v0.14`、API `v0.1`、Python SDK `0.2.9`
> 实施状态：L1 设计（`e219acb`）、L2/L3 日志生产与查询（`a077192`）、统计修复
> （`cf5df9a`）、统计增强实现（`fb7c749`）和文档验收均已完成。

## 1. 目标与范围

为长期运行的 Agent 提供一套不依赖真实服务端的日志、统计和查询能力，使维护者能够回答：

- 某个 Tick 收到了哪些官方结算事件，失败的 `reason_code` 是什么；
- Agent 为该 Tick 生成了什么决策，规划耗时和 A*/搜索预算是否异常；
- Core、Worker、Vanguard、Ranger、资源和敌方 Core 的关键状态如何变化；
- 命令提交、跳过 Tick、会话重启和致命错误发生在什么时间；
- 一段 Tick 区间内的资源、生产、战斗、治疗和移动失败统计是多少。

本阶段只修改 fork 内的日志生产器、查询 CLI、测试、README 和更新日志。不修改官方三个项目、
SDK、服务端协议或 `.arena_core_state.json` 的持久化结构，不把日志发送到外部服务。

## 2. 官方约束

### 2.1 事件来源

官方 SDK 的 `Turn.events` 是上一 Tick 结算后返回的私有 `ResolutionEvent` 元组。每个事件
只保证以下字段：`event_id`、`tick`、`event_type`、`reason_code`、`actor_id`、`target_id`、
`position` 和可选 `values`。日志必须原样保留官方代码，同时提供中文解释；未知事件和未知原因
代码不能被映射成臆造的语义。

`UNIT_MOVE_FAILED.position` 是 Unit 未移动后的原始位置，不是目的格；日志只能记录官方的
`position` 和 Agent 自己已保存的目的格诊断，不能把两者混写为一个字段。

### 2.2 Tick 与提交边界

`game.turns()` 每个 Tick 只产生一次可提交 Turn。日志在收到 Turn 后记录状态快照摘要、规划
完成摘要和提交结果；不另开 `game.events()` 迭代器，不在日志模块中重放事件或修改计划。

官方命令窗口约 15 秒，Agent 内部保留 10 秒规划截止。日志记录 `deadline_exceeded` 和
降级模块，但不能为了记录日志阻塞提交，也不能在 `COMMAND_WINDOW_CLOSED` 后为同一 Tick 重新
提交。

### 2.3 隐私和安全

- 不写入 API Key、完整环境变量、请求正文、`.env` 内容或异常中的凭据；
- Unit 的 UUID 可记录用于排查动作，但不记录 Unit owner 等非必要身份信息；
- `ResolutionEvent.values` 只写官方已返回的 JSON 值，不扩展状态快照；
- 日志文件沿用服务的 `UMask=0077`，只在本机保存，按大小轮转；
- 中文消息可以给人读，原始 `event_type`、`reason_code`、错误码和 Tick 字段供脚本过滤。

## 3. 当前问题

当前 `arena_core_agent.py` 使用一行英文文本拼接状态、事件、动作和指标，存在：

1. 事件、统计、决策和错误没有类别边界，无法稳定过滤；
2. `event_summary()` 丢弃 `event_id`、`actor_id`、`target_id` 和 `values`；
3. `metrics` 只存在于 Tick 文本中，无法按 Tick 区间聚合；
4. 现有轮转文件不是结构化格式，无法可靠处理坏行或保留未知官方事件；
5. 没有查询命令，维护者只能手工 `grep` 日志。

## 4. 总体架构

```text
Turn.events + 当前完整状态 + plan/actions + 异常
                 |
                 v
       arena_core_agent.py 中文日志生产器
                 |
                 v
       arena_core_agent.jsonl[.1 ... .4]
                 |
                 v
             arena_log.py
       events / stats / tail / errors
```

### 4.1 文件和职责

| 文件 | 职责 |
|---|---|
| `arena_core_agent.py` | 生成运行记录，不承担查询和历史聚合 |
| `arena_log.py` | 只读解析轮转 JSONL，提供中文 CLI 和可测试的聚合函数 |
| `arena_core_agent.jsonl` | 当前运行日志；旧文件按 `.1`～`.4` 保留 |
| `.gitignore` | 排除 JSONL 和轮转文件 |
| `docs/logging-system-design.zh-CN.md` | 本设计契约 |

不新建数据库、守护进程或 HTTP 服务。查询命令直接读取本地轮转文件，避免影响线上 Tick。

### 4.2 单条日志信封

每行是一个 UTF-8 JSON 对象，使用稳定的中文键；原始官方代码放在数据对象中：

```json
{
  "时间": "2026-08-16T12:34:56.123+08:00",
  "级别": "信息",
  "类别": "事件",
  "消息": "Unit 移动失败",
  "运行ID": "20260816-123456-a1b2c3d4",
  "模式": "control",
  "Tick": 10583,
  "数据": {
    "事件ID": "3f360e7e-d9bd-4f48-9a51-5cf751b04075",
    "事件类型": "UNIT_MOVE_FAILED",
    "原因代码": "MOVE_DESTINATION_OCCUPIED",
    "执行者ID": "9d3e4941-2816-4a39-a220-df8cd95e877d",
    "目标ID": null,
    "位置": [120, 85],
    "事件值": null
  }
}
```

`时间` 使用带时区的 ISO-8601；`Tick`、ID 和官方代码保持可过滤的原始值；可选字段缺失时
使用 `null`，不把未知值替换成 `未知事件`。消息和类别使用中文，但不翻译官方代码。

## 5. 日志类别和记录时机

### 5.1 生命周期日志

类别：`生命周期`。记录 Agent 启动、目标达成、正常退出、配置错误、会话重连和最终致命停止。
启动记录模式、目标、日志文件和当前记忆数量，不记录 API Key。网络重启记录异常类型和退避秒数。

### 5.2 Tick 统计日志

类别：`统计`，每个实际规划的 Turn 一条。数据字段包括：

- 状态：`玩家状态`、资源/容量、人口、Core 位置/HP/护盾/状态；
- 编制：Worker、Vanguard、Ranger、可见敌人、已知敌方 Core；
- 统计：兵种数量、人口、资源/容量区间摘要、资源占用率和最新 Tick 快照；
- 规划：`决策耗时毫秒`、A* 调用/扩展/缓存命中、预算耗尽、降级模块、生产状态；
- 记忆：已知资源、资源任务、临时阻塞、彻查任务、敌方 Worker 轨迹；
- 结果：本 Tick 动作数量、Core 动作类型、是否达到 harvest 目标。

统计日志只记录摘要，不写完整 `turn.state.objects`，避免文件无限膨胀和泄露不必要信息。

### 5.3 官方事件日志

类别：`事件`，对 `turn.events` 中每个 `ResolutionEvent` 各写一条。中文消息按事件族映射：

| 官方前缀 | 中文类别 |
|---|---|
| `CORE_` | Core 事件 |
| `UNIT_` | Unit 事件 |
| `WORKER_` / `HARVEST_` / `DEPOSIT_` | Worker 经济事件 |
| `SHOT_` / `SWEEP_` / `DESTRUCTION_` | 战斗事件 |
| `BEACON_` / `RESPAWN_` | 信标与重生事件 |
| `MOVE_` | 移动事件 |

每条记录保留完整官方字段和 JSON 可序列化的 `values`。对 `UNIT_MOVE_FAILED` 单独注明
“官方位置为原始位置”，不写入推测的目的格；Agent 目的格若存在，作为独立的
`Agent目的格` 诊断字段。

### 5.4 决策、提交和错误日志

- `决策`：记录动作摘要、每类动作数量和关键策略标签；不重复写整份 SDK `CommandPlan`；
- `提交`：记录已提交、跳过 Tick、HTTP/API 错误分流和 SDK 返回结果，保留错误代码；
- `错误`：记录配置、认证、协议、传输和未知异常的中文消息、异常类型和退避信息。

## 6. 查询命令

默认读取当前目录的 `arena_core_agent.jsonl` 及轮转文件；可用 `--log-dir` 指定部署目录。
命令输出中文表格/摘要，`--json` 输出机器可读 JSON，但 JSON 字段仍使用日志中的中文键。

```bash
# 查看最近 50 条记录
python arena_log.py tail --limit 50

# 查询某个 Tick 的官方事件
python arena_log.py events --tick 10583

# 只看移动失败和命令窗口错误
python arena_log.py events --type UNIT_MOVE_FAILED --type COMMAND_WINDOW_CLOSED

# 汇总 Tick 区间的兵种、资源、生产、事件和规划指标
python arena_log.py stats --from-tick 10000 --to-tick 10583 --json

# 查看最近错误和会话重启
python arena_log.py errors --limit 100
```

查询行为：

- `tail` 按文件时间和行顺序合并轮转日志，坏行跳过并在 stderr 给出中文警告；
- `events` 支持 `--tick`、`--type`、`--reason`、`--actor`、`--limit`；
- `stats` 聚合 `类别=统计` 的记录，计算兵种/人口摘要、资源/容量/占用率、最新快照、Tick 数、
  资源增量、事件计数、事件类别、动作计数、平均/最大决策耗时、预算耗尽 Tick 和生产状态分布；
- `errors` 等价于 `类别=错误` 或 `级别=错误`，按时间倒序输出；
- 无匹配结果返回空集合和成功退出码，参数错误返回非零退出码。

## 7. 兼容、轮转和故障处理

- 不修改 `.arena_core_state.json`，旧状态可直接继续运行；
- 首次启动新版本创建 `arena_core_agent.jsonl`，旧英文日志保留，不尝试猜测旧文本格式；
- 使用 `RotatingFileHandler`，单文件 4 MiB、保留 4 个备份；写入失败只记录 stderr，并让 Agent
  继续按官方 Tick 流程运行，日志不是提交依赖；
- 查询使用逐行解析，单条坏记录不会阻塞其它记录；
- `UMask=0077` 保证日志只对运行用户可读；日志文件进入 `.gitignore`。

## 8. 测试与验收

新增 `test_arena_log.py`，覆盖：

1. 信封序列化使用 UTF-8 中文，且输入/异常文本中 API Key 不会出现在输出；
2. 所有官方事件字段完整保留，未知 `event_type`/`reason_code` 原样查询；
3. `UNIT_MOVE_FAILED` 的原始位置与 Agent 目的格字段不混淆；
4. 轮转文件合并、坏行跳过、Tick/类型/原因/Actor 过滤；
5. 统计聚合的兵种、人口、资源/容量、占用率、事件、动作、耗时和错误计数；
6. CLI 的 `tail`、`events`、`stats`、`errors` 正常输出和空结果行为；
7. 现有 Agent 全量测试、`compileall` 和 `git diff --check` 保持通过。

验收命令：

```bash
python -m unittest -q
python -m compileall -q arena_core_agent.py arena_log.py test_arena_core_agent.py test_arena_log.py
python arena_log.py --help
git diff --check
```

## 9. 分阶段发布与回滚

### 阶段 L1：设计和脱敏契约

提交本文件、README 索引和更新日志。验收设计字段、官方来源和查询命令稳定。

### 阶段 L2：日志生产（已完成，`a077192`）

实现 JSONL 写入、中文事件/统计/决策/提交/错误记录，保留现有 Agent 行为；新增生产集成测试。

### 阶段 L3：查询 CLI（已完成，`a077192`、`cf5df9a`）

实现轮转读取、过滤、统计聚合和命令帮助；新增离线 CLI 测试。

### 阶段 L4：部署文档和最终验收（已完成）

补充 README、更新日志、`.gitignore`，运行全量测试后独立提交并 push。日志功能失败时可回滚
L2/L3 提交，Agent 策略和状态文件不受影响。最终全量测试为 133 个，并确认本地 `HEAD` 与
远端分支一致。

### 阶段 L5：统计增强（已完成，`fb7c749`）

在不改变日志生产和 Agent 行为的前提下，`stats` 增加 Worker/Vanguard/Ranger、人口、资源、
容量和资源占用率摘要，输出区间最新快照及事件类别计数；新增缺失字段、空结果和 CLI JSON
回归测试。
