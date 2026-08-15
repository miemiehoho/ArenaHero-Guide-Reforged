# ArenaHero-Guide-Reforged 官方规则对齐优化详细设计

> 设计日期：2026-08-15  
> 输入：[实现审计报告](implementation-audit.zh-CN.md)  
> 实施分支：`进攻才是最好的防守`  
> 官方基线：Arena Hero 规则 `v0.14`、API `v0.1`、Python SDK `0.2.9`

> 实施状态：阶段 A1（`99b7769`）、A2（`e868de5`）、A3（`0fe6acb`）和
> A4（`b7fa0dd`）已完成；A5 保持可选且未启用。

## 1. 目标与范围

本设计处理审计报告 A-01～A-06：修复 Core 占位、移动失败目的格、Core 迁移交付和命令
错误恢复，增加 15 秒窗口内的规划截止与 Core 恢复动作。目标是在不改变当前 4 Worker、
`2 Vanguard + 1 Ranger` 编制和 40+ 人口巡逻策略的前提下，提高线上动作合法率和长期
运行稳定性。

阶段 A5 的 Champion Beacon 与完整远处资源记忆只保留接口和测试边界，本轮不默认启用。
历史 Beacon 远征实现规模较大，直接恢复会与当前小队、攻击波和目标预留重复建模，不符合
本轮“先修可靠性”的范围。

### 1.1 非目标

- 不重写官方 Python SDK、HTTP/WebSocket 客户端或服务端规则；
- 不改变高人口生产比例、巡逻半径、攻击波大小和目标预留上限；
- 不建立完整服务端模拟器，不连接真实账号做自动验收；
- 不为未知错误猜测协议字段，也不把动态失败当作隐藏信息探针；
- 不提交 `.env`、`.arena_core_state.json`、日志或外层仓库改动。

## 2. 官方契约

### 2.1 占位和结算顺序

每格最多两个可占位实体，Core、Worker、Vanguard、Ranger 都各占一个位置。Worker 动作在
Core 动作前结算；Unit 成功离开 Core 后同 Tick 可以生产，Unit 留在 Core 上时生产必然收到
`CELL_UNIT_LIMIT`。规划占位必须表达“Core 固定占一个位置”，不能只统计 Unit。

### 2.2 移动失败事件

`UNIT_MOVE_FAILED.position` 是失败后 Unit 所在的原始格。服务端事件不返回目的格；Agent 只能
从自己上一 Tick 已提交的 `MoveAction.direction` 和当时位置恢复目的格。没有本地计划上下文时
必须放弃目的格推断。

### 2.3 Core 迁移

Core 迁移持续四个逻辑 Tick，期间不能接收 Worker 交付、治疗、修盾或生产。Unit 不会随 Core
移动。载货 Worker 应保留货物并在安全位置等待，不能反复提交 `DEPOSIT`。

### 2.4 命令窗口和错误恢复

每 Tick 只有一个约 15 秒的全局窗口，Agent 收到状态时剩余时间可能少于 15 秒。一次 POST
替换此前完整 Agent 计划。SDK 已负责传输异常和 502/503/504 的同正文、同幂等键重试；本项目
只负责按结构化 `APIError.error` 决定等待下一 Tick、重启会话或停止服务。

### 2.5 Core 恢复动作

Core 最大 HP 为 5，普通护盾上限为 5；己方持有 Champion Beacon 时护盾上限为 10。
`HEAL`、`REPAIR_SHIELD` 和 `SPAWN` 共用一个 Core 动作槽，均在战斗之后结算。Core `HEAL`
每恢复 1 HP 消耗 1 资源；`REPAIR_SHIELD` 每 Tick 固定消耗 1 资源恢复 1 护盾。

## 3. 阶段 A1：动作合法性修复

### 3.1 Core 进入占位模型

`FriendlyOccupancy` 继续使用每格计数和上限 2，不增加另一套 Core 专用占位类。
`plan_turn()` 初始化改为：

```text
occupied = FriendlyOccupancy([core_position, *friendly_unit_positions])
```

Unit 开始规划时仍调用 `discard(current_position)`，只移除该 Unit 的一个计数；Core 自己的计数
始终保留。结果：

| Core 格状态 | 占位计数 | Unit 可进入 | Core 可生产 |
|---|---:|---:|---:|
| 只有 Core | 1 | 是 | 是 |
| Core + 1 Unit | 2 | 否 | 否 |
| Unit 已计划成功离开 | 1 | 是 | 是 |

生产可用性继续使用 `context.core_pos not in context.occupied`，因为 `__contains__` 表示该格已满。

### 3.2 保存上一 Tick 的移动目的格

`AgentMemory` 增加仅在当前进程有效的字段：

```python
pending_move_tick: int = 0
pending_move_destinations: dict[UUID, Pos]
```

