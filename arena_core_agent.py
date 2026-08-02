from __future__ import annotations

import argparse
from collections import Counter
import heapq
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable
from uuid import UUID

from arena_hero import (
    APIError,
    ArenaHeroClient,
    ArenaHeroError,
    AuthenticationError,
    ConfigurationError,
    CoreState,
    CoreView,
    Direction,
    InvalidActionError,
    MoveAction,
    PlayerStatus,
    PolicyViolationError,
    TransportError,
    UnitView,
    UnitType,
)
from arena_hero.turn import Core, Ranger, Turn, Vanguard, Worker


Pos = tuple[int, int]

DIRECTION_STEPS: tuple[tuple[Direction, Pos], ...] = (
    (Direction.UP, (0, -1)),
    (Direction.RIGHT, (1, 0)),
    (Direction.DOWN, (0, 1)),
    (Direction.LEFT, (-1, 0)),
)

SCOUT_VECTORS: tuple[Pos, ...] = (
    (1, 0),
    (1, 1),
    (0, 1),
    (-1, 1),
    (-1, 0),
    (-1, -1),
    (0, -1),
    (1, -1),
)

HOME_PATROL_OFFSETS: tuple[Pos, ...] = (
    (3, 0),
    (3, 3),
    (0, 3),
    (-3, 3),
    (-3, 0),
    (-3, -3),
    (0, -3),
    (3, -3),
)

HOME_PATROL_RADIUS = 3
HOME_ENGAGE_RADIUS = 12
HOME_VANGUARD_CHASE_RADIUS = 6
ROAM_RADIUS = 32
ROAM_CHASE_STEPS = 8
ROAM_TARGET_LOST_TICKS = 3
ROAM_HELPER_RADIUS = 5
MAX_EMERGENCY_VANGUARDS = 2
CORE_PRESSURE_NUMERATOR = 9
CORE_PRESSURE_DENOMINATOR = 10
ROAM_AGGRESSIVE_SIZE = 3
ROAM_CHASE_TICKS = 8
ROAM_TRAP_CHASE_TICKS = 16
ROAM_CHASE_COOLDOWN_TICKS = 12
ROAM_TRAP_ALLY_RADIUS = 5
ROAM_TRAP_MAX_EXITS = 2
TARGET_WORKERS_CONTROL = 8
TARGET_HOME_VANGUARDS = 1
TARGET_ROAM_VANGUARDS = 1
# 巡逻编成保持两个 Vanguard 配一个 Ranger，再继续补充 Ranger。
ROAM_VANGUARDS_PER_RANGER = 2
# 19 是无需维护费的最后一个人口档；自动生产不得进入收费区间。
# 用户仍可通过手动计划显式增加人口。
MAX_AUTO_POPULATION = 19
# 官方视野半径按对象类型分别计算；资源与敌方 Core 的过期清理共用这套规则。
CORE_VISION_RADIUS = 5
UNIT_VISION_RADII: dict[UnitType, int] = {
    UnitType.WORKER: 3,
    UnitType.VANGUARD: 4,
    UnitType.RANGER: 5,
}
TEMPORARY_BLOCK_TICKS = 8
RESOURCE_REASSIGN_MIN_GAIN = 4
# 距离仍是资源匹配的主要成本，同时用历史负载打散连续任务。
RESOURCE_DISTANCE_COST = 10
RESOURCE_LOAD_COST = 3
COMBAT_THREAT_MEMORY_TICKS = 6
STATE_SAVE_INTERVAL_TICKS = 10
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 4

STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".arena_core_state.json",
)
STATE_TEMP_PATH = f"{STATE_PATH}.tmp"
LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "arena_core_agent.rotating.log",
)


