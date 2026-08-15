# ArenaHero-Guide-Reforged 实现审计报告

> 审计日期：2026-08-15  
> 审计对象：`arena_core_agent.py`、`test_arena_core_agent.py`、部署文件和项目文档  
> 官方基线：Arena Hero 规则 `v0.14`、API `v0.1`、Python SDK `0.2.9`  
> 官方来源提交：`arena-hero-doc` `838a8eb8406e82086ee5750ae39cd68b396d0786`、
> `arena-hero-python` `423d252adcca439669adb3e7b04252e53b4430bd`、
> `arena-hero-skill` `88fe8632a1dceeba58f57db5a0facfb5d5ef5a74`

> 实施状态：阶段 A1（`99b7769`）和 A2（`e868de5`）已完成；A3～A4 待实施，A5 保持可选。

## 1. 审计结论

当前 fork 已完成原有 40+ 人口优化的四个阶段：动态生产饱和、巡逻分层、攻击波与目标
预留、Tick 级 A*/搜索覆盖缓存。这些能力与官方 v0.14 的动态价格、无维护费、Unit
视野和 Ranger 射击规则基本一致，110 个离线测试全部通过。

但当前实现仍存在 4 个会直接影响线上动作合法性或 Runner 存活性的 P0 问题，以及 4 个
需要排入后续阶段的 P1/P2 改进项。现有测试主要验证策略候选，没有覆盖完整的命令窗口、
错误恢复、Core 占位和移动失败上下文，因此“测试全绿”不能证明这些边界已经正确。

## 2. 官方规则核对范围

审计逐项核对了官方三个项目中的以下内容：

| 主题 | 官方依据 | 结论 |
|---|---|---|
| Tick 与 15 秒命令窗口 | `arena-hero-doc/docs/rules/world-and-ticks.md`、`arena-hero-skill/references/agent-command-loop.md` | 当前有 Tick 诊断，但没有硬截止时间和安全降级计划。 |
| 完整计划替换与错误恢复 | `arena-hero-doc/docs/api/commands.md`、`docs/api/errors.md`、SDK `client.py` | 正常使用 `Turn.submit()` 正确；外层异常分流不符合官方重试矩阵。 |
| 视野、障碍和 Ranger 射击 | `arena-hero-doc/docs/rules/map-and-vision.md`、`combat.md` | `visible_from()` 和 `clear_ranger_shot()` 已分别使用 Manhattan/supercover 与八方向中间格规则。 |
| Unit/Core 叠加与移动依赖图 | `arena-hero-doc/docs/rules/movement-and-stacking.md` | 规划器的占位模型漏算 Core；移动失败事件的 `position` 语义使用错误。 |
| Core 容量、生产和动态价格 | `arena-hero-doc/docs/rules/core-and-economy.md`、SDK `rules.py` | 动态价格和无维护费已适配；生产前的 Core 占位判断仍有缺陷。 |
| Core 迁移和 Worker 交付 | `core-and-economy.md`、`api/resolution-results.md` | Core 迁移时仍可能提交必然失败的 `DEPOSIT`。 |
| 同时战斗、死亡和重生 | `arena-hero-doc/docs/rules/combat.md`、`destruction-and-respawn.md` | 当前主要依赖下一份完整状态，尚未覆盖 Core 防御恢复和重生压力测试。 |
| Champion Beacon | `arena-hero-doc/docs/rules/champion-beacon.md`、`arena-hero-skill/references/game-rules.md` | 当前 Agent 没有 Beacon 行为；属于明确的功能缺口而非协议兼容问题。 |

## 3. 已确认问题

### A-01（P0）：Core 没有计入规划占位，可能重复触发 `CELL_UNIT_LIMIT`

位置：`arena_core_agent.py` 的 `FriendlyOccupancy`、`plan_turn()` 占位初始化和
`plan_core_production()`。

当前 `FriendlyOccupancy` 只接收 `turn.units` 的位置；Core 位置没有作为一个可占位实体加入。
官方规则是每格最多两个可占位实体，Core 本身已经占一个位置。因此当一个 Worker 正站在
Core 上时，规划器仍认为 Core 格可以再进入一个 Unit，也认为可以生产。生产动作会在结算
阶段收到 `CORE_SPAWN_FAILED/CELL_UNIT_LIMIT`，Unit 返家也会收到移动或容量失败。

影响：

