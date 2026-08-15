# 更新日志

本文件记录 `ArenaHero-Guide-Reforged` 的可见行为和部署相关变更。提交作者沿用仓库现有 Git
配置；提交信息不包含邮箱或 `Co-authored-by`。

## 2026-08-16

### 受击危险评估与爆兵优化

- 新增 [受击危险评估与爆兵优化详细设计](defense-pressure-and-burst-production-design.zh-CN.md)
  （`e2fe7de`），明确近家危险模型、官方自毁与生产结算顺序、动态价格目标函数、容量溢出和
  分阶段边界。
- 完成 B1 危险评估（`0a858fd`）：按 Core 12 格内敌我战斗单位、Core 损伤和 6 Tick 敌情记忆
  输出 `NONE`、`PRESSURED`、`CRITICAL`，并写入中文 Tick 统计。
- 完成 B2 爆兵经济优化器（`987494b`）：逐单位调用官方 `unit_cost()` 和
  `core_resource_capacity()`，枚举 Worker 自毁数并比较产量、牺牲数、容量溢出和剩余资源。
- 完成 B3 动作集成（`0e65608`）：`CRITICAL` 时绕过未满仓门槛；只有自毁能严格增加可生产
  战斗单位数时，才在同 Tick 提交 Worker `SELF_DESTRUCT` 和 Core `SPAWN`。至少保留 12 名
  Worker，不选择 Champion Beacon 携带者，Core 恢复、迁移和生产清理优先。
- 新增 21 个危险评估、纯优化器和动作集成回归测试，fork 离线测试总数增至 165 个，全部通过。

### Worker 分区探索与战损补员

- 新增 [Worker 分区探索与战损补员详细设计](worker-exploration-and-replenishment-design.zh-CN.md)
  （`c027761`），明确稳定扇区、动态搜索半径、v10 状态迁移、人口峰值和补员状态机。
- 完成 W1 探索分区与动态范围实现（`5b0ad71`）：Worker 按实际数量分配连续扇区，普通搜索和
  资源记忆范围随 Worker 数量扩展到 64 格，外圈目标也不再复用固定八方向。
- 完成 W2 战损补员实现（`b18337b`）：成熟人口发生战损后跳过 Core 满仓门槛，优先补 Worker 至少
  12 名，再按 `2 Vanguard + 1 Ranger` 补齐战斗单位；补员状态持久化为 v10，并增加生产状态和
  人口峰值诊断字段。
- W2 新增 7 个回归测试，fork 离线测试总数增至 144 个，全部通过。

### 文档与部署

- README 同步官方规则对齐 A1～A4、Worker 补员策略和 144 个离线测试基线。
- 审计报告和详细设计回写 A3/A4 实施状态、提交号、剩余 A5 可选边界及最终验证结果。
- 增加已部署实例的日常更新命令，覆盖停止服务、拉取、依赖安装、离线验收、重启和日志检查。
- 新增 [中文日志体系详细设计](logging-system-design.zh-CN.md)，明确官方事件、Tick 统计、
  中文 JSONL 字段和 `arena_log.py` 查询命令的实施契约。
- 完成日志生产与查询实现（`a077192`）：主循环写入中文事件、统计、决策、提交和错误记录，
  `arena_log.py` 提供 `tail`、`events`、`stats`、`errors` 查询；离线测试总数增至 132 个。
- 修复日志统计重复计数和 `--limit 0` 边界行为（`cf5df9a`）：事件计数只采用逐事件记录，
  不再与 Tick 统计中的事件摘要重复累加；显式限制为 0 时返回空结果。
- 新增 [中文日志统计增强详细设计](logging-statistics-design.zh-CN.md)（`cdb400b`），明确兵种、
  人口、资源/容量、占用率、最新快照和事件类别的区间统计契约。
- 完成日志统计增强实现（`fb7c749`）：`stats` 输出 Worker/Vanguard/Ranger 数量摘要、人口和
  资源统计、事件类别计数及最新 Tick 快照；缺失字段不伪造为 0，离线测试总数增至 133 个。
- 新增 [中文日志使用说明](logging-usage.zh-CN.md)，补充部署目录、JSON/PowerShell/`jq` 查询
  示例及区间统计语义。

## 2026-08-15

### 实现审计

- 新增 [实现审计报告](implementation-audit.zh-CN.md)，依据官方规则 `v0.14`、API `v0.1`、
  Python SDK `0.2.9` 和 Skill 文档核对当前 Agent。
- 记录 Core 占位、移动失败上下文、Core 迁移交付、命令错误恢复、15 秒截止、Core 防御动作、
  Champion Beacon 和远处资源记忆等问题及 A1～A5 实施顺序。
- 新增 [官方规则对齐优化详细设计](official-aligned-optimization-design.zh-CN.md)，定义 A1～A4
  的实现契约、动作优先级、错误状态机、规划截止、测试和回滚边界；A5 保持可选。

### 官方规则对齐优化阶段 A4（`b7fa0dd`）

- Core HP 低于 5 时优先执行 `HEAL`，HP 满后再按当前上限执行 `REPAIR_SHIELD`，恢复动作
  优先于自动生产。
- 普通护盾上限为 5；只有官方状态明确显示 Champion Beacon 由己方 Core 或 Unit 携带时才
  使用 10 点上限。
- Core 恢复只消费 Unit 治疗预留后的剩余资源，Core 移动、生产格满或资源不足时不提交动作。
- 新增 Core 治疗、修盾、治疗资源预留、迁移和 Beacon 归属测试；离线测试总数增至 126 个。

