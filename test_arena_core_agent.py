from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from arena_hero import (
    BeaconStatus,
    ChampionBeacon,
    CoreState,
    CoreView,
    Direction,
    PlayerState,
    PlayerStatus,
    ResolutionEvent,
    TerrainView,
    UnitType,
    UnitView,
)
from arena_hero.actions import (
    DepositAction,
    HarvestAction,
    HealAction,
    MoveAction,
    RepairShieldAction,
    ShootAction,
    SpawnAction,
    SweepAction,
)
from arena_hero.turn import Turn

import arena_core_agent as agent


CORE_ID = UUID(int=1)


def controlled_unit(
    unit_id: int,
    unit_type: UnitType,
    position: tuple[int, int],
    *,
    cargo: int = 0,
    hp: int = 10,
):
    kwargs = {
        "kind": "UNIT",
        "id": UUID(int=unit_id),
        "controlled": True,
        "position": position,
        "hp": hp,
        "unit_type": unit_type,
    }
    if unit_type is UnitType.WORKER:
        kwargs["cargo"] = cargo
    return UnitView(**kwargs)


def enemy_unit(
    unit_id: int,
    unit_type: UnitType,
    position: tuple[int, int],
):
    kwargs = dict(
        kind="UNIT",
        id=UUID(int=unit_id),
        controlled=False,
        position=position,
        hp=10,
        unit_type=unit_type,
    )
    return UnitView(**kwargs)


def enemy_core(core_id: int, position: tuple[int, int]):
    return CoreView(
        kind="CORE",
        id=UUID(int=core_id),
        controlled=False,
        owner_username="enemy",
        position=position,
        hp=100,
        shield=10,
        state=CoreState.NORMAL,
    )


def make_turn(
    units,
    *,
    enemies=(),
    resources: int = 0,
    tick: int = 100,
    core_position: tuple[int, int] = (0, 0),
    beacon_position: tuple[int, int] = (100, 100),
    beacon_status: BeaconStatus | None = None,
    beacon_carrier_id: UUID | None = None,
    resource_cells=(),
    obstacle_cells=(),
    events=(),
    core_state: CoreState = CoreState.NORMAL,
    core_hp: int = 5,
    core_shield: int = 5,
):
    core_kwargs = {}
    if core_state is CoreState.MOVING:
        core_kwargs = {
            "move_direction": Direction.RIGHT,
            "move_progress": 1,
            "move_required_ticks": 4,
            "destination": (core_position[0] + 1, core_position[1]),
        }
    core = CoreView(
        kind="CORE",
        id=CORE_ID,
        controlled=True,
        owner_username="tester",
        position=core_position,
        hp=core_hp,
        shield=core_shield,
        state=core_state,
        **core_kwargs,
    )
    terrain = []
    if resource_cells:
        terrain.append(TerrainView(kind="RESOURCE", positions=resource_cells))
    if obstacle_cells:
        terrain.append(TerrainView(kind="OBSTACLE", positions=obstacle_cells))
    objects = (core, *units, *enemies, *terrain)
    state = PlayerState(
        status=PlayerStatus.ACTIVE,
        resources=resources,
        population=len(units),
        champion_beacon=ChampionBeacon(
            position=beacon_position,
            status=beacon_status,
            carrier_id=beacon_carrier_id,
        ),
        objects=objects,
        events=events,
    )
    return Turn(tick=tick, state=state, submitter=lambda plan, key: None)


def workers(count: int, *, start_id: int = 100):
    positions = (
        (5, 0),
        (5, 5),
        (0, 5),
        (-5, 5),
        (-5, 0),
        (-5, -5),
        (0, -5),
        (5, -5),
        (6, 0),
    )
    return [
        controlled_unit(start_id + index, UnitType.WORKER, positions[index])
        for index in range(count)
    ]


