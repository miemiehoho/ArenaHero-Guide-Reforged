# Arena Hero 静态资源搜索 Agent

这是部署在 JDCloud 上的 Arena Hero 长期控制 Agent。当前版本将资源任务静态分配给
Worker，并把这一行为作为后续优化前的可测试基线。

## 当前策略

- 自动生产最多到 19 人，避免进入人口维护费区间。
- Core 可以由用户手动迁移；迁移后会重置依赖旧家位置的侦察、撤退和巡逻目标。
- 不攻击敌方 Core，也不攻击与敌方 Core 同格的单位。
- 巡逻单位达到 3 个后采用激进交战条件，巡逻编成目标为 Vanguard:Ranger = 2:1。
- Ranger 遵循规则 v0.7：Unit 和 Core 不阻挡射击，只有地形障碍物挡住射线。

## 单位状态

Worker 每 Tick 按以下优先级选择行为：

1. 受威胁时逃离。
2. 在撤退期限内继续远离危险区。
3. 载货后返回当前 Core 并交付。
4. 采集脚下资源。
5. 前往已静态分配的资源。
6. 无资源任务时协助拦截敌方 Worker。
7. 按固定扇区侦察或等待。

Vanguard 和 Ranger 各保留一名家园守卫，其余单位负责巡逻。家园单位只在防区内
迎敌；巡逻单位根据局部兵力选择撤退、攻击、追击或继续巡逻。

## 静态分配限制

资源任务会跨 Tick 保留。Worker 已经前往资源 A 时，即使途中发现明显更近的资源 B，
也不会中断 A；B 会分配给其他空闲 Worker。这是本次基线刻意保留的行为，实时全局
重分配应在后续独立提交中实现。

## 运行与测试

服务使用项目目录内的 `.env` 读取 `ARENA_HERO_API_KEY`，该文件不会提交到 Git。

```bash
.venv/bin/python -m unittest -q
.venv/bin/python -m compileall -q arena_core_agent.py test_arena_core_agent.py
systemctl --user status arena-core-agent.service
```
