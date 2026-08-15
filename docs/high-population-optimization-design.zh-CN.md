# 40+ 人口优化详细设计

> 实施状态：阶段 1～4 已完成并推送到 `进攻才是最好的防守`，最终提交为 `3b55bc7`。

## 1. 设计目标

本文是 `ArenaHero-Guide-Reforged` 对
`high-population-optimization.zh-CN.md` 的实施级设计。目标是在不改变低人口既有行为的前提
下，让人口达到 40 和 60 后仍能稳定决策、有效覆盖地图，并在动态生产价格超过 Core 容量时
停止无意义尝试。

本文只设计和修改 fork 内的 `arena_core_agent.py` 及其离线测试，不修改官方三个项目、SDK
或 Arena Hero 服务端规则。

### 1.1 官方约束

以下事实来自工作区 `arena-hero/arena-hero-doc`、`arena-hero/arena-hero-python` 和
`arena-hero/arena-hero-skill`：

- 每个 Tick 只有一个约 15 秒的命令窗口；Agent 必须针对当前完整状态生成一份计划，不能
  重写旧 Tick 的 Tick 编号后重试。
- Core 容量为 `max(10, population * 5)`，不存在每 Tick 人口维护费。
- 生产价格必须使用官方 `unit_cost(unit_type, population)`；当前 Worker、Vanguard、Ranger
  基础价分别为 5、10、12，人口 21 开始每五个人口进入下一档 1.3 倍价格。
- Vanguard 只能对相邻格使用 `SWEEP`；Ranger 只能沿横、纵或精确 45 度对角线在 1～3 格
  使用 `SHOOT`，中间永久障碍会挡住射线。
- `HEAL` 是战斗后的完整动作，Unit 必须与己方静止 Core 同格；动作和最终计划仍由 SDK
  与现有提交流程校验。

### 1.2 当前代码边界

现有 Agent 是单文件、单线程的 Tick 规划器。关键入口和职责如下：

| 位置 | 当前职责 | 本设计的扩展 |
| --- | --- | --- |
| `AgentMemory` | 资源、敌方 Core、小队、巡逻和搜索记忆 | 增加短期诊断、生产饱和和攻击波状态 |
| `CombatSquad` / `sync_combat_squads()` | 按 `2 Vanguard + 1 Ranger` 稳定编队 | 派生防守、快速反应和野战职责 |
| `plan_turn()` | 组织每 Tick 数据、任务和动作规划 | 建立诊断、角色、分区和共享目标分配 |
| `plan_field_squads()` | 野战巡逻、集结、攻击和搜索 | 分区巡逻、批次集结和攻击波 |
| `plan_vanguards()` / `plan_rangers()` | 家园守卫与野战单位行为 | 消费保护小队和目标预留结果 |
| `plan_core_production()` | Worker 与战斗单位生产 | 识别动态价格生产饱和 |
| `first_step_astar()` | 单次 A* 返回第一步 | 统计、Tick 预算和同 Tick 缓存 |
| `update_and_assign_search_missions()` | 逐任务计算搜索覆盖 | 复用 Tick 级视野覆盖缓存 |

不引入新的 Runner、数据库、配置框架或第二套提交器。优先使用现有 dataclass、`AgentMemory`
和 `PlanningContext`，保持测试可以直接构造 `Turn` 离线调用 `plan_turn()`。

## 2. 总体 Tick 流程

每个 Tick 按以下顺序执行，所有中间结构只服务当前权威状态：

```text
Turn 完整状态
  -> 诊断上下文与 Tick 预算
  -> 资源/敌情/障碍观察
  -> 编队同步与职责派生
  -> 巡逻分区、攻击波和防守预留
  -> 搜索视野缓存与资源/Worker 角色
  -> 共享目标预留
  -> Worker、小队、Vanguard、Ranger、Core 行为
  -> 现有占位与动作合法性流程
  -> 日志诊断、状态节流保存、提交当前 Tick
```

任何状态缺失、Core 移动或关键路径不可验证时，保留现有安全行为：当前 Tick 等待、返 Core
或疏散，不复用旧 Tick 的完整计划。

## 3. 运行时数据契约

### 3.1 `PlanningDiagnostics`

诊断不写入持久化状态，只保存最近一次 Tick 的可序列化摘要：

```text
tick
decision_ms
astar_calls
astar_expansions
astar_cache_hits
astar_budget_exhausted
field_squad_count
protected_squad_ids
active_wave_id
gathering_squad_count
stalled_squad_count
production_status
production_wait_reason
```

`first_step_astar()` 每次调用计数，并在达到 Tick 级预算后拒绝低优先级新搜索。高优先级逃跑、
返 Core 和生产格疏散可以使用保留预算；预算耗尽时低优先级巡逻沿用旧目标并 `WAIT`。
日志只记录数字和状态，不写入 API key。