- 交付 Worker 与生产动作同 Tick 时可能提交必然失败的生产；
- 伤员回家时可能再安排第二个 Unit 进入 Core；
- 高人口状态会频繁进入 `spawn-clear`，浪费命令窗口和生产 Tick。

修复方向：初始化占位时加入 `turn.core.position`，让 Core 保持一个固定占位；Unit 离开
Core 时只移除自己的占位。补充“Core+1 Unit 已满、Unit 离开后可生产”的回归测试。

### A-02（P0）：把 `UNIT_MOVE_FAILED.position` 当成失败目标格

位置：`AgentMemory.observe_dynamic_blocks()`。

官方 `UNIT_MOVE_FAILED` 的 `position` 是 Unit 未移动后的原始位置，不是它尝试进入的目的格。
当前代码把该字段保存为 `temporary_blocked_cells`，随后又因为原始格仍有一个友军 Unit 而
通常立即清理。结果是动态目的格没有被记录，规划器会在下一 Tick 重复选择同一被占用路线。

修复方向：在提交计划后按 Unit ID 保存本 Tick 的预计目的格和计划 Tick；下一份状态处理
上一 Tick 的 `UNIT_MOVE_FAILED` 时，只对 `MOVE_DESTINATION_OCCUPIED`、`CELL_UNIT_LIMIT`
等可归因失败使用该目的格。没有对应计划上下文时不猜测目的格，只记录诊断。

### A-03（P0）：Core 迁移期间仍提交 Worker `DEPOSIT`

位置：`plan_workers()` 的载货返航分支。

官方迁移中的 Core 不接收交付，结算结果是 `DEPOSIT_FAILED/CORE_MOVING`；迁移期间 Core
仍可被攻击，Unit 也不能治疗。当前 Worker 只判断 `turn.resources < turn.resource_capacity`
就会在 Core 同格提交 `DEPOSIT`，或继续向正在迁移的 Core 返航。

影响：载货 Worker 每 Tick 消耗一次无效动作；若 Core 正在换家，返航队伍会在错误目的地附近
堆积，拖慢资源恢复。

修复方向：Core 非 `NORMAL` 时禁止新建交付路线；载货 Worker 保留货物，在安全的近家待命点
等待，Core 恢复静止后再返航。补充 `CORE_MOVING` 状态下同格和远处载货 Worker 测试。

### A-04（P0）：外层 Runner 的错误分流违反官方重试矩阵

位置：`main()` 的 `APIError` 和 `ArenaHeroError` 捕获分支。

官方要求：

- `COMMAND_WINDOW_CLOSED`：当前 Tick 不再重试，等待下一份状态；
- `TICK_MISMATCH`：根据新状态重新计算；
- `COMMAND_RATE_LIMITED`：当前来源/Tick 不再提交新请求；
- `UNAUTHORIZED`、`1008` 和 `ProtocolError`：停止并修复凭据、客户端或 SDK；
- 网络失败和 5xx 才使用有界重试。

当前实现将 429 一律当作可恢复会话错误，可能重新创建 Client 并为同一 Tick 生成新计划；
将 `COMMAND_WINDOW_CLOSED`、`TICK_MISMATCH` 等 409 直接作为 fatal；并把 SDK 的
`ProtocolError` 包在通用 `ArenaHeroError` 中无限重连。这会导致：窗口稍微超时后服务直接退出，
限流后重复提交同一 Tick，协议升级时持续重启而不是停机告警。

修复方向：按 `APIError.error` 精确分流，增加“跳过当前 Tick”状态；单独捕获
`ProtocolError`、`PolicyViolationError`、`AuthenticationError` 作为停止类错误；保留 SDK
对传输异常和 502/503/504 的精确重试边界。

## 4. 后续改进项

### A-05（P1）：没有 15 秒硬截止和安全计划降级

`PlanningMetrics.decision_ms` 只在规划完成后统计，不能阻止 A*、视野覆盖或排序超过官方
命令窗口。人口 40/60 的现有测试没有时间上限，也没有在预算耗尽时验证“仍提交完整合法计划”。

建议增加每 Tick 单调时钟截止点：先完成威胁逃跑、当前战斗和 Core 安全动作，再按剩余预算
停止远征搜索，未规划对象显式 `WAIT` 或省略，让 SDK 发送一个简单有效计划。日志记录
`deadline_exceeded` 和降级原因。

