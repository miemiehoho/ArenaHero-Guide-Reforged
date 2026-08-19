# 资源采集与攻击编队分散优化详细设计

> 状态：实施中
> 官方基线：Arena Hero 规则 `v0.14`、API `v0.1`、Python SDK `0.2.9`

## 目标

解决长期运行数场战争后 Worker 围绕己方 Core 停滞、Vanguard/Ranger 围绕己方 Core 或同一集结点聚集的问题。策略应在短期返仓、治疗和防守完成后恢复远程搜索；野战小队应保持独立巡逻和分散攻击位。

## 约束

- Worker 只能在资源格 `HARVEST`，携货后返回静止己方 Core `DEPOSIT`。
- Unit 每 Tick 只能移动一步；每格最多两个实体，Core 所在格只剩一个 Unit 槽位。
- Ranger 只沿横/竖/精确 45 度斜线在 1-3 格射击，障碍物阻挡射线，Unit/Core 不阻挡。
- 每个 Tick 使用最新完整 state 重新生成完整计划，不跨 Tick 复用旧 Turn。

## 方案

### Worker

1. 当前可见资源和持久资源记忆一起进入资源分配器；已有租约继续跨 Tick 保留。
2. 无货、无资源租约、无撤退和治疗需求时，Worker 必须从 Core 外围环序列选择可达搜索点。普通模式使用 12/19/26/32 格环，Worker 数量增加后扩展到 40/48/56/64 格。
3. 环点首步不可达时顺时针换点，连续失败才推进环序；只有没有可达环点时才回退到 Core，并记录 `scout-blocked`。

### 野战编队

1. Rally 只服务当前活动攻击波；非活动波继续各自巡逻，不再统一停在 Rally 点。
2. 集结完成后活动波切换到敌方 Core/Unit 推进目标；每个小队按稳定编号选择不同的 approach cell。
3. Vanguard 优先前排接近，Ranger 优先后侧或合法射击位；找不到射击位时跟随 Vanguard，但不以己方 Core 作为长期目标。
4. 近家紧急防守、撤退、返仓和治疗仍然优先于分散巡逻。

## 分阶段

- A：Worker 资源候选、租约和远程搜索回退。
- B：野战攻击波、队形和攻击位分散。
- C：状态兼容、README/CHANGELOG、回归测试和部署验收。

## 验收

- 无资源离线 Tick 中空载 Worker 连续产生远离 Core 的 `scout`/`outer-scout` MOVE。
- 可见资源可分配，资源租约稳定，采集成功后可继续搜索。
- 有护卫 Core 的活动波集结后推进，非活动波继续 `squad-patrol`。
- 不同小队攻击位不同，Ranger 不被拉回己方 Core。
- `python -m unittest -q`、`compileall` 和 `git diff --check` 通过。
