# 更新日志

本文件记录 `ArenaHero-Guide-Reforged` 的可见行为和部署相关变更。提交作者沿用仓库现有 Git
配置；提交信息不包含邮箱或 `Co-authored-by`。

## 2026-08-15

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

截至本日志最后一条记录，fork 离线测试共 110 个，全部通过；`compileall` 和 `git diff --check`
也已通过。阶段 4 最终提交为 `3b55bc7`，远端分支与本地提交一致。