def load_local_env() -> None:
    """读取项目目录下 .env 中简单的 KEY=VALUE 配置。"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        return


def load_persistent_state() -> dict:
    """读取最近一次原子保存的 Agent 状态，失败时返回空状态。"""
    try:
        with open(STATE_PATH, encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return state if isinstance(state, dict) else {}


def decode_positions(raw_positions) -> set[Pos]:
    """从持久化 JSON 中解析合法的二维整数坐标。"""
    positions: set[Pos] = set()
    if not isinstance(raw_positions, list):
        return positions
    for raw_position in raw_positions:
        if (
            isinstance(raw_position, list)
            and len(raw_position) == 2
            and all(isinstance(value, int) for value in raw_position)
        ):
            positions.add((raw_position[0], raw_position[1]))
    return positions


def save_state(state: dict) -> None:
    """原子写入 Agent 状态，避免留下半写入文件。"""
    with open(STATE_TEMP_PATH, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, ensure_ascii=False, indent=2)
        state_file.write("\n")
    os.replace(STATE_TEMP_PATH, STATE_PATH)


def add(a: Pos, b: Pos) -> Pos:
    return a[0] + b[0], a[1] + b[1]


def manhattan(a: Pos, b: Pos) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def chebyshev(a: Pos, b: Pos) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def supercover_line(start: Pos, target: Pos) -> tuple[Pos, ...]:
    """返回整数 supercover 直线经过的格子，包含起点和终点。"""
    x, y = start
    target_x, target_y = target
    delta_x = abs(target_x - x)
    delta_y = abs(target_y - y)
    step_x = 1 if target_x > x else -1
    step_y = 1 if target_y > y else -1
    covered = [(x, y)]
    progressed_x = 0
    progressed_y = 0

    while progressed_x < delta_x or progressed_y < delta_y:
        horizontal = (1 + 2 * progressed_x) * delta_y
        vertical = (1 + 2 * progressed_y) * delta_x
        if horizontal == vertical:
            # 射线正好穿过格角时，两侧格子都算经过，然后进入对角格。
            previous_x, previous_y = x, y
            x += step_x
            progressed_x += 1
            covered.append((x, y))
            covered.append((previous_x, previous_y + step_y))
            y += step_y
            progressed_y += 1
            covered.append((x, y))
        elif horizontal < vertical:
            x += step_x
            progressed_x += 1
            covered.append((x, y))
        else:
            y += step_y
            progressed_y += 1
            covered.append((x, y))
    return tuple(covered)


def visible_from(
    source: Pos,
    target: Pos,
    radius: int,
    obstacles: set[Pos],
) -> bool:
    """按官方 Manhattan 半径和 supercover 障碍规则判断视野。"""
    if manhattan(source, target) > radius:
        return False
    return not any(cell in obstacles for cell in supercover_line(source, target)[1:-1])


def any_vision_source_sees(
    target: Pos,
    sources: tuple[tuple[Pos, int], ...],
    obstacles: set[Pos],
) -> bool:
    return any(
        visible_from(source, target, radius, obstacles)
        for source, radius in sources
    )


def friendly_vision_sources(
    core: Core | None,
    units: Iterable[Worker | Vanguard | Ranger],
) -> tuple[tuple[Pos, int], ...]:
    """构造当前 Tick 存活己方 Core 与 Unit 的视野源。"""
    sources: list[tuple[Pos, int]] = []
    if core is not None:
        sources.append((tuple(core.position), CORE_VISION_RADIUS))
    for unit in units:
        radius = UNIT_VISION_RADII.get(unit.unit_type)
        if radius is not None:
            sources.append((tuple(unit.position), radius))
    return tuple(sources)


def direction_between(start: Pos, end: Pos) -> Direction | None:
    delta = end[0] - start[0], end[1] - start[1]
    for direction, step in DIRECTION_STEPS:
        if step == delta:
            return direction
    return None


def minimum_cost_resource_matching(
    workers,
    resources: set[Pos],
    worker_loads: dict[UUID, int] | None = None,
) -> list[tuple[UUID, Pos]]:
    """用最小成本为尽可能多的 Worker 匹配互不重复的资源。

    匈牙利匹配在 Worker 较多时仍保持多项式复杂度；旧的位掩码动态
    规划会随已知资源数量指数增长。
    """
    ordered_workers = tuple(sorted(workers, key=lambda worker: str(worker.id)))
    ordered_resources = tuple(sorted(resources))
    worker_loads = worker_loads or {}

    if not ordered_workers or not ordered_resources:
        return []

    def pair_cost(worker, resource: Pos) -> int:
        travel_distance = manhattan(tuple(worker.position), resource)
        load_penalty = worker_loads.get(worker.id, 0) * RESOURCE_LOAD_COST
        return travel_distance * RESOURCE_DISTANCE_COST + load_penalty

    # Hungarian 算法通常要求行数不超过列数。资源较少时转置矩阵，最后再
    # 将匹配结果还原成 (worker, resource)。
    rows_are_workers = len(ordered_workers) <= len(ordered_resources)
    rows = ordered_workers if rows_are_workers else ordered_resources
    columns = ordered_resources if rows_are_workers else ordered_workers
    row_count = len(rows)
    column_count = len(columns)

    potentials_rows = [0] * (row_count + 1)
    potentials_columns = [0] * (column_count + 1)
    matched_row_for_column = [0] * (column_count + 1)
    previous_column = [0] * (column_count + 1)

    for row_index in range(1, row_count + 1):
        matched_row_for_column[0] = row_index
        current_column = 0
        minimums = [sys.maxsize] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[current_column] = True
            matched_row = matched_row_for_column[current_column]
            delta = sys.maxsize
            next_column = 0
            for column_index in range(1, column_count + 1):
                if used[column_index]:
                    continue
                if rows_are_workers:
                    cost = pair_cost(
                        rows[matched_row - 1],
                        columns[column_index - 1],
                    )
                else:
                    cost = pair_cost(
                        columns[column_index - 1],
                        rows[matched_row - 1],
                    )
                reduced_cost = (
                    cost
                    - potentials_rows[matched_row]
                    - potentials_columns[column_index]
                )
                if reduced_cost < minimums[column_index]:
                    minimums[column_index] = reduced_cost
                    previous_column[column_index] = current_column
                if minimums[column_index] < delta:
                    delta = minimums[column_index]
                    next_column = column_index
            for column_index in range(column_count + 1):
                if used[column_index]:
                    potentials_rows[matched_row_for_column[column_index]] += delta
                    potentials_columns[column_index] -= delta
                else:
                    minimums[column_index] -= delta
            current_column = next_column
            if matched_row_for_column[current_column] == 0:
                break
        while True:
            prior = previous_column[current_column]
            matched_row_for_column[current_column] = matched_row_for_column[prior]
            current_column = prior
            if current_column == 0:
                break

    assignments: list[tuple[int, Pos]] = []
    for column_index in range(1, column_count + 1):
        row_index = matched_row_for_column[column_index]
        if row_index == 0:
            continue
        if rows_are_workers:
            assignments.append((row_index - 1, columns[column_index - 1]))
        else:
            assignments.append((column_index - 1, rows[row_index - 1]))
    assignments.sort(key=lambda item: (item[0], item[1]))
    return [
        (ordered_workers[worker_index].id, resource)
        for worker_index, resource in assignments
    ]


def ranger_line_distance(start: Pos, target: Pos) -> int | None:
    """返回 Ranger 合法直线距离；非横竖或精确 45 度斜线返回 None。"""
    delta_x = abs(target[0] - start[0])
    delta_y = abs(target[1] - start[1])
    if delta_x == 0 or delta_y == 0 or delta_x == delta_y:
        return max(delta_x, delta_y)
    return None


def clear_ranger_shot(start: Pos, target: Pos, blockers: set[Pos]) -> bool:
    """按 v0.8 规则判断 Ranger 是否能沿八方向无障碍射击目标。"""
    distance = ranger_line_distance(start, target)
    if distance is None or not 1 <= distance <= 3:
        return False
    delta_x = target[0] - start[0]
    delta_y = target[1] - start[1]
    step = (
        0 if delta_x == 0 else (1 if delta_x > 0 else -1),
        0 if delta_y == 0 else (1 if delta_y > 0 else -1),
    )
    return all(
        (
            start[0] + step[0] * offset,
            start[1] + step[1] * offset,
        )
        not in blockers
        for offset in range(1, distance)
    )


def first_step_astar(
    start: Pos,
    goal: Pos,
    obstacles: set[Pos],
    blocked: set[Pos],
    *,
    max_expansions: int = 5000,
) -> Pos | None:
    if start == goal:
        return start

    frontier: list[tuple[int, int, Pos]] = [(manhattan(start, goal), 0, start)]
    came_from: dict[Pos, Pos | None] = {start: None}
    best_cost: dict[Pos, int] = {start: 0}
    expansions = 0

    while frontier and expansions < max_expansions:
        _, cost, current = heapq.heappop(frontier)
        if cost != best_cost.get(current):
            continue
        expansions += 1
        if current == goal:
            break

        for _, delta in DIRECTION_STEPS:
            nxt = add(current, delta)
            if nxt in obstacles or (nxt in blocked and nxt != goal):
                continue
            new_cost = cost + 1
            if new_cost >= best_cost.get(nxt, sys.maxsize):
                continue
            best_cost[nxt] = new_cost
            came_from[nxt] = current
            heapq.heappush(
                frontier,
                (new_cost + manhattan(nxt, goal), new_cost, nxt),
            )

    if goal not in came_from:
        return None

    cursor = goal
    while came_from[cursor] != start:
        parent = came_from[cursor]
        if parent is None:
            return None
        cursor = parent
    return cursor


def choose_flee_step(
    start: Pos,
    enemies: Iterable[Pos],
    core: Pos,
    obstacles: set[Pos],
    blocked: set[Pos],
    has_cargo: bool,
) -> Pos | None:
    enemy_positions = tuple(enemies)
    choices: list[tuple[tuple[int, int, int], Pos]] = []
    for _, delta in DIRECTION_STEPS:
        nxt = add(start, delta)
        if nxt in obstacles or nxt in blocked:
            continue
        min_enemy_distance = min(manhattan(nxt, enemy) for enemy in enemy_positions)
        core_score = -manhattan(nxt, core) if has_cargo else manhattan(nxt, core)
        choices.append(((min_enemy_distance, core_score, -nxt[0] - nxt[1]), nxt))
    return max(choices, default=(None, None))[1]


class FriendlyOccupancy:
    """按每格最多两个友军单位跟踪本 Tick 的预计占位。"""

    def __init__(self, positions: Iterable[Pos] = ()) -> None:
        self._counts: Counter[Pos] = Counter(positions)

    def count(self, position: Pos) -> int:
        return self._counts.get(position, 0)

    def remove(self, position: Pos) -> None:
        if self._counts.get(position, 0) <= 1:
            self._counts.pop(position, None)
        else:
            self._counts[position] -= 1

    def discard(self, position: Pos) -> None:
        self.remove(position)

    def add(self, position: Pos) -> None:
        self._counts[position] += 1

    def can_enter(self, position: Pos) -> bool:
        return self.count(position) < 2

    def full_cells(self) -> set[Pos]:
        return {position for position, count in self._counts.items() if count >= 2}

    def occupied_cells(self) -> set[Pos]:
        return set(self._counts)

    def __contains__(self, position: Pos) -> bool:
        """成员判断表示目标格已满，而不是仅有单位占用。"""
        return not self.can_enter(position)

    def __iter__(self):
        return iter(self.full_cells())

    def __or__(self, other) -> set[Pos]:
        return self.full_cells() | set(other)

    def __ror__(self, other) -> set[Pos]:
        return set(other) | self.full_cells()


def roam_trap_possible(
    target: Pos,
    roaming_units,
    static_obstacles: set[Pos],
    friendly_cells: set[Pos],
) -> bool:
    """判断附近巡逻单位和地形是否足以封住目标出口。"""
    nearby_allies = sum(
        manhattan(tuple(unit.position), target) <= ROAM_TRAP_ALLY_RADIUS
        for unit in roaming_units
    )
    if nearby_allies < 2:
        return False
    exits = sum(
        add(target, delta) not in static_obstacles
        and add(target, delta) not in friendly_cells
        for _, delta in DIRECTION_STEPS
    )
    return exits <= ROAM_TRAP_MAX_EXITS


@dataclass
class EnemyWorkerTrack:
    position: Pos
    previous_position: Pos | None
    first_seen_position: Pos
    first_seen_tick: int
    last_seen_tick: int
    previous_seen_tick: int

    @property
    def is_moving(self) -> bool:
        return (
            self.previous_position is not None
            and self.previous_position != self.position
            and self.previous_seen_tick == self.last_seen_tick - 1
        )

    @property
    def movement_delta(self) -> Pos:
        if not self.is_moving or self.previous_position is None:
            return (0, 0)
        return (
            self.position[0] - self.previous_position[0],
            self.position[1] - self.previous_position[1],
        )


@dataclass
class AgentMemory:
    # 世界与敌情记忆。
    known_obstacles: set[Pos] = field(default_factory=set)
    known_enemy_cores: dict[UUID, tuple[Pos, int]] = field(default_factory=dict)
    known_resources: set[Pos] = field(default_factory=set)
    temporary_blocked_cells: dict[Pos, int] = field(default_factory=dict)
    known_combat_threats: dict[UUID, tuple[Pos, int]] = field(default_factory=dict)

    # Worker：撤退、侦察、资源搬运和临时拦截状态。
    retreat_until: dict[UUID, int] = field(default_factory=dict)
    retreat_goal: dict[UUID, Pos] = field(default_factory=dict)
    scout_phase: dict[UUID, int] = field(default_factory=dict)
    scout_goal: dict[UUID, Pos] = field(default_factory=dict)
    scout_path_failures: dict[UUID, int] = field(default_factory=dict)
    worker_sector: dict[UUID, int] = field(default_factory=dict)
    productive_sector_cursor: int = 0
    ranger_coverage_active: bool = False
    worker_harvests: dict[UUID, int] = field(default_factory=dict)
    expanded_low_yield: set[UUID] = field(default_factory=set)
    worker_resource_target: dict[UUID, Pos] = field(default_factory=dict)
    resource_deferred_until: dict[Pos, int] = field(default_factory=dict)
    worker_intercept_goal: dict[UUID, tuple[Pos, int]] = field(default_factory=dict)

    # Vanguard/Ranger：家园角色、巡逻目标和追击冷却。
    ranger_patrol_phase: dict[UUID, int] = field(default_factory=dict)
    vanguard_patrol_phase: dict[UUID, int] = field(default_factory=dict)
    roam_phase: dict[UUID, int] = field(default_factory=dict)
    roam_goal: dict[UUID, Pos] = field(default_factory=dict)
    ranger_follow_vanguard: dict[UUID, UUID] = field(default_factory=dict)
    home_vanguard_id: UUID | None = None
    home_ranger_id: UUID | None = None
    enemy_worker_tracks: dict[UUID, EnemyWorkerTrack] = field(default_factory=dict)
    roam_chase_started: dict[UUID, int] = field(default_factory=dict)
    roam_chase_cooldown_until: dict[UUID, int] = field(default_factory=dict)

    # 生命周期与持久化节流。
    last_core_position: Pos | None = None
    last_state_save_tick: int = 0

    @classmethod
    def restore(cls, state: dict) -> "AgentMemory":
        memory = cls(
            known_resources=decode_positions(state.get("known_resources", [])),
            known_obstacles=decode_positions(state.get("known_obstacles", [])),
        )
        for attribute in ("home_vanguard_id", "home_ranger_id"):
            raw_id = state.get(attribute)
            if isinstance(raw_id, str):
                try:
                    setattr(memory, attribute, UUID(raw_id))
                except ValueError:
                    continue
        raw_sectors = state.get("worker_sectors", {})
        if isinstance(raw_sectors, dict):
            for raw_id, sector in raw_sectors.items():
                try:
                    worker_id = UUID(raw_id)
                except (TypeError, ValueError):
                    continue
                if isinstance(sector, int):
                    memory.worker_sector[worker_id] = sector % len(SCOUT_VECTORS)
        raw_enemy_cores = state.get("known_enemy_cores", {})
        if isinstance(raw_enemy_cores, dict):
            for raw_id, sighting in raw_enemy_cores.items():
                try:
                    enemy_id = UUID(raw_id)
                except (TypeError, ValueError):
                    continue
                if (
                    isinstance(sighting, dict)
                    and isinstance(sighting.get("position"), list)
                    and len(sighting["position"]) == 2
                    and all(isinstance(value, int) for value in sighting["position"])
                    and isinstance(sighting.get("tick"), int)
                ):
                    memory.known_enemy_cores[enemy_id] = (
                        tuple(sighting["position"]),
                        sighting["tick"],
                    )
        return memory

    def persistent_state(self) -> dict:
        return {
            "version": 2,
            "known_resources": [
                list(position) for position in sorted(self.known_resources)
            ],
            "known_obstacles": [
                list(position) for position in sorted(self.known_obstacles)
            ],
            "known_enemy_cores": {
                str(enemy_id): {"position": list(position), "tick": tick}
                for enemy_id, (position, tick) in sorted(
                    self.known_enemy_cores.items(),
                    key=lambda item: str(item[0]),
                )
            },
            "worker_sectors": {
                str(worker_id): sector
                for worker_id, sector in sorted(
                    self.worker_sector.items(),
                    key=lambda item: str(item[0]),
                )
            },
            "home_vanguard_id": (
                str(self.home_vanguard_id) if self.home_vanguard_id else None
            ),
            "home_ranger_id": (
                str(self.home_ranger_id) if self.home_ranger_id else None
            ),
        }

    def sync_core_position(self, core: Pos) -> bool:
        """Core 移动后重置所有依赖旧家位置的缓存目标。"""
        previous = self.last_core_position
        self.last_core_position = core
        if previous is None or previous == core:
            return False

        # 资源坐标和敌方轨迹属于世界坐标，仍然有效；侦察、巡逻和撤退目标则
        # 由旧家位置推导，必须清除。
        self.scout_goal.clear()
        self.scout_path_failures.clear()
        self.roam_goal.clear()
        self.retreat_goal.clear()
        return True

    def prune_unit_state(self, workers, vanguards, rangers) -> bool:
        """删除死亡单位的运行时状态，并报告持久化扇区是否变化。"""
        worker_ids = {worker.id for worker in workers}
        vanguard_ids = {unit.id for unit in vanguards}
        ranger_ids = {unit.id for unit in rangers}
        combat_ids = vanguard_ids | ranger_ids
        sectors_before = dict(self.worker_sector)

        for state in (
            self.retreat_until,
            self.retreat_goal,
            self.scout_phase,
            self.scout_goal,
            self.scout_path_failures,
            self.worker_sector,
            self.worker_harvests,
            self.worker_resource_target,
            self.worker_intercept_goal,
        ):
            for unit_id in tuple(state):
                if unit_id not in worker_ids:
                    state.pop(unit_id, None)
        self.expanded_low_yield.intersection_update(worker_ids)

        for state, live_ids in (
            (self.vanguard_patrol_phase, vanguard_ids),
            (self.ranger_patrol_phase, ranger_ids),
            (self.roam_phase, combat_ids),
            (self.roam_goal, combat_ids),
        ):
            for unit_id in tuple(state):
                if unit_id not in live_ids:
                    state.pop(unit_id, None)

        for ranger_id, vanguard_id in tuple(self.ranger_follow_vanguard.items()):
            if ranger_id not in ranger_ids or vanguard_id not in vanguard_ids:
                self.ranger_follow_vanguard.pop(ranger_id, None)

        return self.worker_sector != sectors_before

    def sync_combat_roles(self, vanguards, rangers, core: Pos) -> bool:
        before = self.home_vanguard_id, self.home_ranger_id
        vanguard_by_id = {unit.id: unit for unit in vanguards}
        ranger_by_id = {unit.id: unit for unit in rangers}
        if self.home_vanguard_id not in vanguard_by_id:
            replacement = min(
                vanguards,
                key=lambda unit: (manhattan(tuple(unit.position), core), str(unit.id)),
                default=None,
            )
            self.home_vanguard_id = replacement.id if replacement else None
        if self.home_ranger_id not in ranger_by_id:
            replacement = min(
                rangers,
                key=lambda unit: (manhattan(tuple(unit.position), core), str(unit.id)),
                default=None,
            )
            self.home_ranger_id = replacement.id if replacement else None
        return before != (self.home_vanguard_id, self.home_ranger_id)

    def assign_ranger_follow_targets(
        self,
        rangers: Iterable[Ranger],
        vanguards: Iterable[Vanguard],
    ) -> dict[UUID, UUID]:
        """稳定分配游走 Ranger 的 Vanguard 跟随目标，并优先避免重复。"""
        ordered_rangers = tuple(sorted(rangers, key=lambda unit: str(unit.id)))
        ordered_vanguards = tuple(sorted(vanguards, key=lambda unit: str(unit.id)))
        vanguard_by_id = {unit.id: unit for unit in ordered_vanguards}
        assignments: dict[UUID, UUID] = {}
        target_loads: Counter[UUID] = Counter()
        unassigned: list[Ranger] = []

        # 先保留仍然有效的一对一关系，避免距离轻微变化导致每 Tick 换搭档。
        for ranger in ordered_rangers:
            target_id = self.ranger_follow_vanguard.get(ranger.id)
            if target_id in vanguard_by_id and target_loads[target_id] == 0:
                assignments[ranger.id] = target_id
                target_loads[target_id] += 1
            else:
                unassigned.append(ranger)

        # 先锋不足时才复用目标；优先选择当前负载最少且距离最近的先锋。
        for ranger in unassigned:
            target = min(
                ordered_vanguards,
                key=lambda unit: (
                    target_loads[unit.id],
                    manhattan(tuple(ranger.position), tuple(unit.position)),
                    str(unit.id),
                ),
                default=None,
            )
            if target is None:
                continue
            assignments[ranger.id] = target.id
            target_loads[target.id] += 1

        self.ranger_follow_vanguard = assignments
        return dict(assignments)

    def observe_enemy_cores(
        self,
        visible_enemies,
        vision_sources: tuple[tuple[Pos, int], ...],
        obstacles: set[Pos],
        tick: int,
    ) -> bool:
        before = dict(self.known_enemy_cores)
        visible_core_ids: set[UUID] = set()
        for enemy in visible_enemies:
            if enemy.kind != "CORE":
                continue
            visible_core_ids.add(enemy.id)
            self.known_enemy_cores[enemy.id] = (tuple(enemy.position), tick)

        # 敌方 Core 也会移动。远端目击可跨重启保留；友军重新进入该位置视野且
        # 没看见 Core 时，再删除旧标记。
        for enemy_id, (position, _) in list(self.known_enemy_cores.items()):
            if enemy_id in visible_core_ids:
                continue
            if any_vision_source_sees(position, vision_sources, obstacles):
                self.known_enemy_cores.pop(enemy_id, None)
        return self.known_enemy_cores != before

    def observe_enemy_workers(self, visible_enemies, tick: int) -> None:
        for enemy in visible_enemies:
            if enemy.kind != "UNIT" or enemy.unit_type is not UnitType.WORKER:
                continue
            position = tuple(enemy.position)
            previous = self.enemy_worker_tracks.get(enemy.id)
            if previous is None:
                self.enemy_worker_tracks[enemy.id] = EnemyWorkerTrack(
                    position=position,
                    previous_position=None,
                    first_seen_position=position,
                    first_seen_tick=tick,
                    last_seen_tick=tick,
                    previous_seen_tick=tick,
                )
            elif previous.last_seen_tick != tick:
                self.enemy_worker_tracks[enemy.id] = EnemyWorkerTrack(
                    position=position,
                    previous_position=previous.position,
                    first_seen_position=previous.first_seen_position,
                    first_seen_tick=previous.first_seen_tick,
                    last_seen_tick=tick,
                    previous_seen_tick=previous.last_seen_tick,
                )
        self.enemy_worker_tracks = {
            enemy_id: track
            for enemy_id, track in self.enemy_worker_tracks.items()
            if track.last_seen_tick > tick - ROAM_TARGET_LOST_TICKS
        }

    def can_continue_roam_chase(
        self,
        target_id: UUID,
        tick: int,
        trap_possible: bool,
    ) -> bool:
        cooldown_until = self.roam_chase_cooldown_until.get(target_id, 0)
        if tick < cooldown_until:
            return False
        chase_started = self.roam_chase_started.setdefault(target_id, tick)
        chase_limit = ROAM_TRAP_CHASE_TICKS if trap_possible else ROAM_CHASE_TICKS
        if tick - chase_started < chase_limit:
            return True
        self.roam_chase_started.pop(target_id, None)
        self.roam_chase_cooldown_until[target_id] = tick + ROAM_CHASE_COOLDOWN_TICKS
        return False

    def prune_roam_chases(self, tick: int) -> None:
        self.roam_chase_started = {
            target_id: started
            for target_id, started in self.roam_chase_started.items()
            if started > tick - ROAM_TRAP_CHASE_TICKS
        }
        self.roam_chase_cooldown_until = {
            target_id: expiry
            for target_id, expiry in self.roam_chase_cooldown_until.items()
            if expiry > tick
        }

    def observe_dynamic_blocks(self, events, tick: int) -> None:
        """短暂记住移动占位失败的格子，让单位临时绕行。"""
        self.temporary_blocked_cells = {
            position: expiry
            for position, expiry in self.temporary_blocked_cells.items()
            if expiry > tick
        }
        for event in events:
            if (
                event.event_type == "UNIT_MOVE_FAILED"
                and event.reason_code == "MOVE_DESTINATION_OCCUPIED"
                and event.position is not None
            ):
                self.temporary_blocked_cells[tuple(event.position)] = (
                    tick + TEMPORARY_BLOCK_TICKS
                )

    def observe_resources(
        self,
        visible_resources: set[Pos],
        workers,
        vision_sources: tuple[tuple[Pos, int], ...],
        obstacles: set[Pos],
        events,
    ) -> bool:
        """合并资源目击，并由任意进入视野的友军确认资源消失。"""
        before = set(self.known_resources)
        worker_by_id = {worker.id: worker for worker in workers}

        # 采集成功后立即移除该资源；以后刷新时由新目击重新加入，避免已采集
        # 坐标继续参与当前 Worker 分配。
        for event in events:
            if event.event_type != "HARVEST_SUCCEEDED":
                continue
            target = self.worker_resource_target.pop(event.actor_id, None)
            harvested = target
            if harvested is None and event.position is not None:
                harvested = tuple(event.position)
            if harvested is None:
                worker = worker_by_id.get(event.actor_id)
                if worker is not None:
                    harvested = tuple(worker.position)
            if harvested is not None:
                self.known_resources.discard(harvested)
                self.resource_deferred_until.pop(harvested, None)
                for worker_id, resource in list(self.worker_resource_target.items()):
                    if resource == harvested:
                        self.worker_resource_target.pop(worker_id, None)

        self.known_resources.update(visible_resources)
        for resource in tuple(self.known_resources - visible_resources):
            if not any_vision_source_sees(resource, vision_sources, obstacles):
                continue

            self.known_resources.discard(resource)
            self.resource_deferred_until.pop(resource, None)
            for worker_id, target in list(self.worker_resource_target.items()):
                if target == resource:
                    self.worker_resource_target.pop(worker_id, None)
        return self.known_resources != before

    def sync_worker_sectors(self, workers) -> None:
        """保持现存 Worker 扇区稳定，并优先填补覆盖最少的方向。"""
        if not self.worker_sector:
            worker_count = len(workers)
            for worker_index, worker in enumerate(workers):
                self.worker_sector[worker.id] = (
                    worker_index * len(SCOUT_VECTORS)
                ) // max(1, worker_count)

        for worker in workers:
            if worker.id not in self.worker_sector:
                sector_counts = Counter(self.worker_sector.values())
                sector = min(
                    range(len(SCOUT_VECTORS)),
                    key=lambda candidate: (
                        sector_counts.get(candidate, 0),
                        (candidate - self.productive_sector_cursor)
                        % len(SCOUT_VECTORS),
                    ),
                )
                self.productive_sector_cursor += 1
                self.worker_sector[worker.id] = sector
                self.scout_phase[worker.id] = 0
                self.scout_goal.pop(worker.id, None)

            self.worker_harvests.setdefault(worker.id, 0)
            if worker.cargo > 0:
                self.worker_harvests[worker.id] = max(
                    1,
                    self.worker_harvests[worker.id],
                )

    def observe_worker_harvests(self, events, workers) -> None:
        """只扩大长期零产出、而同伴已经成功采集的侦察方向。"""
        worker_ids = {worker.id for worker in workers}
        for event in events:
            if event.event_type == "HARVEST_SUCCEEDED" and event.actor_id in worker_ids:
                self.worker_harvests[event.actor_id] = (
                    self.worker_harvests.get(event.actor_id, 0) + 1
                )

        leader_count = max(
            (self.worker_harvests.get(worker.id, 0) for worker in workers),
            default=0,
        )
        expanded = {
            worker.id
            for worker in workers
            if self.worker_harvests.get(worker.id, 0) == 0 and leader_count >= 3
        }
        for worker_id in self.expanded_low_yield ^ expanded:
            self.scout_goal.pop(worker_id, None)
        self.expanded_low_yield = expanded

    def sync_ranger_coverage(self, active: bool, workers) -> None:
        """Ranger 开始覆盖家园后，将 Worker 侦察圈外移。"""
        if active == self.ranger_coverage_active:
            return
        self.ranger_coverage_active = active
        for worker in workers:
            self.scout_goal.pop(worker.id, None)

    def assign_resource_targets(
        self,
        workers,
        known_resources: set[Pos],
        visible_resources: set[Pos],
        tick: int,
        path_obstacles: set[Pos],
        blocked_resource_cells: set[Pos],
    ) -> dict[UUID, Pos]:
        """为真正空闲的 Worker 保留一个跨 Tick 的静态资源任务。"""
        worker_by_id = {worker.id: worker for worker in workers}

        # 只释放已经无法执行的任务；目标在负责 Worker 视野外时继续保留。
        for worker_id, resource in list(self.worker_resource_target.items()):
            worker = worker_by_id.get(worker_id)
            if worker is None or worker.cargo > 0:
                self.worker_resource_target.pop(worker_id, None)
                continue
            if (
                tick < self.retreat_until.get(worker_id, 0)
                or resource not in known_resources
                or resource in path_obstacles
                or resource in blocked_resource_cells
            ):
                self.worker_resource_target.pop(worker_id, None)

        self.resource_deferred_until = {
            resource: expiry
            for resource, expiry in self.resource_deferred_until.items()
            if expiry > tick and resource in known_resources
        }

        assignments: dict[UUID, Pos] = {}
        assigned_resources: set[Pos] = set()

        # 已经站在资源上的 Worker 优先占有该资源。
        for worker in workers:
            position = tuple(worker.position)
            if (
                worker.cargo != 0
                or position not in visible_resources
                or tick < self.resource_deferred_until.get(position, 0)
            ):
                continue
            if position in assigned_resources:
                self.worker_resource_target.pop(worker.id, None)
                continue
            self.worker_resource_target[worker.id] = position
            assignments[worker.id] = position
            assigned_resources.add(position)

        # 保留途中任务，避免把赶路 Worker 当成空闲。只有明显更近的空闲 Worker
        # 才能接手同一个既有任务，防止频繁抖动。
        claimed_worker_ids = set(self.worker_resource_target)
        takeover_workers = [
            worker
            for worker in workers
            if worker.cargo == 0
            and worker.id not in claimed_worker_ids
            and tuple(worker.position) not in visible_resources
            and tick >= self.retreat_until.get(worker.id, 0)
        ]
        for owner_id, resource in list(self.worker_resource_target.items()):
            owner = worker_by_id.get(owner_id)
            if (
                owner is None
                or resource in blocked_resource_cells
                or resource in assigned_resources
            ):
                continue
            owner_distance = manhattan(tuple(owner.position), resource)
            replacement = min(
                takeover_workers,
                key=lambda worker: (
                    manhattan(tuple(worker.position), resource),
                    str(worker.id),
                ),
                default=None,
            )
            if replacement is None:
                continue
            replacement_distance = manhattan(tuple(replacement.position), resource)
            if owner_distance - replacement_distance < RESOURCE_REASSIGN_MIN_GAIN:
                continue
            self.worker_resource_target.pop(owner_id, None)
            self.worker_resource_target[replacement.id] = resource
            takeover_workers.remove(replacement)
            takeover_workers.append(owner)

        for worker in workers:
            if worker.id in assignments or worker.cargo > 0:
                continue
            resource = self.worker_resource_target.get(worker.id)
            if resource is None:
                continue
            if resource in assigned_resources:
                self.worker_resource_target.pop(worker.id, None)
                continue
            assignments[worker.id] = resource
            assigned_resources.add(resource)

        idle_workers = [
            worker
            for worker in workers
            if worker.cargo == 0
            and worker.id not in assignments
            and tuple(worker.position) not in visible_resources
            and tick >= self.retreat_until.get(worker.id, 0)
        ]
        available_resources = {
            resource
            for resource in known_resources
            - assigned_resources
            - blocked_resource_cells
            if tick >= self.resource_deferred_until.get(resource, 0)
            and resource not in path_obstacles
        }
        for worker_id, resource in minimum_cost_resource_matching(
            idle_workers,
            available_resources,
            self.worker_harvests,
        ):
            self.worker_resource_target[worker_id] = resource
            assignments[worker_id] = resource
            assigned_resources.add(resource)

        return assignments

    def defer_unreachable_resource(
        self,
        worker_id: UUID,
        resource: Pos,
        tick: int,
    ) -> None:
        """当前无法到达资源时短暂释放任务。"""
        if self.worker_resource_target.get(worker_id) == resource:
            self.worker_resource_target.pop(worker_id, None)
        self.resource_deferred_until[resource] = tick + 4

    def goal_for(
        self,
        worker_id: UUID,
        worker_index: int,
        worker_count: int,
        core: Pos,
        position: Pos,
        obstacles: set[Pos],
    ) -> Pos:
        phase = self.scout_phase.get(worker_id, 0)
        goal = self.scout_goal.get(worker_id)
        if goal is not None and goal not in obstacles and manhattan(position, goal) > 1:
            return goal

        if goal is not None:
            phase += 1

        # 每个 Worker 固定在自己的放射方向，减少移动视野重叠；只调整半径，
        # 不跨入其他 Worker 的扇区。
        base_index = self.worker_sector.get(
            worker_id,
            (worker_index * len(SCOUT_VECTORS)) // max(1, worker_count),
        )
        vector = SCOUT_VECTORS[base_index % len(SCOUT_VECTORS)]
        radius_cycle = (
            (16, 21, 26, 32, 26, 21)
            if worker_id in self.expanded_low_yield
            else (12, 16, 20, 16)
        )
        for _ in range(len(radius_cycle)):
            # 资源每四个 Tick 补充一次。循环访问有限半径，避免无限远离 Core。
            radius = radius_cycle[phase % len(radius_cycle)]
            goal = core[0] + vector[0] * radius, core[1] + vector[1] * radius
            if goal not in obstacles:
                self.scout_phase[worker_id] = phase
                self.scout_goal[worker_id] = goal
                return goal
            phase += 1

        # 若已知障碍异常密集导致所有候选方向不可用，则回退到 Core。
        self.scout_phase[worker_id] = phase
        self.scout_goal[worker_id] = core
        return core

    def roam_goal_for(
        self,
        unit_id: UUID,
        core: Pos,
        position: Pos,
        obstacles: set[Pos],
    ) -> Pos:
        """在敌方 Core 外围和有限边界航点之间循环巡逻。"""
        goal = self.roam_goal.get(unit_id)
        phase = self.roam_phase.get(unit_id, unit_id.int % len(SCOUT_VECTORS))
        if goal is not None and manhattan(position, goal) > 1 and goal not in obstacles:
            return goal
        if goal is not None:
            phase += 1

        core_outskirts: list[Pos] = []
        for enemy_position, _ in sorted(self.known_enemy_cores.values()):
            for offset in ((0, -4), (4, 0), (0, 4), (-4, 0)):
                candidate = add(enemy_position, offset)
                if chebyshev(core, candidate) <= ROAM_RADIUS:
                    core_outskirts.append(candidate)
        frontier = [
            add(core, (vector[0] * radius, vector[1] * radius))
            for radius in (20, 26, 32)
            for vector in SCOUT_VECTORS
        ]
        candidates = core_outskirts + frontier
        for _ in range(len(candidates)):
            candidate = candidates[phase % len(candidates)]
            if candidate not in obstacles:
                self.roam_phase[unit_id] = phase
                self.roam_goal[unit_id] = candidate
                return candidate
            phase += 1
        self.roam_phase[unit_id] = phase
        self.roam_goal[unit_id] = core
        return core


@dataclass
class PlanningContext:
    """汇总一个 Tick 内各单位规划函数共享的事实和可变占位。"""

    turn: Turn
    memory: AgentMemory
    core_pos: Pos
    workers: list[Worker]
    vanguards: list[Vanguard]
    rangers: list[Ranger]
    visible_resources: set[Pos]
    visible_enemy_unit_cells: set[Pos]
    known_enemy_core_cells: set[Pos]
    visible_enemy_cores: tuple[CoreView, ...]
    combat_enemies: tuple[UnitView, ...]
    home_combat_targets: tuple[UnitView, ...]
    home_enemy_units: tuple[UnitView, ...]
    navigation_obstacles: set[Pos]
    danger_cells: set[Pos]
    threat_positions: tuple[Pos, ...]
    occupied: FriendlyOccupancy
    resource_assignments: dict[UUID, Pos]
    roaming_combat_units: tuple[Vanguard | Ranger, ...]
    roam_is_aggressive: bool
    trap_obstacles: set[Pos]
    roam_target_track: EnemyWorkerTrack | None
    roam_target_enemy: UnitView | None
    roam_target_id: UUID | None
    blocker_worker_id: UUID | None
    actions: list[str]


def visible_roam_core_for(
    context: PlanningContext,
    position: Pos,
) -> CoreView | None:
    """激进期选择当前巡逻方形内最近的可见敌方 Core。"""
    if not context.roam_is_aggressive:
        return None
    candidates = tuple(
        core
        for core in context.visible_enemy_cores
        if chebyshev(context.core_pos, tuple(core.position)) <= ROAM_RADIUS
    )
    return min(
        candidates,
        key=lambda core: (manhattan(position, tuple(core.position)), str(core.id)),
        default=None,
    )


def roam_core_reacquire_goal(
    context: PlanningContext,
    position: Pos,
) -> Pos | None:
    """Core 离开视野后，前往最后目击点外围重新获取视野。"""
    if not context.roam_is_aggressive or context.visible_enemy_cores:
        return None
    obstacles = (
        context.navigation_obstacles
        | context.known_enemy_core_cells
        | context.visible_enemy_unit_cells
    )
    candidates: list[Pos] = []
    for enemy_position, _ in context.memory.known_enemy_cores.values():
        for offset in ((0, -4), (4, 0), (0, 4), (-4, 0)):
            candidate = add(enemy_position, offset)
            if (
                chebyshev(context.core_pos, candidate) <= ROAM_RADIUS
                and candidate not in obstacles
            ):
                candidates.append(candidate)
    return min(
        candidates,
        key=lambda candidate: (manhattan(position, candidate), candidate),
        default=None,
    )


def planned_vanguard_positions(
    context: PlanningContext,
    vanguards: Iterable[Vanguard],
) -> dict[UUID, Pos]:
    """读取 Vanguard 本 Tick 的移动动作，返回提交后的预计位置。"""
    positions: dict[UUID, Pos] = {}
    for vanguard in vanguards:
        position: Pos = tuple(vanguard.position)
        action = context.turn.plan.unit_actions.get(vanguard.id)
        if isinstance(action, MoveAction):
            position = add(position, action.direction.delta)
        positions[vanguard.id] = position
    return positions


def ranger_follow_destination(
    context: PlanningContext,
    position: Pos,
    vanguard_position: Pos,
    navigation_obstacles: set[Pos],
) -> Pos | None:
    """选择通往搭档 Vanguard 相邻格的下一步；已相邻时保持原位。"""
    movement_obstacles = (
        navigation_obstacles
        | context.visible_enemy_unit_cells
        | {vanguard_position}
    )
    follow_cells: list[Pos] = []
    for _, delta in DIRECTION_STEPS:
        candidate = add(vanguard_position, delta)
        if (
            chebyshev(context.core_pos, candidate) <= ROAM_RADIUS
            and candidate not in movement_obstacles
            and context.occupied.can_enter(candidate)
        ):
            follow_cells.append(candidate)
    follow_cells.sort(key=lambda cell: (manhattan(position, cell), cell))
    if position in follow_cells:
        return position

    for follow_cell in follow_cells:
        destination = first_step_astar(
            position,
            follow_cell,
            movement_obstacles,
            set(context.occupied),
        )
        if destination is not None and context.occupied.can_enter(destination):
            return destination
    return None


def plan_workers(context: PlanningContext) -> None:
    """按固定优先级规划 Worker：避险、返航、资源、拦截、侦察。"""
    turn = context.turn
    memory = context.memory
    occupied = context.occupied
    actions = context.actions

    # 载货单位先行动，其次处理站在 Core 上的单位，尽早腾出生产格。
    ordered = sorted(
        enumerate(context.workers),
        key=lambda pair: (
            pair[1].cargo == 0,
            tuple(pair[1].position) != context.core_pos,
            str(pair[1].id),
        ),
    )

    for worker_index, worker in ordered:
        position: Pos = tuple(worker.position)
        occupied.discard(position)
        danger = position in context.danger_cells or (
            context.threat_positions
            and min(
                manhattan(position, enemy) for enemy in context.threat_positions
            )
            <= 3
        )

        # 状态 1：当前受威胁时立即逃离，并放弃资源与侦察任务。
        if danger:
            memory.retreat_until[worker.id] = turn.tick + 10
            memory.worker_resource_target.pop(worker.id, None)
            memory.scout_goal.pop(worker.id, None)
            memory.scout_phase[worker.id] = memory.scout_phase.get(worker.id, 0) + 1
            destination = choose_flee_step(
                position,
                context.threat_positions,
                context.core_pos,
                memory.known_obstacles,
                occupied,
                worker.cargo > 0,
            )
            if destination is not None:
                direction = direction_between(position, destination)
                if direction is not None:
                    escape_delta = (
                        destination[0] - position[0],
                        destination[1] - position[1],
                    )
                    memory.retreat_goal[worker.id] = (
                        position[0] + escape_delta[0] * 8,
                        position[1] + escape_delta[1] * 8,
                    )
                    worker.move(direction)
                    occupied.add(destination)
                    actions.append(f"{str(worker.id)[:8]} flee {direction.value}")
                    continue

        # 状态 2：空载 Worker 在撤退期限内继续远离危险区。
        retreating = turn.tick < memory.retreat_until.get(worker.id, 0)
        if retreating and worker.cargo == 0:
            retreat_goal = memory.retreat_goal.get(worker.id, context.core_pos)
            if manhattan(position, retreat_goal) <= 1:
                memory.retreat_until.pop(worker.id, None)
                memory.retreat_goal.pop(worker.id, None)
            else:
                destination = first_step_astar(
                    position,
                    retreat_goal,
                    context.navigation_obstacles,
                    set(occupied),
                )
                if destination is not None and destination not in occupied:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        worker.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(worker.id)[:8]} retreat {direction.value}"
                        )
                        continue

        # 状态 3：载货后只返回当前 Core，不再执行采集或侦察。
        if worker.cargo > 0:
            if position == context.core_pos:
                worker.deposit()
                occupied.add(position)
                actions.append(f"{str(worker.id)[:8]} deposit {worker.cargo}")
                continue

            blocked = set(occupied)
            blocked.discard(context.core_pos)
            destination = first_step_astar(
                position,
                context.core_pos,
                context.navigation_obstacles,
                blocked,
            )
            if destination is not None and destination not in occupied:
                direction = direction_between(position, destination)
                if direction is not None:
                    worker.move(direction)
                    occupied.add(destination)
                    actions.append(f"{str(worker.id)[:8]} return {direction.value}")
                    continue

            worker.wait()
            occupied.add(position)
            actions.append(f"{str(worker.id)[:8]} wait-return")
            continue

        # 状态 4：脚下有当前可见资源时立即采集。
        if (
            position in context.visible_resources
            and turn.tick >= memory.resource_deferred_until.get(position, 0)
        ):
            worker.harvest()
            occupied.add(position)
            actions.append(f"{str(worker.id)[:8]} harvest")
            continue

        # 状态 5：静态资源任务跨 Tick 保留，途中不因发现新资源而改派。
        assigned_resource = context.resource_assignments.get(worker.id)
        if assigned_resource is not None and position == assigned_resource:
            worker.wait()
            occupied.add(position)
            actions.append(
                f"{str(worker.id)[:8]} wait-resource-confirm {assigned_resource}"
            )
            continue

        if assigned_resource is not None:
            destination = first_step_astar(
                position,
                assigned_resource,
                context.navigation_obstacles,
                occupied,
            )
            if destination is not None and destination not in occupied:
                direction = direction_between(position, destination)
                if direction is not None:
                    worker.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(worker.id)[:8]} resource {assigned_resource} "
                        f"{direction.value}"
                    )
                    continue

            memory.defer_unreachable_resource(
                worker.id,
                assigned_resource,
                turn.tick,
            )
            # 本 Tick 的失败通常来自临时占位。等待下个 Tick 重试，不切换为侦察；
            # 永久不可达资源会被上面的延迟机制暂时释放。
            worker.wait()
            occupied.add(position)
            actions.append(
                f"{str(worker.id)[:8]} wait-resource-retry {assigned_resource}"
            )
            continue

        # 状态 6：没有资源任务时，才允许空载 Worker 临时协助拦截敌方 Worker。
        intercept = memory.worker_intercept_goal.get(worker.id)
        if intercept is not None and worker.id == context.blocker_worker_id:
            intercept_goal, expiry = intercept
            if turn.tick <= expiry and position != intercept_goal:
                destination = first_step_astar(
                    position,
                    intercept_goal,
                    context.navigation_obstacles,
                    set(occupied),
                )
                if destination is not None and destination not in occupied:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        worker.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(worker.id)[:8]} intercept {intercept_goal} "
                            f"{direction.value}"
                        )
                        continue
            if position == intercept_goal:
                worker.wait()
                occupied.add(position)
                actions.append(f"{str(worker.id)[:8]} intercept-hold")
                continue

        # 状态 7：其余 Worker 按固定扇区侦察。
        goal = memory.goal_for(
            worker.id,
            worker_index,
            len(context.workers),
            context.core_pos,
            position,
            context.navigation_obstacles,
        )
        destination = first_step_astar(
            position,
            goal,
            context.navigation_obstacles,
            occupied,
        )
        if destination is not None and destination not in occupied:
            direction = direction_between(position, destination)
            if direction is not None:
                worker.move(direction)
                memory.scout_path_failures.pop(worker.id, None)
                occupied.add(destination)
                actions.append(f"{str(worker.id)[:8]} scout {goal} {direction.value}")
                continue

        failures = memory.scout_path_failures.get(worker.id, 0) + 1
        memory.scout_path_failures[worker.id] = failures
        if failures >= 3:
            memory.scout_path_failures.pop(worker.id, None)
            memory.scout_goal.pop(worker.id, None)
            memory.scout_phase[worker.id] = memory.scout_phase.get(worker.id, 0) + 1

        worker.wait()
        occupied.add(position)
        actions.append(f"{str(worker.id)[:8]} wait-scout")


def plan_vanguards(context: PlanningContext) -> None:
    """规划 Vanguard：一名家园守卫，其余单位巡逻、追击或撤退。"""
    memory = context.memory
    occupied = context.occupied
    actions = context.actions
    combat_navigation_obstacles = (
        memory.known_obstacles
        | context.known_enemy_core_cells
        | context.visible_enemy_unit_cells
        | set(memory.temporary_blocked_cells)
    )

    for vanguard in context.vanguards:
        position: Pos = tuple(vanguard.position)
        occupied.discard(position)
        is_home_guard = vanguard.id == memory.home_vanguard_id

        # 巡逻 Vanguard：兵力不足时撤退，否则攻击、追击 Worker 或巡逻。
        if not is_home_guard:
            combat_radius = 10 if context.roam_is_aggressive else 6
            nearby_combat_enemies = tuple(
                enemy
                for enemy in context.combat_enemies
                if tuple(enemy.position) not in context.known_enemy_core_cells
                and manhattan(position, tuple(enemy.position)) <= combat_radius
            )
            local_roaming_allies = tuple(
                unit
                for unit in context.roaming_combat_units
                if manhattan(position, tuple(unit.position)) <= combat_radius
            )
            outnumbered = (
                len(local_roaming_allies) < len(nearby_combat_enemies)
                if context.roam_is_aggressive
                else len(local_roaming_allies) <= len(nearby_combat_enemies)
            )
            if nearby_combat_enemies and outnumbered:
                destination = choose_flee_step(
                    position,
                    (tuple(enemy.position) for enemy in nearby_combat_enemies),
                    context.core_pos,
                    memory.known_obstacles | context.known_enemy_core_cells,
                    set(occupied) | context.visible_enemy_unit_cells,
                    True,
                )
                if destination is not None and destination not in occupied:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        vanguard.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(vanguard.id)[:8]} roam-retreat {direction.value}"
                        )
                        continue

            core_target = visible_roam_core_for(context, position)
            if core_target is not None:
                core_position = tuple(core_target.position)
                if manhattan(position, core_position) == 1:
                    direction = direction_between(position, core_position)
                    if direction is not None:
                        vanguard.sweep(direction)
                        occupied.add(position)
                        actions.append(
                            f"{str(vanguard.id)[:8]} roam-core-sweep {direction.value}"
                        )
                        continue

                core_approach_cells = sorted(
                    (
                        add(core_position, delta)
                        for _, delta in DIRECTION_STEPS
                        if add(core_position, delta) not in combat_navigation_obstacles
                        and add(core_position, delta) not in occupied
                    ),
                    key=lambda cell: (manhattan(position, cell), cell),
                )
                for approach in core_approach_cells:
                    destination = first_step_astar(
                        position,
                        approach,
                        combat_navigation_obstacles,
                        occupied | {core_position},
                    )
                    if destination is None or destination in occupied:
                        continue
                    direction = direction_between(position, destination)
                    if direction is None:
                        continue
                    vanguard.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(vanguard.id)[:8]} roam-core {direction.value}"
                    )
                    break
                else:
                    core_target = None
                if core_target is not None:
                    continue

            target_enemy = None
            target_position: Pos | None = None
            can_engage_combat = nearby_combat_enemies and (
                len(local_roaming_allies) > len(nearby_combat_enemies)
                or (
                    context.roam_is_aggressive
                    and len(local_roaming_allies) >= len(nearby_combat_enemies)
                )
            )
            if can_engage_combat:
                for enemy in sorted(
                    nearby_combat_enemies,
                    key=lambda candidate: manhattan(
                        position,
                        tuple(candidate.position),
                    ),
                ):
                    enemy_position = tuple(enemy.position)
                    trap_possible = roam_trap_possible(
                        enemy_position,
                        context.roaming_combat_units,
                        context.trap_obstacles,
                        occupied.occupied_cells(),
                    )
                    if memory.can_continue_roam_chase(
                        enemy.id,
                        context.turn.tick,
                        trap_possible,
                    ):
                        target_enemy = enemy
                        target_position = enemy_position
                        break
            if target_enemy is None and context.roam_target_track is not None:
                target_enemy = context.roam_target_enemy
                target_position = context.roam_target_track.position

            if target_position is not None:
                if target_enemy is None:
                    if position == target_position:
                        if context.roam_target_id is not None:
                            memory.enemy_worker_tracks.pop(
                                context.roam_target_id,
                                None,
                            )
                        vanguard.wait()
                        occupied.add(position)
                        actions.append(f"{str(vanguard.id)[:8]} roam-search-confirm")
                        continue

                    destination = first_step_astar(
                        position,
                        target_position,
                        memory.known_obstacles | context.known_enemy_core_cells,
                        set(occupied) | context.visible_enemy_unit_cells,
                    )
                    if destination is not None and destination not in occupied:
                        direction = direction_between(position, destination)
                        if direction is not None:
                            vanguard.move(direction)
                            occupied.add(destination)
                            actions.append(
                                f"{str(vanguard.id)[:8]} roam-search "
                                f"{direction.value}"
                            )
                            continue

                if (
                    target_enemy is not None
                    and target_position not in context.known_enemy_core_cells
                    and manhattan(position, target_position) == 1
                ):
                    direction = direction_between(position, target_position)
                    if direction is not None:
                        vanguard.sweep(direction)
                        occupied.add(position)
                        actions.append(
                            f"{str(vanguard.id)[:8]} roam-sweep "
                            f"{target_enemy.unit_type.value} {direction.value}"
                        )
                        continue

                approach_cells = sorted(
                    (
                        add(target_position, delta)
                        for _, delta in DIRECTION_STEPS
                        if add(target_position, delta)
                        not in combat_navigation_obstacles
                        and add(target_position, delta) not in occupied
                    ),
                    key=lambda cell: (manhattan(position, cell), cell),
                )
                for approach in approach_cells:
                    destination = first_step_astar(
                        position,
                        approach,
                        combat_navigation_obstacles,
                        occupied | {target_position},
                    )
                    if destination is None or destination in occupied:
                        continue
                    direction = direction_between(position, destination)
                    if direction is None:
                        continue
                    vanguard.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(vanguard.id)[:8]} roam-hunt {direction.value}"
                    )
                    break
                else:
                    vanguard.wait()
                    occupied.add(position)
                    actions.append(f"{str(vanguard.id)[:8]} roam-hunt-wait")
                continue

            patrol_goal = roam_core_reacquire_goal(context, position)
            if patrol_goal is None:
                patrol_goal = memory.roam_goal_for(
                    vanguard.id,
                    context.core_pos,
                    position,
                    memory.known_obstacles | context.known_enemy_core_cells,
                )
            destination = first_step_astar(
                position,
                patrol_goal,
                combat_navigation_obstacles,
                set(occupied),
            )
            if destination is not None and destination not in occupied:
                direction = direction_between(position, destination)
                if direction is not None:
                    vanguard.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(vanguard.id)[:8]} roam-patrol {patrol_goal} "
                        f"{direction.value}"
                    )
                    continue
            vanguard.wait()
            occupied.add(position)
            actions.append(f"{str(vanguard.id)[:8]} roam-wait")
            continue

        # 家园 Vanguard：只在防御范围内迎击，否则沿 7x7 边界巡逻。
        target_enemy = min(
            (
                enemy
                for enemy in context.home_enemy_units
                if manhattan(context.core_pos, tuple(enemy.position))
                <= HOME_VANGUARD_CHASE_RADIUS
            ),
            key=lambda enemy: (
                enemy.unit_type is UnitType.WORKER,
                manhattan(position, tuple(enemy.position)),
            ),
            default=None,
        )

        if target_enemy is not None:
            target_position: Pos = tuple(target_enemy.position)
            if manhattan(position, target_position) == 1:
                direction = direction_between(position, target_position)
                if direction is not None:
                    vanguard.sweep(direction)
                    occupied.add(position)
                    actions.append(
                        f"{str(vanguard.id)[:8]} defend-sweep {direction.value}"
                    )
                    continue

            approach_cells = sorted(
                (
                    add(target_position, delta)
                    for _, delta in DIRECTION_STEPS
                    if add(target_position, delta) not in combat_navigation_obstacles
                    and add(target_position, delta) not in occupied
                ),
                key=lambda cell: (manhattan(position, cell), cell),
            )
            for approach in approach_cells:
                destination = first_step_astar(
                    position,
                    approach,
                    combat_navigation_obstacles,
                    occupied | {target_position},
                )
                if destination is None or destination in occupied:
                    continue
                direction = direction_between(position, destination)
                if direction is None:
                    continue
                vanguard.move(direction)
                occupied.add(destination)
                actions.append(
                    f"{str(vanguard.id)[:8]} defend-move {direction.value}"
                )
                break
            else:
                vanguard.wait()
                occupied.add(position)
                actions.append(f"{str(vanguard.id)[:8]} defend-wait")
            continue

        patrol_phase = memory.vanguard_patrol_phase.get(vanguard.id, 4)
        moved = False
        for _ in range(len(HOME_PATROL_OFFSETS)):
            patrol_goal = add(
                context.core_pos,
                HOME_PATROL_OFFSETS[patrol_phase % len(HOME_PATROL_OFFSETS)],
            )
            if patrol_goal in combat_navigation_obstacles:
                patrol_phase += 1
                continue
            if position == patrol_goal:
                patrol_phase += 1
                memory.vanguard_patrol_phase[vanguard.id] = patrol_phase
                continue
            destination = first_step_astar(
                position,
                patrol_goal,
                combat_navigation_obstacles | {context.core_pos},
                set(occupied),
            )
            if (
                destination is None
                or destination in occupied
                or (
                    chebyshev(position, context.core_pos) <= HOME_PATROL_RADIUS
                    and chebyshev(destination, context.core_pos)
                    > HOME_PATROL_RADIUS
                )
            ):
                patrol_phase += 1
                continue
            direction = direction_between(position, destination)
            if direction is None:
                patrol_phase += 1
                continue
            vanguard.move(direction)
            occupied.add(destination)
            memory.vanguard_patrol_phase[vanguard.id] = patrol_phase
            actions.append(f"{str(vanguard.id)[:8]} guard-patrol {direction.value}")
            moved = True
            break

        if moved:
            continue
        vanguard.wait()
        occupied.add(position)
        actions.append(f"{str(vanguard.id)[:8]} guard-wait")


def plan_rangers(context: PlanningContext) -> None:
    """规划 Ranger：一名家园守卫，其余单位远程巡逻和协同追击。"""
    memory = context.memory
    occupied = context.occupied
    actions = context.actions

    # v0.8 中 Unit 和 Core 不阻挡八方向射线；移动仍要避让实体和临时占位。
    ranger_navigation_obstacles = (
        memory.known_obstacles
        | context.known_enemy_core_cells
        | set(memory.temporary_blocked_cells)
    )
    ranger_shot_blockers = memory.known_obstacles
    roaming_vanguards = tuple(
        vanguard
        for vanguard in context.vanguards
        if vanguard.id != memory.home_vanguard_id
    )
    roaming_rangers = tuple(
        ranger
        for ranger in context.rangers
        if ranger.id != memory.home_ranger_id
    )
    follow_assignments = memory.assign_ranger_follow_targets(
        roaming_rangers,
        roaming_vanguards,
    )
    vanguard_positions = planned_vanguard_positions(context, roaming_vanguards)

    for ranger in context.rangers:
        position: Pos = tuple(ranger.position)
        occupied.discard(position)
        visible_entity_cells = {
            tuple(enemy.position) for enemy in context.turn.visible_enemies
        }
        is_home_guard = ranger.id == memory.home_ranger_id

        # 巡逻 Ranger：兵力不足时撤退，能直接射击时不为友军绕侧面。
        if not is_home_guard:
            combat_radius = 10 if context.roam_is_aggressive else 6
            nearby_combat_enemies = tuple(
                enemy
                for enemy in context.combat_enemies
                if tuple(enemy.position) not in context.known_enemy_core_cells
                and manhattan(position, tuple(enemy.position)) <= combat_radius
            )
            local_roaming_allies = tuple(
                unit
                for unit in context.roaming_combat_units
                if manhattan(position, tuple(unit.position)) <= combat_radius
            )
            outnumbered = (
                len(local_roaming_allies) < len(nearby_combat_enemies)
                if context.roam_is_aggressive
                else len(local_roaming_allies) <= len(nearby_combat_enemies)
            )
            if nearby_combat_enemies and outnumbered:
                destination = choose_flee_step(
                    position,
                    (tuple(enemy.position) for enemy in nearby_combat_enemies),
                    context.core_pos,
                    memory.known_obstacles | context.known_enemy_core_cells,
                    set(occupied) | context.visible_enemy_unit_cells,
                    True,
                )
                if destination is not None and destination not in occupied:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        ranger.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(ranger.id)[:8]} roam-retreat {direction.value}"
                        )
                        continue

            core_target = visible_roam_core_for(context, position)
            if core_target is not None:
                core_position = tuple(core_target.position)
                if clear_ranger_shot(
                    position,
                    core_position,
                    ranger_shot_blockers,
                ):
                    ranger.shoot(core_target)
                    occupied.add(position)
                    actions.append(
                        f"{str(ranger.id)[:8]} roam-core-shoot"
                    )
                    continue

                # 被障碍物挡住或尚未进入射程时，寻找八方向合法开火位。
                core_firing_cells: list[tuple[int, int, Pos]] = []
                for delta in SCOUT_VECTORS:
                    for distance in range(1, 4):
                        cell = (
                            core_position[0] - delta[0] * distance,
                            core_position[1] - delta[1] * distance,
                        )
                        movement_blockers = (
                            ranger_navigation_obstacles
                            | occupied.occupied_cells()
                            | visible_entity_cells
                        ) - {core_position, cell}
                        if (
                            chebyshev(cell, context.core_pos) <= ROAM_RADIUS
                            and cell not in movement_blockers
                            and clear_ranger_shot(
                                cell,
                                core_position,
                                ranger_shot_blockers,
                            )
                        ):
                            core_firing_cells.append(
                                (
                                    abs(distance - 3),
                                    manhattan(position, cell),
                                    cell,
                                )
                            )

                for _, _, firing_cell in sorted(core_firing_cells):
                    destination = first_step_astar(
                        position,
                        firing_cell,
                        ranger_navigation_obstacles
                        | context.visible_enemy_unit_cells,
                        set(occupied),
                    )
                    if destination is None or destination in occupied:
                        continue
                    direction = direction_between(position, destination)
                    if direction is None:
                        continue
                    ranger.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(ranger.id)[:8]} roam-core-aim {direction.value}"
                    )
                    break
                else:
                    core_target = None
                if core_target is not None:
                    continue

            can_engage_combat = nearby_combat_enemies and (
                len(local_roaming_allies) > len(nearby_combat_enemies)
                or (
                    context.roam_is_aggressive
                    and len(local_roaming_allies) >= len(nearby_combat_enemies)
                )
            )
            roaming_targets = []
            if can_engage_combat:
                for enemy in nearby_combat_enemies:
                    enemy_position = tuple(enemy.position)
                    trap_possible = roam_trap_possible(
                        enemy_position,
                        context.roaming_combat_units,
                        context.trap_obstacles,
                        occupied.occupied_cells(),
                    )
                    if memory.can_continue_roam_chase(
                        enemy.id,
                        context.turn.tick,
                        trap_possible,
                    ):
                        roaming_targets.append(enemy)
            if context.roam_target_enemy is not None:
                roaming_targets.append(context.roam_target_enemy)

            shootable_roaming = [
                enemy
                for enemy in roaming_targets
                if tuple(enemy.position) not in context.known_enemy_core_cells
                and clear_ranger_shot(
                    position,
                    tuple(enemy.position),
                    ranger_shot_blockers,
                )
            ]
            if shootable_roaming:
                target_enemy = min(
                    shootable_roaming,
                    key=lambda enemy: (
                        enemy.unit_type is UnitType.WORKER,
                        abs(
                            (ranger_line_distance(position, tuple(enemy.position)) or 0)
                            - 3
                        ),
                    ),
                )
                ranger.shoot(target_enemy)
                occupied.add(position)
                actions.append(
                    f"{str(ranger.id)[:8]} roam-shoot "
                    f"{target_enemy.unit_type.value}"
                )
                continue

            # 障碍物确实挡线时，寻找射程 3 格附近的可移动开火位。
            firing_cells: list[tuple[int, int, Pos]] = []
            for enemy in roaming_targets:
                target_position = tuple(enemy.position)
                for delta in SCOUT_VECTORS:
                    for distance in range(1, 4):
                        cell = (
                            target_position[0] - delta[0] * distance,
                            target_position[1] - delta[1] * distance,
                        )
                        movement_blockers = (
                            ranger_navigation_obstacles
                            | occupied.occupied_cells()
                            | visible_entity_cells
                        ) - {target_position, cell}
                        if (
                            chebyshev(cell, context.core_pos) <= ROAM_RADIUS
                            and cell not in movement_blockers
                            and clear_ranger_shot(
                                cell,
                                target_position,
                                ranger_shot_blockers,
                            )
                        ):
                            firing_cells.append(
                                (
                                    abs(distance - 3),
                                    manhattan(position, cell),
                                    cell,
                                )
                            )

            moved = False
            for _, _, firing_cell in sorted(firing_cells):
                destination = first_step_astar(
                    position,
                    firing_cell,
                    ranger_navigation_obstacles
                    | context.visible_enemy_unit_cells,
                    set(occupied),
                )
                if destination is None or destination in occupied:
                    continue
                direction = direction_between(position, destination)
                if direction is None:
                    continue
                ranger.move(direction)
                occupied.add(destination)
                actions.append(f"{str(ranger.id)[:8]} roam-aim {direction.value}")
                moved = True
                break
            if moved:
                continue

            follow_target_id = follow_assignments.get(ranger.id)
            follow_target_position = vanguard_positions.get(follow_target_id)
            if follow_target_position is not None:
                destination = ranger_follow_destination(
                    context,
                    position,
                    follow_target_position,
                    ranger_navigation_obstacles,
                )
                if destination == position:
                    ranger.wait()
                    occupied.add(position)
                    actions.append(
                        f"{str(ranger.id)[:8]} roam-follow-hold "
                        f"{str(follow_target_id)[:8]}"
                    )
                    continue
                if destination is not None:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        ranger.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(ranger.id)[:8]} roam-follow "
                            f"{str(follow_target_id)[:8]} {direction.value}"
                        )
                        continue

            patrol_goal = roam_core_reacquire_goal(context, position)
            if patrol_goal is None:
                patrol_goal = memory.roam_goal_for(
                    ranger.id,
                    context.core_pos,
                    position,
                    memory.known_obstacles | context.known_enemy_core_cells,
                )
            destination = first_step_astar(
                position,
                patrol_goal,
                ranger_navigation_obstacles | context.visible_enemy_unit_cells,
                set(occupied),
            )
            if destination is not None and destination not in occupied:
                direction = direction_between(position, destination)
                if direction is not None:
                    ranger.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(ranger.id)[:8]} roam-patrol {patrol_goal} "
                        f"{direction.value}"
                    )
                    continue
            ranger.wait()
            occupied.add(position)
            actions.append(f"{str(ranger.id)[:8]} roam-wait")
            continue

        # 家园 Ranger：优先直接射击，再在 7x7 防区内寻找开火位。
        shootable = [
            enemy
            for enemy in context.home_enemy_units
            if clear_ranger_shot(
                position,
                tuple(enemy.position),
                ranger_shot_blockers,
            )
        ]
        target_enemy = min(
            shootable,
            key=lambda enemy: (
                enemy.unit_type is UnitType.WORKER,
                abs(
                    (ranger_line_distance(position, tuple(enemy.position)) or 0)
                    - 3
                ),
                -(ranger_line_distance(position, tuple(enemy.position)) or 0),
            ),
            default=None,
        )
        if target_enemy is not None:
            ranger.shoot(target_enemy)
            occupied.add(position)
            actions.append(
                f"{str(ranger.id)[:8]} guard-shoot {target_enemy.unit_type.value}"
            )
            continue

        firing_cells: list[tuple[int, int, int, Pos]] = []
        for enemy in context.home_enemy_units:
            target_position = tuple(enemy.position)
            for delta in SCOUT_VECTORS:
                for distance in range(1, 4):
                    cell = (
                        target_position[0] - delta[0] * distance,
                        target_position[1] - delta[1] * distance,
                    )
                    if chebyshev(cell, context.core_pos) > HOME_PATROL_RADIUS:
                        continue
                    movement_blockers = (
                        ranger_navigation_obstacles
                        | occupied.occupied_cells()
                        | visible_entity_cells
                        | {context.core_pos}
                    ) - {target_position, cell}
                    if cell not in movement_blockers and clear_ranger_shot(
                        cell,
                        target_position,
                        ranger_shot_blockers,
                    ):
                        priority = 1 if enemy.unit_type is UnitType.WORKER else 0
                        firing_cells.append(
                            (
                                priority,
                                abs(distance - 3),
                                manhattan(position, cell),
                                cell,
                            )
                        )

        moved = False
        for _, _, _, firing_cell in sorted(
            firing_cells,
            key=lambda item: (item[0], item[1], item[2], item[3]),
        ):
            destination = first_step_astar(
                position,
                firing_cell,
                ranger_navigation_obstacles,
                occupied | visible_entity_cells,
            )
            if (
                destination is None
                or destination in occupied
                or chebyshev(destination, context.core_pos) > HOME_PATROL_RADIUS
            ):
                continue
            direction = direction_between(position, destination)
            if direction is None:
                continue
            ranger.move(direction)
            occupied.add(destination)
            actions.append(f"{str(ranger.id)[:8]} guard-aim {direction.value}")
            moved = True
            break
        if moved:
            continue

        patrol_phase = memory.ranger_patrol_phase.get(ranger.id, 0)
        for _ in range(len(HOME_PATROL_OFFSETS)):
            patrol_goal = add(
                context.core_pos,
                HOME_PATROL_OFFSETS[patrol_phase % len(HOME_PATROL_OFFSETS)],
            )
            if patrol_goal in ranger_navigation_obstacles:
                patrol_phase += 1
                continue
            if position == patrol_goal:
                patrol_phase += 1
                memory.ranger_patrol_phase[ranger.id] = patrol_phase
                continue
            destination = first_step_astar(
                position,
                patrol_goal,
                ranger_navigation_obstacles | {context.core_pos},
                occupied | visible_entity_cells,
            )
            if (
                destination is None
                or destination in occupied
                or (
                    chebyshev(position, context.core_pos) <= HOME_PATROL_RADIUS
                    and chebyshev(destination, context.core_pos)
                    > HOME_PATROL_RADIUS
                )
            ):
                patrol_phase += 1
                continue
            direction = direction_between(position, destination)
            if direction is None:
                patrol_phase += 1
                continue
            ranger.move(direction)
            occupied.add(destination)
            memory.ranger_patrol_phase[ranger.id] = patrol_phase
            actions.append(f"{str(ranger.id)[:8]} ranger-patrol {direction.value}")
            moved = True
            break
        if moved:
            continue

        ranger.wait()
        occupied.add(position)
        actions.append(f"{str(ranger.id)[:8]} ranger-guard-wait")


def plan_core_production(
    context: PlanningContext,
    mode: str,
    target: int,
) -> None:
    """按运行模式规划 Core 生产，并严格遵守 19 人自动上限。"""
    turn = context.turn
    memory = context.memory
    actions = context.actions
    core_available = (
        turn.core.view.state is CoreState.NORMAL
        and context.core_pos not in context.occupied
    )
    if not core_available or turn.state.population >= MAX_AUTO_POPULATION:
        return

    # 控制模式：家园守军、Worker、基础巡逻队，最后才用高库存扩军。
    if mode == "control":
        local_defenders = sum(
            manhattan(tuple(unit.position), context.core_pos) <= HOME_ENGAGE_RADIUS
            for unit in (*context.vanguards, *context.rangers)
        )
        emergency_gap = max(0, len(context.home_combat_targets) - local_defenders)
        max_control_vanguards = (
            TARGET_HOME_VANGUARDS
            + TARGET_ROAM_VANGUARDS
            + MAX_EMERGENCY_VANGUARDS
        )
        roaming_vanguard_count = sum(
            unit.id != memory.home_vanguard_id for unit in context.vanguards
        )
        roaming_ranger_count = sum(
            unit.id != memory.home_ranger_id for unit in context.rangers
        )
        core_pressure_active = (
            turn.resources * CORE_PRESSURE_DENOMINATOR
            >= turn.resource_capacity * CORE_PRESSURE_NUMERATOR
        )
        spawn_type: UnitType | None = None
        spawn_reason = ""
        if memory.home_vanguard_id is None and turn.resources >= 10:
            spawn_type = UnitType.VANGUARD
            spawn_reason = "restore home Vanguard"
        elif memory.home_ranger_id is None and turn.resources >= 12:
            spawn_type = UnitType.RANGER
            spawn_reason = "restore home Ranger"
        elif (
            emergency_gap > 0
            and len(context.vanguards) < max_control_vanguards
            and turn.resources >= 10
        ):
            spawn_type = UnitType.VANGUARD
            spawn_reason = f"emergency gap={emergency_gap}"
        elif len(context.workers) < TARGET_WORKERS_CONTROL and turn.resources >= 5:
            spawn_type = UnitType.WORKER
            spawn_reason = (
                f"expand Workers {len(context.workers) + 1}/"
                f"{TARGET_WORKERS_CONTROL}"
            )
        elif (
            roaming_vanguard_count < TARGET_ROAM_VANGUARDS
            and turn.resources >= 10
        ):
            spawn_type = UnitType.VANGUARD
            spawn_reason = "restore roaming Vanguard"
        elif core_pressure_active:
            next_pressure_type = (
                UnitType.VANGUARD
                if roaming_vanguard_count
                < ROAM_VANGUARDS_PER_RANGER * (roaming_ranger_count + 1)
                else UnitType.RANGER
            )
            pressure_cost = 10 if next_pressure_type is UnitType.VANGUARD else 12
            if turn.resources >= pressure_cost:
                spawn_type = next_pressure_type
                spawn_reason = (
                    f"core pressure {turn.resources}/{turn.resource_capacity} "
                    f"roam={roaming_vanguard_count}V:{roaming_ranger_count}R"
                )
        if spawn_type is not None:
            turn.core.spawn(spawn_type)
            actions.append(f"core spawn {spawn_type.value} ({spawn_reason})")
        return

    # 采集模式：先保证五个 Worker，再按容量或防御缺口补 Vanguard。
    if mode == "harvest":
        spawned_worker = False
        if len(context.workers) < 5:
            worker_threshold = 5 if len(context.workers) < 4 else 15
            if turn.resources >= worker_threshold:
                turn.core.spawn(UnitType.WORKER)
                spawned_worker = True
                actions.append(f"core spawn WORKER (reserve={turn.resources - 5})")
        if not spawned_worker:
            needs_capacity = turn.resource_capacity < target
            defense_is_thin = bool(context.home_combat_targets) and (
                not context.vanguards
                or len(context.home_combat_targets)
                > len(context.rangers) + len(context.vanguards)
            )
            if turn.resources >= 10 and (needs_capacity or defense_is_thin):
                turn.core.spawn(UnitType.VANGUARD)
                reason = (
                    "capacity replacement"
                    if needs_capacity
                    else "defense reinforcement"
                )
                actions.append(f"core spawn VANGUARD ({reason})")


def plan_turn(
    turn,
    memory: AgentMemory,
    target: int = 30,
    mode: str = "harvest",
) -> tuple[list[str], bool]:
    actions: list[str] = []
    memory.known_obstacles.update(tuple(p) for p in turn.obstacle_cells)
    memory.observe_dynamic_blocks(turn.events, turn.tick)

    if mode == "harvest" and turn.resources >= target:
        return actions, True
    if turn.state.status is not PlayerStatus.ACTIVE or turn.core is None:
        return actions, False

    core_pos: Pos = tuple(turn.core.position)
    workers = sorted(turn.workers, key=lambda worker: str(worker.id))
    vanguards = sorted(turn.vanguards, key=lambda unit: str(unit.id))
    rangers = sorted(turn.rangers, key=lambda unit: str(unit.id))
    memory.sync_core_position(core_pos)
    sectors_changed = memory.prune_unit_state(workers, vanguards, rangers)
    roles_changed = memory.sync_combat_roles(vanguards, rangers, core_pos)
    sectors_before = dict(memory.worker_sector)
    memory.sync_worker_sectors(workers)
    sectors_changed = sectors_changed or memory.worker_sector != sectors_before
    memory.observe_worker_harvests(turn.events, workers)
    memory.sync_ranger_coverage(bool(turn.rangers), workers)
    friendly_positions = tuple(tuple(unit.position) for unit in turn.units)
    vision_sources = friendly_vision_sources(turn.core, turn.units)
    visible_resources: set[Pos] = {tuple(position) for position in turn.resource_cells}
    obstacle_resource_conflicts = memory.known_resources & memory.known_obstacles
    resources_changed = bool(obstacle_resource_conflicts)
    if obstacle_resource_conflicts:
        memory.known_resources.difference_update(obstacle_resource_conflicts)
        for resource in obstacle_resource_conflicts:
            memory.resource_deferred_until.pop(resource, None)
        for worker_id, resource in list(memory.worker_resource_target.items()):
            if resource in obstacle_resource_conflicts:
                memory.worker_resource_target.pop(worker_id, None)
    resources_changed = (
        memory.observe_resources(
            visible_resources,
            workers,
            vision_sources,
            memory.known_obstacles,
            turn.events,
        )
        or resources_changed
    )
    visible_enemy_cores = tuple(
        enemy for enemy in turn.visible_enemies if enemy.kind == "CORE"
    )
    combat_enemies = tuple(
        enemy
        for enemy in turn.visible_enemies
        if enemy.kind == "UNIT"
        and enemy.unit_type in {UnitType.VANGUARD, UnitType.RANGER}
    )
    for enemy in combat_enemies:
        memory.known_combat_threats[enemy.id] = (
            tuple(enemy.position),
            turn.tick,
        )
    memory.known_combat_threats = {
        enemy_id: sighting
        for enemy_id, sighting in memory.known_combat_threats.items()
        if sighting[1] >= turn.tick - COMBAT_THREAT_MEMORY_TICKS
    }
    enemy_cores_changed = memory.observe_enemy_cores(
        turn.visible_enemies,
        vision_sources,
        memory.known_obstacles,
        turn.tick,
    )
    memory.observe_enemy_workers(turn.visible_enemies, turn.tick)
    # 敌方 Worker 在视野边缘反复显隐时，短暂保留最后目击格作为寻路障碍，
    # 避免返航 Worker 在两条等价路线之间来回切换。轨迹连续三 Tick 未见即过期。
    known_enemy_worker_cells = {
        track.position for track in memory.enemy_worker_tracks.values()
    }
    danger_cells: set[Pos] = set()
    for threat, _ in memory.known_combat_threats.values():
        for dx in range(-3, 4):
            remaining = 3 - abs(dx)
            for dy in range(-remaining, remaining + 1):
                danger_cells.add((threat[0] + dx, threat[1] + dy))
    # 敌方 Core 和 Worker 没有攻击能力；只有 Core 所在格阻挡移动。
    visible_enemy_unit_cells = {
        tuple(enemy.position) for enemy in turn.visible_enemies if enemy.kind == "UNIT"
    }
    friendly_counts = Counter(friendly_positions)
    for blocked_cell in list(memory.temporary_blocked_cells):
        if (
            friendly_counts.get(blocked_cell, 0) < 2
            and blocked_cell not in visible_enemy_unit_cells
        ):
            memory.temporary_blocked_cells.pop(blocked_cell, None)
    known_enemy_core_cells = {
        position for position, _ in memory.known_enemy_cores.values()
    }
    # 普通 Unit 目标排除与敌方 Core 同格的单位；激进巡逻会单独把 Core 作为目标。
    safe_enemy_units = tuple(
        enemy
        for enemy in turn.visible_enemies
        if enemy.kind == "UNIT" and tuple(enemy.position) not in known_enemy_core_cells
    )
    navigation_obstacles = (
        memory.known_obstacles
        | known_enemy_core_cells
        | danger_cells
        | visible_enemy_unit_cells
        | known_enemy_worker_cells
        | set(memory.temporary_blocked_cells)
    )
    threat_positions = tuple(
        position for position, _ in memory.known_combat_threats.values()
    )
    home_combat_targets = tuple(
        enemy
        for enemy in safe_enemy_units
        if enemy.unit_type in {UnitType.VANGUARD, UnitType.RANGER}
        and manhattan(core_pos, tuple(enemy.position)) <= HOME_ENGAGE_RADIUS
    )
    home_enemy_units = tuple(
        enemy
        for enemy in safe_enemy_units
        if manhattan(core_pos, tuple(enemy.position)) <= HOME_ENGAGE_RADIUS
    )

    # 资源任务跨 Tick 保留；只有空载、无资源任务且不在撤退的 Worker 才空闲。
    occupied = FriendlyOccupancy(tuple(unit.position) for unit in turn.units)
    resource_assignments = memory.assign_resource_targets(
        workers,
        memory.known_resources,
        visible_resources,
        turn.tick,
        navigation_obstacles,
        memory.known_obstacles
        | known_enemy_core_cells
        | visible_enemy_unit_cells
        | known_enemy_worker_cells,
    )
    roaming_combat_units = tuple(
        unit
        for unit in (*vanguards, *rangers)
        if unit.id not in {memory.home_vanguard_id, memory.home_ranger_id}
    )
    memory.prune_roam_chases(turn.tick)
    roam_is_aggressive = len(roaming_combat_units) >= ROAM_AGGRESSIVE_SIZE
    trap_obstacles = memory.known_obstacles | known_enemy_core_cells
    visible_enemy_workers = {
        enemy.id: enemy
        for enemy in safe_enemy_units
        if enemy.unit_type is UnitType.WORKER
    }
    memory.worker_intercept_goal = {
        worker_id: assignment
        for worker_id, assignment in memory.worker_intercept_goal.items()
        if assignment[1] >= turn.tick
    }
    roam_target_track: EnemyWorkerTrack | None = None
    roam_target_enemy = None
    roam_target_id: UUID | None = None
    blocker_worker_id: UUID | None = None
    if roaming_combat_units:
        candidate_tracks = [
            (enemy_id, track)
            for enemy_id, track in memory.enemy_worker_tracks.items()
            # 巡逻边界是以 Core 为中心的方形；对角区域不能按曼哈顿距离误判为越界。
            if chebyshev(core_pos, track.position) <= ROAM_RADIUS
            and manhattan(track.first_seen_position, track.position) <= ROAM_CHASE_STEPS
            and (
                roam_is_aggressive
                or not any(
                    manhattan(track.position, tuple(enemy.position)) <= 6
                    for enemy in combat_enemies
                )
            )
        ]
        candidate_tracks.sort(
            key=lambda item: (
                min(
                    manhattan(tuple(unit.position), item[1].position)
                    for unit in roaming_combat_units
                ),
                item[1].position,
            )
        )
        for enemy_id, track in candidate_tracks:
            trap_possible = roam_trap_possible(
                track.position,
                roaming_combat_units,
                trap_obstacles,
                occupied.occupied_cells(),
            )
            helper = None
            if track.is_moving:
                helper = min(
                    (
                        worker
                        for worker in workers
                        if worker.cargo == 0
                        and worker.id not in resource_assignments
                        and turn.tick >= memory.retreat_until.get(worker.id, 0)
                        and manhattan(tuple(worker.position), track.position)
                        <= ROAM_HELPER_RADIUS
                    ),
                    key=lambda worker: (
                        manhattan(tuple(worker.position), track.position),
                        str(worker.id),
                    ),
                    default=None,
                )
                if helper is None and not roam_is_aggressive:
                    continue
                if helper is not None:
                    intercept_goal = add(track.position, track.movement_delta)
                    intercept_blocked = (
                        intercept_goal in memory.known_obstacles
                        or intercept_goal in known_enemy_core_cells
                        or intercept_goal in visible_enemy_unit_cells
                    )
                    if intercept_blocked and not roam_is_aggressive:
                        continue
                    if not intercept_blocked:
                        blocker_worker_id = helper.id
                        memory.worker_intercept_goal[helper.id] = (
                            intercept_goal,
                            turn.tick + ROAM_TARGET_LOST_TICKS,
                        )
            if not memory.can_continue_roam_chase(
                enemy_id,
                turn.tick,
                trap_possible,
            ):
                if helper is not None and blocker_worker_id == helper.id:
                    blocker_worker_id = None
                    memory.worker_intercept_goal.pop(helper.id, None)
                continue
            roam_target_track = track
            roam_target_enemy = visible_enemy_workers.get(enemy_id)
            roam_target_id = enemy_id
            break

    context = PlanningContext(
        turn=turn,
        memory=memory,
        core_pos=core_pos,
        workers=workers,
        vanguards=vanguards,
        rangers=rangers,
        visible_resources=visible_resources,
        visible_enemy_unit_cells=visible_enemy_unit_cells,
        known_enemy_core_cells=known_enemy_core_cells,
        visible_enemy_cores=visible_enemy_cores,
        combat_enemies=combat_enemies,
        home_combat_targets=home_combat_targets,
        home_enemy_units=home_enemy_units,
        navigation_obstacles=navigation_obstacles,
        danger_cells=danger_cells,
        threat_positions=threat_positions,
        occupied=occupied,
        resource_assignments=resource_assignments,
        roaming_combat_units=roaming_combat_units,
        roam_is_aggressive=roam_is_aggressive,
        trap_obstacles=trap_obstacles,
        roam_target_track=roam_target_track,
        roam_target_enemy=roam_target_enemy,
        roam_target_id=roam_target_id,
        blocker_worker_id=blocker_worker_id,
        actions=actions,
    )
    plan_workers(context)
    plan_vanguards(context)
    plan_rangers(context)
    plan_core_production(context, mode, target)

    if (
        resources_changed
        or roles_changed
        or sectors_changed
        or enemy_cores_changed
        or turn.tick - memory.last_state_save_tick >= STATE_SAVE_INTERVAL_TICKS
    ):
        save_state(memory.persistent_state())
        memory.last_state_save_tick = turn.tick

    return actions, False


def event_summary(turn) -> str:
    if not turn.events:
        return ""
    rendered = []
    for event in turn.events:
        amount = event.resource_amount
        suffix = f":{amount}" if amount is not None else ""
        reason = f"/{event.reason_code}" if event.reason_code else ""
        position = (
            f"@{tuple(event.position)}"
            if event.event_type == "UNIT_MOVE_FAILED" and event.position is not None
            else ""
        )
        rendered.append(f"{event.event_type}{reason}{position}{suffix}")
    return ",".join(rendered)


def configure_run_logger() -> logging.Logger:
    """创建按大小轮转的进程日志，且不记录 API Key。"""
    logger = logging.getLogger("arena_core_agent")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def emit(logger: logging.Logger, message: str, *, error: bool = False) -> None:
    print(message, file=sys.stderr if error else sys.stdout, flush=True)
    (logger.error if error else logger.info)(message)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Arena Hero harvesting or long-term control Agent."
    )
    parser.add_argument(
        "--mode",
        choices=("control", "harvest"),
        default="control",
        help="control runs indefinitely; harvest exits at --target",
    )
    parser.add_argument("--target", type=int, default=30)
    args = parser.parse_args()

    logger = configure_run_logger()
    load_local_env()
    api_key = os.environ.get("ARENA_HERO_API_KEY", "").strip()
    if not api_key:
        emit(logger, "ARENA_HERO_API_KEY is not set", error=True)
        return 2
    if args.mode == "harvest" and args.target < 1:
        emit(logger, "target must be positive", error=True)
        return 2
    memory = AgentMemory.restore(load_persistent_state())
    emit(
        logger,
        f"agent start mode={args.mode} target={args.target} "
        f"auto_reconnect=true time_limit=none "
        f"known_resources={len(memory.known_resources)}",
    )

    reconnect_delay = 1.0
    while True:
        try:
            with ArenaHeroClient(api_key=api_key) as game:
                for turn in game.turns():
                    reconnect_delay = 1.0
                    events = event_summary(turn)
                    actions, reached = plan_turn(
                        turn,
                        memory,
                        target=args.target,
                        mode=args.mode,
                    )
                    core_label = (
                        "none" if turn.core is None else str(tuple(turn.core.position))
                    )
                    target_summary = (
                        ",".join(
                            f"{str(worker_id)[:8]}:{position}"
                            for worker_id, position in sorted(
                                memory.worker_resource_target.items(),
                                key=lambda item: str(item[0]),
                            )
                        )
                        or "none"
                    )
                    emit(
                        logger,
                        f"tick={turn.tick} status={turn.state.status.value} "
                        f"core={core_label} resources={turn.resources}/{turn.resource_capacity} "
                        f"population={turn.state.population} workers={len(turn.workers)} "
                        f"vanguards={len(turn.vanguards)} rangers={len(turn.rangers)} "
                        f"visible_resources={len(turn.resource_cells)} "
                        f"known_resources={len(memory.known_resources)} "
                        f"resource_targets={len(memory.worker_resource_target)} "
                        f"target_map=[{target_summary}] "
                        f"resource_deferred={len(memory.resource_deferred_until)} "
                        f"temporary_blocks={len(memory.temporary_blocked_cells)} "
                        f"expanded_low_yield="
                        f"{','.join(sorted(str(worker_id)[:8] for worker_id in memory.expanded_low_yield)) or 'none'} "
                        f"home_vanguard="
                        f"{str(memory.home_vanguard_id)[:8] if memory.home_vanguard_id else 'none'} "
                        f"home_ranger="
                        f"{str(memory.home_ranger_id)[:8] if memory.home_ranger_id else 'none'} "
                        f"enemy_cores={len(memory.known_enemy_cores)} "
                        f"enemy_worker_tracks={len(memory.enemy_worker_tracks)} "
                        f"events=[{events}]",
                    )

                    if reached:
                        emit(
                            logger,
                            f"TARGET_REACHED resources={turn.resources} "
                            f"capacity={turn.resource_capacity} tick={turn.tick}",
                        )
                        return 0

                    # 由 SDK 生成进程唯一键：同一请求重试复用该键，进程重启后
                    # 也不会与旧 Tick 的计划发生幂等键冲突。
                    turn.submit()
                    emit(
                        logger,
                        "actions=" + ("; ".join(actions) if actions else "WAIT"),
                    )

            raise TransportError("event stream ended unexpectedly")
        except (
            AuthenticationError,
            ConfigurationError,
            InvalidActionError,
            PolicyViolationError,
        ) as exc:
            emit(
                logger,
                f"FATAL_ERROR type={type(exc).__name__} detail={exc}",
                error=True,
            )
            return 5
        except APIError as exc:
            idempotency_conflict = (
                exc.status_code == 409 and exc.error == "IDEMPOTENCY_CONFLICT"
            )
            if (
                not idempotency_conflict
                and exc.status_code != 429
                and exc.status_code < 500
            ):
                emit(
                    logger,
                    f"FATAL_API_ERROR status={exc.status_code} detail={exc}",
                    error=True,
                )
                return 5
            emit(
                logger,
                f"RECOVERABLE_API_ERROR status={exc.status_code} "
                f"retry_in={reconnect_delay:g}s detail={exc}",
                error=True,
            )
        except (ArenaHeroError, OSError, TimeoutError) as exc:
            emit(
                logger,
                f"SESSION_RESTART type={type(exc).__name__} "
                f"retry_in={reconnect_delay:g}s detail={exc}",
                error=True,
            )

        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60.0)


if __name__ == "__main__":
    raise SystemExit(main())