def population_workers(count: int, *, start_id: int = 1000):
    """为人口与动态价格测试创建互不重叠的 Worker。"""
    return [
        controlled_unit(
            start_id + index,
            UnitType.WORKER,
            (index % 8, 10 + index // 8),
        )
        for index in range(count)
    ]


def outer_scout_roster(
    *,
    worker_positions: dict[int, tuple[int, int]] | None = None,
    worker_cargo: dict[int, int] | None = None,
):
    """创建四名 Worker 和十五名战斗单位组成的 19 人控制阵容。"""
    worker_positions = worker_positions or {}
    worker_cargo = worker_cargo or {}
    worker_units = [
        controlled_unit(
            worker.id.int,
            UnitType.WORKER,
            worker_positions.get(worker.id.int, tuple(worker.position)),
            cargo=worker_cargo.get(worker.id.int, 0),
        )
        for worker in workers(4)
    ]
    combat_units = [
        *(
            controlled_unit(
                200 + index,
                UnitType.VANGUARD,
                (10 + index, 10),
            )
            for index in range(10)
        ),
        *(
            controlled_unit(
                300 + index,
                UnitType.RANGER,
                (10 + index, 12),
            )
            for index in range(5)
        ),
    ]
    return [*worker_units, *combat_units]


class AgentTestCase(unittest.TestCase):
    """为各策略分组提供隔离的状态文件和统一规划入口。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path_patch = patch.object(
            agent,
            "STATE_PATH",
            f"{self.temp_dir.name}/state.json",
        )
        self.temp_path_patch = patch.object(
            agent,
            "STATE_TEMP_PATH",
            f"{self.temp_dir.name}/state.json.tmp",
        )
        self.state_path_patch.start()
        self.temp_path_patch.start()

    def tearDown(self):
        self.temp_path_patch.stop()
        self.state_path_patch.stop()
        self.temp_dir.cleanup()

    def plan(self, turn, memory=None, mode="control"):
        memory = memory or agent.AgentMemory()
        actions, reached = agent.plan_turn(turn, memory, mode=mode)
        self.assertFalse(reached)
        return turn.plan, actions, memory


class ResourceTests(AgentTestCase):
    """资源占位、匹配和静态任务分配。"""

    def test_friendly_cell_accepts_two_but_not_three(self):
        occupancy = agent.FriendlyOccupancy([(1, 1)])
        self.assertTrue(occupancy.can_enter((1, 1)))
        occupancy.add((1, 1))
        self.assertFalse(occupancy.can_enter((1, 1)))
        self.assertIn((1, 1), occupancy.full_cells())

    def test_any_friendly_unit_confirms_missing_resource_in_vision(self):
        resource = (5, 0)
        owner = controlled_unit(100, UnitType.WORKER, (20, 0))
        ranger = controlled_unit(200, UnitType.RANGER, (0, 0))
        memory = agent.AgentMemory(
            known_resources={resource},
            worker_resource_target={owner.id: resource},
        )

        self.plan(make_turn([owner, ranger]), memory)

        self.assertNotIn(resource, memory.known_resources)
        self.assertNotIn(owner.id, memory.worker_resource_target)

    def test_resource_outside_all_friendly_vision_is_preserved(self):
        resource = (6, 0)
        ranger = controlled_unit(200, UnitType.RANGER, (0, 0))
        memory = agent.AgentMemory(known_resources={resource})

        self.plan(make_turn([ranger]), memory)

        self.assertIn(resource, memory.known_resources)

    def test_worker_and_vanguard_use_their_own_vision_radius(self):
        worker_resource = (4, 0)
        worker = controlled_unit(100, UnitType.WORKER, (0, 0))
        worker_memory = agent.AgentMemory(known_resources={worker_resource})
        self.plan(
            make_turn([worker], core_position=(20, 0)),
            worker_memory,
        )
        self.assertIn(worker_resource, worker_memory.known_resources)

        vanguard_resource = (5, 0)
        vanguard = controlled_unit(200, UnitType.VANGUARD, (0, 0))
        vanguard_memory = agent.AgentMemory(known_resources={vanguard_resource})
        self.plan(
            make_turn([vanguard], core_position=(20, 0)),
            vanguard_memory,
        )
        self.assertIn(vanguard_resource, vanguard_memory.known_resources)

    def test_ranger_and_core_vision_can_confirm_missing_resource(self):
        ranger_resource = (5, 0)
        ranger = controlled_unit(200, UnitType.RANGER, (0, 0))
        ranger_memory = agent.AgentMemory(known_resources={ranger_resource})
        self.plan(
            make_turn([ranger], core_position=(20, 0)),
            ranger_memory,
        )
        self.assertNotIn(ranger_resource, ranger_memory.known_resources)

        core_resource = (5, 0)
        core_memory = agent.AgentMemory(known_resources={core_resource})
        self.plan(
            make_turn([], core_position=(0, 0)),
            core_memory,
        )
        self.assertNotIn(core_resource, core_memory.known_resources)

    def test_obstacle_blocks_resource_disappearance_confirmation(self):
        resource = (2, 2)
        ranger = controlled_unit(200, UnitType.RANGER, (0, 0))
        memory = agent.AgentMemory(
            known_resources={resource},
            known_obstacles={(1, 0)},
        )

        self.plan(make_turn([ranger], core_position=(20, 0)), memory)

        self.assertIn(resource, memory.known_resources)

    def test_core_visibility_marks_stale_enemy_core_for_verification(self):
        enemy_id = UUID(int=400)
        memory = agent.AgentMemory(
            known_enemy_cores={enemy_id: ((5, 0), 99)},
            assault_target_id=enemy_id,
            assault_target_kind="CORE",
            assault_target_position=(5, 0),
            assault_guarded=True,
            assault_gathering=True,
        )

        self.plan(make_turn([], core_position=(0, 0)), memory)

        self.assertIn(enemy_id, memory.known_enemy_cores)
        self.assertIn(enemy_id, memory.enemy_core_missing)
        self.assertIsNone(memory.assault_target_id)
        self.assertFalse(memory.assault_guarded)
        self.assertFalse(memory.assault_gathering)

    def test_v7_state_migration_drops_enemy_core_and_assault_state(self):
        enemy_id = UUID(int=400)
        state = {
            "version": 7,
            "known_resources": [[3, 4]],
            "known_obstacles": [[5, 6]],
            "known_enemy_cores": {
                str(enemy_id): {"position": [79, 114], "tick": 900},
            },
            "assault_target_id": str(enemy_id),
            "assault_target_kind": "CORE",
            "assault_target_position": [79, 114],
            "assault_target_last_seen_tick": 900,
            "assault_guarded": True,
            "assault_gathering": True,
            "assault_rally_position": [20, 20],
            "squad_assignments": {str(UUID(int=201)): 1},
        }

        memory = agent.AgentMemory.restore(state)

        self.assertEqual(memory.known_resources, {(3, 4)})
        self.assertEqual(memory.known_obstacles, {(5, 6)})
        self.assertEqual(memory.squad_assignments, {UUID(int=201): 1})
        self.assertEqual(memory.known_enemy_cores, {})
        self.assertIsNone(memory.assault_target_id)
        self.assertIsNone(memory.assault_target_kind)
        self.assertIsNone(memory.assault_target_position)
        self.assertFalse(memory.assault_guarded)
        self.assertFalse(memory.assault_gathering)
        self.assertIsNone(memory.assault_rally_position)

    def test_core_relocation_preserves_world_enemy_core_and_assault_state(self):
        enemy_id = UUID(int=400)
        memory = agent.AgentMemory(
            known_enemy_cores={enemy_id: ((79, 114), 900)},
            assault_target_id=enemy_id,
            assault_target_kind="CORE",
            assault_target_position=(79, 114),
            assault_guarded=True,
            assault_gathering=True,
            known_resources={(3, 4)},
            known_obstacles={(5, 6)},
            squad_assignments={UUID(int=201): 1},
        )
        memory.sync_core_position((0, 0))
        memory.sync_core_position((10, 10))

        self.assertEqual(memory.known_enemy_cores, {enemy_id: ((79, 114), 900)})
        self.assertEqual(memory.assault_target_id, enemy_id)
        self.assertTrue(memory.assault_guarded)
        self.assertTrue(memory.assault_gathering)
        self.assertEqual(memory.known_resources, {(3, 4)})
        self.assertEqual(memory.known_obstacles, {(5, 6)})
        self.assertEqual(memory.squad_assignments, {UUID(int=201): 1})


class ProductionTests(AgentTestCase):
    """基础生产顺序、满仓扩编和动态价格。"""

    @staticmethod
    def mature_roster():
        """四名 Worker、十一名 Vanguard、五名 Ranger 的 20 人阵容。"""
        return [
            *workers(4),
            *(
                controlled_unit(
                    200 + index,
                    UnitType.VANGUARD,
                    (10 + index, 10),
                )
                for index in range(11)
            ),
            *(
                controlled_unit(
                    300 + index,
                    UnitType.RANGER,
                    (10 + index, 12),
                )
                for index in range(5)
            ),
        ]

    @staticmethod
    def saturation_roster():
        """87 人口、下一单位为 Ranger 的高人口控制阵容。"""
        return [
            *workers(4),
            *(
                controlled_unit(
                    2000 + index,
                    UnitType.VANGUARD,
                    (10 + index % 20, 10 + index // 20),
                )
                for index in range(56)
            ),
            *(
                controlled_unit(
                    3000 + index,
                    UnitType.RANGER,
                    (30 + index % 20, 20 + index // 20),
                )
                for index in range(27)
            ),
        ]

    def test_plan_records_tick_and_astar_metrics(self):
        memory = agent.AgentMemory()

        self.plan(make_turn(workers(4), resources=10), memory)

        metrics = memory.last_plan_metrics
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.tick, 100)
        self.assertGreater(metrics.astar_calls, 0)
        self.assertGreater(metrics.astar_expansions, 0)
        self.assertGreaterEqual(metrics.decision_ms, 0)

    def test_expired_deadline_keeps_finished_worker_action_and_core_decision(self):
        depositor = controlled_unit(100, UnitType.WORKER, (0, 0), cargo=1)
        idle = controlled_unit(101, UnitType.WORKER, (5, 0))
        with patch.object(agent, "PLANNING_BUDGET_SECONDS", -1.0):
            deposit_plan, actions, memory = self.plan(
                make_turn([depositor, idle], resources=9),
            )
            core_plan, _, core_memory = self.plan(
                make_turn([idle], resources=10),
            )

        self.assertIsInstance(
            deposit_plan.unit_actions[depositor.id],
            DepositAction,
        )
        self.assertTrue(memory.last_plan_metrics.deadline_exceeded)
        self.assertIn("workers", memory.last_plan_metrics.degraded_sections)
        self.assertTrue(any("deposit 1" in item for item in actions))
        self.assertIsInstance(core_plan.core_action, SpawnAction)
        self.assertTrue(core_memory.last_plan_metrics.deadline_exceeded)

    def test_astar_stops_without_throwing_after_expired_deadline(self):
        metrics = agent.PlanningMetrics(tick=100, deadline_at=0.0)
        agent._ACTIVE_PLANNING_METRICS = metrics
        try:
            destination = agent.first_step_astar(
                (0, 0),
                (20, 0),
                set(),
                set(),
            )
        finally:
            agent._ACTIVE_PLANNING_METRICS = None

        self.assertIsNone(destination)
        self.assertTrue(metrics.deadline_exceeded)
        self.assertIn("astar", metrics.degraded_sections)

    def test_population_87_marks_ranger_production_saturated_once(self):
        memory = agent.AgentMemory()
        roster = self.saturation_roster()

        first_actions = self.plan(
            make_turn(roster, resources=435, tick=100),
            memory,
        )[1]
        second_actions = self.plan(
            make_turn(roster, resources=435, tick=101),
            memory,
        )[1]

        self.assertEqual(agent.unit_cost(UnitType.RANGER, 87), 472)
        self.assertEqual(memory.production_status, "SATURATED")
        self.assertEqual(
            [action for action in first_actions if "production-status=SATURATED" in action],
            [
                "core production-status=SATURATED type=RANGER cost=472 "
                "capacity=435 reason=price-exceeds-capacity"
            ],
        )
        self.assertFalse(
            any("production-status=SATURATED" in action for action in second_actions)
        )

    def test_harvest_mode_still_stops_at_target(self):
        turn = make_turn([], resources=30)
        actions, reached = agent.plan_turn(
            turn,
            agent.AgentMemory(),
            target=30,
            mode="harvest",
        )
        self.assertTrue(reached)
        self.assertEqual(actions, [])
        self.assertEqual(turn.plan.unit_actions, {})

    def test_control_stops_workers_at_four_then_starts_frontline(self):
        plan, _, _ = self.plan(make_turn(workers(3), resources=10))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.WORKER)

        plan, _, _ = self.plan(make_turn(workers(4), resources=10))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)

    def test_control_fills_each_squad_as_two_vanguards_one_ranger(self):
        units = [
            *workers(4),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
        ]
        plan, _, _ = self.plan(make_turn(units, resources=10))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)

        units.append(controlled_unit(202, UnitType.VANGUARD, (1, 0)))
        plan, actions, _ = self.plan(make_turn(units, resources=12))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.RANGER)
        self.assertTrue(any("fill squad=0" in action for action in actions))

    def test_twentieth_unit_can_spawn_before_core_is_full(self):
        units = [
            *workers(4),
            *(controlled_unit(200 + index, UnitType.VANGUARD, (index, 5)) for index in range(10)),
            *(controlled_unit(300 + index, UnitType.RANGER, (index, 7)) for index in range(4)),
        ]
        turn = make_turn(units, resources=12)
        plan, _, _ = self.plan(turn)
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.RANGER)

    def test_population_above_twenty_waits_until_core_is_full(self):
        units = [
            *outer_scout_roster(),
            controlled_unit(999, UnitType.VANGUARD, (30, 0)),
        ]

        plan, _, _ = self.plan(make_turn(units, resources=99))
        self.assertIsNone(plan.core_action)

        plan, _, _ = self.plan(make_turn(units, resources=100))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)

    def test_mature_combat_loss_starts_worker_replenishment_before_core_is_full(self):
        memory = agent.AgentMemory()
        mature = self.mature_roster()
        self.plan(make_turn(mature, resources=0, tick=100), memory)
        damaged = [unit for unit in mature if unit.id != UUID(int=210)]

        turn = make_turn(damaged, resources=5, tick=101)
        plan, actions, memory = self.plan(turn, memory)

        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.WORKER)
        self.assertTrue(memory.replenishment_active)
        self.assertEqual(memory.population_peak, 20)
        self.assertEqual(memory.replenishment_target_population, 28)
        self.assertTrue(any("replenish Workers 5/12" in item for item in actions))
        statistics = agent.turn_statistics(turn, memory, False)
        self.assertTrue(statistics["补员激活"])
        self.assertEqual(statistics["历史峰值人口"], 20)
        self.assertEqual(statistics["补员目标人口"], 28)

    def test_replenishment_prioritizes_worker_until_twelve(self):
        units = [
            *population_workers(11),
            *(
                controlled_unit(2000 + index, UnitType.VANGUARD, (20 + index, 20))
                for index in range(10)
            ),
            *(
                controlled_unit(3000 + index, UnitType.RANGER, (20 + index, 22))
                for index in range(5)
            ),
        ]
        memory = agent.AgentMemory(
            population_peak=20,
            replenishment_target_population=28,
            replenishment_active=True,
        )
        worker_cost = agent.unit_cost(UnitType.WORKER, len(units))

        plan, _, _ = self.plan(make_turn(units, resources=worker_cost), memory)

        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.WORKER)

    def test_replenishment_restores_vanguard_after_workers_reach_twelve(self):
        units = [
            *population_workers(12),
            *(
                controlled_unit(2000 + index, UnitType.VANGUARD, (20 + index, 20))
                for index in range(10)
            ),
            *(
                controlled_unit(3000 + index, UnitType.RANGER, (20 + index, 22))
                for index in range(5)
            ),
        ]
        memory = agent.AgentMemory(
            population_peak=20,
            replenishment_target_population=28,
            replenishment_active=True,
        )
        vanguard_cost = agent.unit_cost(UnitType.VANGUARD, len(units))

        plan, _, _ = self.plan(make_turn(units, resources=vanguard_cost), memory)

        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)

    def test_replenishment_exits_then_ranger_loss_can_trigger_again(self):
        units = [
            *population_workers(12),
            *(
                controlled_unit(2000 + index, UnitType.VANGUARD, (20 + index, 20))
                for index in range(11)
            ),
            *(
                controlled_unit(3000 + index, UnitType.RANGER, (20 + index, 22))
                for index in range(5)
            ),
        ]
        memory = agent.AgentMemory(
            population_peak=20,
            replenishment_target_population=28,
            replenishment_active=True,
        )

        plan, _, memory = self.plan(make_turn(units, resources=0, tick=100), memory)
        self.assertIsNone(plan.core_action)
        self.assertFalse(memory.replenishment_active)
        self.assertEqual(memory.population_peak, 28)
        self.assertEqual(memory.replenishment_target_population, 0)

        damaged = [unit for unit in units if unit.id != UUID(int=3004)]
        ranger_cost = agent.unit_cost(UnitType.RANGER, len(damaged))
        plan, _, memory = self.plan(
            make_turn(damaged, resources=ranger_cost, tick=101),
            memory,
        )
        self.assertTrue(memory.replenishment_active)
        self.assertEqual(memory.replenishment_target_population, 28)
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.RANGER)

    def test_replenishment_waits_for_worker_resources_and_moving_core(self):
        units = [
            *population_workers(11),
            *(
                controlled_unit(2000 + index, UnitType.VANGUARD, (20 + index, 20))
                for index in range(15)
            ),
        ]
        worker_cost = agent.unit_cost(UnitType.WORKER, len(units))
        memory = agent.AgentMemory(
            population_peak=28,
            replenishment_target_population=29,
            replenishment_active=True,
        )

        plan, _, memory = self.plan(
            make_turn(units, resources=worker_cost - 1),
            memory,
        )
        self.assertIsNone(plan.core_action)
        self.assertEqual(memory.production_status, "RESOURCE_WAIT")
        self.assertEqual(
            memory.production_wait_reason,
            "worker-replenishment-insufficient-resources",
        )

        moving_memory = agent.AgentMemory(
            population_peak=28,
            replenishment_target_population=29,
            replenishment_active=True,
        )
        plan, _, moving_memory = self.plan(
            make_turn(units, resources=worker_cost, core_state=CoreState.MOVING),
            moving_memory,
        )
        self.assertIsNone(plan.core_action)
        self.assertEqual(moving_memory.production_status, "BLOCKED")

    def test_worker_replenishment_marks_dynamic_price_saturated(self):
        units = [
            *population_workers(11),
            *(
                controlled_unit(
                    5000 + index,
                    UnitType.VANGUARD,
                    (20 + index % 20, 20 + index // 20),
                )
                for index in range(94)
            ),
        ]
        memory = agent.AgentMemory(
            population_peak=106,
            replenishment_target_population=107,
            replenishment_active=True,
        )

        plan, _, memory = self.plan(make_turn(units, resources=525), memory)

        self.assertGreater(agent.unit_cost(UnitType.WORKER, 105), 525)
        self.assertIsNone(plan.core_action)
        self.assertEqual(memory.production_status, "SATURATED")
        self.assertEqual(memory.production_wait_reason, "price-exceeds-capacity")

    def test_replenishment_state_migrates_v9_and_round_trips_v10(self):
        worker_id = UUID(int=401)
        migrated = agent.AgentMemory.restore(
            {
                "version": 9,
                "worker_sectors": {str(worker_id): 11},
            }
        )
        self.assertEqual(migrated.persistent_state()["version"], 10)
        self.assertEqual(migrated.worker_sector[worker_id], 11)
        self.assertEqual(migrated.population_peak, 0)
        self.assertEqual(migrated.replenishment_target_population, 0)
        self.assertFalse(migrated.replenishment_active)

        memory = agent.AgentMemory(
            worker_sector={worker_id: 11},
            population_peak=28,
            replenishment_target_population=36,
            replenishment_active=True,
        )
        restored = agent.AgentMemory.restore(memory.persistent_state())
        self.assertEqual(restored.worker_sector[worker_id], 11)
        self.assertEqual(restored.population_peak, 28)
        self.assertEqual(restored.replenishment_target_population, 36)
        self.assertTrue(restored.replenishment_active)

    def test_population_above_twenty_keeps_two_vanguards_one_ranger_ratio(self):
        units = [
            *outer_scout_roster(),
            controlled_unit(999, UnitType.VANGUARD, (30, 0)),
            controlled_unit(1000, UnitType.VANGUARD, (31, 0)),
        ]

        plan, _, _ = self.plan(make_turn(units, resources=105))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.RANGER)

    def test_full_core_does_not_spawn_when_dynamic_price_is_unaffordable(self):
        units = population_workers(100)

        plan, _, _ = self.plan(make_turn(units, resources=500))

        self.assertEqual(agent.unit_cost(UnitType.VANGUARD, 100), 865)
        self.assertIsNone(plan.core_action)


class DefensePressureTests(AgentTestCase):
    """Core 近家危险分级和短期敌情记忆。"""

    @staticmethod
    def pressure(
        *,
        enemies=(),
        defenders=(),
        known_threats=None,
        tick=100,
        core_hp=5,
        core_shield=5,
    ):
        turn = make_turn(
            [*defenders],
            enemies=enemies,
            tick=tick,
            core_hp=core_hp,
            core_shield=core_shield,
        )
        return agent.defense_pressure_for(
            turn.core,
            turn.visible_enemies,
            [
                unit
                for unit in turn.vanguards + turn.rangers
            ],
            known_threats or {},
            tick,
        )

    def test_no_nearby_enemy_is_none(self):
        pressure = self.pressure(
            enemies=[enemy_unit(401, UnitType.VANGUARD, (20, 0))],
            defenders=[controlled_unit(501, UnitType.VANGUARD, (2, 0))],
        )
        self.assertEqual(pressure.level, "NONE")
        self.assertEqual(pressure.enemy_count, 0)

    def test_one_enemy_without_local_advantage_is_pressured_not_burst(self):
        pressure = self.pressure(
            enemies=[enemy_unit(401, UnitType.RANGER, (8, 0))],
            defenders=[controlled_unit(501, UnitType.VANGUARD, (2, 0))],
        )
        self.assertEqual(pressure.level, "PRESSURED")
        self.assertFalse(pressure.burst_required)

    def test_many_enemies_against_thin_defense_is_critical(self):
        pressure = self.pressure(
            enemies=[
                enemy_unit(401, UnitType.VANGUARD, (8, 0)),
                enemy_unit(402, UnitType.VANGUARD, (8, 1)),
                enemy_unit(403, UnitType.RANGER, (8, 2)),
            ],
            defenders=[controlled_unit(501, UnitType.VANGUARD, (2, 0))],
        )
        self.assertEqual(pressure.level, "CRITICAL")
        self.assertEqual(pressure.enemy_count, 3)
        self.assertEqual(pressure.defender_count, 1)

    def test_damaged_core_with_two_enemies_is_critical(self):
        pressure = self.pressure(
            enemies=[
                enemy_unit(401, UnitType.VANGUARD, (8, 0)),
                enemy_unit(402, UnitType.RANGER, (8, 1)),
            ],
            defenders=[
                controlled_unit(501, UnitType.VANGUARD, (2, 0)),
                controlled_unit(502, UnitType.RANGER, (2, 1)),
            ],
            core_hp=4,
        )
        self.assertEqual(pressure.level, "CRITICAL")
        self.assertTrue(pressure.core_damaged)

    def test_visible_and_remembered_threats_deduplicate_and_expire(self):
        visible = enemy_unit(401, UnitType.VANGUARD, (8, 0))
        pressure = self.pressure(
            enemies=[visible],
            known_threats={
                visible.id: ((8, 0), 100),
                UUID(int=402): ((7, 1), 100),
            },
        )
        self.assertEqual(pressure.enemy_count, 2)

        expired = self.pressure(
            known_threats={UUID(int=402): ((7, 1), 100)},
            tick=107,
        )
        self.assertEqual(expired.level, "NONE")
        self.assertEqual(expired.enemy_count, 0)

    def test_plan_statistics_include_defense_pressure(self):
        turn = make_turn(
            [controlled_unit(501, UnitType.VANGUARD, (2, 0))],
            enemies=[
                enemy_unit(401, UnitType.VANGUARD, (8, 0)),
                enemy_unit(402, UnitType.VANGUARD, (8, 1)),
                enemy_unit(403, UnitType.RANGER, (8, 2)),
            ],
        )
        memory = agent.AgentMemory()
        self.plan(turn, memory)
        statistics = agent.turn_statistics(turn, memory, False)
        self.assertEqual(statistics["防御危险等级"], "CRITICAL")
        self.assertEqual(statistics["近家敌方战斗单位数"], 3)
        self.assertEqual(statistics["近家防守单位数"], 1)


class BurstPlannerTests(unittest.TestCase):
    """动态价格、容量损失和 Worker 自毁候选的纯计算。"""

    def test_n40_same_yield_keeps_all_workers(self):
        plan = agent.choose_defense_burst_plan(
            population=40,
            resources=200,
            worker_count=16,
            vanguard_count=16,
            ranger_count=8,
        )
        self.assertEqual(plan.sacrifice_count, 0)
        self.assertEqual(plan.spawn_count, 5)

    def test_n60_price_tier_makes_three_worker_sacrifices_profitable(self):
        plan = agent.choose_defense_burst_plan(
            population=60,
            resources=300,
            worker_count=15,
            vanguard_count=30,
            ranger_count=15,
        )
        self.assertEqual(plan.sacrifice_count, 3)
        self.assertEqual(plan.spawn_count, 3)
        self.assertEqual(plan.start_population, 57)

    def test_n80_chooses_fewer_of_two_equal_yield_sacrifices(self):
        plan = agent.choose_defense_burst_plan(
            population=80,
            resources=400,
            worker_count=20,
            vanguard_count=40,
            ranger_count=20,
        )
        self.assertEqual(plan.spawn_count, 2)
        self.assertEqual(plan.sacrifice_count, 7)

    def test_capacity_overflow_is_removed_before_production(self):
        plan = agent.simulate_burst_plan(
            population=40,
            resources=200,
            vanguard_count=16,
            ranger_count=8,
            sacrifice_count=4,
        )
        self.assertEqual(plan.retained_resources, 180)
        self.assertEqual(plan.spawn_count, 5)

    def test_twelve_workers_or_fewer_never_enter_sacrifice_candidates(self):
        plan = agent.choose_defense_burst_plan(
            population=60,
            resources=300,
            worker_count=12,
            vanguard_count=32,
            ranger_count=16,
        )
        self.assertEqual(plan.sacrifice_count, 0)

    def test_dynamic_price_and_capacity_can_stop_all_candidates(self):
        plan = agent.choose_defense_burst_plan(
            population=100,
            resources=500,
            worker_count=20,
            vanguard_count=53,
            ranger_count=27,
        )
        self.assertEqual(plan.spawn_count, 0)
        self.assertEqual(plan.sacrifice_count, 0)

    def test_sequence_follows_two_vanguard_one_ranger(self):
        plan = agent.simulate_burst_plan(
            population=20,
            resources=42,
            vanguard_count=0,
            ranger_count=0,
        )
        self.assertEqual(
            plan.spawn_types,
            (UnitType.VANGUARD, UnitType.VANGUARD, UnitType.RANGER),
        )


class WorkerTests(AgentTestCase):
    """Worker 状态、Core 迁移和资源任务生命周期。"""

    def test_core_relocation_rebases_cached_exploration_goals(self):
        units = [
            controlled_unit(100, UnitType.WORKER, (8, 0)),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (1, 0)),
            controlled_unit(203, UnitType.VANGUARD, (20, 0)),
            controlled_unit(204, UnitType.VANGUARD, (20, 1)),
            controlled_unit(301, UnitType.RANGER, (1, 1)),
            controlled_unit(302, UnitType.RANGER, (20, 2)),
        ]
        memory = agent.AgentMemory()
        self.plan(make_turn(units, tick=100, core_position=(0, 0)), memory)
        old_worker_goal = memory.scout_goal[UUID(int=100)]
        old_roam_goal = memory.squad_patrol_goal[1]

        self.plan(make_turn(units, tick=101, core_position=(10, 0)), memory)
        new_worker_goal = memory.scout_goal[UUID(int=100)]
        new_roam_goal = memory.squad_patrol_goal[1]

        self.assertNotEqual(new_worker_goal, old_worker_goal)
        self.assertNotEqual(new_roam_goal, old_roam_goal)
        self.assertEqual(memory.home_vanguard_id, UUID(int=201))
        self.assertEqual(memory.home_ranger_id, UUID(int=301))

    def test_resource_matching_scales_to_manual_worker_population(self):
        units = population_workers(19)
        resources = {(index, 20) for index in range(11)}
        assignments = agent.minimum_cost_resource_matching(units, resources)
        self.assertEqual(len(assignments), len(resources))
        self.assertEqual(len({worker_id for worker_id, _ in assignments}), len(resources))
        self.assertEqual({resource for _, resource in assignments}, resources)

    def test_en_route_worker_keeps_static_claim_when_closer_resource_appears(self):
        first = controlled_unit(100, UnitType.WORKER, (2, 0))
        second = controlled_unit(101, UnitType.WORKER, (20, 0))
        original_resource = (10, 0)
        closer_new_resource = (3, 0)
        memory = agent.AgentMemory(
            known_resources={original_resource, closer_new_resource},
            worker_resource_target={first.id: original_resource},
        )

        assignments = memory.assign_resource_targets(
            [first, second],
            memory.known_resources,
            {original_resource, closer_new_resource},
            tick=100,
            path_obstacles=set(),
            blocked_resource_cells=set(),
        )

        self.assertEqual(assignments[first.id], original_resource)
        self.assertEqual(assignments[second.id], closer_new_resource)

    def test_dead_unit_state_is_pruned_without_reassigning_live_worker(self):
        live_worker = controlled_unit(100, UnitType.WORKER, (2, 0))
        live_vanguard = controlled_unit(200, UnitType.VANGUARD, (0, 1))
        dead_worker_id = UUID(int=999)
        dead_vanguard_id = UUID(int=998)
        memory = agent.AgentMemory(
            worker_sector={live_worker.id: 3, dead_worker_id: 7},
            scout_ring_index={live_worker.id: 1, dead_worker_id: 2},
            worker_resource_target={live_worker.id: (10, 0), dead_worker_id: (20, 0)},
            retreat_until={dead_worker_id: 200},
            scout_goal={dead_worker_id: (30, 0)},
            worker_harvests={dead_worker_id: 4},
            expanded_low_yield={dead_worker_id},
            vanguard_patrol_phase={live_vanguard.id: 2, dead_vanguard_id: 6},
            roam_goal={dead_vanguard_id: (40, 0)},
        )

        changed = memory.prune_unit_state(
            [live_worker],
            [live_vanguard],
            [],
        )

        self.assertTrue(changed)
        self.assertEqual(memory.worker_sector, {live_worker.id: 3})
        self.assertEqual(
            memory.worker_resource_target,
            {live_worker.id: (10, 0)},
        )
        self.assertNotIn(dead_worker_id, memory.retreat_until)
        self.assertEqual(memory.scout_ring_index, {live_worker.id: 1})
        self.assertNotIn(dead_worker_id, memory.scout_goal)
        self.assertNotIn(dead_worker_id, memory.worker_harvests)
        self.assertNotIn(dead_worker_id, memory.expanded_low_yield)
        self.assertEqual(memory.vanguard_patrol_phase, {live_vanguard.id: 2})
        self.assertNotIn(dead_vanguard_id, memory.roam_goal)
        persisted_sectors = memory.persistent_state()["worker_sectors"]
        self.assertEqual(persisted_sectors, {str(live_worker.id): 3})

    def test_return_route_does_not_reverse_when_enemy_worker_leaves_view(self):
        worker_id = UUID(int=100)
        memory = agent.AgentMemory()
        first_worker = controlled_unit(
            100,
            UnitType.WORKER,
            (0, 0),
            cargo=1,
        )
        stationary_enemy = enemy_unit(401, UnitType.WORKER, (1, 0))

        first_plan, _, _ = self.plan(
            make_turn(
                [first_worker],
                enemies=[stationary_enemy],
                tick=100,
                core_position=(2, 0),
            ),
            memory,
        )
        self.assertEqual(first_plan.unit_actions[worker_id].direction.value, "UP")

        second_worker = controlled_unit(
            100,
            UnitType.WORKER,
            (0, -1),
            cargo=1,
        )
        second_plan, _, _ = self.plan(
            make_turn(
                [second_worker],
                tick=101,
                core_position=(2, 0),
            ),
            memory,
        )

        self.assertEqual(second_plan.unit_actions[worker_id].direction.value, "RIGHT")
        self.assertIn(stationary_enemy.id, memory.enemy_worker_tracks)

    def test_full_capacity_workers_clear_core_and_do_not_block_spawn(self):
        first = controlled_unit(100, UnitType.WORKER, (0, 0), cargo=1)
        second = controlled_unit(101, UnitType.WORKER, (0, 0), cargo=1)

        plan, actions, _ = self.plan(
            make_turn([first, second], resources=10),
        )

        first_action = plan.unit_actions[first.id]
        second_action = plan.unit_actions[second.id]
        self.assertIsInstance(first_action, MoveAction)
        self.assertIsInstance(second_action, MoveAction)
        self.assertNotEqual(first_action.direction, second_action.direction)
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertTrue(any("capacity-stage" in action for action in actions))

    def test_full_capacity_worker_still_flees_nearby_enemy(self):
        worker = controlled_unit(100, UnitType.WORKER, (0, 0), cargo=1)
        enemy = enemy_unit(400, UnitType.VANGUARD, (1, 0))

        plan, actions, _ = self.plan(
            make_turn(
                [worker],
                enemies=[enemy],
                resources=5,
                core_position=(10, 10),
            ),
        )

        self.assertIsInstance(plan.unit_actions[worker.id], MoveAction)
        self.assertTrue(any("flee" in action for action in actions))
        self.assertFalse(any("capacity-" in action for action in actions))

    def test_worker_deposits_again_after_capacity_is_available(self):
        worker = controlled_unit(100, UnitType.WORKER, (0, 0), cargo=1)

        plan, _, _ = self.plan(
            make_turn([worker], resources=4),
        )

        self.assertIsInstance(plan.unit_actions[worker.id], DepositAction)

    def test_worker_deposit_keeps_core_cell_full_and_blocks_spawn(self):
        worker = controlled_unit(100, UnitType.WORKER, (0, 0), cargo=1)

        plan, _, _ = self.plan(make_turn([worker], resources=9))

        self.assertIsInstance(plan.unit_actions[worker.id], DepositAction)
        self.assertIsNone(plan.core_action)

    def test_worker_leaving_core_allows_same_tick_spawn(self):
        worker = controlled_unit(100, UnitType.WORKER, (0, 0), cargo=1)

        plan, _, _ = self.plan(make_turn([worker], resources=10))

        self.assertIsInstance(plan.unit_actions[worker.id], MoveAction)
        self.assertIsInstance(plan.core_action, SpawnAction)

    def test_second_injured_worker_does_not_enter_occupied_core(self):
        depositor = controlled_unit(100, UnitType.WORKER, (0, 0), cargo=1)
        injured = controlled_unit(101, UnitType.WORKER, (1, 0), hp=1)

        plan, _, _ = self.plan(make_turn([depositor, injured], resources=1))

        self.assertIsInstance(plan.unit_actions[depositor.id], DepositAction)
        injured_action = plan.unit_actions[injured.id]
        self.assertFalse(
            isinstance(injured_action, MoveAction)
            and injured_action.direction is Direction.LEFT
        )

    def test_moving_core_stages_loaded_workers_without_deposit(self):
        for position in ((0, 0), (5, 0)):
            with self.subTest(position=position):
                worker = controlled_unit(
                    100,
                    UnitType.WORKER,
                    position,
                    cargo=1,
                )

                plan, actions, _ = self.plan(
                    make_turn(
                        [worker],
                        resources=0,
                        core_state=CoreState.MOVING,
                    )
                )

                self.assertNotIsInstance(
                    plan.unit_actions[worker.id],
                    DepositAction,
                )
                self.assertTrue(
                    any("moving-core-" in action for action in actions)
                )

    def test_move_failure_uses_recorded_destination_not_event_position(self):
        worker_id = UUID(int=100)
        memory = agent.AgentMemory(
            pending_move_tick=100,
            pending_move_destinations={worker_id: (1, 0)},
        )
        event = ResolutionEvent(
            event_id=UUID(int=900),
            tick=100,
            event_type="UNIT_MOVE_FAILED",
            reason_code="MOVE_DESTINATION_OCCUPIED",
            actor_id=worker_id,
            position=(0, 0),
        )

        memory.observe_dynamic_blocks([event], 101)

        self.assertIn((1, 0), memory.temporary_blocked_cells)
        self.assertNotIn((0, 0), memory.temporary_blocked_cells)
        self.assertEqual(memory.pending_move_tick, 0)
        self.assertFalse(memory.pending_move_destinations)

    def test_move_failure_without_matching_plan_does_not_guess_destination(self):
        worker_id = UUID(int=100)
        memory = agent.AgentMemory(
            pending_move_tick=99,
            pending_move_destinations={worker_id: (1, 0)},
        )
        event = ResolutionEvent(
            event_id=UUID(int=901),
            tick=100,
            event_type="UNIT_MOVE_FAILED",
            reason_code="MOVE_DESTINATION_OCCUPIED",
            actor_id=worker_id,
            position=(0, 0),
        )

        memory.observe_dynamic_blocks([event], 101)

        self.assertFalse(memory.temporary_blocked_cells)

    def test_plan_records_move_destination_for_next_tick(self):
        worker = controlled_unit(100, UnitType.WORKER, (0, 0), cargo=1)
        memory = agent.AgentMemory()

        plan, _, memory = self.plan(
            make_turn([worker], resources=10, tick=100),
            memory,
        )

        action = plan.unit_actions[worker.id]
        self.assertIsInstance(action, MoveAction)
        delta = next(
            step
            for direction, step in agent.DIRECTION_STEPS
            if direction is action.direction
        )
        self.assertEqual(memory.pending_move_tick, 100)
        self.assertEqual(
            memory.pending_move_destinations[worker.id],
            agent.add((0, 0), delta),
        )


class HealingTests(AgentTestCase):
    """空闲残血单位返家、支付治疗和 Core 入口防堵。"""

    def test_idle_worker_returns_to_stationary_core_when_full_heal_is_affordable(self):
        worker = controlled_unit(100, UnitType.WORKER, (1, 0), hp=1)

        plan, actions, _ = self.plan(make_turn([worker], resources=1))

        action = plan.unit_actions[worker.id]
        self.assertIsInstance(action, MoveAction)
        self.assertEqual(action.direction.value, "LEFT")
        self.assertTrue(any("worker-heal-return amount=1" in item for item in actions))

    def test_idle_worker_on_core_queues_full_heal(self):
        worker = controlled_unit(100, UnitType.WORKER, (0, 0), hp=1)

        plan, actions, _ = self.plan(make_turn([worker], resources=1))

        self.assertIsInstance(plan.unit_actions[worker.id], HealAction)
        self.assertTrue(any("worker-heal amount=1" in item for item in actions))

    def test_full_idle_worker_exits_core_before_resuming_scouting(self):
        worker = controlled_unit(100, UnitType.WORKER, (0, 0), hp=2)

        plan, actions, _ = self.plan(make_turn([worker], resources=0))

        self.assertIsInstance(plan.unit_actions[worker.id], MoveAction)
        self.assertTrue(any("worker-heal-exit" in item for item in actions))

    def test_moving_core_does_not_start_unit_healing(self):
        worker = controlled_unit(100, UnitType.WORKER, (0, 0), hp=1)

        plan, actions, _ = self.plan(
            make_turn(
                [worker],
                resources=1,
                core_state=CoreState.MOVING,
            ),
        )

        self.assertNotIsInstance(plan.unit_actions[worker.id], HealAction)
        self.assertFalse(any("worker-heal" in item for item in actions))

    def test_worker_with_resource_assignment_does_not_abandon_it_to_heal(self):
        worker = controlled_unit(100, UnitType.WORKER, (1, 0), hp=1)

        plan, actions, memory = self.plan(
            make_turn(
                [worker],
                resources=1,
                resource_cells=[(2, 0)],
            ),
        )

        action = plan.unit_actions[worker.id]
        self.assertIsInstance(action, MoveAction)
        self.assertEqual(action.direction.value, "RIGHT")
        self.assertEqual(memory.worker_resource_target[worker.id], (2, 0))
        self.assertFalse(any("worker-heal" in item for item in actions))

    def test_loaded_worker_deposits_before_healing(self):
        worker = controlled_unit(
            100,
            UnitType.WORKER,
            (0, 0),
            cargo=1,
            hp=1,
        )

        plan, actions, _ = self.plan(make_turn([worker], resources=1))

        self.assertIsInstance(plan.unit_actions[worker.id], DepositAction)
        self.assertTrue(any("deposit 1" in item for item in actions))
        self.assertFalse(any("worker-heal" in item for item in actions))

    def test_loaded_full_worker_deposits_before_core_egress(self):
        worker = controlled_unit(
            100,
            UnitType.WORKER,
            (0, 0),
            cargo=1,
            hp=2,
        )

        plan, actions, _ = self.plan(make_turn([worker], resources=1))

        self.assertIsInstance(plan.unit_actions[worker.id], DepositAction)
        self.assertTrue(any("deposit 1" in item for item in actions))
        self.assertFalse(any("worker-heal-exit" in item for item in actions))

    def test_insufficient_resources_keep_damaged_worker_out_of_core(self):
        worker = controlled_unit(100, UnitType.WORKER, (1, 0), hp=1)

        plan, actions, _ = self.plan(make_turn([worker], resources=0))

        action = plan.unit_actions[worker.id]
        self.assertNotIsInstance(action, HealAction)
        if isinstance(action, MoveAction):
            self.assertNotEqual(action.direction.value, "LEFT")
        self.assertTrue(any("worker-heal-defer" in item for item in actions))

    def test_one_resource_allows_only_one_damaged_worker_into_core(self):
        first = controlled_unit(100, UnitType.WORKER, (1, 0), hp=1)
        second = controlled_unit(101, UnitType.WORKER, (-1, 0), hp=1)

        plan, _, _ = self.plan(make_turn([first, second], resources=1))

        first_action = plan.unit_actions[first.id]
        second_action = plan.unit_actions[second.id]
        self.assertIsInstance(first_action, MoveAction)
        self.assertEqual(first_action.direction.value, "LEFT")
        self.assertFalse(
            isinstance(second_action, MoveAction)
            and second_action.direction.value == "RIGHT"
        )

    def test_idle_home_vanguard_heals_but_enemy_contact_still_has_priority(self):
        vanguard = controlled_unit(201, UnitType.VANGUARD, (0, 0), hp=3)

        heal_plan, heal_actions, _ = self.plan(
            make_turn([vanguard], resources=1),
        )
        self.assertIsInstance(heal_plan.unit_actions[vanguard.id], HealAction)
        self.assertTrue(any("guard-heal amount=1" in item for item in heal_actions))

        enemy = enemy_unit(400, UnitType.VANGUARD, (1, 0))
        combat_plan, combat_actions, _ = self.plan(
            make_turn([vanguard], enemies=[enemy], resources=1),
        )
        self.assertIsInstance(combat_plan.unit_actions[vanguard.id], SweepAction)
        self.assertFalse(any("guard-heal" in item for item in combat_actions))

    def test_healed_home_vanguard_exits_core_on_next_tick(self):
        memory = agent.AgentMemory()
        injured = controlled_unit(201, UnitType.VANGUARD, (0, 0), hp=3)
        first_plan, _, memory = self.plan(
            make_turn([injured], resources=1, tick=100),
            memory,
        )
        self.assertIsInstance(first_plan.unit_actions[injured.id], HealAction)

        healed = controlled_unit(201, UnitType.VANGUARD, (0, 0), hp=4)
        second_plan, actions, _ = self.plan(
            make_turn([healed], resources=0, tick=101),
            memory,
        )

        self.assertIsInstance(second_plan.unit_actions[healed.id], MoveAction)
        self.assertTrue(any("guard-heal-exit" in item for item in actions))

    def test_healing_reservation_prevents_unaffordable_core_spawn(self):
        vanguard = controlled_unit(201, UnitType.VANGUARD, (0, 0), hp=1)
        units = [*workers(4), vanguard]

        plan, _, _ = self.plan(make_turn(units, resources=10))

        self.assertIsInstance(plan.unit_actions[vanguard.id], HealAction)
        self.assertIsNone(plan.core_action)


class CoreSurvivalTests(AgentTestCase):
    """Core 生存动作优先于生产，并遵守资源与 Beacon 上限。"""

    def test_injured_core_heals_before_production(self):
        plan, actions, _ = self.plan(
            make_turn([], resources=10, core_hp=4, core_shield=5),
        )

        self.assertIsInstance(plan.core_action, HealAction)
        self.assertTrue(any("core heal" in item for item in actions))

    def test_core_repairs_shield_after_hp_is_full(self):
        plan, actions, _ = self.plan(
            make_turn([], resources=10, core_hp=5, core_shield=4),
        )

        self.assertIsInstance(plan.core_action, RepairShieldAction)
        self.assertTrue(any("core repair-shield" in item for item in actions))

    def test_unit_healing_reservation_leaves_no_core_recovery_budget(self):
        worker = controlled_unit(100, UnitType.WORKER, (2, 0), hp=1)
        plan, _, _ = self.plan(
            make_turn([worker], resources=1, core_hp=4, core_shield=5),
        )

        self.assertIsInstance(plan.unit_actions[worker.id], MoveAction)
        self.assertIsNone(plan.core_action)

    def test_moving_core_does_not_heal_or_repair(self):
        plan, _, _ = self.plan(
            make_turn(
                [],
                resources=10,
                core_hp=4,
                core_shield=4,
                core_state=CoreState.MOVING,
            ),
        )

        self.assertIsNone(plan.core_action)

    def test_friendly_beacon_raises_core_shield_cap_to_ten(self):
        plan, actions, _ = self.plan(
            make_turn(
                [],
                resources=10,
                core_hp=5,
                core_shield=5,
                beacon_status=BeaconStatus.CARRIED,
                beacon_carrier_id=CORE_ID,
            ),
        )

        self.assertIsInstance(plan.core_action, RepairShieldAction)
        self.assertTrue(any("shield=5/10" in item for item in actions))

    def test_unknown_or_enemy_beacon_carrier_keeps_normal_shield_cap(self):
        for carrier_id in (UUID(int=9999), None):
            with self.subTest(carrier_id=carrier_id):
                plan, _, _ = self.plan(
                    make_turn(
                        [],
                        resources=10,
                        core_hp=5,
                        core_shield=5,
                        beacon_status=BeaconStatus.CARRIED if carrier_id else None,
                        beacon_carrier_id=carrier_id,
                    ),
                )

                self.assertIsInstance(plan.core_action, SpawnAction)


class ResourceScoutTests(AgentTestCase):
    """普通资源搜索时四名 Worker 的 12-32 格方环扫描。"""

    def test_four_workers_start_at_unique_quarter_ring_offsets_below_threshold(self):
        units = outer_scout_roster()

        _, actions, memory = self.plan(make_turn(units, resources=79))

        worker_ids = [UUID(int=value) for value in range(100, 104)]
        route = agent.square_ring_waypoints(
            (0, 0),
            12,
            agent.RESOURCE_SCOUT_WAYPOINT_STEP,
        )
        expected = {
            worker_id: route[(sector * len(route)) // len(worker_ids)]
            for worker_id, sector in zip(worker_ids, range(len(worker_ids)))
        }
        self.assertFalse(memory.outer_scout_active)
        self.assertFalse(any("outer-scout " in action for action in actions))
        self.assertEqual(
            {worker_id: memory.scout_goal[worker_id] for worker_id in worker_ids},
            expected,
        )
        self.assertEqual(len(set(expected.values())), 4)

    def test_twelve_workers_receive_unique_sectors_and_scout_goals(self):
        roster = population_workers(12)
        memory = agent.AgentMemory()
        memory.sync_worker_sectors(roster)

        goals = [
            memory.goal_for(
                worker.id,
                index,
                len(roster),
                (0, 0),
                tuple(worker.position),
                set(),
            )
            for index, worker in enumerate(roster)
        ]

        self.assertEqual(
            memory.worker_sector,
            {worker.id: index for index, worker in enumerate(roster)},
        )
        self.assertEqual(len(set(goals)), len(roster))
        self.assertTrue(all(agent.chebyshev((0, 0), goal) == 12 for goal in goals))

    def test_worker_count_expands_resource_scout_and_memory_radii(self):
        self.assertEqual(agent.resource_scout_radii_for(4)[-1], 32)
        self.assertEqual(agent.resource_scout_radii_for(8)[-1], 40)
        self.assertEqual(agent.resource_scout_radii_for(12)[-1], 48)
        self.assertEqual(agent.resource_scout_radii_for(16)[-1], 56)
        self.assertEqual(agent.resource_scout_radii_for(20)[-1], 64)
        self.assertEqual(agent.resource_memory_radius_for(4), 36)
        self.assertEqual(agent.resource_memory_radius_for(12), 48)
        self.assertEqual(agent.resource_memory_radius_for(20), 64)

    def test_resource_ring_route_is_clockwise_and_uses_expected_radii(self):
        route = agent.square_ring_waypoints(
            (0, 0),
            32,
            agent.RESOURCE_SCOUT_WAYPOINT_STEP,
        )
        corners = ((-32, -32), (32, -32), (32, 32), (-32, 32))
        corner_indices = [route.index(corner) for corner in corners]

        self.assertEqual(corner_indices, sorted(corner_indices))
        self.assertTrue(
            all(
                agent.chebyshev(first, second)
                <= agent.RESOURCE_SCOUT_WAYPOINT_STEP
                for first, second in zip(route, route[1:] + route[:1])
            )
        )
        self.assertEqual(agent.RESOURCE_SCOUT_RADII, (12, 19, 26, 32))
        self.assertEqual(
            agent.RESOURCE_SCOUT_RING_SEQUENCE,
            (12, 19, 26, 32, 26, 19),
        )

    def test_blocked_resource_scout_goal_is_skipped(self):
        worker_id = UUID(int=100)
        memory = agent.AgentMemory(worker_sector={worker_id: 0})
        route = agent.square_ring_waypoints(
            (0, 0),
            12,
            agent.RESOURCE_SCOUT_WAYPOINT_STEP,
        )

        goal = memory.goal_for(
            worker_id,
            0,
            4,
            (0, 0),
            (5, 0),
            {route[0]},
        )

        self.assertEqual(goal, route[1])
        self.assertEqual(memory.scout_phase[worker_id], 1)

    def test_three_path_failures_advance_resource_scout_goal(self):
        worker = controlled_unit(100, UnitType.WORKER, (5, 0))
        obstacles = ((4, 0), (6, 0), (5, -1), (5, 1))
        memory = agent.AgentMemory()

        for tick in range(100, 103):
            _, actions, memory = self.plan(
                make_turn(
                    [worker],
                    resources=0,
                    tick=tick,
                    obstacle_cells=obstacles,
                ),
                memory,
            )
            self.assertTrue(any("wait-scout" in action for action in actions))

        self.assertEqual(memory.scout_phase[worker.id], 1)
        self.assertNotIn(worker.id, memory.scout_goal)
        self.assertNotIn(worker.id, memory.scout_path_failures)

    def test_resource_assignment_and_healing_override_resource_scout(self):
        assigned = controlled_unit(100, UnitType.WORKER, (5, 0))
        _, actions, memory = self.plan(
            make_turn([assigned], resource_cells=[(6, 0)]),
        )
        self.assertIn((6, 0), memory.worker_resource_target.values())
        self.assertNotIn(assigned.id, memory.scout_goal)
        self.assertFalse(any(" scout " in action for action in actions))

        injured = controlled_unit(100, UnitType.WORKER, (1, 0), hp=1)
        plan, heal_actions, heal_memory = self.plan(
            make_turn([injured], resources=1),
        )
        self.assertIsInstance(plan.unit_actions[injured.id], MoveAction)
        self.assertTrue(any("worker-heal-return" in action for action in heal_actions))
        self.assertNotIn(injured.id, heal_memory.scout_goal)

    def test_leaving_outer_mode_resumes_resource_ring(self):
        units = outer_scout_roster()
        memory = agent.AgentMemory()
        self.plan(make_turn(units, resources=95, tick=100), memory)

        _, actions, memory = self.plan(
            make_turn(units, resources=94, tick=101),
            memory,
        )

        self.assertFalse(memory.outer_scout_active)
        self.assertTrue(any("workers outer-scout-complete" in action for action in actions))
        self.assertTrue(memory.scout_goal)
        self.assertTrue(
            all(
                agent.chebyshev((0, 0), goal) == 12
                for goal in memory.scout_goal.values()
            )
        )


class OuterScoutTests(AgentTestCase):
    """高库存时四名 Worker 的 32-64 格方环扫描。"""

    def test_incremental_worker_spawns_are_rebalanced_into_quarter_sectors(self):
        memory = agent.AgentMemory()

        for count in range(1, 5):
            memory.sync_worker_sectors(workers(count))

        worker_ids = [UUID(int=value) for value in range(100, 104)]
        self.assertEqual(
            memory.worker_sector,
            dict(zip(worker_ids, range(4))),
        )

    def test_twelve_workers_use_unique_outer_ring_offsets(self):
        roster = population_workers(12)
        memory = agent.AgentMemory()
        memory.sync_worker_sectors(roster)

        goals = [
            memory.outer_scout_goal_for(
                worker.id,
                (0, 0),
                tuple(worker.position),
                set(),
                worker_index=index,
                worker_count=len(roster),
            )
            for index, worker in enumerate(roster)
        ]

        self.assertEqual(len(set(goals)), len(roster))
        self.assertTrue(all(agent.chebyshev((0, 0), goal) == 32 for goal in goals))

    def test_adjacent_persisted_sectors_are_migrated_and_reset(self):
        worker_ids = [UUID(int=value) for value in range(100, 104)]
        memory = agent.AgentMemory(
            worker_sector=dict(zip(worker_ids, (0, 2, 4, 6))),
            outer_scout_ring_index={worker_id: 2 for worker_id in worker_ids},
            outer_scout_step={worker_id: 8 for worker_id in worker_ids},
            outer_scout_goal={worker_id: (32, 32) for worker_id in worker_ids},
            outer_scout_path_failures={worker_id: 1 for worker_id in worker_ids},
        )

        memory.sync_worker_sectors(workers(4))

        self.assertEqual(
            memory.worker_sector,
            dict(zip(worker_ids, range(4))),
        )
        self.assertFalse(memory.outer_scout_ring_index)
        self.assertFalse(memory.outer_scout_step)
        self.assertFalse(memory.outer_scout_goal)
        self.assertFalse(memory.outer_scout_path_failures)

    def test_outer_mode_repairs_persisted_layout_before_assigning_goals(self):
        worker_ids = [UUID(int=value) for value in range(100, 104)]
        memory = agent.AgentMemory(
            worker_sector=dict(zip(worker_ids, (0, 2, 4, 6))),
        )

        _, _, memory = self.plan(
            make_turn(outer_scout_roster(), resources=95),
            memory,
        )

        self.assertEqual(
            memory.worker_sector,
            dict(zip(worker_ids, range(4))),
        )
        scout_ids = worker_ids[2:]
        goals = [memory.outer_scout_goal[worker_id] for worker_id in scout_ids]
        self.assertEqual(len(set(goals)), len(goals))
        self.assertTrue(
            all(
                agent.manhattan(first, second)
                >= agent.OUTER_SCOUT_MIN_GOAL_DISTANCE
                for index, first in enumerate(goals)
                for second in goals[index + 1 :]
            )
        )

    def test_outer_scout_skips_goal_inside_another_worker_vision(self):
        first_id = UUID(int=100)
        second_id = UUID(int=101)
        route = agent.square_ring_waypoints((0, 0), 32)
        memory = agent.AgentMemory(
            worker_sector={first_id: 1, second_id: 2},
            outer_scout_step={second_id: 35},
        )

        first_goal = memory.outer_scout_goal_for(
            first_id,
            (0, 0),
            (0, 0),
            set(),
        )
        second_goal = memory.outer_scout_goal_for(
            second_id,
            (0, 0),
            (0, 0),
            set(),
            {first_goal},
        )

        self.assertEqual(first_goal, route[5])
        self.assertNotEqual(second_goal, first_goal)
        self.assertGreaterEqual(
            agent.manhattan(second_goal, first_goal),
            agent.OUTER_SCOUT_MIN_GOAL_DISTANCE,
        )

    def test_outer_scout_requires_population_nineteen_and_full_core(self):
        cases = (
            (outer_scout_roster(), 94, False),
            (outer_scout_roster()[:-1], 90, False),
            (outer_scout_roster(), 95, True),
        )

        for units, resources, expected_active in cases:
            with self.subTest(population=len(units), resources=resources):
                _, actions, memory = self.plan(
                    make_turn(units, resources=resources),
                )
                self.assertEqual(memory.outer_scout_active, expected_active)
                self.assertEqual(
                    any("outer-scout " in action for action in actions),
                    expected_active,
                )

    def test_loaded_worker_deposits_above_old_threshold_when_core_has_capacity(self):
        units = outer_scout_roster(
            worker_positions={100: (0, 0)},
            worker_cargo={100: 1},
        )

        plan, actions, memory = self.plan(make_turn(units, resources=94))

        self.assertFalse(memory.outer_scout_active)
        self.assertIsInstance(plan.unit_actions[UUID(int=100)], DepositAction)
        self.assertTrue(any("deposit 1" in action for action in actions))

    def test_workers_keep_harvesting_above_one_hundred_with_free_capacity(self):
        units = [
            *outer_scout_roster(worker_positions={100: (5, 0)}),
            controlled_unit(999, UnitType.VANGUARD, (30, 0)),
            controlled_unit(1000, UnitType.VANGUARD, (31, 0)),
            controlled_unit(1001, UnitType.RANGER, (32, 0)),
        ]

        plan, actions, memory = self.plan(
            make_turn(units, resources=101, resource_cells=[(5, 0)]),
        )

        self.assertEqual(len(units), 22)
        self.assertFalse(memory.outer_scout_active)
        self.assertIsInstance(plan.unit_actions[UUID(int=100)], HarvestAction)
        self.assertTrue(any("00000000 harvest" in action for action in actions))

    def test_loaded_worker_scans_when_core_is_full(self):
        units = outer_scout_roster(
            worker_positions={100: (0, 0)},
            worker_cargo={100: 1},
        )

        plan, actions, memory = self.plan(make_turn(units, resources=95))

        self.assertTrue(memory.outer_scout_active)
        self.assertIsInstance(plan.unit_actions[UUID(int=100)], MoveAction)
        self.assertTrue(any("carrier-" in action for action in actions))
        self.assertEqual(
            sum("worker-role" in action and "role=CARRIER" in action for action in actions),
            2,
        )
        self.assertTrue(any("capacity-" in action for action in actions))

    def test_immediate_threat_overrides_outer_scout(self):
        units = outer_scout_roster(worker_positions={100: (5, 0)})
        threat = enemy_unit(400, UnitType.VANGUARD, (6, 0))

        plan, actions, memory = self.plan(
            make_turn(units, enemies=[threat], resources=95),
        )

        self.assertTrue(memory.outer_scout_active)
        self.assertIsInstance(plan.unit_actions[UUID(int=100)], MoveAction)
        self.assertTrue(any("00000000 flee " in action for action in actions))
        self.assertNotIn(UUID(int=100), memory.outer_scout_goal)

    def test_existing_retreat_overrides_outer_scout(self):
        units = outer_scout_roster(worker_positions={100: (5, 0)})
        memory = agent.AgentMemory(
            retreat_until={UUID(int=100): 110},
            retreat_goal={UUID(int=100): (9, 0)},
        )

        _, actions, memory = self.plan(
            make_turn(units, resources=95, tick=100),
            memory,
        )

        self.assertTrue(memory.outer_scout_active)
        self.assertTrue(any("00000000 retreat " in action for action in actions))
        self.assertNotIn(UUID(int=100), memory.outer_scout_goal)

    def test_leaving_outer_scout_clears_progress_and_restores_resources(self):
        units = outer_scout_roster()
        memory = agent.AgentMemory()
        self.plan(make_turn(units, resources=95, tick=100), memory)
        self.assertTrue(memory.outer_scout_goal)

        _, actions, memory = self.plan(
            make_turn(
                units,
                resources=94,
                tick=101,
                resource_cells=[(6, 0)],
            ),
            memory,
        )

        self.assertFalse(memory.outer_scout_active)
        self.assertFalse(memory.outer_scout_ring_index)
        self.assertFalse(memory.outer_scout_step)
        self.assertFalse(memory.outer_scout_goal)
        self.assertFalse(memory.outer_scout_path_failures)
        self.assertIn((6, 0), memory.worker_resource_target.values())
        self.assertTrue(any("workers outer-scout-complete" in action for action in actions))

    def test_four_workers_start_at_unique_quarter_ring_offsets(self):
        units = outer_scout_roster()

        _, _, memory = self.plan(make_turn(units, resources=95))

        worker_ids = [UUID(int=value) for value in range(102, 104)]
        route = agent.square_ring_waypoints((0, 0), 32)
        expected = {
            worker_id: route[(sector * len(route)) // 4]
            for worker_id, sector in zip(worker_ids, (2, 3))
        }
        self.assertEqual(
            {worker_id: memory.outer_scout_goal[worker_id] for worker_id in worker_ids},
            expected,
        )
        self.assertEqual(len(set(expected.values())), 2)

    def test_square_ring_route_is_clockwise_and_covers_outer_annulus(self):
        route = agent.square_ring_waypoints((0, 0), 32)
        corners = ((-32, -32), (32, -32), (32, 32), (-32, 32))
        corner_indices = [route.index(corner) for corner in corners]

        self.assertEqual(corner_indices, sorted(corner_indices))
        self.assertTrue(
            all(
                agent.chebyshev(first, second)
                <= agent.OUTER_SCOUT_WAYPOINT_STEP
                for first, second in zip(route, route[1:] + route[:1])
            )
        )

        waypoints = {
            waypoint
            for radius in agent.OUTER_SCOUT_RADII
            for waypoint in agent.square_ring_waypoints((0, 0), radius)
        }
        missing = {
            (x, y)
            for x in range(-64, 65)
            for y in range(-64, 65)
            if 32 <= agent.chebyshev((0, 0), (x, y)) <= 64
            and min(agent.chebyshev((x, y), waypoint) for waypoint in waypoints) > 3
        }
        self.assertFalse(missing)

    def test_blocked_outer_goal_is_skipped(self):
        worker_id = UUID(int=100)
        memory = agent.AgentMemory(worker_sector={worker_id: 0})
        route = agent.square_ring_waypoints((0, 0), 32)

        goal = memory.outer_scout_goal_for(
            worker_id,
            (0, 0),
            (5, 0),
            {route[0]},
        )

        self.assertEqual(goal, route[1])
        self.assertEqual(memory.outer_scout_step[worker_id], 1)

    def test_three_path_failures_advance_outer_goal(self):
        units = outer_scout_roster(worker_positions={102: (5, 0)})
        obstacles = ((4, 0), (6, 0), (5, -1), (5, 1))
        memory = agent.AgentMemory()

        for tick in range(100, 103):
            _, actions, memory = self.plan(
                make_turn(
                    units,
                    resources=95,
                    tick=tick,
                    obstacle_cells=obstacles,
                ),
                memory,
            )
            self.assertTrue(
                any(
                    action.startswith("00000000 outer-scout-hold")
                    for action in actions
                )
            )

        worker_id = UUID(int=102)
        self.assertEqual(memory.outer_scout_step[worker_id], 1)
        self.assertNotIn(worker_id, memory.outer_scout_goal)
        self.assertNotIn(worker_id, memory.outer_scout_path_failures)


class RangerCombatTests(AgentTestCase):
    """Ranger 的 v0.8 八方向射线规则。"""

    def test_ranger_diagonal_shot_geometry(self):
        self.assertTrue(agent.clear_ranger_shot((0, 0), (2, 2), set()))
        self.assertTrue(
            agent.clear_ranger_shot((0, 0), (2, 2), {(1, 0)})
        )
        self.assertFalse(
            agent.clear_ranger_shot((0, 0), (2, 2), {(1, 1)})
        )
        self.assertFalse(agent.clear_ranger_shot((0, 0), (2, 1), set()))
        self.assertFalse(agent.clear_ranger_shot((0, 0), (4, 4), set()))

    def test_ranger_shoots_through_friendly_unit_and_enemy_core(self):
        units = [
            controlled_unit(201, UnitType.RANGER, (0, 0)),
            controlled_unit(202, UnitType.WORKER, (0, 1)),
        ]
        enemies = [
            enemy_core(400, (0, 2)),
            enemy_unit(401, UnitType.VANGUARD, (0, 3)),
        ]
        plan, _, _ = self.plan(make_turn(units, enemies=enemies))
        self.assertIsInstance(plan.unit_actions[UUID(int=201)], ShootAction)


class SquadStrategyTests(AgentTestCase):
    """2V1R 编制、独立巡逻、统一集结和守家紧急旁路。"""

    @staticmethod
    def roster():
        return [
            *workers(4),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (1, 0)),
            controlled_unit(301, UnitType.RANGER, (1, 1)),
            controlled_unit(203, UnitType.VANGUARD, (10, 0)),
            controlled_unit(204, UnitType.VANGUARD, (10, 1)),
            controlled_unit(302, UnitType.RANGER, (9, 0)),
            controlled_unit(205, UnitType.VANGUARD, (-10, 0)),
            controlled_unit(206, UnitType.VANGUARD, (-10, 1)),
            controlled_unit(303, UnitType.RANGER, (-9, 0)),
        ]

    @staticmethod
    def squad_memory():
        return agent.AgentMemory(
            squad_assignments={
                UUID(int=201): 0,
                UUID(int=202): 0,
                UUID(int=301): 0,
                UUID(int=203): 1,
                UUID(int=204): 1,
                UUID(int=302): 1,
                UUID(int=205): 2,
                UUID(int=206): 2,
                UUID(int=303): 2,
            },
        )

    @staticmethod
    def high_population_roster(population: int):
        """生成完整 2V1R 阵容，用于 40/60 人口职责边界测试。"""
        worker_count = 4
        combat_population = population - worker_count
        squad_count = combat_population // 3
        units = population_workers(worker_count)
        for squad_index in range(squad_count):
            base_x = 10 + squad_index * 4
            units.extend(
                (
                    controlled_unit(
                        200 + squad_index * 2,
                        UnitType.VANGUARD,
                        (base_x, 0),
                    ),
                    controlled_unit(
                        201 + squad_index * 2,
                        UnitType.VANGUARD,
                        (base_x, 1),
                    ),
                    controlled_unit(
                        500 + squad_index,
                        UnitType.RANGER,
                        (base_x + 1, 0),
                    ),
                )
            )
        for extra_index in range(combat_population - squad_count * 3):
            units.append(
                controlled_unit(
                    800 + extra_index,
                    UnitType.VANGUARD,
                    (100 + extra_index, 0),
                )
            )
        return units

    @staticmethod
    def complete_squad(squad_id: int) -> agent.CombatSquad:
        return agent.CombatSquad(
            squad_id=squad_id,
            vanguard_ids=(UUID(int=1000 + squad_id * 2), UUID(int=1001 + squad_id * 2)),
            ranger_ids=(UUID(int=2000 + squad_id),),
        )

    def test_high_population_patrol_bands_preserve_low_population_layout(self):
        self.assertEqual(
            agent.squad_patrol_radii_for(39, 0),
            agent.SQUAD_PATROL_RADII,
        )
        self.assertEqual(agent.squad_patrol_radii_for(40, 0), (12, 19, 26, 32))
        self.assertEqual(agent.squad_patrol_radii_for(40, 3), (12, 19, 26, 32))
        self.assertEqual(agent.squad_patrol_radii_for(40, 4), (32, 39, 49, 56))
        self.assertEqual(agent.squad_patrol_radii_for(60, 8), (56, 63, 73, 80))

    def test_protected_squad_roles_follow_population_boundaries(self):
        squads = tuple(self.complete_squad(squad_id) for squad_id in range(4))
        self.assertEqual(
            agent.protected_squad_roles(19, squads),
            {0: "HOME_GUARD"},
        )
        self.assertEqual(
            agent.protected_squad_roles(20, squads),
            {0: "HOME_GUARD", 1: "RAPID_RESPONSE"},
        )
        self.assertEqual(
            agent.protected_squad_roles(40, squads),
            {0: "HOME_GUARD", 1: "HOME_GUARD", 2: "RAPID_RESPONSE"},
        )
        self.assertEqual(
            agent.protected_squad_roles(60, squads),
            {0: "HOME_GUARD", 1: "HOME_GUARD", 2: "RAPID_RESPONSE"},
        )

    def test_assault_waves_use_stable_sorted_batches(self):
        squads = [
            self.complete_squad(4),
            agent.CombatSquad(3, (UUID(int=3000),), (UUID(int=4000),)),
            self.complete_squad(1),
            self.complete_squad(5),
            self.complete_squad(2),
        ]
        waves = agent.assault_wave_groups(squads, wave_size=3)
        self.assertEqual(
            tuple(tuple(squad.squad_id for squad in wave) for wave in waves),
            ((1, 2, 4), (5,)),
        )

    def test_high_population_plan_keeps_three_protected_squads_out_of_patrol(self):
        for population in (40, 60):
            with self.subTest(population=population):
                _, actions, memory = self.plan(
                    make_turn(self.high_population_roster(population))
                )
                self.assertEqual(memory.last_plan_metrics.protected_squad_count, 3)
                self.assertTrue(any("squad-role team=0 role=HOME_GUARD" in item for item in actions))
                self.assertTrue(any("squad-role team=1 role=HOME_GUARD" in item for item in actions))
                self.assertTrue(any("squad-role team=2 role=RAPID_RESPONSE" in item for item in actions))
                self.assertNotIn(0, memory.squad_patrol_goal)
                self.assertNotIn(1, memory.squad_patrol_goal)
                self.assertNotIn(2, memory.squad_patrol_goal)
                self.assertFalse(memory.last_plan_metrics.deadline_exceeded)
                self.assertLess(
                    memory.last_plan_metrics.decision_ms,
                    agent.PLANNING_BUDGET_SECONDS * 1000,
                )

    def test_assault_rally_is_selected_per_wave(self):
        squads = tuple(self.complete_squad(squad_id) for squad_id in (1, 2, 3, 4))
        unit_by_id = {}
        for squad_id in (1, 2, 3, 4):
            squad = self.complete_squad(squad_id)
            for index, unit_id in enumerate(squad.vanguard_ids + squad.ranger_ids):
                unit_by_id[unit_id] = controlled_unit(
                    unit_id.int,
                    UnitType.VANGUARD if index < 2 else UnitType.RANGER,
                    (10 + squad_id * 10, index),
                )
        first_rally = agent.choose_assault_rally(squads[:3], unit_by_id, (100, 0))
        second_rally = agent.choose_assault_rally(squads[3:], unit_by_id, (100, 0))
        self.assertIsNotNone(first_rally)
        self.assertIsNotNone(second_rally)
        self.assertNotEqual(first_rally, second_rally)

    def test_target_reservations_cap_repeat_attackers_by_target_type(self):
        worker_target = enemy_unit(401, UnitType.WORKER, (20, 0))
        combat_target = enemy_unit(402, UnitType.VANGUARD, (20, 1))
        core_target = enemy_core(403, (20, 2))
        reservations = agent.build_target_reservations(
            (worker_target, combat_target, core_target),
        )
        self.assertEqual(reservations[worker_target.id].max_attackers, 1)
        self.assertEqual(reservations[combat_target.id].max_attackers, 2)
        self.assertEqual(reservations[core_target.id].max_attackers, 3)

        context = type("ReservationContext", (), {
            "target_reservations": reservations,
        })()
        self.assertTrue(agent.reserve_target_attacker(context, worker_target, UUID(int=1)))
        self.assertFalse(agent.reserve_target_attacker(context, worker_target, UUID(int=2)))
        self.assertTrue(agent.reserve_target_attacker(context, combat_target, UUID(int=1)))
        self.assertTrue(agent.reserve_target_attacker(context, combat_target, UUID(int=2)))
        self.assertFalse(agent.reserve_target_attacker(context, combat_target, UUID(int=3)))

    def test_worker_roles_are_stable_and_carriers_are_first_two(self):
        roster = workers(4)
        active_roles = agent.worker_roles_for(roster, True)
        inactive_roles = agent.worker_roles_for(roster, False)
        self.assertEqual(
            [active_roles[worker.id] for worker in roster],
            ["CARRIER", "CARRIER", "SCOUT", "SCOUT"],
        )
        self.assertTrue(all(role == "STANDARD" for role in inactive_roles.values()))

    def test_outer_scout_carrier_keeps_visible_resource_task_when_core_is_full(self):
        units = outer_scout_roster(worker_positions={100: (5, 0)})
        plan, actions, memory = self.plan(
            make_turn(units, resources=95, resource_cells=[(5, 0)]),
        )
        self.assertTrue(memory.outer_scout_active)
        self.assertIsInstance(plan.unit_actions[UUID(int=100)], HarvestAction)
        self.assertTrue(any("worker-role" in action and "role=CARRIER" in action for action in actions))

    def test_squads_are_stable_persisted_two_vanguards_one_ranger(self):
        memory = self.squad_memory()
        memory.squad_regroup_goal[1] = (12, 4)
        memory.squad_regroup_interrupted.add(1)
        _, _, memory = self.plan(make_turn(self.roster()), memory)

        squads = memory.combat_squads(
            [unit for unit in self.roster() if unit.unit_type is UnitType.VANGUARD],
            [unit for unit in self.roster() if unit.unit_type is UnitType.RANGER],
        )
        self.assertEqual(len(squads), 3)
        self.assertTrue(all(squad.complete for squad in squads))
        self.assertEqual(squads[0].unit_ids, {UUID(int=201), UUID(int=202), UUID(int=301)})

        restored = agent.AgentMemory.restore(memory.persistent_state())
        self.assertEqual(restored.persistent_state()["version"], 10)
        self.assertEqual(restored.squad_assignments, memory.squad_assignments)
        self.assertEqual(restored.squad_regroup_goal, memory.squad_regroup_goal)
        self.assertEqual(
            restored.squad_regroup_interrupted,
            memory.squad_regroup_interrupted,
        )

    def test_complete_field_squads_patrol_independently_without_global_gather(self):
        _, actions, memory = self.plan(make_turn(self.roster()), self.squad_memory())

        self.assertFalse(memory.assault_gathering)
        self.assertIn(1, memory.squad_patrol_goal)
        self.assertIn(2, memory.squad_patrol_goal)
        self.assertTrue(any("squad-patrol" in action and "team=1" in action for action in actions))
        self.assertTrue(any("squad-patrol" in action and "team=2" in action for action in actions))
        self.assertFalse(any("squad-gather" in action for action in actions))

    def test_idle_damaged_field_squad_member_returns_home_to_heal(self):
        units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                tuple(unit.position),
                hp=3 if unit.id == UUID(int=203) else unit.hp,
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]

        plan, actions, _ = self.plan(
            make_turn(units, resources=1),
            self.squad_memory(),
        )

        action = plan.unit_actions[UUID(int=203)]
        self.assertIsInstance(action, MoveAction)
        self.assertEqual(action.direction.value, "LEFT")
        self.assertTrue(
            any(
                "squad-heal team=1-return amount=1" in item
                for item in actions
            )
        )

    def test_full_squad_member_exits_core_before_injured_leader_enters(self):
        positions = {
            203: (-1, 0),
            204: (0, 0),
            302: (-2, 0),
        }
        hit_points = {
            203: 3,
            204: 4,
            302: 2,
        }
        units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                positions.get(unit.id.int, tuple(unit.position)),
                hp=hit_points.get(unit.id.int, unit.hp),
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]

        plan, actions, _ = self.plan(
            make_turn(units, resources=1),
            self.squad_memory(),
        )

        healed_member_action = plan.unit_actions[UUID(int=204)]
        injured_leader_action = plan.unit_actions[UUID(int=203)]
        self.assertIsInstance(healed_member_action, MoveAction)
        self.assertEqual(healed_member_action.direction.value, "UP")
        self.assertIsInstance(injured_leader_action, MoveAction)
        self.assertEqual(injured_leader_action.direction.value, "RIGHT")
        self.assertTrue(
            any("squad-heal team=1-exit" in item for item in actions)
        )
        self.assertTrue(
            any(
                "squad-heal team=1-return amount=1" in item
                for item in actions
            )
        )

    def test_squad_ranger_can_follow_beyond_old_core_roam_boundary(self):
        positions = {
            203: (28, 0),
            204: (28, 1),
            302: (24, 0),
        }
        units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                positions.get(unit.id.int, tuple(unit.position)),
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]
        memory = self.squad_memory()
        memory.squad_patrol_goal[1] = (32, 0)

        plan, _, _ = self.plan(make_turn(units), memory)

        self.assertIsInstance(plan.unit_actions[UUID(int=302)], MoveAction)

    def test_spread_squad_locks_reachable_regroup_area(self):
        positions = {
            203: (32, 0),
            204: (33, 0),
            302: (20, 0),
        }
        units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                positions.get(unit.id.int, tuple(unit.position)),
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]

        plan, actions, memory = self.plan(make_turn(units), self.squad_memory())

        self.assertIn(1, memory.squad_regroup_goal)
        self.assertIsInstance(plan.unit_actions[UUID(int=302)], MoveAction)
        self.assertTrue(any("regroup-start team=1" in action for action in actions))

    def test_regroup_finishes_only_after_squad_returns_within_four(self):
        memory = self.squad_memory()
        memory.squad_regroup_goal[1] = (10, 0)

        _, actions, memory = self.plan(make_turn(self.roster()), memory)

        self.assertNotIn(1, memory.squad_regroup_goal)
        self.assertTrue(any("regroup-complete team=1" in action for action in actions))

    def test_combat_end_recomputes_regroup_goal_from_current_positions(self):
        memory = self.squad_memory()
        memory.squad_regroup_goal[1] = (0, 0)
        combat_positions = {
            203: (20, 0),
            204: (21, 0),
            302: (10, 0),
        }
        combat_units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                combat_positions.get(unit.id.int, tuple(unit.position)),
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]
        self.plan(
            make_turn(
                combat_units,
                enemies=[enemy_unit(401, UnitType.VANGUARD, (20, 2))],
                tick=100,
            ),
            memory,
        )
        self.assertIn(1, memory.squad_regroup_interrupted)

        safe_positions = {
            203: (30, 0),
            204: (31, 0),
            302: (20, 0),
        }
        safe_units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                safe_positions.get(unit.id.int, tuple(unit.position)),
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]
        self.plan(make_turn(safe_units, tick=101), memory)
        self.assertEqual(memory.squad_regroup_goal[1], (0, 0))

        _, actions, memory = self.plan(make_turn(safe_units, tick=102), memory)

        self.assertNotEqual(memory.squad_regroup_goal[1], (0, 0))
        self.assertNotIn(1, memory.squad_regroup_interrupted)
        self.assertTrue(
            any("regroup-after-combat team=1" in action for action in actions)
        )

    def test_stalled_regroup_reselects_center(self):
        positions = {
            203: (32, 0),
            204: (33, 0),
            302: (20, 0),
        }
        units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                positions.get(unit.id.int, tuple(unit.position)),
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]
        memory = self.squad_memory()
        memory.squad_regroup_goal[1] = (32, 0)
        memory.squad_regroup_last_distance[1] = agent.squad_regroup_distance(
            [
                unit
                for unit in units
                if unit.id in {UUID(int=203), UUID(int=204), UUID(int=302)}
            ],
            (32, 0),
        )
        memory.squad_regroup_stall_ticks[1] = agent.SQUAD_REGROUP_STALL_TICKS - 1

        _, actions, memory = self.plan(make_turn(units), memory)

        self.assertNotEqual(memory.squad_regroup_goal[1], (32, 0))
        self.assertTrue(any("regroup-repath team=1" in action for action in actions))

    def test_unguarded_enemy_core_is_attacked_without_global_gather(self):
        _, actions, memory = self.plan(
            make_turn(self.roster(), enemies=[enemy_core(400, (20, 0))]),
            self.squad_memory(),
        )

        self.assertFalse(memory.assault_gathering)
        self.assertFalse(memory.assault_guarded)
        self.assertIsNone(memory.assault_rally_position)
        self.assertTrue(any("squad-assault" in action for action in actions))
        self.assertFalse(
            any(
                "squad-assault" in action and "team=2" in action
                for action in actions
            )
        )

    def test_unguarded_core_at_home_distance_64_launches_distant_field_squad(self):
        _, actions, memory = self.plan(
            make_turn(self.roster(), enemies=[enemy_core(400, (64, 0))]),
            self.squad_memory(),
        )

        self.assertEqual(memory.assault_target_position, (64, 0))
        self.assertFalse(memory.assault_guarded)
        self.assertFalse(memory.assault_gathering)
        self.assertTrue(
            any(
                "squad-assault" in action and "team=1" in action
                for action in actions
            )
        )
        self.assertFalse(
            any(
                "squad-assault" in action and "team=2" in action
                for action in actions
            )
        )

    def test_gathered_field_squads_switch_to_joint_assault(self):
        units = self.roster()
        positions = {
            203: (8, 0), 204: (8, 1), 302: (7, 0),
            205: (9, 0), 206: (9, 1), 303: (8, 2),
        }
        units = [
            controlled_unit(unit.id.int, unit.unit_type, positions.get(unit.id.int, tuple(unit.position)))
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in units
        ]

        _, actions, memory = self.plan(
            make_turn(
                units,
                enemies=[
                    enemy_core(400, (20, 0)),
                    enemy_unit(401, UnitType.VANGUARD, (19, 2)),
                ],
            ),
            self.squad_memory(),
        )

        self.assertFalse(memory.assault_gathering)
        self.assertTrue(memory.assault_guarded)
        self.assertTrue(any("squads assault-ready" in action for action in actions))
        self.assertTrue(any("squad-assault" in action and "team=1" in action for action in actions))
        self.assertTrue(any("squad-assault" in action and "team=2" in action for action in actions))

    def test_guarded_core_at_home_distance_64_starts_global_gather(self):
        _, actions, memory = self.plan(
            make_turn(
                self.roster(),
                enemies=[
                    enemy_core(400, (64, 0)),
                    enemy_unit(401, UnitType.VANGUARD, (64, 1)),
                ],
            ),
            self.squad_memory(),
        )

        self.assertTrue(memory.assault_guarded)
        self.assertTrue(memory.assault_gathering)
        self.assertIsNotNone(memory.assault_rally_position)
        self.assertTrue(any("squad-gather" in action for action in actions))
        self.assertFalse(any("squad-assault" in action for action in actions))

    def test_guarded_core_at_home_distance_65_only_nearby_squad_attacks(self):
        positions = {
            203: (41, 0),
            204: (41, 1),
            302: (40, 0),
        }
        units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                positions.get(unit.id.int, tuple(unit.position)),
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]

        _, actions, memory = self.plan(
            make_turn(
                units,
                enemies=[
                    enemy_core(400, (65, 0)),
                    enemy_unit(401, UnitType.VANGUARD, (65, 1)),
                ],
            ),
            self.squad_memory(),
        )

        self.assertTrue(memory.assault_guarded)
        self.assertFalse(memory.assault_gathering)
        self.assertTrue(any("squad-assault" in action and "team=1" in action for action in actions))
        self.assertFalse(any("squad-assault" in action and "team=2" in action for action in actions))

    def test_far_guarded_core_without_nearby_squad_keeps_patrolling(self):
        _, actions, memory = self.plan(
            make_turn(
                self.roster(),
                enemies=[
                    enemy_core(400, (65, 0)),
                    enemy_unit(401, UnitType.RANGER, (65, 1)),
                ],
            ),
            self.squad_memory(),
        )

        self.assertEqual(memory.assault_target_position, (65, 0))
        self.assertTrue(memory.assault_guarded)
        self.assertFalse(memory.assault_gathering)
        self.assertFalse(any("squad-assault" in action for action in actions))
        self.assertTrue(any("squad-patrol" in action and "team=1" in action for action in actions))
        self.assertTrue(any("squad-patrol" in action and "team=2" in action for action in actions))

    def test_gather_wait_is_interrupted_by_incoming_ranger_fire(self):
        memory = self.squad_memory()
        enemy = enemy_unit(401, UnitType.RANGER, (13, 0))

        plan, actions, _ = self.plan(
            make_turn(self.roster(), enemies=[enemy, enemy_core(400, (20, 0))]),
            memory,
        )

        self.assertIsInstance(plan.unit_actions[UUID(int=203)], MoveAction)
        self.assertTrue(any("squad-counter-fire" in action for action in actions))

    def test_ranger_disengages_when_enemy_vanguard_is_adjacent(self):
        units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                (10, 2) if unit.id == UUID(int=302) else tuple(unit.position),
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]
        enemy = enemy_unit(401, UnitType.VANGUARD, (10, 3))

        plan, actions, _ = self.plan(
            make_turn(units, enemies=[enemy]),
            self.squad_memory(),
        )

        self.assertIsInstance(plan.unit_actions[UUID(int=302)], MoveAction)
        self.assertTrue(any("squad-ranger-disengage" in action for action in actions))

    def test_ranger_supports_vanguard_locked_with_enemy_vanguard(self):
        positions = {
            203: (8, 0),
            204: (8, 1),
            302: (8, 2),
        }
        units = [
            controlled_unit(
                unit.id.int,
                unit.unit_type,
                positions.get(unit.id.int, tuple(unit.position)),
            )
            if unit.unit_type is not UnitType.WORKER
            else unit
            for unit in self.roster()
        ]
        enemy = enemy_unit(401, UnitType.VANGUARD, (9, 0))

        plan, actions, _ = self.plan(
            make_turn(units, enemies=[enemy]),
            self.squad_memory(),
        )

        self.assertIsInstance(plan.unit_actions[UUID(int=302)], MoveAction)
        self.assertTrue(any("squad-ranger-support-aim" in action for action in actions))

    def test_known_enemy_core_target_survives_memory_timeout(self):
        core_id = UUID(int=400)
        memory = agent.AgentMemory(
            known_enemy_cores={core_id: ((25, 0), 100)},
            assault_target_id=core_id,
            assault_target_kind="CORE",
            assault_target_position=(25, 0),
            assault_target_last_seen_tick=100,
        )

        memory.sync_assault_target([], (0, 0), 120)

        self.assertEqual(memory.assault_target_id, core_id)
        self.assertEqual(memory.assault_target_position, (25, 0))

    def test_destroyed_home_squad_triggers_support_and_rebuild(self):
        units = [
            unit
            for unit in self.roster()
            if unit.id not in {UUID(int=201), UUID(int=202), UUID(int=301)}
        ]
        enemy = enemy_unit(401, UnitType.VANGUARD, (4, 0))

        plan, actions, memory = self.plan(
            make_turn(units, enemies=[enemy], resources=10),
            self.squad_memory(),
        )

        self.assertEqual(memory.home_vanguard_id, None)
        self.assertTrue(any("squad-home-support" in action for action in actions))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)
        self.assertTrue(any("fill squad=0" in action for action in actions))

    def test_enemy_core_memory_is_not_limited_by_roam_boundary(self):
        _, actions, memory = self.plan(
            make_turn(self.roster(), enemies=[enemy_core(400, (25, 0))]),
            self.squad_memory(),
        )

        self.assertIsNotNone(memory.assault_target_id)
        self.assertFalse(memory.assault_gathering)
        self.assertTrue(any("squad-assault" in action for action in actions))

    def test_cell_unit_limit_starts_spawn_clearing_and_suppresses_production(self):
        worker = controlled_unit(100, UnitType.WORKER, (0, 0))
        event = ResolutionEvent(
            event_id=UUID(int=900),
            tick=100,
            event_type="CORE_SPAWN_FAILED",
            reason_code="CELL_UNIT_LIMIT",
        )

        plan, actions, memory = self.plan(
            make_turn([worker], resources=10, events=[event]),
        )

        self.assertIsNone(plan.core_action)
        self.assertEqual(memory.spawn_clear_until, 103)
        self.assertIsInstance(plan.unit_actions[worker.id], MoveAction)
        self.assertTrue(any("spawn-clear" in action for action in actions))


class V2SearchTests(AgentTestCase):
    @staticmethod
    def roster():
        return SquadStrategyTests.roster()

    @staticmethod
    def squad_memory():
        return SquadStrategyTests.squad_memory()

    def test_astar_reuses_same_tick_result_and_invalidates_dynamic_blockers(self):
        metrics = agent.PlanningMetrics(100)
        with patch.object(agent, "_ACTIVE_PLANNING_METRICS", metrics):
            first = agent.first_step_astar((0, 0), (3, 0), set(), set())
            second = agent.first_step_astar((0, 0), (3, 0), set(), set())
            changed = agent.first_step_astar((0, 0), (3, 0), set(), {(1, 0)})

        self.assertEqual(first, second)
        self.assertEqual(first, (1, 0))
        self.assertIsNotNone(changed)
        self.assertEqual(metrics.astar_calls, 3)
        self.assertEqual(metrics.astar_cache_hits, 1)

    def test_search_coverage_reuses_same_tick_result_and_keeps_empty_results(self):
        metrics = agent.PlanningMetrics(100)
        area = agent.core_search_cells((10, 10))
        with patch.object(agent, "_ACTIVE_PLANNING_METRICS", metrics):
            first = agent.search_coverage_from((10, 10), 3, area, set())
            second = agent.search_coverage_from((10, 10), 3, area, set())
            blocked = agent.search_coverage_from(
                (10, 10),
                3,
                area,
                area,
            )
            blocked_again = agent.search_coverage_from(
                (10, 10),
                3,
                area,
                area,
            )

        self.assertEqual(first, second)
        self.assertFalse(blocked)
        self.assertEqual(blocked, blocked_again)
        self.assertEqual(metrics.search_coverage_cache_hits, 2)

    def test_squad_patrol_moves_from_radius_twelve_to_nineteen(self):
        memory = agent.AgentMemory()
        first = memory.squad_patrol_goal_for(1, 0, 1, (0, 0), set())
        self.assertEqual(agent.chebyshev((0, 0), first), 12)

        route = agent.square_ring_waypoints(
            (0, 0),
            12,
            agent.SQUAD_PATROL_WAYPOINT_STEP,
        )
        for _ in route:
            memory.advance_squad_patrol(1, (0, 0))
        second = memory.squad_patrol_goal_for(1, 0, 1, (0, 0), set())

        self.assertEqual(memory.squad_patrol_ring_index[1], 1)
        self.assertEqual(agent.chebyshev((0, 0), second), 19)

    def test_squad_patrol_sectors_start_at_distinct_goals(self):
        memory = agent.AgentMemory()
        first = memory.squad_patrol_goal_for(1, 0, 2, (0, 0), set())
        second = memory.squad_patrol_goal_for(2, 1, 2, (0, 0), set())

        self.assertNotEqual(first, second)
        self.assertEqual(agent.chebyshev((0, 0), first), 12)
        self.assertEqual(agent.chebyshev((0, 0), second), 12)

    def test_worker_resource_track_infers_core_on_opposite_side(self):
        moving_away = agent.EnemyWorkerTrack(
            position=(10, 0),
            previous_position=(9, 0),
            first_seen_position=(9, 0),
            first_seen_tick=99,
            last_seen_tick=100,
            previous_seen_tick=99,
        )
        moving_toward = agent.EnemyWorkerTrack(
            position=(10, 0),
            previous_position=(9, 0),
            first_seen_position=(9, 0),
            first_seen_tick=99,
            last_seen_tick=100,
            previous_seen_tick=99,
        )

        away_guess = agent.infer_enemy_core_guess(
            moving_away,
            ((0, 0),),
            set(),
            {(0, 0)},
        )
        toward_guess = agent.infer_enemy_core_guess(
            moving_toward,
            ((0, 0),),
            set(),
            {(20, 0)},
        )

        self.assertEqual(away_guess, (24, 0))
        self.assertEqual(toward_guess, (-6, 0))
        self.assertLessEqual(agent.chebyshev((0, 0), away_guess), 24)

    def test_single_worker_sighting_starts_search_without_chase(self):
        memory = self.squad_memory()
        _, actions, memory = self.plan(
            make_turn(
                self.roster(),
                enemies=[enemy_unit(401, UnitType.WORKER, (12, 0))],
            ),
            memory,
        )

        self.assertTrue(memory.squad_search_missions)
        self.assertTrue(any("squad-search-start" in item for item in actions))
        self.assertFalse(any("squad-hunt" in item or "roam-chase" in item for item in actions))

    def test_worker_guess_cooldown_blocks_repeated_assignment(self):
        worker_id = UUID(int=401)
        memory = self.squad_memory()
        memory.worker_guess_cooldown_until[worker_id] = 164

        self.plan(
            make_turn(
                self.roster(),
                enemies=[enemy_unit(401, UnitType.WORKER, (12, 0))],
                tick=100,
            ),
            memory,
        )

        self.assertFalse(memory.squad_search_missions)

    def test_search_uses_independent_member_goals_without_regrouping(self):
        memory = self.squad_memory()
        memory.squad_regroup_goal[1] = (0, 0)
        memory.squad_search_missions[1] = agent.SquadSearchMission(
            kind="guess",
            center=(20, 0),
            started_tick=100,
            source_worker_id=UUID(int=401),
        )

        _, actions, memory = self.plan(make_turn(self.roster(), tick=101), memory)

        mission = memory.squad_search_missions[1]
        self.assertEqual(len(set(mission.unit_goals.values())), 3)
        self.assertNotIn(1, memory.squad_regroup_goal)
        self.assertTrue(any("squad-search team=1" in item for item in actions))
        self.assertFalse(any("squad-regroup" in item and "team=1" in item for item in actions))

    def test_one_time_coverage_completes_core_verification(self):
        core_id = UUID(int=400)
        center = (25, 0)
        memory = self.squad_memory()
        memory.known_enemy_cores[core_id] = (center, 99)
        memory.enemy_core_missing.add(core_id)
        memory.squad_search_missions[1] = agent.SquadSearchMission(
            kind="verify",
            center=center,
            started_tick=100,
            core_id=core_id,
            covered_cells=agent.core_search_cells(center) - {(10, 0)},
        )

        self.plan(make_turn(self.roster(), tick=101), memory)

        self.assertNotIn(1, memory.squad_search_missions)
        self.assertNotIn(core_id, memory.known_enemy_cores)
        self.assertEqual(memory.squad_search_cooldown_until[1], 165)

    def test_unguarded_missing_core_is_verified_before_guarded_core(self):
        guarded_id = UUID(int=400)
        unguarded_id = UUID(int=401)
        units = [
            unit
            for unit in self.roster()
            if unit.id not in {UUID(int=205), UUID(int=206), UUID(int=303)}
        ]
        memory = self.squad_memory()
        for unit_id in {UUID(int=205), UUID(int=206), UUID(int=303)}:
            memory.squad_assignments.pop(unit_id, None)
        memory.known_enemy_cores = {
            guarded_id: ((40, 0), 99),
            unguarded_id: ((50, 0), 99),
        }
        memory.enemy_core_guarded = {guarded_id: True, unguarded_id: False}
        memory.enemy_core_missing = {guarded_id, unguarded_id}

        self.plan(make_turn(units, tick=100), memory)

        self.assertEqual(memory.squad_search_missions[1].core_id, unguarded_id)

    def test_search_timeout_exits_but_keeps_unconfirmed_core(self):
        core_id = UUID(int=400)
        center = (100, 100)
        memory = self.squad_memory()
        memory.known_enemy_cores[core_id] = (center, 99)
        memory.enemy_core_missing.add(core_id)
        memory.squad_search_missions[1] = agent.SquadSearchMission(
            kind="verify",
            center=center,
            started_tick=100,
            core_id=core_id,
        )

        _, actions, memory = self.plan(
            make_turn(self.roster(), tick=100 + agent.CORE_SEARCH_MAX_TICKS),
            memory,
        )

        self.assertNotIn(1, memory.squad_search_missions)
        self.assertIn(core_id, memory.known_enemy_cores)
        self.assertIn(core_id, memory.enemy_core_missing)
        self.assertTrue(any("search-inconclusive" in item for item in actions))

    def test_search_with_no_remaining_goal_exits_without_sticking(self):
        core_id = UUID(int=400)
        center = (100, 100)
        memory = self.squad_memory()
        memory.known_enemy_cores[core_id] = (center, 99)
        memory.enemy_core_missing.add(core_id)
        memory.squad_search_missions[1] = agent.SquadSearchMission(
            kind="verify",
            center=center,
            started_tick=100,
            core_id=core_id,
            failed_goals=agent.core_search_cells(center),
        )

        _, actions, memory = self.plan(make_turn(self.roster(), tick=101), memory)

        self.assertNotIn(1, memory.squad_search_missions)
        self.assertIn(core_id, memory.known_enemy_cores)
        self.assertTrue(any("search-inconclusive" in item for item in actions))

    def test_own_core_destruction_event_removes_record_immediately(self):
        core_id = UUID(int=400)
        memory = self.squad_memory()
        memory.known_enemy_cores[core_id] = ((20, 0), 99)
        event = ResolutionEvent(
            event_id=UUID(int=900),
            tick=100,
            event_type="DESTRUCTION_PARTICIPATION",
            reason_code="CORE",
            target_id=core_id,
            position=(20, 0),
        )

        self.plan(make_turn(self.roster(), tick=100, events=[event]), memory)

        self.assertNotIn(core_id, memory.known_enemy_cores)

    def test_visible_core_cancels_guess_and_restarts_assault(self):
        memory = self.squad_memory()
        memory.squad_search_missions[1] = agent.SquadSearchMission(
            kind="guess",
            center=(20, 0),
            started_tick=99,
            source_worker_id=UUID(int=401),
        )

        _, actions, memory = self.plan(
            make_turn(self.roster(), enemies=[enemy_core(400, (20, 0))]),
            memory,
        )

        self.assertFalse(memory.squad_search_missions)
        self.assertEqual(memory.assault_target_id, UUID(int=400))
        self.assertTrue(any("squad-assault" in item for item in actions))

    def test_v8_state_migrates_enemy_core_into_v10(self):
        core_id = UUID(int=400)
        restored = agent.AgentMemory.restore(
            {
                "version": 8,
                "core_position": [0, 0],
                "known_enemy_cores": {
                    str(core_id): {"position": [25, 0], "tick": 100},
                },
            }
        )

        self.assertEqual(restored.known_enemy_cores[core_id], ((25, 0), 100))
        self.assertEqual(restored.persistent_state()["version"], 10)

    def test_v9_search_mission_round_trips(self):
        worker_id = UUID(int=401)
        memory = self.squad_memory()
        memory.squad_search_missions[1] = agent.SquadSearchMission(
            kind="guess",
            center=(20, 0),
            started_tick=100,
            source_worker_id=worker_id,
            covered_cells={(4, 0), (5, 0)},
            unit_goals={UUID(int=203): (12, 0)},
        )

        restored = agent.AgentMemory.restore(memory.persistent_state())

        mission = restored.squad_search_missions[1]
        self.assertEqual(mission.source_worker_id, worker_id)
        self.assertEqual(mission.covered_cells, {(4, 0), (5, 0)})
        self.assertEqual(mission.unit_goals[UUID(int=203)], (12, 0))


class ResourceMemoryTests(AgentTestCase):
    def test_resource_pool_keeps_radius_36_and_prunes_radius_37(self):
        inside = (36, 36)
        outside = (37, 0)
        worker = controlled_unit(100, UnitType.WORKER, (0, 1))
        memory = agent.AgentMemory(
            known_resources={inside, outside},
            worker_resource_target={worker.id: outside},
            resource_deferred_until={outside: 200},
        )

        _, _, memory = self.plan(
            make_turn(
                [worker],
                resource_cells=(inside, outside),
            ),
            memory,
        )

        self.assertIn(inside, memory.known_resources)
        self.assertNotIn(outside, memory.known_resources)
        self.assertNotEqual(memory.worker_resource_target.get(worker.id), outside)
        self.assertNotIn(outside, memory.resource_deferred_until)

    def test_ring_worker_can_claim_resources_seen_beyond_radius_32(self):
        first = controlled_unit(100, UnitType.WORKER, (32, 0))
        second = controlled_unit(101, UnitType.WORKER, (32, 1))
        resources = ((35, 0), (36, 1))

        _, _, memory = self.plan(
            make_turn([first, second], resource_cells=resources),
        )

        self.assertEqual(memory.known_resources, set(resources))
        self.assertEqual(
            set(memory.worker_resource_target.values()),
            set(resources),
        )

    def test_twelve_workers_expand_resource_memory_to_radius_48(self):
        inside = (48, 0)
        outside = (49, 0)

        _, _, memory = self.plan(
            make_turn(population_workers(12), resource_cells=(inside, outside)),
        )

        self.assertIn(inside, memory.known_resources)
        self.assertNotIn(outside, memory.known_resources)

    def test_core_relocation_prunes_resources_outside_new_roam_square(self):
        resource = (36, 0)
        worker = controlled_unit(100, UnitType.WORKER, (0, 1))
        memory = agent.AgentMemory(known_resources={resource})

        self.plan(make_turn([worker], resource_cells=(resource,)), memory)
        self.assertIn(resource, memory.known_resources)

        self.plan(
            make_turn([worker], tick=101, core_position=(100, 0)),
            memory,
        )
        self.assertNotIn(resource, memory.known_resources)


class RunnerErrorTests(unittest.TestCase):
    """官方命令错误矩阵的 Runner 分流。"""

    def test_submission_errors_follow_official_dispositions(self):
        cases = (
            (409, "COMMAND_WINDOW_CLOSED", "SKIP_TICK"),
            (409, "TICK_MISMATCH", "SKIP_TICK"),
            (429, "COMMAND_RATE_LIMITED", "SKIP_TICK"),
            (429, "COMMAND_CONCURRENCY_LIMIT", "SKIP_TICK"),
            (503, "TICK_NOT_READY", "SKIP_TICK"),
            (500, "INTERNAL_ERROR", "RESTART_SESSION"),
            (409, "IDEMPOTENCY_CONFLICT", "FATAL"),
            (422, "INVALID_COMMAND", "FATAL"),
            (401, "UNAUTHORIZED", "FATAL"),
        )

        for status_code, error_code, expected in cases:
            with self.subTest(error=error_code):
                error = agent.APIError(
                    status_code=status_code,
                    error=error_code,
                )
                self.assertEqual(
                    agent.submission_error_disposition(error),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