### 3.2 生产状态

生产规划在选择下一单位类型后先计算官方价格：

```python
cost = unit_cost(next_type, turn.state.population)
```

状态分为：

- `READY`：价格不超过 Core 容量，继续沿用当前 `2V1R`/Worker 补位顺序；
- `RESOURCE_WAIT`：价格未超过容量，但当前资源不足；
- `SATURATED`：`cost > resource_capacity`，本 Tick 不提交 `SPAWN`，记录下一单位类型、价格、
  容量和原因；
- `BLOCKED`：生产格被占用或处于 `CELL_UNIT_LIMIT` 清理期。

饱和状态不需要跨进程保存。每 Tick 只在下一单位类型、人口、容量或价格规则变化时重新评估，
避免人口 87 附近重复尝试同一必失败生产。

### 3.3 小队职责

小队编号继续由现有稳定排序产生，职责按人口和完整小队顺序派生，不把职责写死到 Unit UUID：

| 人口 | `HOME_GUARD` | `RAPID_RESPONSE` | `FIELD` |
| --- | --- | --- | --- |
| `<20` | 小队 0 | 无 | 其余完整小队 |
| `20～39` | 小队 0 | 小队 1 | 其余完整小队 |
| `>=40` | 小队 0、1 | 小队 2 | 其余完整小队 |

保护职责中的 Unit 不参与首轮远征，但仍由现有 Vanguard/Ranger 家园逻辑巡逻和迎击。快速反应
队使用近家防区，发生家园威胁时优先响应；实现阶段不改变生产比例，只改变任务预留。

### 3.4 巡逻分区

人口低于 40 时保持现有 12～32 格行为。人口达到 40 后，按野战小队序号（不含保护职责）
分配固定方环带：

| 野战序号 | 方环带 |
| --- | --- |
| 1～4 | 12～32 |
| 5～8 | 32～56 |
| 其余 | 56～80 |

每个方环继续使用现有稳定扇区、方向和路径失败推进。Core 迁移后使用新位置计算，不保留旧
绝对目标。巡逻带由排序后的野战小队序号派生，因此 Unit 死亡或补位不会随机抖动。

### 3.5 攻击波

发现有守军的敌方 Core 时，只让野战小队按固定批次进入攻击：

- `wave_size = 3`，最后一批可以少于 3 支；
- 批次按完整野战小队 ID 排序，每批拥有独立 Rally Point；
- 活动批次先在安全集结点完成最低集结，再向敌方 Core 推进；
- 后续批次保持巡逻或快速反应职责，活动批次完成集结并开始接敌后才进入队列；
- 活动批次失去完整战力时，取消其集结并推进下一批；
- 敌方 Core 不再受保护或目标消失时，清空攻击波状态，所有小队恢复巡逻。

Rally Point 仍使用现有 `choose_assault_rally()` 的障碍和安全距离规则，但按批次输入小队
领导者，避免所有单位共享一个集结点。

### 3.6 目标预留

每 Tick 在行为规划前为可见敌方 Unit 生成确定性预留表：

```text
target_id -> (reserved_unit_ids, max_attackers, reason)
```

默认上限：敌方 Worker 1 名攻击者，敌方 Vanguard/Ranger 2 名攻击者，敌方 Core 3 名攻击者；
紧急目标同样允许 3 名攻击者。实现按紧急状态、目标类型和敌方 UUID 稳定排序。预留只约束主动
攻击，不阻止紧急自卫、低 HP 逃跑或 Core 防守。Vanguard 负责相邻屏障，Ranger 只在合法射线
和未超过预留时开火。

### 3.7 Worker 角色

Core 满仓且人口达到外圈探索阈值时，四名 Worker 按稳定 UUID 顺序派生：前两名为
`CARRIER`，后两名为 `SCOUT`。`CARRIER` 保留已知资源任务、载货返航和生产容量释放后的
优先交付；`SCOUT` 使用现有外圈扫描。遇到威胁、载货或返航优先级时，角色只是默认职责，
不得覆盖现有逃跑和交付逻辑。

## 4. 搜索与寻路优化

### 4.1 A* 预算和缓存

`first_step_astar()` 增加可选诊断对象，不改变返回值。每个 Tick 建立短生命周期缓存，键由
`(start, goal, static_obstacle_signature, blocked_signature, max_expansions)` 构成；只缓存
静态障碍未变化且同一步骤可复用的第一步，动态占位变化时自然失效。缓存不跨 Tick、不写磁盘。

路径优先级如下：

1. 威胁逃跑、载货返 Core、生产格疏散；
2. 战斗接敌、攻击波集结和快速反应；
3. 搜索、巡逻和低优先级外圈探索。

### 4.2 搜索覆盖缓存

