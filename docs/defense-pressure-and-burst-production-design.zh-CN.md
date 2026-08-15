# ArenaHero 受击危险评估与爆兵优化详细设计

> 设计日期：2026-08-16
> 适用分支：`进攻才是最好的防守`
> 官方基线：Arena Hero 规则 `v0.14`、API `v0.1`、Python SDK `0.2.9`
> 状态：已完成（D1/B1/B2/B3）

## 1. 背景与现状

当前 Agent 已在 Core 12 格内发现敌方 Vanguard/Ranger 时召回巡逻小队，并让守家单位迎击；
但生产仍主要服从正常经济节奏：人口达到 20 后，如果 Core 未满仓且不在战损补员期，会进入
`RESOURCE_WAIT`。这会导致敌军数量明显占优时，库存足以生产战斗单位却不紧急扩军。

官方动态价格还带来一个反直觉选择：Unit 自毁先于当 Tick 的 `SPAWN` 定价。主动自毁部分
Worker 可以降低人口和生产价格，但同时没有退款，还会降低 Core 容量并立即销毁溢出库存。
因此不能只比较“下一名单位便宜多少”，必须比较当前库存能完成的整段生产数量。

## 2. 官方事实与实现边界

实现依据官方三个仓库确认以下结算事实：

1. 任意 Unit 可提交 `SELF_DESTRUCT`，在移动前移除，无资源退款、无范围伤害；Worker 货物在
   原格形成可采集掉落物，Beacon 也会落地且本 Tick 不能重新拾取。
2. Core 每 Tick 最多生产一个 Unit；`SPAWN` 在移动和战斗之后结算，新 Unit 本 Tick 不能行动。
3. 服务器在同 Tick Unit 自毁和战斗死亡后，按结算时存活人口重新计算生产价格：

   ```text
   price = unit_cost(unit_type, settlement_population)
   ```

4. 人口下降后 Core 容量立即变为 `core_resource_capacity(population)`，也就是
   `max(10, population * 5)`；现有库存超过新容量的部分被永久销毁。
5. Unit 治疗先于 Core 动作结算，Core `HEAL/REPAIR_SHIELD/SPAWN` 共用一个动作槽；现有 Core
   生存优先级不能被爆兵覆盖。
6. 每份新 `state` 是权威完整状态，当前 Tick 无法知道未来敌方移动、战损和资源捕获；优化器
   只使用当前库存，不预测未来采集收入。

## 3. 目标与非目标

### 3.1 目标

- 将 Core 周边危险稳定分为 `NONE`、`PRESSURED`、`CRITICAL`。
- 只有兵力明显不足的 `CRITICAL` 状态才绕过满仓门槛进行爆兵。
- 枚举直接生产和自毁部分 Worker 两类方案，选择当前资源能生成最多战斗单位的方案。
- 保留至少 12 名 Worker，保持既有 `2 Vanguard + 1 Ranger` 战斗编制。
- 将判断依据、选中方案和预计产量写入中文日志，便于线上核对。

### 3.2 非目标

- 不自毁 Vanguard/Ranger，不自毁 Core，不把 Worker 降到 12 以下。
- 不改变攻击伤害、巡逻范围、目标预留、战损补员和资源采集算法。
- 不引入未来资源收益、敌方隐藏兵力、随机战损或长期经济模型。
- 不用近似价格公式替代官方 SDK，不引入通用整数规划库。

## 4. 危险评估

### 4.1 输入

每 Tick 在规划动作前读取：

- Core 12 格内当前可见敌方 Vanguard/Ranger；
- `known_combat_threats` 中最近 6 Tick、最后位置仍在 Core 12 格内的敌方战斗单位；
- 当前实际位于 Core 12 格内的己方 Vanguard/Ranger；
- Core HP 是否低于 5、普通护盾是否低于 5。

可见与记忆敌人按 UUID 去重。敌方 Worker 和 Core 不计入攻击单位数量；两种战斗单位每次合法
攻击均造成 1 点基础伤害，因此第一版按单位数比较，不发明额外战力权重。

### 4.2 等级