字段不写入 `.arena_core_state.json`。WebSocket 断线由同一个 SDK Client 自动重连，进程内映射
仍然可用；服务进程重启后缺少映射时直接跳过一次动态封锁，不用每 Tick 强制写入状态文件。

规划结束后从 `turn.plan.unit_actions` 提取 `MoveAction`，结合该 Turn 中 Unit 的原始位置计算
预计目的格。处理下一份 `state.events` 时仅在以下条件全部满足时使用：

1. `event.tick == pending_move_tick`；
2. `event.actor_id` 在映射中；
3. 原因属于 `MOVE_CONTESTED`、`MOVE_SWAP_BLOCKED`、`MOVE_DESTINATION_OCCUPIED`、
   `MOVE_DEPENDENCY_FAILED` 或 `CELL_UNIT_LIMIT`。

当前状态确认目标格已经有容量时立即移除临时封锁；计数必须包含 Core。地形失败和坐标溢出
不进入临时占位，因为它们不是动态实体冲突。

### 3.3 Core 迁移期间载货待命

`plan_workers()` 在载货返航分支前判断 `turn.core.view.state`：

- `NORMAL`：保持现有返航和 `DEPOSIT`；
- `MOVING`：不提交 `DEPOSIT`，使用现有 `full_capacity_worker_destination()` 选择近家、非 Core
  的安全待命点；无法移动则 `WAIT`；
- Core 缺失：`plan_turn()` 已在进入 Worker 规划前返回，不产生动作。

日志动作使用 `moving-core-stage` 和 `moving-core-hold`，便于与满仓待命区分。

### 3.4 A1 测试

- Core 上有 Worker 交付时不生产；
- Core 上满血 Unit 未离开时不安排另一名伤员进入；
- Unit 计划离开 Core 后允许同 Tick 生产；
- 移动失败使用保存的目的格，不使用事件原始位置；
- 没有匹配 Tick/Unit 的移动失败不猜测目的格；
- Core `MOVING` 时，同格和远处载货 Worker 均不提交 `DEPOSIT`。

## 4. 阶段 A2：Runner 错误状态机

### 4.1 纯函数分流

新增 `submission_error_disposition(error: APIError) -> str`，只读取 `status_code` 和 `error`：

| 错误 | 处置 |
|---|---|
| `COMMAND_WINDOW_CLOSED`、`TICK_MISMATCH` | `SKIP_TICK` |
| `COMMAND_RATE_LIMITED`、`COMMAND_CONCURRENCY_LIMIT` | `SKIP_TICK` |
| `TICK_NOT_READY` | `SKIP_TICK`，等待服务端新状态或重连 |
| `INTERNAL_ERROR`、其他 5xx | `RESTART_SESSION` |
| `IDEMPOTENCY_CONFLICT`、`INVALID_COMMAND`、其他 4xx | `FATAL` |

`COMMAND_CONCURRENCY_LIMIT` 理论上允许按 `Retry-After` 重试，但 SDK 的 `APIError` 不暴露响应头，
且本 Agent 每 Tick 正常只提交一次。为避免创建新幂等键放大并发，当前 Tick 直接跳过。

### 4.2 在 Turn 循环内跳过

`turn.submit()` 的 `SKIP_TICK` 错误必须在 `for turn in game.turns()` 内处理，保持当前 WebSocket
会话并等待下一份 Turn；不能退出 `with ArenaHeroClient` 后立刻重连同一 Tick。记录
`skipped_tick`，即使 SDK 重发同一 Tick 也不再次提交。

`RESTART_SESSION` 抛回外层有限退避；`FATAL` 记录结构化错误并返回非零退出码。

### 4.3 停止类 SDK 异常

`AuthenticationError`、`ConfigurationError`、`InvalidActionError`、`PolicyViolationError`、
`ProtocolError` 均停止服务。`ProtocolError` 通常意味着 SDK 与服务端协议版本不一致，继续重连
不会恢复。`TransportError`、`OSError`、`TimeoutError` 保持会话重启。

### 4.4 A2 测试

- 表驱动测试覆盖所有 disposition；
- `COMMAND_WINDOW_CLOSED` 和 `COMMAND_RATE_LIMITED` 不触发同 Tick 第二次提交；
- `ProtocolError` 属于停止类；
- 500/传输错误仍走有限会话重启，不绕过 SDK 重试边界。

## 5. 阶段 A3：规划截止和安全降级（已完成，`0fe6acb`）

### 5.1 内部预算

定义 `PLANNING_BUDGET_SECONDS = 10.0`。从收到 Turn 并进入 `plan_turn()` 开始使用
`time.perf_counter()` 计算 `deadline_at`，预留约 5 秒给计划编码、HTTP 请求和调度抖动。

`PlanningMetrics` 增加：

