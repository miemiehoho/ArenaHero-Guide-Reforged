# ArenaHero-Guide-Reforged

这是从 [ArenaHero-nearly-perfect-guide](https://github.com/VelvetEvening/ArenaHero-nearly-perfect-guide)
fork 的 Arena Hero 长期控制 Agent，面向官方游戏规则 v0.14 继续维护。当前分支为
“进攻才是最好的防守”，版本为 v2；防守基线 `ecf5b65` 已保存为 annotated Tag
`固若金汤正式版v1`，可直接用于回滚。项目使用官方 Arena Hero Python SDK `0.2.9`。
截至 2026-08-16，40+ 人口优化四阶段、官方规则对齐 A1～A4 和中文日志体系均已完成，
离线测试共 133 个。

## 友情链接

- [LINUX DO - 新的理想型社区](https://linux.do/)

## 社区讨论

- [Arena Hero 项目讨论帖](https://linux.do/t/topic/2714382)

## 设计文档

- [实现审计报告](docs/implementation-audit.zh-CN.md)
- [官方规则对齐优化详细设计](docs/official-aligned-optimization-design.zh-CN.md)
- [40+ 人口优化建议](docs/high-population-optimization.zh-CN.md)
- [40+ 人口优化详细设计](docs/high-population-optimization-design.zh-CN.md)
- [更新日志](docs/changelog.zh-CN.md)
- [中文日志体系详细设计](docs/logging-system-design.zh-CN.md)
- [中文日志统计增强详细设计](docs/logging-statistics-design.zh-CN.md)
- [中文日志使用说明](docs/logging-usage.zh-CN.md)

## 当前策略

- 前 20 个 Unit 按基础价格自动扩张；官方 v0.14 已删除每 Tick 维护费和欠费伤害。
- 人口达到 20 后，只有 Core 资源达到容量上限时才继续生产，并按每 2 个 `VANGUARD`
  搭配 1 个 `RANGER` 的编制扩张；生产价格使用 SDK `unit_cost()` 的动态价格。
- 当下一单位价格超过当前 Core 最大容量时进入 `SATURATED`，停止重复提交必然失败的生产。
- Core 可以由用户手动迁移；迁移后会重置依赖旧家位置的侦察、撤退和巡逻目标。
- 持久化状态版本为 9；v8 会保留敌方 Core 和攻击状态并迁移到 v9，v7 及更早状态不会
  携带旧敌方 Core 或攻击状态。Core 换家后仍保留世界坐标下的敌方 Core 与彻查任务。
- 控制模式固定生产 4 名 Worker，其余人口严格按 `2 Vanguard + 1 Ranger` 编成小队。
- 20～39 人口保留 0 号守家队和 1 号快速反应队；40 人口起保留 0、1 号守家队和
  2 号快速反应队。
- 40 人口以下的野战小队保持 12～32 格巡逻；40 人口起按野战序号分配 12～32、32～56、
  56～80 三层方环。
- 有守军的敌方 Core 按每波最多 3 支完整野战小队独立集结和推进，后续波次继续巡逻等待。
- 主动攻击共享目标预留：敌方 Worker、战斗单位和 Core 默认最多分别分配 1、2、3 名攻击者；
  紧急自卫和家园防守不受该限制。
- 普通巡逻保持队形；Core 彻查任务期间解除队形，按单位真实视野分散覆盖。
- Ranger 跟随本队 Vanguard 并优先处于行军方向后侧；敌方 Vanguard 贴身时先脱离，
  不会站在队伍最前方与近战单位硬换血。
- Ranger 遵循规则 v0.8：支持横竖和精确 45 度斜线，射程为 1-3 格；Unit 和 Core 不阻挡
  射击，只有射线上的地形障碍物挡住射线。
- A* 和搜索覆盖结果使用当前 Tick 的短生命周期缓存；每 Tick 使用 10 秒内部规划预算，预算
  耗尽时停止低优先级搜索并保留已规划动作，日志记录截止状态和降级模块。

资源记忆按官方视野规则更新：Core、Worker、Vanguard、Ranger 的视野半径分别为 5、3、4、5，
并考虑障碍物的 supercover 遮挡。视野外的已探索资源会保留在本地资源池，重新进入真实视野且
确认该格为空时才会删除。资源池接纳当前 Core 切比雪夫距离 36 格以内的资源，保证 Worker 在
32 格最外方环上发现的资源不会因越过旧边界而被丢弃；Core 迁移后会立即清除范围外资源以及
相关 Worker 任务。

普通资源搜索状态下，没有资源任务的 Worker 会在半径
`12 → 19 → 26 → 32 → 26 → 19` 的方形环上顺时针扫描。四名 Worker 使用稳定扇区错开约
四分之一周长，目标间距不超过 7 格；目标被障碍占据时跳过，连续三次无法寻路时也会推进到
下一个目标。资源任务、载货返航、威胁逃跑和治疗均保持更高优先级。

Core 资源达到容量时，Worker 会暂停采集和交付，并分散到家园周围的固定待命点，等待自动生产
释放容量。已在 Core 格上的 Worker 优先移开，避免满仓载货单位长期占满生产格；Core 消耗资源后
自动恢复原任务。
若生产因 `CELL_UNIT_LIMIT` 失败，Core 会短暂停产，Core 及相邻格上的空闲单位优先向外疏散。

控制模式达到 19 人口且 Core 已满仓时，四名 Worker 按稳定 UUID 顺序派生职责：前两名为
`CARRIER`，保留已知资源任务，并让载货单位在家园附近等待容量释放；后两名为 `SCOUT`，转入
以当前 Core 为中心的 32～64 格外圈探索。扫描半径按 `32、39、46、53、60、64` 向外推进后再
向内返回；外圈目标会避开其他 Worker 的当前目标和位置，并保持至少 7 格间距，防止 3 格视野
长期重叠。目标被障碍占据时跳过，连续三次寻路失败时推进到下一个目标。人口低于 19 或 Core
不再满仓后立即清理外圈进度，并恢复原有资源分配、返航和普通侦察流程。

Worker 没有确定资源搬运任务、战斗单位没有敌方目标需要处理时，若 HP 未满会优先返回静止的
己方 Core 补血。Worker 和 Ranger 满血为 2，Vanguard 满血为 4；治疗每恢复 1 HP 消耗
1 Core 资源，并一次预留补满所需的全部费用。资源不足时，远处单位保持原有空闲任务，已在
Core 或入口相邻格的残血单位会优先离开入口，不会进入 Core 后反复提交无法支付的治疗。
单位受威胁、战斗撤退、搬运资源、守家迎敌、追击和集结攻击仍高于治疗；Core 移动期间不会
安排单位治疗。治疗预留后的剩余资源才可用于本 Tick 的自动生产。治疗完成后的满血空闲单位
会在下一 Tick 强制选择合法相邻格离开 Core，再恢复原小队或侦察逻辑；同队伤员可在退场单位
腾出位置后接替进入。载货 Worker 的返航和交付仍高于这条退场规则，不会因满血而跳过交付。

Core 的恢复动作优先于自动生产：HP 低于 5 时消耗剩余资源执行 `HEAL`；HP 已满而护盾低于
当前上限时执行 `REPAIR_SHIELD`。普通护盾上限为 5，只有官方状态明确显示 Champion Beacon
由己方 Core 或 Unit 携带时才按 10 处理。Unit 治疗已预留的资源不会被 Core 重复使用。

## 单位状态

Worker 每 Tick 按以下优先级选择行为：

1. 受威胁时逃离。
2. 在撤退期限内继续远离危险区。
3. 载货且 Core 尚有容量时返回并交付。
4. 没有资源任务且不在撤退状态、残血时，资源足够才返家补满。
5. 高库存外圈模式下，载货 `CARRIER` 在近家待命，空载 `CARRIER` 继续执行资源任务。
6. `SCOUT` 执行 32～64 格外圈扫描。
7. 没有资源任务的满仓 `CARRIER` 分散待命并腾空生产格。
8. 采集脚下资源或前往已静态分配的资源。
9. 非外圈模式沿 12～32 格方环错位顺时针扫描或等待。

守家队沿用原有防区、目标优先级和开火位规则。守家队减员后，生产会优先补齐 0 号队；
敌方战斗单位已经进入 Core 防区时，所有非守家单位跳过集结等待并立即回援。

人口低于 40 时，各支非保护完整小队按 `12 → 19 → 26 → 32 → 26 → 19` 方环独立巡逻。
人口达到 40 后，野战小队按稳定序号分布到 12～32、32～56 和 56～80 三层方环。各队稳定
错开扇区，避免新增战力长期集中在家门口或 32 格边界。任一我方单位发现敌方 Core 后，会持久
记录其位置和最后一次护卫状态。
Core 周围没有发现敌方 Vanguard/Ranger 时，若敌方 Core 距我方当前 Core 不超过 64 格，由
最近的完整野战小队直接远征，不再要求该小队已在目标 24 格内；发现护卫且距离不超过 64 格时，
完整野战小队按每波最多 3 支分批选择安全集结点，活动波完成集结后推进，后续波次继续巡逻。
超过 64 格的目标仍要求至少一支
完整非守家小队已在目标 24 格内，否则只保留目标记忆并继续巡逻。集结等待期间仍保持即时
自卫：Vanguard 会反击贴身单位和逼近正在开火的 Ranger，己方 Ranger 会优先支援正在与队友交战的敌方
Vanguard，再寻找 Core 射击位；被敌方 Vanguard 贴身时优先拉开距离。

主动攻击前会建立当前 Tick 的共享目标预留。敌方 Worker 只分配 1 名攻击者，敌方
Vanguard/Ranger 最多分配 2 名，敌方 Core 最多分配 3 名；紧急自卫、低血量撤退和 Core
防守始终优先，不会被预留上限阻断。

巡逻队看到敌方 Worker 后不再持续追击。连续轨迹会结合已知资源判断 Worker 是靠近还是离开
资源，从而推测敌方 Core 方向；单次目击则沿远离最近我方单位的方向给出保守猜测。猜测中心
距离最近我方单位最多 24 格，由最近且不在冷却的完整小队彻查其切比雪夫半径 16 的方形区域。
彻查时三名成员不保持队形，而是优先选择新增覆盖最多、相互视野重叠最少的目标。每个可通行
格子只需进入我方真实视野一次；完成后该队冷却 64 Tick。连续寻路失败会更换目标，任务超过
256 Tick 或无法继续覆盖时会退出并恢复巡逻，不会卡死。

旧 Core 坐标重新进入视野但 Core 不在时不会立即删除，而是标为待确认并按同样的 16 格区域
彻查。只有收到我方 `DESTRUCTION_PARTICIPATION/CORE` 事件，或整个区域均被视野覆盖且未发现
Core，才删除记录；彻查不完整则保留记录并在冷却后重试。任何单位重新发现 Core 都会立即刷新
位置并恢复攻击：无护卫 Core 由最近小队追杀，有护卫 Core 继续执行全队集结。

冠军信标远征机制已经取消，信标状态不再影响战斗单位编制、巡逻或攻击目标。

## 静态分配限制

资源任务会跨 Tick 保留。Worker 已经前往资源 A 时，即使途中发现明显更近的资源 B，
也不会中断 A；B 会分配给其他空闲 Worker。这是本次基线刻意保留的行为，实时全局
重分配应在后续独立提交中实现。

## 运行与测试

服务使用项目目录内的 `.env` 读取 `ARENA_HERO_API_KEY`，该文件不会提交到 Git。

```bash
.venv/bin/python -m unittest -q
.venv/bin/python -m compileall -q arena_core_agent.py arena_log.py test_arena_core_agent.py test_arena_log.py
systemctl --user status arena-core-agent.service
```

### 查询中文日志

Agent 默认将 UTF-8 中文结构化日志写入 `arena_core_agent.jsonl`，并按大小保留轮转文件；日志不
包含 API Key 或完整状态快照。查询命令不连接服务端：

```bash
python arena_log.py tail --limit 50
python arena_log.py events --type UNIT_MOVE_FAILED --reason CELL_UNIT_LIMIT
python arena_log.py stats --from-tick 10000 --to-tick 10583 --json
python arena_log.py errors --limit 100
```

需要查询已部署目录时，在该目录执行命令，或使用 `--log-dir /path/to/the/WorkingDirectory`。
`events` 保留官方 `event_type`、`reason_code` 和事件值；`stats` 汇总兵种、人口、资源/容量、
资源占用率、最新快照、事件类别、动作、耗时、生产状态和规划预算。详细字段和 PowerShell/
`jq` 示例见 [中文日志使用说明](docs/logging-usage.zh-CN.md)。

### 更新已部署实例

服务文件中的 `WorkingDirectory`、`EnvironmentFile` 和 `ExecStart` 决定实际运行目录；项目
目录名称可以继续沿用旧目录，不需要为了切换 fork 而移动文件。首次从旧仓库切换到本 fork 时，
在服务停止后执行：

```bash
systemctl --user stop arena-core-agent.service
systemctl --user cat arena-core-agent.service
cd /path/to/the/WorkingDirectory
git status --short
cp .env .env.backup
cp .arena_core_state.json .arena_core_state.json.backup 2>/dev/null || true
git remote set-url origin https://github.com/miemiehoho/ArenaHero-Guide-Reforged.git
git fetch --prune origin
git switch --track -c "进攻才是最好的防守" "origin/进攻才是最好的防守"
```

如果本地已经存在该分支，则使用：

```bash
git switch "进攻才是最好的防守"
git pull --ff-only origin "进攻才是最好的防守"
```

更新后重新安装依赖、运行离线验收，再重启服务：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -q
.venv/bin/python -m compileall -q arena_core_agent.py arena_log.py test_arena_core_agent.py test_arena_log.py
systemctl --user daemon-reload
systemctl --user restart arena-core-agent.service
systemctl --user status arena-core-agent.service
journalctl --user -u arena-core-agent.service -n 100 --no-pager
```

以后每次更新只需在运行目录执行 `git pull --ff-only origin "进攻才是最好的防守"`，验收通过
后重启服务。`.env`、`.arena_core_state.json` 和日志文件不会随 Git 更新覆盖；若 `git status`
显示有手工修改，应先处理冲突再拉取。

日常更新到最新版本可直接按以下顺序执行：

```bash
cd /path/to/the/WorkingDirectory
systemctl --user stop arena-core-agent.service
git pull --ff-only origin "进攻才是最好的防守"
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -q
.venv/bin/python -m compileall -q arena_core_agent.py arena_log.py test_arena_core_agent.py test_arena_log.py
systemctl --user restart arena-core-agent.service
journalctl --user -u arena-core-agent.service -n 100 --no-pager
```
