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
    PlayerState,
    PlayerStatus,
    TerrainView,
    UnitType,
    UnitView,
)
from arena_hero.actions import (
    MoveAction,
    PickupBeaconAction,
    ShootAction,
    SpawnAction,
    SweepAction,
    WaitAction,
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
):
    kwargs = {
        "kind": "UNIT",
        "id": UUID(int=unit_id),
        "controlled": True,
        "position": position,
        "hp": 10,
        "unit_type": unit_type,
    }
    if unit_type is UnitType.WORKER:
        kwargs["cargo"] = cargo
    return UnitView(**kwargs)


def enemy_unit(unit_id: int, unit_type: UnitType, position: tuple[int, int]):
    return UnitView(
        kind="UNIT",
        id=UUID(int=unit_id),
        controlled=False,
        position=position,
        hp=10,
        unit_type=unit_type,
    )


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
):
    core = CoreView(
        kind="CORE",
        id=CORE_ID,
        controlled=True,
        owner_username="tester",
        position=core_position,
        hp=100,
        shield=10,
        state=CoreState.NORMAL,
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
        population_tier=0,
        upkeep_next_tick=0,
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
    """为人口上限测试创建互不重叠的 Worker。"""
    return [
        controlled_unit(
            start_id + index,
            UnitType.WORKER,
            (index % 8, 10 + index // 8),
        )
        for index in range(count)
    ]


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

    def test_core_visibility_cleans_stale_enemy_core_with_no_units(self):
        enemy_id = UUID(int=400)
        memory = agent.AgentMemory(
            known_enemy_cores={enemy_id: ((5, 0), 99)},
        )

        self.plan(make_turn([], core_position=(0, 0)), memory)

        self.assertNotIn(enemy_id, memory.known_enemy_cores)


class ProductionTests(AgentTestCase):
    """基础生产顺序和自动人口上限。"""

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

    def test_control_production_restores_home_vanguard_first(self):
        units = [
            *workers(5),
            controlled_unit(200, UnitType.RANGER, (0, 1)),
        ]
        plan, actions, _ = self.plan(make_turn(units, resources=10))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)
        self.assertTrue(any("restore home Vanguard" in action for action in actions))

    def test_control_production_restores_home_ranger_second(self):
        units = [
            *workers(5),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
        ]
        plan, _, _ = self.plan(make_turn(units, resources=12))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.RANGER)

    def test_control_expands_workers_before_roaming_vanguard(self):
        units = [
            *workers(7),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.RANGER, (1, 0)),
        ]
        plan, _, _ = self.plan(make_turn(units, resources=10))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.WORKER)

    def test_automatic_population_stops_at_nineteen(self):
        units = population_workers(19)
        for mode in ("control", "harvest"):
            turn = make_turn(units, resources=100, tick=200, core_position=(0, 0))
            actions, reached = agent.plan_turn(
                turn,
                agent.AgentMemory(),
                target=200,
                mode=mode,
            )
            self.assertFalse(reached)
            self.assertIsNone(turn.plan.core_action)
            self.assertFalse(any("core spawn" in action for action in actions))

    def test_automatic_population_can_reach_but_not_exceed_nineteen(self):
        turn = make_turn(population_workers(18), resources=10)
        plan, _, _ = self.plan(turn)
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)


class WorkerTests(AgentTestCase):
    """Worker 状态、Core 迁移和资源任务生命周期。"""

    def test_core_relocation_rebases_cached_exploration_goals(self):
        units = [
            controlled_unit(100, UnitType.WORKER, (8, 0)),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (20, 0)),
            controlled_unit(203, UnitType.RANGER, (1, 0)),
        ]
        memory = agent.AgentMemory()
        self.plan(make_turn(units, tick=100, core_position=(0, 0)), memory)
        old_worker_goal = memory.scout_goal[UUID(int=100)]
        old_roam_goal = memory.roam_goal[UUID(int=202)]

        self.plan(make_turn(units, tick=101, core_position=(10, 0)), memory)
        new_worker_goal = memory.scout_goal[UUID(int=100)]
        new_roam_goal = memory.roam_goal[UUID(int=202)]

        self.assertNotEqual(new_worker_goal, old_worker_goal)
        self.assertNotEqual(new_roam_goal, old_roam_goal)
        self.assertEqual(memory.home_vanguard_id, UUID(int=201))
        self.assertEqual(memory.home_ranger_id, UUID(int=203))

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


