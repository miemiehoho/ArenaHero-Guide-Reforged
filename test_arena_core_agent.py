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
    ResolutionEvent,
    TerrainView,
    UnitType,
    UnitView,
)
from arena_hero.actions import (
    DepositAction,
    MoveAction,
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
        units = [
            *workers(4),
            *(controlled_unit(200 + index, UnitType.VANGUARD, (index, 5)) for index in range(10)),
            *(controlled_unit(300 + index, UnitType.RANGER, (index, 7)) for index in range(4)),
        ]
        turn = make_turn(units, resources=12)
        plan, _, _ = self.plan(turn)
        self.assertIsInstance(plan.core_action, SpawnAction)
        self.assertIs(plan.core_action.unit_type, UnitType.RANGER)


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
        self.assertEqual(restored.persistent_state()["version"], 6)
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
                enemies=[enemy_unit(401, UnitType.WORKER, (20, 2))],
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

    def test_enemy_core_discovery_gathers_all_field_squads_before_attack(self):
        _, actions, memory = self.plan(
            make_turn(self.roster(), enemies=[enemy_core(400, (20, 0))]),
            self.squad_memory(),
        )

        self.assertTrue(memory.assault_gathering)
        self.assertIsNotNone(memory.assault_rally_position)
        self.assertTrue(any("squad-gather" in action and "team=1" in action for action in actions))
        self.assertTrue(any("squad-gather" in action and "team=2" in action for action in actions))
        self.assertFalse(any("squad-assault-sweep" in action for action in actions))

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
            make_turn(units, enemies=[enemy_core(400, (20, 0))]),
            self.squad_memory(),
        )

        self.assertFalse(memory.assault_gathering)
        self.assertTrue(any("squads assault-ready" in action for action in actions))
        self.assertTrue(any("squad-assault" in action and "team=1" in action for action in actions))
        self.assertTrue(any("squad-assault" in action and "team=2" in action for action in actions))

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

    def test_enemy_outside_roam_boundary_does_not_trigger_gather(self):
        _, actions, memory = self.plan(
            make_turn(self.roster(), enemies=[enemy_core(400, (25, 0))]),
            self.squad_memory(),
        )

        self.assertIsNone(memory.assault_target_id)
        self.assertFalse(memory.assault_gathering)
        self.assertFalse(any("squad-gather" in action for action in actions))

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


class ResourceMemoryTests(AgentTestCase):
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