| 等级 | 条件 | 行为 |
|---|---|---|
| `NONE` | Core 12 格内没有当前或有效记忆的敌方战斗单位 | 沿用正常经济与巡逻 |
| `PRESSURED` | 有敌军，且敌军不少于本地防守或 Core 已受损，但未达到临界条件 | 召回、迎击；不自毁 Worker |
| `CRITICAL` | 敌军至少 3 名且多于本地防守；或本地无防守且敌军至少 2 名；或 Core 已受损且至少 2 名敌军不少于本地防守 | 绕过满仓门槛，执行爆兵规划 |

等级每 Tick 重新计算；旧威胁在 6 Tick 后自然过期。危险从 `CRITICAL` 降级后立即停止紧急生产，
回到既有战损补员与正常满仓扩编路径。

## 5. 爆兵经济优化器

### 5.1 候选范围

设当前人口为 `N`、Core 当前可用于生产的资源为 `R`、Worker 数为 `W`。枚举：

```text
k = 0 .. max(0, W - 12)
```

`k=0` 是直接生产；`k>0` 是本 Tick 自毁 `k` 名 Worker。Champion Beacon 携带者不进入候选，
实际可自毁数不足时缩小上界。Worker 选择按“空载优先、Cargo 少优先、远离 Core 优先、UUID
稳定顺序”确定，避免为了相同收益丢弃更多在途资源，并优先保留更可能及时返家的 Worker。

### 5.2 容量与有效库存

每个候选先计算：

```text
start_population(k) = N - k
capacity(k) = core_resource_capacity(start_population(k))
retained_resources(k) = min(R, capacity(k))
```

`R - retained_resources(k)` 是自毁后立即损失的库存。不能把这部分继续用于后续生产，也不能把
Worker 自毁当作退款。

### 5.3 逐单位模拟

从 `start_population(k)` 和 `retained_resources(k)` 开始，沿现有 `2V1R` 缺口顺序逐个选择下一
战斗单位。每一步都调用：

```text
cost_i = unit_cost(next_type, current_population)
current_capacity = core_resource_capacity(current_population)
```

只有 `cost_i <= remaining_resources` 且 `cost_i <= current_capacity` 时计入一名，然后扣除资源、
人口加一并重新计算下一名价格。第一名对应当前 Tick 的 Core 动作，后续单位只是对现有库存的
可生产数量预测；后续 Tick 会用新权威状态重新计算。

### 5.4 目标函数

候选按以下确定性顺序选择：

1. `spawn_count` 最大；
2. 产量相同时 `sacrifice_count` 最小；
3. 仍相同时 `remaining_resources` 最大。

因此自毁方案只有在比 `k=0` 严格多生成战斗单位时才会胜出。价格降低但总产量不变时保留 Worker。

### 5.5 官方价格示例

以下示例按满仓库存和既有 `2V1R` 顺序计算：

| 人口与编制 | 直接生产 | 最优 Worker 自毁 | 选中结果 |
|---|---:|---:|---|
| N=40，16W/16V/8R，R=200 | 5 名 | k=1～4 仍为 5 名 | k=0，不自毁 |
| N=60，15W/30V/15R，R=300 | 2 名 | k=3 可生产 3 名 | k=3 |
| N=80，20W/40V/20R，R=400 | 1 名 | k=7 可生产 2 名 | k=7 |

N=80 的 k=8 虽也能生产 2 名，但目标函数优先 k=7。高人口时如果所有候选的下一单位价格仍
超过对应容量，则产量均为 0，必须选择 k=0，不能白白自毁 Worker。

## 6. 动作集成与优先级

Core 决策顺序调整为：

```text
Core HEAL / REPAIR_SHIELD
-> 生产清理期与 Core 可用性
-> CRITICAL：爆兵经济规划
-> 非 CRITICAL：Worker 战损补员
-> 20 人口后正常满仓门槛
-> 既有 2V1R 正常生产
```

`CRITICAL` 期间暂缓 Worker 补员，但不允许自毁到 12 以下；危险降级后，既有补员状态机会继续
恢复 Worker 和人口。选中 `k>0` 时，先覆盖相应 Worker 的本 Tick 动作为 `SELF_DESTRUCT`，再让
Core `SPAWN` 预测序列中的第一名战斗单位。官方按正确结算顺序处理二者。