### A-06（P1）：没有 Core `HEAL`/`REPAIR_SHIELD` 决策

官方 SDK 和规则均支持 Core `HEAL`、`REPAIR_SHIELD`。当前 Agent 只为 Unit 规划 `HEAL`，
Core 受伤或护盾耗尽后只能继续生产/等待，长期运行会让家园防守能力单向下降。

建议按“Core 生存 > 家园即时防守 > 必要补编 > 普通生产”排序：Core HP 有缺口时保留
`HEAL` 预算；静止且护盾未满时在没有更高优先级生产的 Tick 修盾；持有 Beacon 时使用
10 点护盾上限，其他情况使用 5 点上限。补充资源不足、迁移中和遭受攻击的结算测试。

### A-07（P2）：Champion Beacon 只被 fixture 建模，Agent 没有主动策略

官方 Beacon 坐标始终公开，拾取、放下、死亡掉落、护盾上限和 Worker 采集加成都已在 SDK
中提供。当前 `arena_core_agent.py` 没有 `pickup_beacon()`、`drop_beacon()` 或 Beacon
状态决策；历史提交曾有远征实现，但当前分支已不再包含。

建议在 Core 防守和命令恢复稳定后，再设计“2V1R 最小远征队 + 近家失 beacon 恢复”的可选
策略，并明确不在 Beacon 状态不可见时猜测敌方载体。

### A-08（P1）：资源记忆半径是策略截断，不是官方视野边界

`plan_turn()` 把当前状态里的可见资源先限制在以 Core 为中心的 Chebyshev 36 格内。官方
状态可能包含远处野战 Unit 视野内的资源；这些资源并非不可见，只是当前策略不接纳。高人口
野战扩大后，这会丢弃已付出移动成本获得的资源信息。

建议把“官方当前可见资源”和“是否纳入 Worker 经济任务”拆成两个集合：先完整保存当前可见
资源，再由策略半径决定是否分配 Worker。

## 5. 已验证的正确部分

- 动态价格调用官方 SDK `unit_cost()`，没有保留旧版维护费或欠费伤害逻辑；
- `visible_from()` 使用 Manhattan 视野半径和 supercover 障碍遮挡；
- `clear_ranger_shot()` 使用横、竖、精确 45° 斜线的真实中间格，不把 Unit/Core 当作射线障碍；
- 生产比例、40+ 人口巡逻层、攻击波上限、目标预留和 Tick 级缓存已有对应测试；
- 状态文件使用临时文件加 `os.replace()` 原子替换，`.env`、状态文件和日志未纳入提交。

## 6. 测试与验收缺口

当前 110 个测试覆盖策略函数和 fixture 计划，但缺少：

1. Core 作为第二个占位实体时的生产和返家容量测试；
2. 使用官方事件语义的 `UNIT_MOVE_FAILED` 目的格关联测试；
3. Core 迁移期间 `DEPOSIT` 不应提交的测试；
4. `COMMAND_WINDOW_CLOSED`、`TICK_MISMATCH`、`COMMAND_RATE_LIMITED`、`ProtocolError` 的 Runner 分流测试；
5. 40/60 人口在规定时间预算内完成或降级提交的压力测试；
6. Core `HEAL`、`REPAIR_SHIELD` 和 Beacon 行为的回放测试。

## 7. 审计后的实施优先级

| 阶段 | 范围 | 目标提交内容 |
|---|---|---|
| 阶段 A1 | Core 占位、迁移交付抑制、移动失败上下文 | 消除必然失败的动作并补齐回归测试 |
| 阶段 A2 | API 错误分流、当前 Tick 跳过和 ProtocolError 停止 | 遵守官方命令窗口与重试边界 |
| 阶段 A3 | 15 秒截止、简单安全计划和高人口压力验收 | 防止 40+ 人口规划阻塞提交 |
| 阶段 A4 | Core 治疗/修盾 | 恢复长期家园生存能力 |
| 阶段 A5（可选） | Champion Beacon 与完整可见资源记忆 | 在核心可靠性稳定后增加收益策略 |

阶段 A1～A4 是当前维护目标；A5 需要单独评估线上 Beacon 争夺风险，不因“官方支持”而
强行启用。每个阶段完成后独立测试、更新本报告或详细设计的实施状态，并使用不包含个人
邮箱的提交信息推送到 `进攻才是最好的防守`。