class PressureProductionTests(AgentTestCase):
    """资源充足时的巡逻编成生产规则。"""

    def test_complete_composition_banks_core(self):
        units = [
            *workers(8),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.RANGER, (1, 0)),
        ]
        plan, _, _ = self.plan(make_turn(units, resources=40))
        self.assertIsNone(plan.core_action)

    def test_ninety_percent_capacity_adds_roaming_vanguard(self):
        units = [
            *workers(8),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.RANGER, (1, 0)),
        ]
        plan, actions, _ = self.plan(make_turn(units, resources=50))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)
        self.assertTrue(any("core pressure 50/55" in action for action in actions))

    def test_pressure_ratio_buys_ranger_after_two_roaming_vanguards(self):
        units = [
            *workers(8),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.VANGUARD, (11, 0)),
            controlled_unit(205, UnitType.RANGER, (1, 0)),
        ]
        plan, _, _ = self.plan(make_turn(units, resources=59))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.RANGER)


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


class DefensiveProductionTests(AgentTestCase):
    """巡逻比例延续和家园紧急生产规则。"""

    def test_pressure_ratio_returns_to_vanguard_after_roaming_ranger(self):
        units = [
            *workers(8),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.VANGUARD, (11, 0)),
            controlled_unit(204, UnitType.VANGUARD, (12, 0)),
            controlled_unit(205, UnitType.RANGER, (1, 0)),
            controlled_unit(206, UnitType.RANGER, (10, 1)),
        ]
        plan, _, _ = self.plan(make_turn(units, resources=63))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)

    def test_emergency_vanguards_are_capped_at_two(self):
        units = [
            *workers(8),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (8, 0)),
            controlled_unit(203, UnitType.VANGUARD, (8, 1)),
            controlled_unit(204, UnitType.VANGUARD, (8, 2)),
            controlled_unit(205, UnitType.RANGER, (1, 0)),
        ]
        enemies = [
            enemy_unit(300 + index, UnitType.VANGUARD, (4, index)) for index in range(6)
        ]
        plan, _, _ = self.plan(make_turn(units, enemies=enemies, resources=50))
        self.assertIsNone(plan.core_action)

    def test_outnumbered_home_spawns_emergency_vanguard(self):
        units = [
            *workers(8),
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.RANGER, (1, 0)),
        ]
        enemies = [
            enemy_unit(300, UnitType.VANGUARD, (4, 0)),
            enemy_unit(301, UnitType.VANGUARD, (4, 1)),
            enemy_unit(302, UnitType.RANGER, (4, 2)),
            enemy_unit(303, UnitType.RANGER, (5, 2)),
        ]
        plan, actions, _ = self.plan(make_turn(units, enemies=enemies, resources=10))
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)
        self.assertTrue(any("emergency gap=" in action for action in actions))


