# Arena Hero Guide Reforged 资源采集与攻击编队分散优化详细设计

> 文档状态：已完成（无需发布）
> 目标项目：`agent-projects/ArenaHero-Guide-Reforged`
> 官方基线：Arena Hero 规则 `v0.14`、API `v0.1`、Python SDK `0.2.9`
> 发布影响：`none`

## 1. 背景和目标

根据 [Arena Hero Guide Reforged Bohrium 部署教程](../../../docs/third-party-project-deployment/miemiehoho-arena-hero-guide-reforged-bohrium.zh-CN.md) 长期运行数场战争后，出现两类策略退化：

1. Worker 围绕己方 Core 聚集，远处资源搜索和采集明显减少；
2. Vanguard/Ranger 围绕己方 Core 或同一个集结点聚集，攻击单位没有保持野战间距。

本阶段只修复第三方 Agent 的策略回退路径，不修改服务端、SDK、协议和官方规则。

## 2. 官方约束

- Worker 只有站在资源格才能 `HARVEST`，携货后必须回到静止己方 Core 同格 `DEPOSIT`。
- Unit 每 Tick 只能移动一步，每格最多两个实体；Core 所在格只有一个 Unit 槽位。
- Ranger 只能沿横线、竖线或精确 45 度斜线在 1-3 格射击，Unit/Core 不阻挡射线，障碍物阻挡射线。
- 每 Tick 收到完整 state 后重新计算完整计划；旧 Turn 不跨 Tick 复用。

来源：[`docs/official/arena-hero-official-reference.zh-CN.md`](../../../docs/official/arena-hero-official-reference.zh-CN.md) 和官方三个子模块。

## 3. 现状根因

当前脚本已经具备资源记忆、普通/外圈搜索、2V1R 小队和攻击波，但长期运行时存在两个共同的回退问题：

- Worker 的资源租约、当前可见资源和环形搜索目标在不同分支之间切换；当目标首步受占位影响时，容易连续 `wait-scout`，没有强制选择新的远离 Core 目标。
- 野战小队在集结、防守、Ranger 跟随和目标失去视野时共享若干默认目标；活动波和非活动波缺少足够强的空间分离，Ranger 的通用跟随回退还可能把队伍重新拉近己方 Core。

## 4. 设计方案

### 4.1 Worker

- 可见资源与持久资源记忆同时进入分配器；有效租约继续跨 Tick 保留。
- 没有资源、货物、撤退和治疗需求时，Worker 必须从当前 Core 外围环序列选择可达目标，普通模式覆盖 12/19/26/32 格，Worker 数量增长后覆盖 40/48/56/64 格。
- 当前环点首步失败时先顺时针换点，连续失败才推进环序；Core 只能作为无可行环点的降级目标，并写入诊断动作。
- 载货返仓、采集和治疗优先级不变；这些状态结束后立即回到搜索流程。

### 4.2 攻击编队

- Rally 只属于当前活动攻击波；非活动波继续自己的巡逻环，不再全部停在 Rally 点。
- 活动波集结完成后必须切换为敌方 Core/Unit 推进目标；每个小队按稳定编号取得不同 approach cell。
- Vanguard 优先前排接近，Ranger 优先后侧或合法射击位；找不到射击位才跟随 Vanguard，跟随目标不以己方 Core 为中心。
- 紧急近家防守仍可抢占上述规则，战斗、自卫和撤退优先级不变。

### 4.3 状态和日志

- 优先复用现有 v10 状态字段。确实新增持久化字段时才递增状态版本，并提供 v10 默认迁移。
- 保留 `resource`、`scout`、`outer-scout`、`squad-patrol`、`squad-gather`、`squad-assault` 等动作标签，增加阻塞/换点原因。

## 5. 分阶段范围

| 阶段 | 范围 | 交付 |
|---|---|---|
| A | Worker 资源候选、租约和远程搜索回退 | 代码、回归测试、独立 commit/push |
| B | 野战攻击波、队形和攻击位分散 | 代码、回归测试、独立 commit/push |
| C | 状态兼容、README/CHANGELOG、最终验收 | 文档、测试记录、独立 commit/push |

## 6. 验收

- 无资源离线 Tick 中，空载 Worker 产生远离 Core 的持续 MOVE，不长期停在 Core。
- 可见资源可被分配，途中任务稳定，采集成功后可继续搜索。
- 有护卫 Core 的活动波集结后推进；非活动波继续巡逻；不同小队攻击位不相同。
- Ranger 保持后侧/合法射击位，不被通用跟随逻辑拉回 Core。
- 目标仓库全量 unittest、compileall、`git diff --check` 通过。

## 7. 回滚和发布边界

每个阶段在目标嵌套仓库建立独立 commit 并推送远端分支，阶段之间可直接回退到上一 commit。根仓库只提交本设计和任务记录。本阶段发布影响为 `none`，不创建 ArenaHero-Nexus 稳定 tag，不修改 Docker 镜像和用户凭据。

## 8. 实施记录

- 阶段 A（`36fbdf0`）：当前 Tick 可见资源直接进入候选分配；Worker 搜索目标避开本 Tick 友军占位，A* 首步失败时换用下一环点，连续失败记录 `scout-blocked`；新增资源和远程搜索回归测试。
- 阶段 B（`e02ee16`）：Rally 仅作用于活动攻击波；非活动小队继续 `squad-patrol`；按小队编号轮换敌方目标接近格，Ranger 优先合法射击位并排除己方 Core；新增攻击分散回归测试。
- 阶段 C：未新增持久化字段，v10 状态格式保持兼容；README 与更新日志已同步，最终离线测试 171 项全部通过。
