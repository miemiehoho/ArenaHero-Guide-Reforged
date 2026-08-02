from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from arena_hero import (
    ChampionBeacon,
    CoreState,
    CoreView,
    PlayerState,
    PlayerStatus,
    UnitType,
    UnitView,
)
from arena_hero.actions import ShootAction, SpawnAction, SweepAction
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
    objects = (core, *units, *enemies)
    state = PlayerState(
        status=PlayerStatus.ACTIVE,
        resources=resources,
        population=len(units),
        population_tier=0,
        upkeep_next_tick=0,
        champion_beacon=ChampionBeacon(position=(100, 100), status=None),
        objects=objects,
        events=(),
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
    """Ranger 的 v0.7 射线规则。"""

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


if __name__ == "__main__":
    unittest.main()