class CombatTests(AgentTestCase):
    """Core 保护、家园防守、巡逻追击和协助拦截。"""

    def test_enemy_core_is_never_attacked(self):
        units = [controlled_unit(201, UnitType.VANGUARD, (0, 0))]
        turn = make_turn(units, enemies=[enemy_core(400, (1, 0))])
        plan, _, _ = self.plan(turn)
        self.assertNotIsInstance(plan.unit_actions[UUID(int=201)], SweepAction)

    def test_aggressive_roaming_vanguard_sweeps_visible_core(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 0)),
            controlled_unit(202, UnitType.VANGUARD, (1, 2)),
            controlled_unit(203, UnitType.VANGUARD, (6, 0)),
            controlled_unit(204, UnitType.RANGER, (0, 1)),
            controlled_unit(205, UnitType.RANGER, (0, 3)),
        ]
        memory = agent.AgentMemory(
            home_vanguard_id=UUID(int=201),
            home_ranger_id=UUID(int=204),
        )
        plan, actions, _ = self.plan(
            make_turn(units, enemies=[enemy_core(400, (2, 2))]),
            memory,
        )

        self.assertIsInstance(
            plan.unit_actions[UUID(int=202)],
            SweepAction,
        )
        self.assertTrue(any("roam-core-sweep" in action for action in actions))

    def test_aggressive_roaming_ranger_prioritizes_visible_core(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (6, 0)),
            controlled_unit(203, UnitType.VANGUARD, (6, 1)),
            controlled_unit(204, UnitType.RANGER, (1, 0)),
            controlled_unit(205, UnitType.RANGER, (0, 0)),
        ]
        memory = agent.AgentMemory(
            home_vanguard_id=UUID(int=201),
            home_ranger_id=UUID(int=204),
        )
        core = enemy_core(400, (2, 2))
        plan, actions, _ = self.plan(
            make_turn(
                units,
                enemies=[core, enemy_unit(401, UnitType.VANGUARD, (2, 2))],
            ),
            memory,
        )

        ranger_action = plan.unit_actions[UUID(int=205)]
        self.assertIsInstance(ranger_action, ShootAction)
        self.assertEqual(ranger_action.target_id, core.id)
        self.assertTrue(any("roam-core-shoot" in action for action in actions))

    def test_aggressive_core_priority_keeps_outnumbered_retreat(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 0)),
            controlled_unit(202, UnitType.VANGUARD, (1, 2)),
            controlled_unit(203, UnitType.VANGUARD, (6, 0)),
            controlled_unit(204, UnitType.RANGER, (0, 1)),
            controlled_unit(205, UnitType.RANGER, (0, 3)),
        ]
        enemies = [
            enemy_core(400, (2, 2)),
            enemy_unit(401, UnitType.VANGUARD, (1, 1)),
            enemy_unit(402, UnitType.VANGUARD, (1, 3)),
            enemy_unit(403, UnitType.RANGER, (2, 1)),
            enemy_unit(404, UnitType.RANGER, (2, 3)),
        ]
        memory = agent.AgentMemory(
            home_vanguard_id=UUID(int=201),
            home_ranger_id=UUID(int=204),
        )

        plan, actions, _ = self.plan(make_turn(units, enemies=enemies), memory)

        self.assertNotIsInstance(
            plan.unit_actions[UUID(int=202)],
            SweepAction,
        )
        self.assertFalse(any("roam-core" in action for action in actions))
        self.assertTrue(any("roam-retreat" in action for action in actions))

    def test_unit_sharing_enemy_core_cell_is_not_attacked(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 0)),
            controlled_unit(202, UnitType.RANGER, (0, -1)),
        ]
        enemies = [
            enemy_core(400, (1, 0)),
            enemy_unit(401, UnitType.WORKER, (1, 0)),
        ]
        plan, _, _ = self.plan(make_turn(units, enemies=enemies))
        self.assertNotIsInstance(plan.unit_actions[UUID(int=201)], SweepAction)
        self.assertNotIsInstance(plan.unit_actions[UUID(int=202)], ShootAction)

    def test_static_enemy_worker_is_hunted_by_roaming_vanguard(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.RANGER, (1, 0)),
        ]
        enemies = [enemy_unit(401, UnitType.WORKER, (11, 0))]
        plan, _, _ = self.plan(make_turn(units, enemies=enemies))
        self.assertIsInstance(plan.unit_actions[UUID(int=202)], SweepAction)

    def test_static_worker_in_diagonal_roam_area_is_not_filtered_out(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (25, 25)),
            controlled_unit(203, UnitType.RANGER, (1, 0)),
        ]
        enemies = [enemy_unit(401, UnitType.WORKER, (26, 25))]

        plan, actions, _ = self.plan(make_turn(units, enemies=enemies))

        self.assertIsInstance(plan.unit_actions[UUID(int=202)], SweepAction)
        self.assertTrue(any("roam-sweep WORKER" in action for action in actions))

    def test_enemy_core_diagonal_outskirts_are_valid_roam_goals(self):
        memory = agent.AgentMemory(
            known_enemy_cores={UUID(int=400): ((25, 25), 100)},
        )

        goal = memory.roam_goal_for(
            UUID(int=202),
            (0, 0),
            (0, 0),
            set(),
        )

        self.assertIn(goal, {(25, 21), (29, 25), (25, 29), (21, 25)})

    def test_roaming_ranger_can_reposition_in_diagonal_area(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (25, 24)),
            controlled_unit(203, UnitType.RANGER, (0, 2)),
            controlled_unit(204, UnitType.RANGER, (25, 25)),
        ]
        enemies = [enemy_unit(401, UnitType.VANGUARD, (28, 25))]
        memory = agent.AgentMemory(known_obstacles={(27, 25)})

        plan, actions, _ = self.plan(
            make_turn(units, enemies=enemies),
            memory,
        )

        self.assertTrue(hasattr(plan.unit_actions[UUID(int=204)], "direction"))
        self.assertTrue(any("roam-aim" in action for action in actions))

    def test_roaming_rangers_follow_distinct_vanguards(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.VANGUARD, (-10, 0)),
            controlled_unit(204, UnitType.RANGER, (1, 0)),
            controlled_unit(205, UnitType.RANGER, (6, 0)),
            controlled_unit(206, UnitType.RANGER, (-6, 0)),
        ]
        memory = agent.AgentMemory(
            home_vanguard_id=UUID(int=201),
            home_ranger_id=UUID(int=204),
        )

        plan, actions, memory = self.plan(make_turn(units), memory)

        assignments = {
            ranger_id: memory.ranger_follow_vanguard[ranger_id]
            for ranger_id in (UUID(int=205), UUID(int=206))
        }
        self.assertEqual(
            set(assignments.values()),
            {UUID(int=202), UUID(int=203)},
        )
        self.assertIsInstance(plan.unit_actions[UUID(int=205)], MoveAction)
        self.assertIsInstance(plan.unit_actions[UUID(int=206)], MoveAction)
        self.assertEqual(
            sum("roam-follow " in action for action in actions),
            2,
        )

        starts = {UUID(int=205): (6, 0), UUID(int=206): (-6, 0)}
        vanguard_starts = {UUID(int=202): (10, 0), UUID(int=203): (-10, 0)}
        for ranger_id, vanguard_id in assignments.items():
            vanguard_action = plan.unit_actions[vanguard_id]
            ranger_action = plan.unit_actions[ranger_id]
            self.assertIsInstance(vanguard_action, MoveAction)
            planned_vanguard = agent.add(
                vanguard_starts[vanguard_id],
                vanguard_action.direction.delta,
            )
            ranger_destination = agent.add(
                starts[ranger_id],
                ranger_action.direction.delta,
            )
            self.assertLess(
                agent.manhattan(ranger_destination, planned_vanguard),
                agent.manhattan(starts[ranger_id], planned_vanguard),
            )

    def test_ranger_follow_assignments_remain_stable(self):
        memory = agent.AgentMemory()
        first_rangers = (
            controlled_unit(205, UnitType.RANGER, (9, 0)),
            controlled_unit(206, UnitType.RANGER, (-9, 0)),
        )
        vanguards = (
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.VANGUARD, (-10, 0)),
        )
        initial = memory.assign_ranger_follow_targets(first_rangers, vanguards)

        moved_rangers = (
            controlled_unit(205, UnitType.RANGER, (-9, 0)),
            controlled_unit(206, UnitType.RANGER, (9, 0)),
        )
        updated = memory.assign_ranger_follow_targets(moved_rangers, vanguards)

        self.assertEqual(updated, initial)
        self.assertEqual(len(set(updated.values())), 2)

    def test_ranger_follow_targets_balance_when_vanguards_are_fewer(self):
        memory = agent.AgentMemory()
        rangers = tuple(
            controlled_unit(unit_id, UnitType.RANGER, (8 + unit_id - 205, 0))
            for unit_id in (205, 206, 207)
        )
        vanguards = (
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.VANGUARD, (-10, 0)),
        )

        assignments = memory.assign_ranger_follow_targets(rangers, vanguards)
        target_counts = sorted(
            list(assignments.values()).count(vanguard.id)
            for vanguard in vanguards
        )

        self.assertEqual(target_counts, [1, 2])

    def test_home_patrol_move_stays_inside_seven_by_seven_square(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 0)),
            controlled_unit(202, UnitType.RANGER, (0, 1)),
        ]
        plan, _, memory = self.plan(make_turn(units))
        for unit_id in (UUID(int=201), UUID(int=202)):
            action = plan.unit_actions[unit_id]
            if hasattr(action, "direction"):
                start = (0, 0) if unit_id.int == 201 else (0, 1)
                destination = agent.add(start, action.direction.delta)
                self.assertLessEqual(agent.chebyshev(destination, (0, 0)), 3)
        self.assertEqual(memory.home_vanguard_id, UUID(int=201))
        self.assertEqual(memory.home_ranger_id, UUID(int=202))

    def test_moving_worker_without_helper_is_not_chased(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.RANGER, (1, 0)),
        ]
        enemy = enemy_unit(401, UnitType.WORKER, (11, 0))
        memory = agent.AgentMemory()
        memory.enemy_worker_tracks[enemy.id] = agent.EnemyWorkerTrack(
            position=(10, 0),
            previous_position=(9, 0),
            first_seen_position=(9, 0),
            first_seen_tick=98,
            last_seen_tick=99,
            previous_seen_tick=98,
        )
        _, actions, _ = self.plan(
            make_turn(units, enemies=[enemy], tick=100),
            memory,
        )
        self.assertTrue(
            any(action.startswith("00000000 roam-patrol") for action in actions)
        )
        self.assertFalse(any("roam-hunt" in action for action in actions))

    def test_three_roamers_chase_moving_worker_without_helper(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.VANGUARD, (9, 1)),
            controlled_unit(204, UnitType.VANGUARD, (9, 2)),
            controlled_unit(205, UnitType.RANGER, (1, 0)),
        ]
        enemy = enemy_unit(401, UnitType.WORKER, (11, 0))
        memory = agent.AgentMemory()
        memory.enemy_worker_tracks[enemy.id] = agent.EnemyWorkerTrack(
            position=(10, 0),
            previous_position=(9, 0),
            first_seen_position=(9, 0),
            first_seen_tick=98,
            last_seen_tick=99,
            previous_seen_tick=98,
        )
        plan, actions, _ = self.plan(
            make_turn(units, enemies=[enemy], tick=100),
            memory,
        )
        self.assertTrue(any("roam-hunt" in action for action in actions))
        self.assertTrue(
            any(
                isinstance(plan.unit_actions[UUID(int=unit_id)], SweepAction)
                for unit_id in (202, 203, 204)
            )
        )

    def test_three_roamers_engage_equal_enemy_combat_force(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (9, 0)),
            controlled_unit(203, UnitType.VANGUARD, (9, 1)),
            controlled_unit(204, UnitType.VANGUARD, (9, 2)),
            controlled_unit(205, UnitType.RANGER, (1, 0)),
        ]
        enemies = [
            enemy_unit(401, UnitType.VANGUARD, (10, 0)),
            enemy_unit(402, UnitType.VANGUARD, (10, 1)),
            enemy_unit(403, UnitType.RANGER, (10, 2)),
        ]
        plan, _, _ = self.plan(make_turn(units, enemies=enemies, tick=100))
        self.assertTrue(
            any(
                isinstance(plan.unit_actions[UUID(int=unit_id)], SweepAction)
                for unit_id in (202, 203, 204)
            )
        )

    def test_chase_stops_after_eight_ticks_without_trap(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(203, UnitType.VANGUARD, (10, 1)),
            controlled_unit(204, UnitType.VANGUARD, (10, 2)),
            controlled_unit(205, UnitType.RANGER, (1, 0)),
        ]
        enemy = enemy_unit(401, UnitType.WORKER, (20, 0))
        memory = agent.AgentMemory()
        memory.enemy_worker_tracks[enemy.id] = agent.EnemyWorkerTrack(
            position=(19, 0),
            previous_position=(18, 0),
            first_seen_position=(18, 0),
            first_seen_tick=91,
            last_seen_tick=99,
            previous_seen_tick=98,
        )
        memory.roam_chase_started[enemy.id] = 92
        _, actions, memory = self.plan(
            make_turn(units, enemies=[enemy], tick=100),
            memory,
        )
        self.assertFalse(any("roam-hunt" in action for action in actions))
        self.assertGreater(memory.roam_chase_cooldown_until[enemy.id], 100)

    def test_confirmed_trap_extends_chase_to_sixteen_ticks(self):
        target_id = UUID(int=401)
        memory = agent.AgentMemory()
        memory.roam_chase_started[target_id] = 92
        self.assertTrue(memory.can_continue_roam_chase(target_id, 100, True))
        self.assertFalse(memory.can_continue_roam_chase(target_id, 108, True))

    def test_moving_worker_recruits_idle_explorer_to_intercept(self):
        worker_units = workers(8)
        worker_units[0] = controlled_unit(100, UnitType.WORKER, (11, 2))
        units = [
            *worker_units,
            controlled_unit(201, UnitType.VANGUARD, (0, 1)),
            controlled_unit(202, UnitType.VANGUARD, (9, 0)),
            controlled_unit(203, UnitType.RANGER, (1, 0)),
        ]
        enemy = enemy_unit(401, UnitType.WORKER, (11, 0))
        memory = agent.AgentMemory()
        memory.enemy_worker_tracks[enemy.id] = agent.EnemyWorkerTrack(
            position=(10, 0),
            previous_position=(9, 0),
            first_seen_position=(9, 0),
            first_seen_tick=98,
            last_seen_tick=99,
            previous_seen_tick=98,
        )
        _, actions, _ = self.plan(
            make_turn(units, enemies=[enemy], tick=100),
            memory,
        )
        self.assertTrue(any(" intercept " in action for action in actions))

    def test_enemy_worker_track_expires_after_three_unseen_ticks(self):
        enemy_id = UUID(int=401)
        memory = agent.AgentMemory()
        memory.enemy_worker_tracks[enemy_id] = agent.EnemyWorkerTrack(
            position=(10, 0),
            previous_position=None,
            first_seen_position=(10, 0),
            first_seen_tick=100,
            last_seen_tick=100,
            previous_seen_tick=100,
        )
        memory.observe_enemy_workers((), 102)
        self.assertIn(enemy_id, memory.enemy_worker_tracks)
        memory.observe_enemy_workers((), 103)
        self.assertNotIn(enemy_id, memory.enemy_worker_tracks)