`plan_turn()` 每 Tick 复用友军视野来源和覆盖结果，`search_goal_for()` 不再为相同输入重复
计算视野覆盖。缓存键包含来源位置、视野半径、由任务中心推导的区域签名和障碍签名；任务只
读取执行小队所需区域时，优先使用小队覆盖子集。

## 5. 状态、日志和兼容性

- 诊断、Worker 角色、职责和攻击波均可由当前 Tick 派生；只有现有资源、敌方 Core 和小队
  进度继续按原格式持久化，避免无必要的状态版本升级。
- 新增日志字段使用稳定键值格式，例如 `metrics astar_calls=...`、
  `production status=SATURATED type=RANGER cost=472 capacity=435`、
  `squad-role team=2 role=RAPID_RESPONSE`。
- 低于 40 人口、没有敌方 Core 或没有满仓时，保留现有巡逻、生产、搜索和 Worker 行为。
- 新逻辑只能使用当前 Turn 的官方可见实体和已过期策略记忆，不能把历史敌方位置当作当前
  可攻击目标。

## 6. 分阶段实施与验收

### 阶段 0：设计与基线（已完成，`11bcf60`）

交付本文件、记录当前 40/60 人口基线、确认官方 SDK 规则和测试入口。验收：文档中的函数、
数据契约和阶段顺序与当前代码一致。

### 阶段 1：压力观测与生产饱和（已完成，`4633613`）

修改 `first_step_astar()` 诊断、`plan_turn()` Tick 指标和 `plan_core_production()` 饱和
判断；新增 40/60 人口、价格超过容量和重复生产抑制测试。验收：现有测试全通过，新测试能
解释每次生产等待原因，且不改变低人口生产。

### 阶段 2：巡逻分区、动态防守与攻击波（已完成，`5c0d2b1`）

新增职责派生、动态巡逻方环、保护小队过滤和分批 Rally。验收：40 人口至少两支保护小队和
一支快速反应队，野战小队分布到三层方环；有守军 Core 时不会把所有野战小队拉到同一点。

### 阶段 3：目标预留与 Worker 分工（已完成，`a26761c`）

新增每 Tick 目标预留和满仓 Worker 角色，接入现有小队自卫、攻击、返航和外圈扫描。验收：
同一目标不会被无限重复锁定，载货 Worker 不因探索角色跳过交付，Core 满仓时仍能释放生产格。

### 阶段 4：A* 与搜索覆盖优化（已完成，`3b55bc7`）

加入 Tick 级路径统计、短生命周期缓存和搜索覆盖缓存，并沿用现有单次 A* 扩展上限。验收：
缓存命中、A* 扩展数和决策耗时进入日志；动态占位变化后仍返回与无缓存版本相同的合法第一步；
40/60 人口压力测试无长期卡死。

每阶段独立提交并推送。提交标题不包含邮箱或 `Co-authored-by`，作者沿用当前仓库 Git 配置。
阶段失败时只回滚当前阶段提交，不删除用户的 `.env`、状态文件或其他未跟踪数据。

## 7. 测试计划

### 单元测试

- `patrol_band_for()`：低人口兼容、40 人口三层分区、排序稳定；
- `protected_squad_ids()`：20、40、60 人口职责边界；
- 攻击波分组、独立 Rally、活动批次推进和目标消失清理；
- `build_target_reservations()`：上限、确定性、紧急目标例外和目标类型优先级；
- 生产饱和：人口 87 的 Ranger 价格 472 对容量 435 时停止重复生产；
- Worker `CARRIER`/`SCOUT` 分工与载货返航；
- A* 统计/缓存命中和搜索覆盖缓存失效。

### 压力 fixture

使用现有 `make_turn()` 构造至少 40 和 60 人口的完整阵容，分别覆盖：

1. 无敌情分层巡逻；
2. Core 附近遭袭和快速反应；
3. 有守军 Core 的三支攻击波；
4. 远距离敌方 Core 记忆；
5. Core 满仓、Worker 载货和生产饱和。

每个 fixture 连续运行多个 Tick，断言动作确定性、没有重复目标失控、没有路径失败无限增长，
并检查 `memory.last_plan_metrics` 或等价日志摘要。

### 验收命令

```bash
python -m unittest -q
python -m compileall -q arena_core_agent.py test_arena_core_agent.py
git diff --check
```

不在离线测试中提交真实 API Turn；联网运行只在用户明确部署新版本后执行。

## 8. 回滚和发布

每个阶段在 fork 的默认分支上单独提交并 `git push origin "进攻才是最好的防守"`。提交前确认：

```bash
git status --short
python -m unittest -q
python -m compileall -q arena_core_agent.py test_arena_core_agent.py
git diff --check
```

网络失败时保留本地提交，下一次唤醒先执行 `git status`、`git log` 和 `git push`，成功后再进入
下一阶段。任何阶段不得提交 `.env`、`.arena_core_state.json` 或日志文件。