以下情况一律不提交 Worker 自毁：

- Core 正在迁移、生产清理期生效或预计生产格仍满；
- 治疗预留后资源不足以支付首个战斗单位；
- 所有方案产量为 0，或自毁方案没有严格提高产量；
- 可保留 Worker 少于 12，或候选 Worker 正携带 Champion Beacon。

## 7. 数据结构与诊断

新增不持久化派生对象：

```text
DefensePressure(level, enemy_count, defender_count, core_damaged)
BurstPlan(sacrifice_count, start_population, retained_resources,
          spawn_types, remaining_resources)
```

危险和方案来自当前权威状态，每 Tick 重算，不写入 `.arena_core_state.json`，因此状态版本保持 v10。
`PlanningMetrics` 和中文 Tick 统计增加：

- `防御危险等级`
- `近家敌方战斗单位数`
- `近家防守单位数`
- `直接爆兵预计数量`
- `选中爆兵预计数量`
- `爆兵自毁Worker数`
- `爆兵预计剩余资源`

动作摘要示例：

```text
defense pressure=CRITICAL enemies=5 defenders=2
00000000 worker self-destruct defense-burst cargo=0
core spawn VANGUARD (defense-burst direct=2 selected=3 sacrifice=3)
```

## 8. 测试设计

### B1：危险评估

- 无敌人得到 `NONE`；敌我均衡或单个敌人得到 `PRESSURED`；
- 三名以上敌人且本地防守不足、零防守遇两敌、受损 Core 遇两敌得到 `CRITICAL`；
- 可见与记忆敌人按 UUID 去重，6 Tick 后过期；远处敌军不触发近家危险。

### B2：优化器

- N=40 同产量不自毁；N=60 选择 k=3；N=80 选择 k=7；
- 容量缩小销毁库存后重新计算，不能使用溢出资源；
- 12 名或更少 Worker 时只评估 k=0；相同产量选更少自毁；
- 逐个跨越动态价格档，生产序列保持 `2V1R`；价格超过容量时停止。

### B3：动作集成

- `CRITICAL` 且 Core 未满仓仍生产战斗单位；`PRESSURED/NONE` 保持旧门槛；
- 选中自毁方案时同 Tick 存在 Worker `SELF_DESTRUCT` 和 Core `SPAWN`；
- Worker 不低于 12，不选择 Beacon 携带者，优先空载 Worker；
- 资源不足、迁移、生产格阻塞、Core 生存动作时不自毁；
- 危险期暂缓 Worker 补员，危险解除后恢复；日志字段与动作数量正确。

## 9. 分阶段发布

### D1：详细设计

已在 `e2fe7de` 新增本文和 README 索引，并核对官方事实、链接和格式。

### B1：危险评估与诊断

已在 `0a858fd` 实现纯危险评估、规划上下文和诊断，不改变当时的生产或自毁行为。

### B2：动态价格优化器

已在 `987494b` 实现纯经济模拟和候选选择，不提交 Worker 自毁动作。

### B3：动作集成

已在 `0e65608` 接入 `CRITICAL` 生产优先级、Worker 自毁和 Core 生产，并覆盖官方动作边界。

### D2：文档验收

README、本文状态、日志使用说明和更新日志已同步；最终离线测试共 165 个。

## 10. 实现回写

- 危险与爆兵方案仍是每 Tick 派生数据，没有新增持久化字段，状态版本保持 v10。
- B3 先完成全部 Unit 动作规划，再在确实提高可生产数量时覆盖所选 Worker 动作为
  `SELF_DESTRUCT`；规划占位同步释放其预计落点，随后提交当 Tick 的 Core `SPAWN`。
- 危险等级写入 Tick 统计，不额外增加一条重复的危险动作摘要；爆兵动作摘要保留直接/选中
  产量、自毁数和预计剩余资源。
- Core 恢复动作、迁移、生产清理期与生产格可用性检查均在爆兵之前；危险解除后沿用既有
  Worker 战损补员状态。