class ExpeditionTests(AgentTestCase):
    """冠军信标远征的触发、编队、战斗、返航和补员。"""

    @staticmethod
    def full_roster():
        return [
            *workers(8),
            *(
                controlled_unit(
                    201 + index,
                    UnitType.VANGUARD,
                    (10 + index, index % 2),
                )
                for index in range(7)
            ),
            *(
                controlled_unit(
                    301 + index,
                    UnitType.RANGER,
                    (10 + index, 3),
                )
                for index in range(4)
            ),
        ]

    @staticmethod
    def role_memory():
        return agent.AgentMemory(
            home_vanguard_id=UUID(int=201),
            home_ranger_id=UUID(int=301),
        )

    def test_expedition_launch_requires_nineteen_population_and_over_fifty(self):
        roster = self.full_roster()
        memory = self.role_memory()
        self.plan(make_turn(roster[:-1], resources=51), memory)
        self.assertFalse(memory.expedition_active)

        memory = self.role_memory()
        self.plan(make_turn(roster, resources=50), memory)
        self.assertFalse(memory.expedition_active)

        memory = self.role_memory()
        _, actions, memory = self.plan(make_turn(roster, resources=51), memory)
        self.assertTrue(memory.expedition_active)
        self.assertEqual(len(memory.expedition_vanguard_ids), 2)
        self.assertEqual(len(memory.expedition_ranger_ids), 1)
        self.assertNotIn(memory.home_vanguard_id, memory.expedition_unit_ids)
        self.assertNotIn(memory.home_ranger_id, memory.expedition_unit_ids)
        self.assertTrue(any("expedition-" in action for action in actions))

        restored = agent.AgentMemory.restore(memory.persistent_state())
        self.assertEqual(restored.persistent_state()["version"], 3)
        self.assertEqual(restored.expedition_unit_ids, memory.expedition_unit_ids)
        self.assertEqual(restored.expedition_leader_id, memory.expedition_leader_id)

    def test_expedition_leader_waits_for_member_beyond_formation_radius(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (0, 0)),
            controlled_unit(203, UnitType.VANGUARD, (6, 0)),
            controlled_unit(301, UnitType.RANGER, (0, -1)),
            controlled_unit(302, UnitType.RANGER, (1, 0)),
        ]
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_vanguard_ids = {UUID(int=202), UUID(int=203)}
        memory.expedition_ranger_ids = {UUID(int=302)}
        memory.expedition_leader_id = UUID(int=202)

        plan, actions, _ = self.plan(
            make_turn(units, beacon_position=(20, 0)),
            memory,
        )

        self.assertIsInstance(plan.unit_actions[UUID(int=202)], WaitAction)
        self.assertIsInstance(plan.unit_actions[UUID(int=203)], MoveAction)
        self.assertTrue(any("expedition-follow" in action for action in actions))

    def test_expedition_attacks_without_outnumbered_retreat(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (0, 0)),
            controlled_unit(203, UnitType.VANGUARD, (0, 1)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
            controlled_unit(302, UnitType.RANGER, (0, 2)),
        ]
        enemies = [enemy_unit(401, UnitType.WORKER, (1, 0))]
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_vanguard_ids = {UUID(int=202), UUID(int=203)}
        memory.expedition_ranger_ids = {UUID(int=302)}
        memory.expedition_leader_id = UUID(int=202)

        plan, actions, _ = self.plan(make_turn(units, enemies=enemies), memory)

        self.assertIsInstance(plan.unit_actions[UUID(int=202)], SweepAction)
        self.assertTrue(any("expedition-sweep" in action for action in actions))
        self.assertFalse(any("retreat" in action for action in actions))

    def test_expedition_ranger_shoots_and_prioritizes_beacon_carrier(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (0, 0)),
            controlled_unit(203, UnitType.VANGUARD, (1, 1)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
            controlled_unit(302, UnitType.RANGER, (0, 1)),
        ]
        carrier = enemy_unit(401, UnitType.VANGUARD, (0, 4))
        nearby_worker = enemy_unit(402, UnitType.WORKER, (1, 0))
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_vanguard_ids = {UUID(int=202), UUID(int=203)}
        memory.expedition_ranger_ids = {UUID(int=302)}
        memory.expedition_leader_id = UUID(int=202)

        plan, actions, _ = self.plan(
            make_turn(
                units,
                enemies=[carrier, nearby_worker],
                beacon_position=(0, 4),
                beacon_status=BeaconStatus.CARRIED,
                beacon_carrier_id=carrier.id,
            ),
            memory,
        )

        ranger_action = plan.unit_actions[UUID(int=302)]
        self.assertIsInstance(ranger_action, ShootAction)
        self.assertEqual(ranger_action.target_id, carrier.id)
        self.assertTrue(any("expedition-shoot" in action for action in actions))

    def test_expedition_stops_chasing_same_target_after_eight_ticks(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (0, 0)),
            controlled_unit(203, UnitType.VANGUARD, (0, 1)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
            controlled_unit(302, UnitType.RANGER, (1, 0)),
        ]
        enemy = enemy_unit(401, UnitType.VANGUARD, (5, 0))
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_vanguard_ids = {UUID(int=202), UUID(int=203)}
        memory.expedition_ranger_ids = {UUID(int=302)}
        memory.expedition_leader_id = UUID(int=202)
        memory.expedition_target_id = enemy.id
        memory.expedition_chase_started_tick = 92

        _, actions, memory = self.plan(
            make_turn(
                units,
                enemies=[enemy],
                tick=100,
                beacon_position=(0, 20),
            ),
            memory,
        )

        self.assertFalse(any("expedition-engage" in action for action in actions))
        self.assertGreater(memory.expedition_chase_cooldown_until[enemy.id], 100)

    def test_expedition_picks_up_ground_beacon_and_returns_home(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (5, 0)),
            controlled_unit(203, UnitType.VANGUARD, (5, 1)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
            controlled_unit(302, UnitType.RANGER, (4, 0)),
        ]
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_vanguard_ids = {UUID(int=202), UUID(int=203)}
        memory.expedition_ranger_ids = {UUID(int=302)}
        memory.expedition_leader_id = UUID(int=202)

        plan, _, memory = self.plan(
            make_turn(
                units,
                beacon_position=(5, 0),
                beacon_status=BeaconStatus.GROUND,
            ),
            memory,
        )
        self.assertIsInstance(
            plan.unit_actions[UUID(int=202)],
            PickupBeaconAction,
        )

        plan, actions, _ = self.plan(
            make_turn(
                units,
                tick=101,
                beacon_position=(5, 0),
                beacon_status=BeaconStatus.CARRIED,
                beacon_carrier_id=UUID(int=202),
            ),
            memory,
        )
        self.assertIsInstance(plan.unit_actions[UUID(int=202)], MoveAction)
        self.assertTrue(any("expedition-lead" in action for action in actions))

    def test_beacon_carrier_at_core_becomes_persistent_keeper(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (0, 0)),
            controlled_unit(203, UnitType.VANGUARD, (0, 1)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
            controlled_unit(302, UnitType.RANGER, (1, 0)),
        ]
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_vanguard_ids = {UUID(int=202), UUID(int=203)}
        memory.expedition_ranger_ids = {UUID(int=302)}
        memory.expedition_leader_id = UUID(int=202)

        plan, _, memory = self.plan(
            make_turn(
                units,
                beacon_position=(0, 0),
                beacon_status=BeaconStatus.CARRIED,
                beacon_carrier_id=UUID(int=202),
            ),
            memory,
        )

        self.assertIsInstance(plan.unit_actions[UUID(int=202)], WaitAction)
        self.assertFalse(memory.expedition_active)
        self.assertEqual(memory.beacon_keeper_id, UUID(int=202))
        self.assertEqual(memory.expedition_unit_ids, set())

    def test_beacon_keeper_follows_relocated_core(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (0, 0)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
        ]
        memory = self.role_memory()
        memory.beacon_keeper_id = UUID(int=202)

        plan, actions, _ = self.plan(
            make_turn(
                units,
                core_position=(2, 0),
                beacon_position=(0, 0),
                beacon_status=BeaconStatus.CARRIED,
                beacon_carrier_id=UUID(int=202),
            ),
            memory,
        )

        self.assertIsInstance(plan.unit_actions[UUID(int=202)], MoveAction)
        self.assertTrue(any("beacon-keeper" in action for action in actions))

    def test_dropped_beacon_near_home_uses_one_local_recovery_unit(self):
        units = [
            *workers(8),
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (1, 0)),
            controlled_unit(203, UnitType.VANGUARD, (8, 0)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
            controlled_unit(302, UnitType.RANGER, (7, 0)),
        ]
        memory = self.role_memory()
        memory.beacon_keeper_id = UUID(int=999)

        plan, actions, memory = self.plan(
            make_turn(
                units,
                resources=60,
                beacon_position=(1, 0),
                beacon_status=BeaconStatus.GROUND,
            ),
            memory,
        )

        self.assertTrue(memory.expedition_active)
        self.assertTrue(memory.expedition_home_recovery)
        self.assertEqual(memory.expedition_unit_ids, {UUID(int=202)})
        self.assertIsInstance(
            plan.unit_actions[UUID(int=202)],
            PickupBeaconAction,
        )
        self.assertFalse(any("expedition reinforcement" in action for action in actions))

    def test_ground_beacon_near_home_does_not_bypass_launch_threshold(self):
        units = self.full_roster()[:-1]
        memory = self.role_memory()

        plan, _, memory = self.plan(
            make_turn(
                units,
                resources=50,
                beacon_position=tuple(units[8].position),
                beacon_status=BeaconStatus.GROUND,
            ),
            memory,
        )

        self.assertFalse(memory.expedition_active)
        self.assertFalse(
            any(
                isinstance(action, PickupBeaconAction)
                for action in plan.unit_actions.values()
            )
        )

    def test_lost_home_beacon_relaunches_full_expedition_when_ready(self):
        units = self.full_roster()
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_home_recovery = True
        memory.expedition_vanguard_ids = {UUID(int=202)}
        memory.expedition_leader_id = UUID(int=202)

        plan, actions, memory = self.plan(
            make_turn(
                units,
                resources=60,
                beacon_position=(4, 0),
                beacon_status=BeaconStatus.GROUND,
            ),
            memory,
        )

        self.assertFalse(memory.expedition_home_recovery)
        self.assertTrue(memory.expedition_active)
        self.assertEqual(len(memory.expedition_vanguard_ids), 2)
        self.assertEqual(len(memory.expedition_ranger_ids), 1)
        self.assertIsNone(plan.core_action)
        self.assertTrue(any("expedition-" in action for action in actions))

    def test_home_recovery_waits_for_launch_conditions_when_beacon_is_lost(self):
        units = [
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (4, 0)),
            controlled_unit(203, UnitType.VANGUARD, (8, 0)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
            controlled_unit(302, UnitType.RANGER, (7, 0)),
        ]
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_home_recovery = True
        memory.expedition_vanguard_ids = {UUID(int=202)}
        memory.expedition_leader_id = UUID(int=202)

        _, _, memory = self.plan(
            make_turn(
                units,
                resources=50,
                beacon_position=(4, 0),
                beacon_status=BeaconStatus.GROUND,
            ),
            memory,
        )

        self.assertFalse(memory.expedition_active)
        self.assertFalse(memory.expedition_home_recovery)
        self.assertEqual(memory.expedition_unit_ids, set())

    def test_expedition_reinforcement_requires_sixty_resources(self):
        units = [
            *workers(8),
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(202, UnitType.VANGUARD, (10, 0)),
            controlled_unit(204, UnitType.VANGUARD, (12, 0)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
            controlled_unit(302, UnitType.RANGER, (10, 1)),
            controlled_unit(303, UnitType.RANGER, (12, 1)),
        ]
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_vanguard_ids = {UUID(int=202)}
        memory.expedition_ranger_ids = {UUID(int=302)}
        memory.expedition_leader_id = UUID(int=202)

        plan, actions, memory = self.plan(make_turn(units, resources=59), memory)
        self.assertFalse(any("expedition reinforcement" in action for action in actions))

        plan, actions, memory = self.plan(
            make_turn(units, resources=60, tick=101),
            memory,
        )
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)
        self.assertTrue(any("expedition reinforcement" in action for action in actions))
        self.assertNotIn(UUID(int=204), memory.expedition_unit_ids)

        recruited_units = [
            *units,
            controlled_unit(999, UnitType.VANGUARD, (0, 1)),
        ]
        self.plan(make_turn(recruited_units, resources=50, tick=102), memory)
        self.assertIn(UUID(int=999), memory.expedition_vanguard_ids)
        self.assertIn(UUID(int=999), memory.expedition_reinforcement_ids)
        self.assertNotIn(UUID(int=204), memory.expedition_unit_ids)

    def test_fully_lost_expedition_rebuilds_from_core(self):
        units = [
            *workers(8),
            controlled_unit(201, UnitType.VANGUARD, (-1, 0)),
            controlled_unit(204, UnitType.VANGUARD, (12, 0)),
            controlled_unit(301, UnitType.RANGER, (-1, 1)),
            controlled_unit(303, UnitType.RANGER, (12, 1)),
        ]
        memory = self.role_memory()
        memory.expedition_active = True
        memory.expedition_vanguard_ids = {UUID(int=202), UUID(int=203)}
        memory.expedition_ranger_ids = {UUID(int=302)}
        memory.expedition_leader_id = UUID(int=202)

        plan, actions, memory = self.plan(
            make_turn(units, resources=60),
            memory,
        )

        self.assertTrue(memory.expedition_assembling)
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.VANGUARD)
        self.assertTrue(any("expedition reinforcement" in action for action in actions))


class ExpeditionResourceTests(AgentTestCase):
    def test_resource_pool_keeps_radius_32_and_prunes_radius_33(self):
        inside = (32, 32)
        outside = (33, 0)
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

    def test_core_relocation_prunes_resources_outside_new_roam_square(self):
        resource = (32, 0)
        worker = controlled_unit(100, UnitType.WORKER, (0, 1))
        memory = agent.AgentMemory(known_resources={resource})

        self.plan(make_turn([worker], resource_cells=(resource,)), memory)
        self.assertIn(resource, memory.known_resources)

        self.plan(
            make_turn([worker], tick=101, core_position=(100, 0)),
            memory,
        )
        self.assertNotIn(resource, memory.known_resources)


if __name__ == "__main__":
    unittest.main()