```text
deadline_at
deadline_exceeded
degraded_sections
```

### 5.2 检查点

必须保留的顺序：

```text
状态/记忆更新
-> 当前家园威胁与资源返航
-> 已接敌小队动作
-> 普通巡逻、彻查和远征
-> Core 动作
-> 提交
```

`first_step_astar()` 在扩展循环中检查截止时间；`search_goal_for()` 在候选循环中检查截止时间。
预算耗尽后返回当前可用的 `None`/候选，不抛异常。普通巡逻和彻查可以降级为 `WAIT`；已规划的
动作保留。Core 动作仍执行一次 O(1) 决策。

官方协议中未写入 `unit_actions` 的 Unit 按 `WAIT` 处理，所以安全计划不需要为所有 Unit
显式写 `WaitAction`。不能复用上一 Tick 计划或只改 Tick 号。

### 5.3 观测与验收

日志增加 `deadline_exceeded` 和 `degraded_sections`。测试使用可控单调时钟模拟预算耗尽，验证：

- 当前 Turn 仍能构造并提交合法完整计划；
- 已完成的逃跑、返航和战斗动作不被清空；
- 未完成的搜索/巡逻对象退化为本 Tick `WAIT`；
- 40/60 人口 fixture 在本机预算内完成，并记录决策耗时和 A* 预算状态。

## 6. 阶段 A4：Core 生存动作（已完成，`b7fa0dd`）

### 6.1 动作优先级

在现有 `plan_core_production()` 前增加 Core 恢复决策，最终顺序：

1. Core 非 `NORMAL` 或 Core 格已满：不提交恢复/生产；
2. Core HP `< 5` 且有可用资源：`core.heal()`；
3. Core HP 已满、护盾低于当前上限且有可用资源：`core.repair_shield()`；
4. 其余情况执行当前生产策略。

Unit 治疗先于 Core 动作结算，因此使用 `context.healing_resources` 的剩余额度，不重复花费已经
为 Unit 治疗保留的资源。Core `HEAL` 可部分恢复，所以至少 1 资源即可提交。

### 6.2 Beacon 护盾上限

当 `turn.beacon.status == CARRIED` 且 `carrier_id` 等于己方 Core 或任一己方 Unit 时，上限为 10；
其他情况为 5。Beacon 状态不可见时不得猜测归属，按普通上限 5 处理。

### 6.3 A4 测试

- Core 缺 HP 时优先于生产并提交 `HealAction`；
- Core HP 满、护盾缺口时提交 `RepairShieldAction`；
- Unit 治疗预留后资源为零时不提交 Core 恢复；
- Core 迁移中不恢复；
- 己方持有 Beacon 时修到 10，敌方或未知载体不提高上限；
- Core 满状态保持原生产行为。

## 7. 阶段 A5：可选能力边界

### 7.1 Champion Beacon

后续若启用，使用当前 `CombatSquad` 选择一支完整野战小队，不再恢复历史独立 Expedition
编队模型。只在 Beacon 为 `GROUND` 且远征队有明确当前路径时拾取；己方载体返家后由保护小队
接管。状态不可见时只使用公开坐标，不猜测载体阵营。

### 7.2 资源记忆

拆分：

```text
observed_resources = turn.resource_cells 的完整当前可见集合
eligible_resources = 策略半径、危险和路径过滤后的 Worker 候选集合
```

`known_resources` 合并完整观察；`assign_resource_targets()` 只消费候选集合。自然资源和 Cargo
仍只能按位置记忆，数量以官方状态不可见处理。

A5 保持可选且未启用，不阻塞本轮目标验收。

## 8. 兼容、持久化与回滚

- A1 的移动目的格为进程内临时状态，不提高 `STATE_VERSION`；
- A2/A3 不改变状态文件结构；
- A4 不持久化 Core 恢复意图，每 Tick 只信任当前完整状态；
- 每阶段独立提交，失败时只回滚该阶段提交；
- 旧 `.arena_core_state.json` 可直接加载；服务重启最多丢失一次移动失败目的格关联；
- 作者名保持 `miemiehoho`，提交邮箱使用 GitHub no-reply 地址，提交信息不写个人邮箱或
  `Co-authored-by`。

## 9. 阶段验收命令

每阶段至少执行：

```bash
python -m unittest -q
python -m compileall -q arena_core_agent.py test_arena_core_agent.py
git diff --check
```

阶段提交前确认只包含 `arena_core_agent.py`、`test_arena_core_agent.py` 和对应文档；提交后执行：

```bash
git push origin "进攻才是最好的防守"
```

A1～A4 完成后共运行 126 个离线测试，并通过 `compileall` 与 `git diff --check`。README、
审计报告、详细设计和更新日志同步后，确认本地 `HEAD` 与
`origin/进攻才是最好的防守` 一致。