### 官方规则对齐优化阶段 A3（`0fe6acb`）

- 增加 10 秒内部规划预算，并使用 `time.perf_counter()` 记录截止点。
- A*、彻查候选、Worker、小队、Vanguard 和 Ranger 循环在预算耗尽后停止低优先级规划，
  不清空已生成动作，最终 Core 决策仍会执行。
- 运行日志新增 `deadline_exceeded` 和 `degraded_sections`，用于定位发生降级的模块。
- 新增确定性过期截止测试及 40/60 人口预算内 smoke 断言；离线测试总数增至 120 个。

### 官方规则对齐优化阶段 A1（`99b7769`）

- 将 Core 计入每格两个实体的规划占位，避免 Core 上已有 Unit 时重复提交生产或返家动作。
- 根据上一 Tick 已提交的 `MoveAction` 目的格解释 `UNIT_MOVE_FAILED`，不再把官方事件中的原始
  位置误当成失败目标。
- Core 迁移期间让载货 Worker 近家待命，不再提交必然失败的 `DEPOSIT`。
- 新增容量、迁移和移动失败上下文回归测试；离线测试总数增至 117 个。

### 官方规则对齐优化阶段 A2（`e868de5`）

- 按官方 `APIError.error` 矩阵区分当前 Tick 跳过、会话重启和致命停止。
- `COMMAND_WINDOW_CLOSED`、`TICK_MISMATCH`、限流和 `TICK_NOT_READY` 不再为同一 Tick 重复提交。
- `ProtocolError`、认证失败和 WebSocket `1008` 进入停止分支；新增错误矩阵回归测试，离线
  测试总数增至 118 个。

### 高人口优化阶段 4（`3b55bc7`）

- 为 `first_step_astar()` 增加当前 Tick 的短生命周期缓存。
- A* 缓存键包含起点、目标、静态障碍签名、动态占位签名和扩展预算；动态占位或障碍变化时
  自动失效，不跨 Tick、不写入状态文件。
- 为搜索任务的视野覆盖计算增加 Tick 级缓存，减少多个任务重复遍历相同视野。
- 日志诊断增加 `astar_cache_hits` 和 `search_cache_hits`。
- 增加缓存命中、动态占位失效和空覆盖结果复用测试。

### 高人口优化阶段 3（`a26761c`）

- 为可见敌方目标建立当前 Tick 的共享攻击预留：敌方 Worker、战斗单位和 Core 默认最多分配
  1、2、3 名主动攻击者。
- 紧急自卫、低 HP 撤退和 Core 防守不受主动攻击预留限制。
- Core 满仓且人口达到外圈探索阈值时，Worker 按稳定 UUID 顺序分为两名 `CARRIER` 和两名
  `SCOUT`。
- `CARRIER` 保留资源任务；载货但暂时无法交付时在家园附近待命，不会被外圈扫描逻辑吞掉。
- `SCOUT` 承担 32～64 格外圈探索，离开外圈模式后恢复原有资源和侦察流程。

### 高人口优化阶段 2（`5c0d2b1`）

- 人口达到 40 后将野战小队分配到 12～32、32～56、56～80 三层巡逻带。
- 按人口派生守家和快速反应职责：40 人口起为 0、1 号守家，2 号快速反应。
- 有守军的敌方 Core 按每波最多 3 支完整小队分批集结和推进，每个波次独立选择 Rally Point。
- 攻击波完成接敌后才推进下一波，目标消失或战力不足时清理攻击波状态。
- 持久化攻击波索引，Agent 重启后不会无条件回到第一波。

### 高人口优化阶段 1（`4633613`）

- 增加 Tick 决策耗时、A* 调用数、扩展数和预算耗尽次数诊断。
- 增加生产状态 `READY`、`RESOURCE_WAIT`、`SATURATED` 和 `BLOCKED`。
- 当下一单位价格超过 Core 容量时停止重复提交生产，并只在状态键变化时再次记录饱和状态。
- 增加人口 87、Ranger 价格 472、Core 容量 435 的生产饱和回归测试。

### 设计与基线（`11bcf60`）

- 新增 [40+ 人口优化详细设计](high-population-optimization-design.zh-CN.md)。
- 核对 `arena-hero` 下官方文档、Python SDK 和 Agent Skill 的 Tick、容量、生产、战斗及治疗
  规则。
- 确定四个实施阶段、离线测试边界、回滚方式和部署兼容要求。

## 部署说明

- 现有部署目录可以继续使用旧目录名；服务文件的 `WorkingDirectory`、`EnvironmentFile` 和
  `ExecStart` 决定实际运行位置。
- 首次切换到 fork 时，将远端改为
  `https://github.com/miemiehoho/ArenaHero-Guide-Reforged.git`，切换到
  `进攻才是最好的防守` 分支后再执行 `git pull --ff-only`。
- 更新前应停止 systemd 服务并备份 `.env`、`.arena_core_state.json`；这两个文件不会提交到
  Git，也不会被正常更新覆盖。
- 更新后先运行离线测试和 `compileall`，再执行 `systemctl --user daemon-reload`、重启服务，
  并通过 `journalctl --user -u arena-core-agent.service` 检查启动日志。

## 验证记录

截至本日志最后一条记录，fork 离线测试共 165 个，全部通过；`compileall`、`git diff --check`
和日志查询 CLI 检查也已通过。防守爆兵阶段代码提交为 `0e65608`。
