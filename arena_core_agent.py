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
ROAM_HOME_RESPONSE_RADIUS = 18
ROAM_RADIUS = 24
RESOURCE_MEMORY_RADIUS = 36
RESOURCE_SCOUT_RADII: tuple[int, ...] = (12, 19, 26, 32)
RESOURCE_SCOUT_RING_SEQUENCE: tuple[int, ...] = (
    *RESOURCE_SCOUT_RADII,
    *reversed(RESOURCE_SCOUT_RADII[1:-1]),
)
RESOURCE_SCOUT_WAYPOINT_STEP = 7
RESOURCE_SCOUT_PATH_FAILURES = 3
OUTER_SCOUT_RESOURCE_THRESHOLD = 80
OUTER_SCOUT_RADII: tuple[int, ...] = (32, 39, 46, 53, 60, 64)
OUTER_SCOUT_RING_SEQUENCE: tuple[int, ...] = (
    *OUTER_SCOUT_RADII,
    *reversed(OUTER_SCOUT_RADII[1:-1]),
)
OUTER_SCOUT_WAYPOINT_STEP = 7
OUTER_SCOUT_PATH_FAILURES = 3
# Worker 视野半径为 3；外圈目标至少相隔 7 格，避免两个视野相交。
OUTER_SCOUT_MIN_GOAL_DISTANCE = 7
ROAM_CHASE_STEPS = 8
ROAM_TARGET_LOST_TICKS = 3
ROAM_HELPER_RADIUS = 5
ROAM_AGGRESSIVE_SIZE = 3
ROAM_CHASE_TICKS = 8
ROAM_TRAP_CHASE_TICKS = 16
ROAM_CHASE_COOLDOWN_TICKS = 12
ROAM_TRAP_ALLY_RADIUS = 5
ROAM_TRAP_MAX_EXITS = 2
TARGET_WORKERS_CONTROL = 4
SQUAD_VANGUARDS = 2
SQUAD_RANGERS = 1
SQUAD_FORMATION_RADIUS = 4
SQUAD_FOLLOW_DISTANCE = 2
SQUAD_REGROUP_TRIGGER_DISTANCE = 8
SQUAD_REGROUP_AREA_RADIUS = 2
SQUAD_REGROUP_SAFE_TICKS = 2
SQUAD_REGROUP_STALL_TICKS = 3
ASSAULT_GATHER_RADIUS = 5
ASSAULT_TARGET_MEMORY_TICKS = 6
# Core 周围发现具备攻击力的敌方单位后，巡逻队在该距离外集结。
ASSAULT_CORE_GUARD_RADIUS = 6
ASSAULT_CORE_SAFE_DISTANCE = 6
# 只有当前 Core 64 格内的有护卫敌方 Core 才触发全体集结。
ASSAULT_HOME_CORE_DISTANCE = 64
SPAWN_CLEAR_TICKS = 3
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
UNIT_MAX_HP: dict[UnitType, int] = {
    UnitType.WORKER: 2,
    UnitType.VANGUARD: 4,
    UnitType.RANGER: 2,
}
TEMPORARY_BLOCK_TICKS = 8
RESOURCE_REASSIGN_MIN_GAIN = 4
# 距离仍是资源匹配的主要成本，同时用历史负载打散连续任务。
RESOURCE_DISTANCE_COST = 10
RESOURCE_LOAD_COST = 3
COMBAT_THREAT_MEMORY_TICKS = 6
STATE_SAVE_INTERVAL_TICKS = 10
STATE_VERSION = 8
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


def decode_uuid(raw_value) -> UUID | None:
    if not isinstance(raw_value, str):
        return None
    try:
        return UUID(raw_value)
    except ValueError:
        return None


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


def inclusive_axis_steps(start: int, end: int, step: int) -> tuple[int, ...]:
    """生成包含两端且相邻距离不超过 step 的单调坐标。"""
    direction = 1 if end >= start else -1
    values = [start]
    while abs(end - values[-1]) > step:
        values.append(values[-1] + direction * step)
    if values[-1] != end:
        values.append(end)
    return tuple(values)


def square_ring_waypoints(
    core: Pos,
    radius: int,
    step: int = OUTER_SCOUT_WAYPOINT_STEP,
) -> tuple[Pos, ...]:
    """从左上角开始，沿方形环顺时针生成覆盖四边和四角的目标。"""
    left = core[0] - radius
    right = core[0] + radius
    top = core[1] - radius
    bottom = core[1] + radius
    horizontal_forward = inclusive_axis_steps(left, right, step)
    vertical_forward = inclusive_axis_steps(top, bottom, step)
    horizontal_reverse = inclusive_axis_steps(right, left, step)
    vertical_reverse = inclusive_axis_steps(bottom, top, step)
    return tuple(
        [(x, top) for x in horizontal_forward]
        + [(right, y) for y in vertical_forward[1:]]
        + [(x, bottom) for x in horizontal_reverse[1:]]
        + [(left, y) for y in vertical_reverse[1:-1]]
    )


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


@dataclass(frozen=True)
class CombatSquad:
    squad_id: int
    vanguard_ids: tuple[UUID, ...]
    ranger_ids: tuple[UUID, ...]

    @property
    def unit_ids(self) -> set[UUID]:
        return set(self.vanguard_ids) | set(self.ranger_ids)

    @property
    def complete(self) -> bool:
        return (
            len(self.vanguard_ids) == SQUAD_VANGUARDS
            and len(self.ranger_ids) == SQUAD_RANGERS
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
    scout_ring_index: dict[UUID, int] = field(default_factory=dict)
    scout_phase: dict[UUID, int] = field(default_factory=dict)
    scout_goal: dict[UUID, Pos] = field(default_factory=dict)
    scout_path_failures: dict[UUID, int] = field(default_factory=dict)
    outer_scout_active: bool = False
    outer_scout_ring_index: dict[UUID, int] = field(default_factory=dict)
    outer_scout_step: dict[UUID, int] = field(default_factory=dict)
    outer_scout_goal: dict[UUID, Pos] = field(default_factory=dict)
    outer_scout_path_failures: dict[UUID, int] = field(default_factory=dict)
    worker_sector: dict[UUID, int] = field(default_factory=dict)
    productive_sector_cursor: int = 0
    ranger_coverage_active: bool = False
    worker_harvests: dict[UUID, int] = field(default_factory=dict)
    expanded_low_yield: set[UUID] = field(default_factory=set)
    worker_resource_target: dict[UUID, Pos] = field(default_factory=dict)
    resource_deferred_until: dict[Pos, int] = field(default_factory=dict)
    worker_intercept_goal: dict[UUID, tuple[Pos, int]] = field(default_factory=dict)

    # Vanguard/Ranger：持久化 2V1R 小队、巡逻目标和统一集结状态。
    ranger_patrol_phase: dict[UUID, int] = field(default_factory=dict)
    vanguard_patrol_phase: dict[UUID, int] = field(default_factory=dict)
    roam_phase: dict[UUID, int] = field(default_factory=dict)
    roam_goal: dict[UUID, Pos] = field(default_factory=dict)
    ranger_follow_vanguard: dict[UUID, UUID] = field(default_factory=dict)
    home_vanguard_id: UUID | None = None
    home_ranger_id: UUID | None = None
    squad_assignments: dict[UUID, int] = field(default_factory=dict)
    squad_patrol_goal: dict[int, Pos] = field(default_factory=dict)
    squad_regroup_goal: dict[int, Pos] = field(default_factory=dict)
    squad_regroup_interrupted: set[int] = field(default_factory=set)
    squad_regroup_safe_ticks: dict[int, int] = field(default_factory=dict)
    squad_regroup_last_distance: dict[int, int] = field(default_factory=dict)
    squad_regroup_stall_ticks: dict[int, int] = field(default_factory=dict)
    assault_target_id: UUID | None = None
    assault_target_kind: str | None = None
    assault_target_position: Pos | None = None
    assault_target_last_seen_tick: int = 0
    assault_guarded: bool = False
    assault_gathering: bool = False
    assault_rally_position: Pos | None = None
    enemy_worker_tracks: dict[UUID, EnemyWorkerTrack] = field(default_factory=dict)
    roam_chase_started: dict[UUID, int] = field(default_factory=dict)
    roam_chase_cooldown_until: dict[UUID, int] = field(default_factory=dict)

    spawn_clear_until: int = 0

    # 生命周期与持久化节流。
    last_core_position: Pos | None = None
    last_state_save_tick: int = 0

    @classmethod
    def restore(cls, state: dict) -> "AgentMemory":
        memory = cls(
            known_resources=decode_positions(state.get("known_resources", [])),
            known_obstacles=decode_positions(state.get("known_obstacles", [])),
        )
        state_version = state.get("version")
        restore_enemy_state = (
            isinstance(state_version, int) and state_version >= STATE_VERSION
        )
        raw_core_position = state.get("core_position")
        if (
            restore_enemy_state
            and isinstance(raw_core_position, list)
            and len(raw_core_position) == 2
            and all(isinstance(value, int) for value in raw_core_position)
        ):
            memory.last_core_position = tuple(raw_core_position)
        for attribute in ("home_vanguard_id", "home_ranger_id"):
            raw_id = state.get(attribute)
            if isinstance(raw_id, str):
                try:
                    setattr(memory, attribute, UUID(raw_id))
                except ValueError:
                    continue
        raw_squad_assignments = state.get("squad_assignments", {})
        if isinstance(raw_squad_assignments, dict):
            for raw_id, squad_id in raw_squad_assignments.items():
                try:
                    unit_id = UUID(raw_id)
                except (TypeError, ValueError):
                    continue
                if isinstance(squad_id, int) and squad_id >= 0:
                    memory.squad_assignments[unit_id] = squad_id
        raw_squad_goals = state.get("squad_patrol_goals", {})
        if isinstance(raw_squad_goals, dict):
            for raw_id, raw_position in raw_squad_goals.items():
                try:
                    squad_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if (
                    squad_id >= 0
                    and isinstance(raw_position, list)
                    and len(raw_position) == 2
                    and all(isinstance(value, int) for value in raw_position)
                ):
                    memory.squad_patrol_goal[squad_id] = tuple(raw_position)
        raw_regroup_goals = state.get("squad_regroup_goals", {})
        if isinstance(raw_regroup_goals, dict):
            for raw_id, raw_position in raw_regroup_goals.items():
                try:
                    squad_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if (
                    squad_id > 0
                    and isinstance(raw_position, list)
                    and len(raw_position) == 2
                    and all(isinstance(value, int) for value in raw_position)
                ):
                    memory.squad_regroup_goal[squad_id] = tuple(raw_position)
        raw_interrupted = state.get("squad_regroup_interrupted", [])
        if isinstance(raw_interrupted, list):
            memory.squad_regroup_interrupted = {
                squad_id
                for squad_id in raw_interrupted
                if isinstance(squad_id, int) and squad_id > 0
            }
        if restore_enemy_state:
            memory.assault_target_id = decode_uuid(state.get("assault_target_id"))
            target_kind = state.get("assault_target_kind")
            if target_kind in {"CORE", "UNIT"}:
                memory.assault_target_kind = target_kind
            raw_target_position = state.get("assault_target_position")
            if (
                isinstance(raw_target_position, list)
                and len(raw_target_position) == 2
                and all(isinstance(value, int) for value in raw_target_position)
            ):
                memory.assault_target_position = tuple(raw_target_position)
            last_seen_tick = state.get("assault_target_last_seen_tick")
            if isinstance(last_seen_tick, int) and last_seen_tick >= 0:
                memory.assault_target_last_seen_tick = last_seen_tick
            memory.assault_guarded = state.get("assault_guarded") is True
            memory.assault_gathering = state.get("assault_gathering") is True
            raw_rally = state.get("assault_rally_position")
            if (
                isinstance(raw_rally, list)
                and len(raw_rally) == 2
                and all(isinstance(value, int) for value in raw_rally)
            ):
                memory.assault_rally_position = tuple(raw_rally)
        spawn_clear_until = state.get("spawn_clear_until")
        if isinstance(spawn_clear_until, int) and spawn_clear_until >= 0:
            memory.spawn_clear_until = spawn_clear_until
        raw_sectors = state.get("worker_sectors", {})
        if isinstance(raw_sectors, dict):
            for raw_id, sector in raw_sectors.items():
                try:
                    worker_id = UUID(raw_id)
                except (TypeError, ValueError):
                    continue
                if isinstance(sector, int):
                    memory.worker_sector[worker_id] = sector % len(SCOUT_VECTORS)
        if restore_enemy_state:
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
            "version": STATE_VERSION,
            "core_position": (
                list(self.last_core_position)
                if self.last_core_position is not None
                else None
            ),
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
            "squad_assignments": {
                str(unit_id): squad_id
                for unit_id, squad_id in sorted(
                    self.squad_assignments.items(),
                    key=lambda item: str(item[0]),
                )
            },
            "squad_patrol_goals": {
                str(squad_id): list(position)
                for squad_id, position in sorted(self.squad_patrol_goal.items())
            },
            "squad_regroup_goals": {
                str(squad_id): list(position)
                for squad_id, position in sorted(self.squad_regroup_goal.items())
            },
            "squad_regroup_interrupted": sorted(self.squad_regroup_interrupted),
            "assault_target_id": (
                str(self.assault_target_id) if self.assault_target_id else None
            ),
            "assault_target_kind": self.assault_target_kind,
            "assault_target_position": (
                list(self.assault_target_position)
                if self.assault_target_position is not None
                else None
            ),
            "assault_target_last_seen_tick": self.assault_target_last_seen_tick,
            "assault_guarded": self.assault_guarded,
            "assault_gathering": self.assault_gathering,
            "assault_rally_position": (
                list(self.assault_rally_position)
                if self.assault_rally_position is not None
                else None
            ),
            "spawn_clear_until": self.spawn_clear_until,
        }

    def sync_core_position(self, core: Pos) -> bool:
        """Core 移动后重置所有依赖旧家位置的缓存目标。"""
        previous = self.last_core_position
        self.last_core_position = core
        if previous is None or previous == core:
            return False

        # 资源坐标和敌方轨迹属于世界坐标，仍然有效；侦察、巡逻和撤退目标则
        # 由旧家位置推导，必须清除。
        self.scout_ring_index.clear()
        self.scout_phase.clear()
        self.scout_goal.clear()
        self.scout_path_failures.clear()
        self.outer_scout_ring_index.clear()
        self.outer_scout_step.clear()
        self.outer_scout_goal.clear()
        self.outer_scout_path_failures.clear()
        self.roam_goal.clear()
        self.squad_patrol_goal.clear()
        self.squad_regroup_goal.clear()
        self.squad_regroup_interrupted.clear()
        self.squad_regroup_safe_ticks.clear()
        self.squad_regroup_last_distance.clear()
        self.squad_regroup_stall_ticks.clear()
        self.retreat_goal.clear()
        self.known_enemy_cores.clear()
        self.clear_assault()
        return True

    def prune_unit_state(self, workers, vanguards, rangers, tick: int = 0) -> bool:
        """删除死亡单位的运行时状态，并报告持久化扇区是否变化。"""
        worker_ids = {worker.id for worker in workers}
        vanguard_ids = {unit.id for unit in vanguards}
        ranger_ids = {unit.id for unit in rangers}
        combat_ids = vanguard_ids | ranger_ids
        sectors_before = dict(self.worker_sector)
        squads_before = dict(self.squad_assignments)

        for state in (
            self.retreat_until,
            self.retreat_goal,
            self.scout_ring_index,
            self.scout_phase,
            self.scout_goal,
            self.scout_path_failures,
            self.outer_scout_ring_index,
            self.outer_scout_step,
            self.outer_scout_goal,
            self.outer_scout_path_failures,
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
        self.squad_assignments = {
            unit_id: squad_id
            for unit_id, squad_id in self.squad_assignments.items()
            if unit_id in combat_ids
        }

        return (
            self.worker_sector != sectors_before
            or self.squad_assignments != squads_before
        )

    def combat_squads(
        self,
        vanguards: Iterable[Vanguard],
        rangers: Iterable[Ranger],
    ) -> tuple[CombatSquad, ...]:
        vanguard_ids = {unit.id for unit in vanguards}
        ranger_ids = {unit.id for unit in rangers}
        squad_ids = sorted(set(self.squad_assignments.values()))
        return tuple(
            CombatSquad(
                squad_id=squad_id,
                vanguard_ids=tuple(
                    sorted(
                        unit_id
                        for unit_id, assigned_id in self.squad_assignments.items()
                        if assigned_id == squad_id and unit_id in vanguard_ids
                    )
                ),
                ranger_ids=tuple(
                    sorted(
                        unit_id
                        for unit_id, assigned_id in self.squad_assignments.items()
                        if assigned_id == squad_id and unit_id in ranger_ids
                    )
                ),
            )
            for squad_id in squad_ids
        )

    def sync_combat_squads(
        self,
        vanguards,
        rangers,
        core: Pos,
    ) -> bool:
        """稳定维护 2V1R 编制；0 号完整小队永久负责守家。"""
        before = (
            dict(self.squad_assignments),
            dict(self.squad_patrol_goal),
            dict(self.squad_regroup_goal),
            set(self.squad_regroup_interrupted),
            self.home_vanguard_id,
            self.home_ranger_id,
        )
        vanguard_by_id = {unit.id: unit for unit in vanguards}
        ranger_by_id = {unit.id: unit for unit in rangers}
        live_ids = set(vanguard_by_id) | set(ranger_by_id)
        self.squad_assignments = {
            unit_id: squad_id
            for unit_id, squad_id in self.squad_assignments.items()
            if unit_id in live_ids and squad_id >= 0
        }

        # v4 只保存单个守家 Vanguard/Ranger；首次升级时把它们种到 0 号队。
        if self.home_vanguard_id in vanguard_by_id:
            self.squad_assignments.setdefault(self.home_vanguard_id, 0)
        if self.home_ranger_id in ranger_by_id:
            self.squad_assignments.setdefault(self.home_ranger_id, 0)

        # 清理损坏或旧版本造成的超编，超出的单位在下面重新分配。
        for squad in self.combat_squads(vanguards, rangers):
            for unit_id in squad.vanguard_ids[SQUAD_VANGUARDS:]:
                self.squad_assignments.pop(unit_id, None)
            for unit_id in squad.ranger_ids[SQUAD_RANGERS:]:
                self.squad_assignments.pop(unit_id, None)

        def role_count(squad_id: int, unit_type: UnitType) -> int:
            role_ids = (
                set(vanguard_by_id)
                if unit_type is UnitType.VANGUARD
                else set(ranger_by_id)
            )
            return sum(
                assigned_id == squad_id and unit_id in role_ids
                for unit_id, assigned_id in self.squad_assignments.items()
            )

        def assign_role(units, unit_type: UnitType, capacity: int) -> None:
            unassigned = sorted(
                (unit for unit in units if unit.id not in self.squad_assignments),
                key=lambda unit: (
                    manhattan(tuple(unit.position), core),
                    str(unit.id),
                ),
            )
            for unit in unassigned:
                squad_ids = sorted(set(self.squad_assignments.values()) | {0})
                candidates = [
                    squad_id
                    for squad_id in squad_ids
                    if role_count(squad_id, unit_type) < capacity
                ]
                if unit_type is UnitType.RANGER:
                    full_frontline = [
                        squad_id
                        for squad_id in candidates
                        if role_count(squad_id, UnitType.VANGUARD)
                        == SQUAD_VANGUARDS
                    ]
                    if full_frontline:
                        candidates = full_frontline
                if not candidates:
                    candidates = [max(squad_ids, default=-1) + 1]
                self.squad_assignments[unit.id] = candidates[0]

        assign_role(vanguards, UnitType.VANGUARD, SQUAD_VANGUARDS)
        assign_role(rangers, UnitType.RANGER, SQUAD_RANGERS)

        squads = self.combat_squads(vanguards, rangers)
        home_squad = next((squad for squad in squads if squad.squad_id == 0), None)
        self.home_vanguard_id = (
            home_squad.vanguard_ids[0]
            if home_squad and home_squad.vanguard_ids
            else None
        )
        self.home_ranger_id = (
            home_squad.ranger_ids[0]
            if home_squad and home_squad.ranger_ids
            else None
        )
        live_squad_ids = {squad.squad_id for squad in squads}
        self.squad_patrol_goal = {
            squad_id: goal
            for squad_id, goal in self.squad_patrol_goal.items()
            if squad_id in live_squad_ids and squad_id != 0
        }
        self.squad_regroup_goal = {
            squad_id: goal
            for squad_id, goal in self.squad_regroup_goal.items()
            if squad_id in live_squad_ids and squad_id != 0
        }
        self.squad_regroup_interrupted.intersection_update(
            live_squad_ids - {0}
        )
        for state in (
            self.squad_regroup_safe_ticks,
            self.squad_regroup_last_distance,
            self.squad_regroup_stall_ticks,
        ):
            for squad_id in tuple(state):
                if squad_id not in live_squad_ids or squad_id == 0:
                    state.pop(squad_id, None)
        after = (
            dict(self.squad_assignments),
            dict(self.squad_patrol_goal),
            dict(self.squad_regroup_goal),
            set(self.squad_regroup_interrupted),
            self.home_vanguard_id,
            self.home_ranger_id,
        )
        return before != after

    def clear_assault(self) -> None:
        self.assault_target_id = None
        self.assault_target_kind = None
        self.assault_target_position = None
        self.assault_target_last_seen_tick = 0
        self.assault_guarded = False
        self.assault_gathering = False
        self.assault_rally_position = None

    def sync_assault_target(
        self,
        visible_enemies,
        core: Pos,
        tick: int,
    ) -> bool:
        """记录敌方 Core；只有发现 Core 护卫时才要求巡逻队先集结。"""
        before = (
            self.assault_target_id,
            self.assault_target_kind,
            self.assault_target_position,
            self.assault_target_last_seen_tick,
            self.assault_guarded,
            self.assault_gathering,
            self.assault_rally_position,
        )
        visible_cores = tuple(
            enemy
            for enemy in visible_enemies
            if enemy.kind == "CORE"
        )
        target = min(
            visible_cores,
            key=lambda enemy: (
                manhattan(core, tuple(enemy.position)),
                str(enemy.id),
            ),
            default=None,
        )
        target_id = target.id if target is not None else None
        target_position = tuple(target.position) if target is not None else None
        if target is None and self.known_enemy_cores:
            target_id, (target_position, _) = min(
                self.known_enemy_cores.items(),
                key=lambda item: (manhattan(core, item[1][0]), str(item[0])),
            )
        if target is not None:
            target_changed = target.id != self.assault_target_id
            if target_changed:
                self.assault_guarded = False
                self.assault_gathering = False
                self.assault_rally_position = None
            self.assault_target_id = target.id
            self.assault_target_kind = target.kind
            self.assault_target_position = tuple(target.position)
            self.assault_target_last_seen_tick = tick
            has_guard = any(
                enemy.kind == "UNIT"
                and enemy.unit_type in {UnitType.VANGUARD, UnitType.RANGER}
                and manhattan(tuple(target.position), tuple(enemy.position))
                <= ASSAULT_CORE_GUARD_RADIUS
                for enemy in visible_enemies
            )
            if has_guard:
                if not self.assault_guarded:
                    self.assault_rally_position = None
                self.assault_guarded = True
                self.assault_gathering = True
            elif not self.assault_guarded:
                self.assault_gathering = False
        elif target_id is not None and target_position is not None:
            if target_id != self.assault_target_id:
                self.assault_guarded = False
                self.assault_gathering = False
                self.assault_rally_position = None
            self.assault_target_id = target_id
            self.assault_target_kind = "CORE"
            self.assault_target_position = target_position
            if any(
                enemy.kind == "UNIT"
                and enemy.unit_type in {UnitType.VANGUARD, UnitType.RANGER}
                and manhattan(target_position, tuple(enemy.position))
                <= ASSAULT_CORE_GUARD_RADIUS
                for enemy in visible_enemies
            ):
                self.assault_guarded = True
                self.assault_gathering = True
        elif self.assault_target_id is not None:
            if self.assault_target_kind == "CORE":
                sighting = self.known_enemy_cores.get(self.assault_target_id)
            else:
                sighting = self.known_combat_threats.get(self.assault_target_id)
            if sighting is not None:
                self.assault_target_position = sighting[0]
            elif self.assault_target_kind == "CORE":
                self.clear_assault()
            elif tick - self.assault_target_last_seen_tick > ASSAULT_TARGET_MEMORY_TICKS:
                self.clear_assault()
        if (
            self.assault_target_kind == "CORE"
            and self.assault_target_position is not None
            and chebyshev(core, self.assault_target_position)
            > ASSAULT_HOME_CORE_DISTANCE
        ):
            self.assault_gathering = False
            self.assault_rally_position = None
        after = (
            self.assault_target_id,
            self.assault_target_kind,
            self.assault_target_position,
            self.assault_target_last_seen_tick,
            self.assault_guarded,
            self.assault_gathering,
            self.assault_rally_position,
        )
        return before != after

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

    def observe_spawn_blocks(self, events, tick: int) -> None:
        """出生格达到单位上限后短暂停产，并让家门口单位向外疏散。"""
        if any(
            event.event_type == "CORE_SPAWN_FAILED"
            and event.reason_code == "CELL_UNIT_LIMIT"
            for event in events
        ):
            self.spawn_clear_until = max(
                self.spawn_clear_until,
                tick + SPAWN_CLEAR_TICKS,
            )

    def prune_resources_outside(self, core: Pos, radius: int) -> bool:
        """删除当前 Core 最大巡逻方形外的资源及其 Worker 任务。"""
        removed = {
            resource
            for resource in self.known_resources
            if chebyshev(core, resource) > radius
        }
        if not removed:
            return False
        self.known_resources.difference_update(removed)
        for resource in removed:
            self.resource_deferred_until.pop(resource, None)
        for worker_id, target in tuple(self.worker_resource_target.items()):
            if target in removed:
                self.worker_resource_target.pop(worker_id, None)
        return True

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
        """按当前 Worker 数量均匀分配扇区，并迁移旧的相邻布局。"""
        ordered_workers = tuple(sorted(workers, key=lambda worker: str(worker.id)))
        worker_count = len(ordered_workers)
        desired_sectors = {
            worker.id: (worker_index * len(SCOUT_VECTORS))
            // max(1, worker_count)
            for worker_index, worker in enumerate(ordered_workers)
        }
        if self.worker_sector != desired_sectors:
            # Worker 逐个生产时，旧的“游标补位”会得到 0、1、2、3，
            # 而不是四等分的 0、2、4、6。布局改变后清掉所有侦察进度，
            # 避免旧目标与新的扇区偏移叠加。
            self.worker_sector = desired_sectors
            self.scout_ring_index.clear()
            self.scout_phase.clear()
            self.scout_goal.clear()
            self.scout_path_failures.clear()
            self.outer_scout_ring_index.clear()
            self.outer_scout_step.clear()
            self.outer_scout_goal.clear()
            self.outer_scout_path_failures.clear()

        for worker in ordered_workers:
            self.scout_ring_index.setdefault(worker.id, 0)
            self.scout_phase.setdefault(worker.id, 0)
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

    def sync_outer_scout_mode(self, active: bool) -> bool:
        """切换高库存外圈扫描，并清理另一模式遗留的 Worker 任务。"""
        if active == self.outer_scout_active:
            return False
        self.outer_scout_active = active
        self.outer_scout_ring_index.clear()
        self.outer_scout_step.clear()
        self.outer_scout_goal.clear()
        self.outer_scout_path_failures.clear()
        if active:
            self.worker_resource_target.clear()
            self.worker_intercept_goal.clear()
            self.scout_goal.clear()
            self.scout_path_failures.clear()
        return True

    def advance_outer_scout(self, worker_id: UUID) -> None:
        """沿当前方环顺时针前进；绕环一周后切换到下一扫描半径。"""
        ring_index = self.outer_scout_ring_index.get(worker_id, 0)
        radius = OUTER_SCOUT_RING_SEQUENCE[
            ring_index % len(OUTER_SCOUT_RING_SEQUENCE)
        ]
        route_length = len(square_ring_waypoints((0, 0), radius))
        step = self.outer_scout_step.get(worker_id, 0) + 1
        if step >= route_length:
            step = 0
            ring_index = (ring_index + 1) % len(OUTER_SCOUT_RING_SEQUENCE)
        self.outer_scout_ring_index[worker_id] = ring_index
        self.outer_scout_step[worker_id] = step
        self.outer_scout_goal.pop(worker_id, None)
        self.outer_scout_path_failures.pop(worker_id, None)

    def outer_scout_goal_for(
        self,
        worker_id: UUID,
        core: Pos,
        position: Pos,
        obstacles: set[Pos],
        separation_points: Iterable[Pos] = (),
    ) -> Pos:
        """选择外圈方环上的下一个顺时针目标，并按稳定扇区错开起点。"""
        separation_points = tuple(separation_points)

        def is_safe(candidate: Pos) -> bool:
            return candidate not in obstacles and all(
                manhattan(candidate, other) >= OUTER_SCOUT_MIN_GOAL_DISTANCE
                for other in separation_points
            )

        goal = self.outer_scout_goal.get(worker_id)
        if goal is not None and is_safe(goal) and manhattan(position, goal) > 1:
            return goal
        if goal is not None:
            self.advance_outer_scout(worker_id)

        max_candidates = sum(
            len(square_ring_waypoints((0, 0), radius))
            for radius in OUTER_SCOUT_RING_SEQUENCE
        )
        for _ in range(max_candidates):
            ring_index = self.outer_scout_ring_index.get(worker_id, 0)
            radius = OUTER_SCOUT_RING_SEQUENCE[
                ring_index % len(OUTER_SCOUT_RING_SEQUENCE)
            ]
            route = square_ring_waypoints(core, radius)
            step = self.outer_scout_step.get(worker_id, 0)
            sector = self.worker_sector.get(worker_id, 0) % len(SCOUT_VECTORS)
            offset = (sector * len(route)) // len(SCOUT_VECTORS)
            candidate = route[(step + offset) % len(route)]
            if is_safe(candidate):
                self.outer_scout_goal[worker_id] = candidate
                return candidate
            self.advance_outer_scout(worker_id)

        self.outer_scout_goal[worker_id] = core
        return core

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

    def advance_scout(self, worker_id: UUID) -> None:
        """沿普通资源方环顺时针前进；绕环一周后切换扫描半径。"""
        ring_index = self.scout_ring_index.get(worker_id, 0)
        radius = RESOURCE_SCOUT_RING_SEQUENCE[
            ring_index % len(RESOURCE_SCOUT_RING_SEQUENCE)
        ]
        route_length = len(
            square_ring_waypoints(
                (0, 0),
                radius,
                RESOURCE_SCOUT_WAYPOINT_STEP,
            )
        )
        step = self.scout_phase.get(worker_id, 0) + 1
        if step >= route_length:
            step = 0
            ring_index = (ring_index + 1) % len(RESOURCE_SCOUT_RING_SEQUENCE)
        self.scout_ring_index[worker_id] = ring_index
        self.scout_phase[worker_id] = step
        self.scout_goal.pop(worker_id, None)
        self.scout_path_failures.pop(worker_id, None)

    def goal_for(
        self,
        worker_id: UUID,
        worker_index: int,
        worker_count: int,
        core: Pos,
        position: Pos,
        obstacles: set[Pos],
    ) -> Pos:
        goal = self.scout_goal.get(worker_id)
        if goal is not None and goal not in obstacles and manhattan(position, goal) > 1:
            return goal

        if goal is not None:
            self.advance_scout(worker_id)

        max_candidates = sum(
            len(
                square_ring_waypoints(
                    (0, 0),
                    radius,
                    RESOURCE_SCOUT_WAYPOINT_STEP,
                )
            )
            for radius in RESOURCE_SCOUT_RING_SEQUENCE
        )
        for _ in range(max_candidates):
            ring_index = self.scout_ring_index.get(worker_id, 0)
            radius = RESOURCE_SCOUT_RING_SEQUENCE[
                ring_index % len(RESOURCE_SCOUT_RING_SEQUENCE)
            ]
            route = square_ring_waypoints(
                core,
                radius,
                RESOURCE_SCOUT_WAYPOINT_STEP,
            )
            step = self.scout_phase.get(worker_id, 0)
            sector = self.worker_sector.get(
                worker_id,
                (worker_index * len(SCOUT_VECTORS)) // max(1, worker_count),
            ) % len(SCOUT_VECTORS)
            offset = (sector * len(route)) // len(SCOUT_VECTORS)
            candidate = route[(step + offset) % len(route)]
            if candidate not in obstacles:
                self.scout_goal[worker_id] = candidate
                return candidate
            self.advance_scout(worker_id)

        # 若所有普通方环候选点都被已知障碍覆盖，则回退到 Core。
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
    safe_enemy_units: tuple[UnitView, ...]
    combat_enemies: tuple[UnitView, ...]
    home_combat_targets: tuple[UnitView, ...]
    roam_home_response_targets: tuple[UnitView, ...]
    home_enemy_units: tuple[UnitView, ...]
    navigation_obstacles: set[Pos]
    danger_cells: set[Pos]
    threat_positions: tuple[Pos, ...]
    occupied: FriendlyOccupancy
    resource_assignments: dict[UUID, Pos]
    reserved_combat_ids: set[UUID]
    combat_squads: tuple[CombatSquad, ...]
    home_squad_ids: set[UUID]
    field_combat_ids: set[UUID]
    assault_target: CoreView | UnitView | None
    roaming_combat_units: tuple[Vanguard | Ranger, ...]
    roam_is_aggressive: bool
    trap_obstacles: set[Pos]
    roam_target_track: EnemyWorkerTrack | None
    roam_target_enemy: UnitView | None
    roam_target_id: UUID | None
    blocker_worker_id: UUID | None
    spawn_clearing: bool
    outer_scout_active: bool
    healing_resources: int
    actions: list[str]


def idle_core_exit_destination(
    context: PlanningContext,
    unit_id: UUID,
    position: Pos,
    obstacles: set[Pos],
) -> Pos | None:
    """为需要腾空 Core 或入口的空闲单位选择合法相邻格。"""
    start_index = unit_id.int % len(DIRECTION_STEPS)
    rotated_steps = (
        DIRECTION_STEPS[start_index:]
        + DIRECTION_STEPS[:start_index]
    )
    candidates = []
    for order, (_, delta) in enumerate(rotated_steps):
        candidate = add(position, delta)
        if (
            candidate == context.core_pos
            or candidate in obstacles
            or not context.occupied.can_enter(candidate)
        ):
            continue
        candidates.append(
            (
                -manhattan(candidate, context.core_pos),
                context.occupied.count(candidate),
                order,
                candidate,
            )
        )
    return min(candidates, default=(None, None, None, None))[3]


def plan_idle_core_egress(
    context: PlanningContext,
    unit,
    obstacles: set[Pos],
    label: str,
) -> bool:
    """满血空闲单位必须先离开 Core，再恢复巡逻或侦察。"""
    max_hp = UNIT_MAX_HP[unit.unit_type]
    position: Pos = tuple(unit.position)
    if unit.hp < max_hp or position != context.core_pos:
        return False

    destination = idle_core_exit_destination(
        context,
        unit.id,
        position,
        obstacles,
    )
    if destination is not None:
        direction = direction_between(position, destination)
        if direction is not None:
            unit.move(direction)
            context.occupied.add(destination)
            context.actions.append(
                f"{str(unit.id)[:8]} {label}-exit {direction.value}"
            )
            return True

    unit.wait()
    context.occupied.add(position)
    context.actions.append(f"{str(unit.id)[:8]} {label}-exit-hold")
    return True


def plan_idle_healing(
    context: PlanningContext,
    unit,
    obstacles: set[Pos],
    label: str,
) -> bool:
    """为无战斗或资源任务的残血单位规划返家和一次补满治疗。"""
    max_hp = UNIT_MAX_HP[unit.unit_type]
    deficit = max(0, max_hp - unit.hp)
    if deficit == 0 or context.turn.core.view.state is not CoreState.NORMAL:
        return False

    position: Pos = tuple(unit.position)
    distance = manhattan(position, context.core_pos)
    if context.healing_resources < deficit:
        # 距离入口至多一步时主动避开 Core，防止下一步误入后长期占位。
        if distance <= 1:
            destination = idle_core_exit_destination(
                context,
                unit.id,
                position,
                obstacles,
            )
            if destination is not None:
                direction = direction_between(position, destination)
                if direction is not None:
                    unit.move(direction)
                    context.occupied.add(destination)
                    context.actions.append(
                        f"{str(unit.id)[:8]} {label}-defer "
                        f"need={deficit} have={context.healing_resources} "
                        f"{direction.value}"
                    )
                    return True
            unit.wait()
            context.occupied.add(position)
            context.actions.append(
                f"{str(unit.id)[:8]} {label}-defer-hold "
                f"need={deficit} have={context.healing_resources}"
            )
            return True
        return False

    if position == context.core_pos:
        context.healing_resources -= deficit
        unit.heal()
        context.occupied.add(position)
        context.actions.append(
            f"{str(unit.id)[:8]} {label} amount={deficit}"
        )
        return True

    if distance == 1:
        if not context.occupied.can_enter(context.core_pos):
            return False
        destination = context.core_pos
    else:
        blocked = context.occupied.full_cells()
        blocked.discard(context.core_pos)
        destination = first_step_astar(
            position,
            context.core_pos,
            obstacles,
            blocked,
        )
        if destination is None or not context.occupied.can_enter(destination):
            return False

    direction = direction_between(position, destination)
    if direction is None:
        return False
    context.healing_resources -= deficit
    unit.move(direction)
    context.occupied.add(destination)
    context.actions.append(
        f"{str(unit.id)[:8]} {label}-return amount={deficit} "
        f"{direction.value}"
    )
    return True


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
    *,
    core_radius: int | None = ROAM_RADIUS,
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
            (core_radius is None or chebyshev(context.core_pos, candidate) <= core_radius)
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


def core_clear_destination(
    context: PlanningContext,
    position: Pos,
    unit_index: int,
) -> Pos | None:
    """出生失败后，将 Core 及相邻格上的单位向外分散。"""
    current_distance = manhattan(position, context.core_pos)
    if current_distance > 1:
        return None
    rotated_steps = (
        DIRECTION_STEPS[unit_index % len(DIRECTION_STEPS) :]
        + DIRECTION_STEPS[: unit_index % len(DIRECTION_STEPS)]
    )
    candidates = (
        add(position, delta)
        for _, delta in rotated_steps
    )
    return min(
        (
            cell
            for cell in candidates
            if manhattan(cell, context.core_pos) > current_distance
            and cell not in context.navigation_obstacles
            and context.occupied.can_enter(cell)
        ),
        key=lambda cell: (context.occupied.count(cell), cell),
        default=None,
    )


def roam_ranger_response_step(
    context: PlanningContext,
    position: Pos,
    target: Pos,
    obstacles: set[Pos],
) -> Pos | None:
    """寻找靠近近家威胁的合法开火位。"""
    firing_cells: list[tuple[int, int, Pos]] = []
    for delta in SCOUT_VECTORS:
        for distance in range(1, 4):
            cell = (
                target[0] - delta[0] * distance,
                target[1] - delta[1] * distance,
            )
            if (
                chebyshev(cell, context.core_pos) <= ROAM_RADIUS
                and cell not in obstacles
                and context.occupied.can_enter(cell)
                and clear_ranger_shot(cell, target, context.memory.known_obstacles)
            ):
                firing_cells.append(
                    (abs(distance - 3), manhattan(position, cell), cell)
                )
    for _, _, firing_cell in sorted(firing_cells):
        destination = first_step_astar(
            position,
            firing_cell,
            obstacles,
            set(context.occupied),
        )
        if destination is not None and context.occupied.can_enter(destination):
            return destination
    return combat_approach_step(context, position, target, obstacles)


def full_capacity_worker_destination(
    context: PlanningContext,
    position: Pos,
    worker_index: int,
) -> tuple[Pos | None, Pos]:
    """满仓时将 Worker 分散到家园待命点，并优先腾空 Core 格。"""
    core = context.core_pos
    occupied = context.occupied
    obstacles = context.navigation_obstacles | {core}
    staging_goal = add(
        core,
        HOME_PATROL_OFFSETS[worker_index % len(HOME_PATROL_OFFSETS)],
    )

    if position == core:
        rotated_steps = (
            DIRECTION_STEPS[worker_index % len(DIRECTION_STEPS) :]
            + DIRECTION_STEPS[: worker_index % len(DIRECTION_STEPS)]
        )
        candidates = sorted(
            (add(core, delta) for _, delta in rotated_steps),
            key=lambda cell: occupied.count(cell),
        )
        destination = next(
            (
                cell
                for cell in candidates
                if cell not in context.navigation_obstacles
                and occupied.can_enter(cell)
            ),
            None,
        )
        return destination, staging_goal

    for offset_index in range(len(HOME_PATROL_OFFSETS)):
        offset = HOME_PATROL_OFFSETS[
            (worker_index + offset_index) % len(HOME_PATROL_OFFSETS)
        ]
        goal = add(core, offset)
        if goal in obstacles:
            continue
        staging_goal = goal
        if position == goal:
            return position, staging_goal
        destination = first_step_astar(
            position,
            goal,
            obstacles,
            set(occupied),
        )
        if destination is not None and occupied.can_enter(destination):
            return destination, staging_goal
    return None, staging_goal


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

        if (
            context.spawn_clearing
            and worker.cargo == 0
            and not context.home_combat_targets
        ):
            destination = core_clear_destination(context, position, worker_index)
            if destination is not None:
                direction = direction_between(position, destination)
                if direction is not None:
                    worker.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(worker.id)[:8]} spawn-clear {direction.value}"
                    )
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

        # 状态 3：进入外圈扫描前先交付现有货物；Core 满仓时无法交付，
        # 载货 Worker 也直接参加扫描。
        if worker.cargo > 0 and turn.resources < turn.resource_capacity:
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

        assigned_resource = context.resource_assignments.get(worker.id)
        active_intercept = (
            worker.id == context.blocker_worker_id
            and worker.id in memory.worker_intercept_goal
        )
        worker_idle = (
            worker.cargo == 0
            and assigned_resource is None
            and not active_intercept
        )
        if worker_idle and (
            plan_idle_core_egress(
                context,
                worker,
                context.navigation_obstacles,
                "worker-heal",
            )
            or plan_idle_healing(
                context,
                worker,
                context.navigation_obstacles,
                "worker-heal",
            )
        ):
            continue

        # 状态 4：19 人口且 Core 资源达到 80 后，四名 Worker 在 32-64 格
        # 方环上错位顺时针扫描；资源下降后由模式切换恢复正常任务。
        if context.outer_scout_active:
            separation_points = {
                tuple(other.position)
                for other in context.workers
                if other.id != worker.id
            }
            separation_points.update(
                goal
                for other_id, goal in memory.outer_scout_goal.items()
                if other_id != worker.id
            )
            goal = memory.outer_scout_goal_for(
                worker.id,
                context.core_pos,
                position,
                context.navigation_obstacles,
                separation_points,
            )
            destination = first_step_astar(
                position,
                goal,
                context.navigation_obstacles,
                set(occupied),
            )
            if destination is not None and destination not in occupied:
                direction = direction_between(position, destination)
                if direction is not None:
                    worker.move(direction)
                    memory.outer_scout_path_failures.pop(worker.id, None)
                    occupied.add(destination)
                    actions.append(
                        f"{str(worker.id)[:8]} outer-scout {goal} "
                        f"{direction.value}"
                    )
                    continue

            failures = memory.outer_scout_path_failures.get(worker.id, 0) + 1
            memory.outer_scout_path_failures[worker.id] = failures
            if failures >= OUTER_SCOUT_PATH_FAILURES:
                memory.advance_outer_scout(worker.id)
            worker.wait()
            occupied.add(position)
            actions.append(f"{str(worker.id)[:8]} outer-scout-hold {goal}")
            continue

        # 状态 5：非外圈模式下，Core 满仓后分散待命并优先腾空生产格。
        if turn.resources >= turn.resource_capacity:
            destination, staging_goal = full_capacity_worker_destination(
                context,
                position,
                worker_index,
            )
            if destination is not None and destination != position:
                direction = direction_between(position, destination)
                if direction is not None:
                    worker.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(worker.id)[:8]} capacity-stage "
                        f"{staging_goal} {direction.value}"
                    )
                    continue
            worker.wait()
            occupied.add(position)
            actions.append(
                f"{str(worker.id)[:8]} capacity-hold {staging_goal}"
            )
            continue

        # 状态 6：脚下有当前可见资源时立即采集。
        if (
            position in context.visible_resources
            and turn.tick >= memory.resource_deferred_until.get(position, 0)
        ):
            worker.harvest()
            occupied.add(position)
            actions.append(f"{str(worker.id)[:8]} harvest")
            continue

        # 状态 7：静态资源任务跨 Tick 保留，途中不因发现新资源而改派。
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

        # 状态 8：没有资源任务时，才允许空载 Worker 临时协助拦截敌方 Worker。
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

        # 状态 9：其余 Worker 在 12-32 格方环上错位顺时针扫描。
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
        if failures >= RESOURCE_SCOUT_PATH_FAILURES:
            memory.advance_scout(worker.id)

        worker.wait()
        occupied.add(position)
        actions.append(f"{str(worker.id)[:8]} wait-scout")


def combat_approach_step(
    context: PlanningContext,
    position: Pos,
    target: Pos,
    obstacles: set[Pos],
) -> Pos | None:
    """选择通往目标相邻格的下一步，不进入敌方实体格。"""
    candidates = sorted(
        (
            add(target, delta)
            for _, delta in DIRECTION_STEPS
            if add(target, delta) not in obstacles
            and context.occupied.can_enter(add(target, delta))
        ),
        key=lambda cell: (manhattan(position, cell), cell),
    )
    for candidate in candidates:
        destination = first_step_astar(
            position,
            candidate,
            obstacles,
            set(context.occupied),
        )
        if destination is not None and context.occupied.can_enter(destination):
            return destination
    return None


def combat_move_toward(
    context: PlanningContext,
    position: Pos,
    goal: Pos,
    obstacles: set[Pos],
) -> Pos | None:
    destination = first_step_astar(
        position,
        goal,
        obstacles,
        set(context.occupied),
    )
    if destination is None or not context.occupied.can_enter(destination):
        return None
    return destination


def squad_ranger_follow_destination(
    context: PlanningContext,
    position: Pos,
    leader_position: Pos,
    mission_goal: Pos,
    obstacles: set[Pos],
) -> Pos | None:
    """让 Ranger 留在 Vanguard 后侧，避免远程单位顶到行军最前方。"""
    goal_vector = (
        mission_goal[0] - leader_position[0],
        mission_goal[1] - leader_position[1],
    )
    candidates: list[tuple[int, int, Pos]] = []
    for delta in SCOUT_VECTORS:
        for distance in (1, 2):
            cell = (
                leader_position[0] + delta[0] * distance,
                leader_position[1] + delta[1] * distance,
            )
            relative = (
                cell[0] - leader_position[0],
                cell[1] - leader_position[1],
            )
            dot = relative[0] * goal_vector[0] + relative[1] * goal_vector[1]
            if (
                dot <= 0
                and cell not in obstacles
                and context.occupied.can_enter(cell)
            ):
                candidates.append((dot, manhattan(position, cell), cell))
    if position in {candidate[2] for candidate in candidates}:
        return position
    for _, _, cell in sorted(candidates, key=lambda item: (item[0], item[1], item[2])):
        destination = first_step_astar(
            position,
            cell,
            obstacles,
            set(context.occupied),
        )
        if destination is not None and context.occupied.can_enter(destination):
            return destination
    return ranger_follow_destination(
        context,
        position,
        leader_position,
        obstacles,
        core_radius=None,
    )


def ranger_attack_destination(
    context: PlanningContext,
    position: Pos,
    target: Pos,
    obstacles: set[Pos],
) -> Pos | None:
    """寻找 Ranger 可达的 1-3 格射击位，优先保留 2-3 格安全距离。"""
    candidates: list[tuple[int, int, Pos]] = []
    for delta_x in range(-3, 4):
        for delta_y in range(-3, 4):
            candidate = (target[0] + delta_x, target[1] + delta_y)
            distance = ranger_line_distance(candidate, target)
            if distance is None or not 1 <= distance <= 3:
                continue
            if (
                candidate in obstacles
                or candidate == target
                or not context.occupied.can_enter(candidate)
                or not clear_ranger_shot(candidate, target, obstacles)
            ):
                continue
            candidates.append(
                (
                    0 if distance >= 2 else 1,
                    manhattan(position, candidate),
                    candidate,
                )
            )
    for _, _, candidate in sorted(candidates):
        destination = first_step_astar(
            position,
            candidate,
            obstacles,
            set(context.occupied),
        )
        if destination is not None and context.occupied.can_enter(destination):
            return destination
    return None


def squad_spread(members: Iterable[Vanguard | Ranger]) -> int:
    """返回小队成员之间最大的曼哈顿距离。"""
    positions = tuple(tuple(unit.position) for unit in members)
    return max(
        (
            manhattan(left, right)
            for index, left in enumerate(positions)
            for right in positions[index + 1 :]
        ),
        default=0,
    )


def choose_squad_regroup_goal(
    members: Iterable[Vanguard | Ranger],
    obstacles: set[Pos],
    *,
    excluded: set[Pos] | None = None,
) -> Pos | None:
    """在成员坐标中位点附近选择所有人都可达的集结中心。"""
    positions = tuple(tuple(unit.position) for unit in members)
    if not positions:
        return None
    median = (
        sorted(position[0] for position in positions)[len(positions) // 2],
        sorted(position[1] for position in positions)[len(positions) // 2],
    )
    candidates = set(positions)
    for radius in range(5):
        for delta_x in range(-radius, radius + 1):
            delta_y = radius - abs(delta_x)
            candidates.add((median[0] + delta_x, median[1] + delta_y))
            candidates.add((median[0] + delta_x, median[1] - delta_y))
    candidates.difference_update(excluded or set())
    candidates.difference_update(obstacles)
    ordered_candidates = sorted(
        candidates,
        key=lambda candidate: (
            sum(manhattan(position, candidate) for position in positions),
            max(manhattan(position, candidate) for position in positions),
            candidate,
        ),
    )
    return next(
        (
            candidate
            for candidate in ordered_candidates
            if all(
                first_step_astar(position, candidate, obstacles, set()) is not None
                for position in positions
            )
        ),
        None,
    )


def squad_regroup_distance(
    members: Iterable[Vanguard | Ranger],
    goal: Pos,
) -> int:
    """衡量全队进入集结区域还需移动的总距离。"""
    return sum(
        max(
            0,
            manhattan(tuple(unit.position), goal) - SQUAD_REGROUP_AREA_RADIUS,
        )
        for unit in members
    )


def squad_regroup_destination(
    context: PlanningContext,
    position: Pos,
    goal: Pos,
    obstacles: set[Pos],
) -> Pos | None:
    """前往集结中心周围的空闲区域，避免成员争抢同一个格子。"""
    if manhattan(position, goal) <= SQUAD_REGROUP_AREA_RADIUS:
        return position
    candidates = sorted(
        (
            (goal[0] + delta_x, goal[1] + delta_y)
            for delta_x in range(
                -SQUAD_REGROUP_AREA_RADIUS,
                SQUAD_REGROUP_AREA_RADIUS + 1,
            )
            for delta_y in range(
                -SQUAD_REGROUP_AREA_RADIUS,
                SQUAD_REGROUP_AREA_RADIUS + 1,
            )
            if abs(delta_x) + abs(delta_y) <= SQUAD_REGROUP_AREA_RADIUS
        ),
        key=lambda cell: (manhattan(position, cell), cell),
    )
    for candidate in candidates:
        if candidate in obstacles or not context.occupied.can_enter(candidate):
            continue
        destination = first_step_astar(
            position,
            candidate,
            obstacles,
            set(context.occupied),
        )
        if destination is not None and context.occupied.can_enter(destination):
            return destination
    return None


def clear_squad_regroup(memory: AgentMemory, squad_id: int) -> None:
    memory.squad_regroup_goal.pop(squad_id, None)
    memory.squad_regroup_interrupted.discard(squad_id)
    memory.squad_regroup_safe_ticks.pop(squad_id, None)
    memory.squad_regroup_last_distance.pop(squad_id, None)
    memory.squad_regroup_stall_ticks.pop(squad_id, None)


def choose_assault_rally(
    squads: tuple[CombatSquad, ...],
    unit_by_id: dict[UUID, Vanguard | Ranger],
    target_position: Pos | None = None,
) -> Pos | None:
    """选择远离敌方 Core 的完整巡逻队集结点。"""
    leader_positions = tuple(
        tuple(unit_by_id[squad.vanguard_ids[0]].position)
        for squad in squads
        if squad.squad_id != 0
        and squad.complete
        and squad.vanguard_ids[0] in unit_by_id
    )
    if not leader_positions:
        return None
    candidates = set(leader_positions)
    if target_position is not None:
        for origin in leader_positions:
            for radius in range(1, ASSAULT_CORE_SAFE_DISTANCE + 5):
                for delta_x in range(-radius, radius + 1):
                    delta_y = radius - abs(delta_x)
                    candidates.add((origin[0] + delta_x, origin[1] + delta_y))
                    candidates.add((origin[0] + delta_x, origin[1] - delta_y))
        safe_candidates = {
            candidate
            for candidate in candidates
            if manhattan(candidate, target_position) >= ASSAULT_CORE_SAFE_DISTANCE
        }
        if safe_candidates:
            candidates = safe_candidates
    return min(
        candidates,
        key=lambda candidate: (
            sum(manhattan(candidate, other) for other in leader_positions),
            max(manhattan(candidate, other) for other in leader_positions),
            candidate,
        ),
        default=None,
    )


def plan_field_squads(context: PlanningContext) -> None:
    """按 2V1R 小队巡逻；发现强敌时先统一集结，再共同出击。"""
    memory = context.memory
    occupied = context.occupied
    actions = context.actions
    unit_by_id = {
        unit.id: unit for unit in (*context.vanguards, *context.rangers)
    }
    visible_enemy_by_id = {
        enemy.id: enemy for enemy in context.turn.visible_enemies
    }
    field_squads = tuple(
        squad for squad in context.combat_squads if squad.squad_id != 0
    )
    complete_field_squads = tuple(squad for squad in field_squads if squad.complete)
    if not field_squads:
        return

    if (
        memory.assault_guarded
        and memory.assault_gathering
        and memory.assault_target_position is not None
        and memory.assault_rally_position is None
    ):
        memory.assault_rally_position = choose_assault_rally(
            complete_field_squads,
            unit_by_id,
            memory.assault_target_position,
        )
    if memory.assault_gathering and memory.assault_rally_position is not None:
        gathered_ids = set().union(
            *(squad.unit_ids for squad in complete_field_squads),
        ) if complete_field_squads else set()
        if gathered_ids and all(
            unit_id in unit_by_id
            and manhattan(
                tuple(unit_by_id[unit_id].position),
                memory.assault_rally_position,
            ) <= ASSAULT_GATHER_RADIUS
            for unit_id in gathered_ids
        ):
            memory.assault_gathering = False
            actions.append("squads assault-ready")

    static_obstacles = (
        memory.known_obstacles
        | context.known_enemy_core_cells
        | set(memory.temporary_blocked_cells)
    )
    target_object = visible_enemy_by_id.get(memory.assault_target_id)
    target_position = memory.assault_target_position
    home_emergency_target = min(
        context.home_combat_targets,
        key=lambda enemy: (
            manhattan(context.core_pos, tuple(enemy.position)),
            str(enemy.id),
        ),
        default=None,
    )
    home_emergency = home_emergency_target is not None
    if home_emergency_target is not None:
        target_object = home_emergency_target
        target_position = tuple(home_emergency_target.position)

    assault_core_target = (
        not home_emergency
        and memory.assault_target_kind == "CORE"
        and target_position is not None
    )
    assault_core_near_home = (
        assault_core_target
        and chebyshev(context.core_pos, target_position)
        <= ASSAULT_HOME_CORE_DISTANCE
    )
    joint_assault = (
        assault_core_near_home
        and memory.assault_guarded
    )
    primary_assault_squad_id: int | None = None
    if (
        assault_core_target
        and not memory.assault_gathering
        and not joint_assault
    ):
        primary_squad = min(
            (
                squad
                for squad in complete_field_squads
                if any(unit_id in unit_by_id for unit_id in squad.unit_ids)
            ),
            key=lambda squad: (
                min(
                    manhattan(
                        tuple(unit_by_id[unit_id].position),
                        target_position,
                    )
                    for unit_id in squad.unit_ids
                    if unit_id in unit_by_id
                ),
                squad.squad_id,
            ),
            default=None,
        )
        if primary_squad is not None and (
            assault_core_near_home
            or min(
                chebyshev(tuple(unit_by_id[unit_id].position), target_position)
                for unit_id in primary_squad.unit_ids
                if unit_id in unit_by_id
            )
            <= ROAM_RADIUS
        ):
            primary_assault_squad_id = primary_squad.squad_id
        else:
            # 远处目标只保留在记忆中，避免 Worker/守家单位把完整小队拉去
            # 执行一条超出巡逻边界的长距离 A*。
            target_object = None
            target_position = None
    for squad in field_squads:
        members = [unit_by_id[unit_id] for unit_id in squad.unit_ids if unit_id in unit_by_id]
        if not members:
            continue
        leader = unit_by_id.get(squad.vanguard_ids[0]) if squad.vanguard_ids else None
        if leader is None:
            leader = min(members, key=lambda unit: str(unit.id))
        leader_position: Pos = tuple(leader.position)
        squad_target_object = target_object
        squad_target_position = target_position
        if (
            primary_assault_squad_id is not None
            and squad.squad_id != primary_assault_squad_id
        ):
            squad_target_object = None
            squad_target_position = None
        nearby_worker = min(
            (
                enemy
                for enemy in context.safe_enemy_units
                if enemy.unit_type is UnitType.WORKER
                and any(
                    manhattan(tuple(unit.position), tuple(enemy.position)) <= 6
                    for unit in members
                )
            ),
            key=lambda enemy: (
                manhattan(leader_position, tuple(enemy.position)),
                str(enemy.id),
            ),
            default=None,
        )
        if squad_target_position is None and nearby_worker is not None:
            squad_target_object = nearby_worker
            squad_target_position = tuple(nearby_worker.position)
        squad_attack_now = squad_target_position is not None and (
            home_emergency
            or nearby_worker is not None
            or not memory.assault_gathering
        )
        spread = squad_spread(members)
        nearby_combat_threat = any(
            any(
                manhattan(tuple(unit.position), tuple(enemy.position))
                <= SQUAD_REGROUP_TRIGGER_DISTANCE
                for unit in members
            )
            for enemy in context.combat_enemies
        )
        squad_in_combat = (
            home_emergency
            or nearby_worker is not None
            or nearby_combat_threat
        )
        squad_idle_for_healing = (
            not squad_in_combat
            and squad_target_position is None
            and not memory.assault_gathering
        )
        engaged_vanguard_targets = tuple(
            enemy
            for enemy in context.combat_enemies
            if enemy.unit_type is UnitType.VANGUARD
            and any(
                friendly.unit_type is UnitType.VANGUARD
                and manhattan(tuple(friendly.position), tuple(enemy.position)) == 1
                for friendly in members
            )
        )
        squad_support_target = min(
            engaged_vanguard_targets,
            key=lambda enemy: (
                min(
                    manhattan(tuple(friendly.position), tuple(enemy.position))
                    for friendly in members
                    if friendly.unit_type is UnitType.VANGUARD
                ),
                str(enemy.id),
            ),
            default=None,
        )
        regroup_goal = memory.squad_regroup_goal.get(squad.squad_id)
        regroup_paused = False
        regroup_started = False
        regroup_obstacles = static_obstacles | context.visible_enemy_unit_cells

        if squad_in_combat:
            if regroup_goal is not None or spread > SQUAD_REGROUP_TRIGGER_DISTANCE:
                memory.squad_regroup_interrupted.add(squad.squad_id)
                memory.squad_regroup_safe_ticks[squad.squad_id] = 0
        elif squad.squad_id in memory.squad_regroup_interrupted:
            safe_ticks = memory.squad_regroup_safe_ticks.get(squad.squad_id, 0) + 1
            memory.squad_regroup_safe_ticks[squad.squad_id] = safe_ticks
            if safe_ticks < SQUAD_REGROUP_SAFE_TICKS:
                regroup_paused = True
            else:
                memory.squad_regroup_interrupted.discard(squad.squad_id)
                memory.squad_regroup_safe_ticks.pop(squad.squad_id, None)
                if spread <= SQUAD_FORMATION_RADIUS:
                    clear_squad_regroup(memory, squad.squad_id)
                    regroup_goal = None
                    actions.append(f"squad regroup-complete team={squad.squad_id}")
                else:
                    regroup_goal = choose_squad_regroup_goal(
                        members,
                        regroup_obstacles,
                    )
                    if regroup_goal is not None:
                        memory.squad_regroup_goal[squad.squad_id] = regroup_goal
                        memory.squad_regroup_last_distance[squad.squad_id] = (
                            squad_regroup_distance(members, regroup_goal)
                        )
                        memory.squad_regroup_stall_ticks[squad.squad_id] = 0
                        regroup_started = True
                        actions.append(
                            f"squad regroup-after-combat team={squad.squad_id} "
                            f"goal={regroup_goal}"
                        )
                    else:
                        memory.squad_regroup_goal.pop(squad.squad_id, None)
                        memory.squad_regroup_last_distance.pop(squad.squad_id, None)
                        memory.squad_regroup_stall_ticks.pop(squad.squad_id, None)
                        actions.append(
                            f"squad regroup-retry team={squad.squad_id}"
                        )
        elif regroup_goal is not None and spread <= SQUAD_FORMATION_RADIUS:
            clear_squad_regroup(memory, squad.squad_id)
            regroup_goal = None
            actions.append(f"squad regroup-complete team={squad.squad_id}")
        elif regroup_goal is None and spread > SQUAD_REGROUP_TRIGGER_DISTANCE:
            regroup_goal = choose_squad_regroup_goal(
                members,
                regroup_obstacles,
            )
            if regroup_goal is not None:
                memory.squad_regroup_goal[squad.squad_id] = regroup_goal
                memory.squad_regroup_last_distance[squad.squad_id] = (
                    squad_regroup_distance(members, regroup_goal)
                )
                memory.squad_regroup_stall_ticks[squad.squad_id] = 0
                regroup_started = True
                actions.append(
                    f"squad regroup-start team={squad.squad_id} goal={regroup_goal}"
                )

        regroup_active = (
            regroup_goal is not None
            and not squad_in_combat
            and not regroup_paused
        )
        if regroup_active and not regroup_started:
            regroup_distance = squad_regroup_distance(members, regroup_goal)
            previous_distance = memory.squad_regroup_last_distance.get(
                squad.squad_id,
                regroup_distance + 1,
            )
            if regroup_distance < previous_distance:
                memory.squad_regroup_stall_ticks[squad.squad_id] = 0
            else:
                memory.squad_regroup_stall_ticks[squad.squad_id] = (
                    memory.squad_regroup_stall_ticks.get(squad.squad_id, 0) + 1
                )
            memory.squad_regroup_last_distance[squad.squad_id] = regroup_distance
            if (
                memory.squad_regroup_stall_ticks[squad.squad_id]
                >= SQUAD_REGROUP_STALL_TICKS
            ):
                replacement = choose_squad_regroup_goal(
                    members,
                    regroup_obstacles,
                    excluded={regroup_goal},
                )
                if replacement is not None:
                    regroup_goal = replacement
                    memory.squad_regroup_goal[squad.squad_id] = replacement
                    memory.squad_regroup_last_distance[squad.squad_id] = (
                        squad_regroup_distance(members, replacement)
                    )
                    actions.append(
                        f"squad regroup-repath team={squad.squad_id} "
                        f"goal={replacement}"
                    )
                memory.squad_regroup_stall_ticks[squad.squad_id] = 0

        if home_emergency:
            mission_goal = squad_target_position
            mission_label = "squad-home-support"
        elif regroup_paused:
            mission_goal = regroup_goal or leader_position
            mission_label = "squad-regroup-observe"
        elif regroup_active:
            mission_goal = regroup_goal
            mission_label = "squad-regroup"
        elif not squad.complete:
            mission_goal = add(
                context.core_pos,
                HOME_PATROL_OFFSETS[squad.squad_id % len(HOME_PATROL_OFFSETS)],
            )
            mission_label = "squad-stage"
        elif memory.assault_gathering and memory.assault_rally_position is not None:
            mission_goal = memory.assault_rally_position
            mission_label = "squad-gather"
        elif squad_target_position is not None:
            mission_goal = squad_target_position
            mission_label = "squad-hunt" if nearby_worker is not None else "squad-assault"
        else:
            patrol_goal = memory.squad_patrol_goal.get(squad.squad_id)
            if patrol_goal is None or leader_position == patrol_goal:
                patrol_goal = memory.roam_goal_for(
                    leader.id,
                    context.core_pos,
                    leader_position,
                    memory.known_obstacles | context.known_enemy_core_cells,
                )
                memory.squad_patrol_goal[squad.squad_id] = patrol_goal
            mission_goal = patrol_goal
            mission_label = "squad-patrol"

        planned_leader_position = leader_position
        ordered_members = sorted(
            members,
            key=lambda unit: (
                not (
                    squad_idle_for_healing
                    and tuple(unit.position) == context.core_pos
                    and unit.hp >= UNIT_MAX_HP[unit.unit_type]
                ),
                unit.id != leader.id,
                unit.unit_type is UnitType.RANGER,
                str(unit.id),
            ),
        )
        for unit in ordered_members:
            position: Pos = tuple(unit.position)
            occupied.discard(position)

            if squad_idle_for_healing and (
                plan_idle_core_egress(
                    context,
                    unit,
                    static_obstacles | context.visible_enemy_unit_cells,
                    f"squad-heal team={squad.squad_id}",
                )
                or plan_idle_healing(
                    context,
                    unit,
                    static_obstacles | context.visible_enemy_unit_cells,
                    f"squad-heal team={squad.squad_id}",
                )
            ):
                continue

            adjacent_vanguards = tuple(
                enemy
                for enemy in context.combat_enemies
                if enemy.unit_type is UnitType.VANGUARD
                and manhattan(position, tuple(enemy.position)) == 1
            )
            if unit.unit_type is UnitType.RANGER and adjacent_vanguards:
                destination = choose_flee_step(
                    position,
                    (tuple(enemy.position) for enemy in adjacent_vanguards),
                    context.core_pos,
                    memory.known_obstacles | context.known_enemy_core_cells,
                    occupied.occupied_cells() | context.visible_enemy_unit_cells,
                    True,
                )
                if destination is not None:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        unit.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(unit.id)[:8]} squad-ranger-disengage "
                            f"{direction.value}"
                        )
                        continue

            incoming_threats = tuple(
                enemy
                for enemy in context.combat_enemies
                if (
                    enemy.unit_type is UnitType.VANGUARD
                    and manhattan(position, tuple(enemy.position)) == 1
                )
                or (
                    enemy.unit_type is UnitType.RANGER
                    and clear_ranger_shot(
                        tuple(enemy.position),
                        position,
                        memory.known_obstacles,
                    )
                )
            )
            shootable_threat = min(
                (
                    enemy
                    for enemy in incoming_threats
                    if unit.unit_type is UnitType.RANGER
                    and clear_ranger_shot(
                        position,
                        tuple(enemy.position),
                        memory.known_obstacles,
                    )
                ),
                key=lambda enemy: manhattan(position, tuple(enemy.position)),
                default=None,
            )
            if shootable_threat is not None:
                unit.shoot(shootable_threat)
                occupied.add(position)
                actions.append(f"{str(unit.id)[:8]} squad-self-defense-shoot")
                continue
            if (
                unit.unit_type is UnitType.RANGER
                and squad_support_target is not None
            ):
                support_position = tuple(squad_support_target.position)
                if clear_ranger_shot(
                    position,
                    support_position,
                    memory.known_obstacles,
                ):
                    unit.shoot(squad_support_target)
                    occupied.add(position)
                    actions.append(
                        f"{str(unit.id)[:8]} squad-ranger-support-shoot"
                    )
                    continue
                destination = ranger_attack_destination(
                    context,
                    position,
                    support_position,
                    static_obstacles | context.visible_enemy_unit_cells,
                )
                if destination is not None:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        unit.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(unit.id)[:8]} squad-ranger-support-aim "
                            f"{direction.value}"
                        )
                        continue
            ranged_attacker = min(
                (
                    enemy
                    for enemy in incoming_threats
                    if enemy.unit_type is UnitType.RANGER
                ),
                key=lambda enemy: manhattan(position, tuple(enemy.position)),
                default=None,
            )
            if unit.unit_type is UnitType.VANGUARD and ranged_attacker is not None:
                destination = combat_approach_step(
                    context,
                    position,
                    tuple(ranged_attacker.position),
                    static_obstacles | context.visible_enemy_unit_cells,
                )
                if destination is not None:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        unit.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(unit.id)[:8]} squad-counter-fire "
                            f"{direction.value}"
                        )
                        continue

            adjacent_enemy = min(
                (
                    enemy
                    for enemy in context.safe_enemy_units
                    if manhattan(position, tuple(enemy.position)) == 1
                ),
                key=lambda enemy: (
                    enemy.unit_type is UnitType.WORKER,
                    str(enemy.id),
                ),
                default=None,
            )
            if unit.unit_type is UnitType.VANGUARD and adjacent_enemy is not None:
                direction = direction_between(position, tuple(adjacent_enemy.position))
                if direction is not None:
                    unit.sweep(direction)
                    occupied.add(position)
                    actions.append(
                        f"{str(unit.id)[:8]} squad-self-defense {direction.value}"
                    )
                    continue

            if (
                unit.unit_type is UnitType.VANGUARD
                and squad_target_object is not None
                and squad_attack_now
                and manhattan(position, squad_target_position) == 1
            ):
                direction = direction_between(position, squad_target_position)
                if direction is not None:
                    unit.sweep(direction)
                    occupied.add(position)
                    actions.append(
                        f"{str(unit.id)[:8]} squad-assault-sweep {direction.value}"
                    )
                    continue
            if (
                unit.unit_type is UnitType.RANGER
                and squad_target_object is not None
                and squad_attack_now
                and clear_ranger_shot(
                    position,
                    squad_target_position,
                    memory.known_obstacles,
                )
            ):
                unit.shoot(squad_target_object)
                occupied.add(position)
                actions.append(f"{str(unit.id)[:8]} squad-assault-shoot")
                continue

            destination: Pos | None = None
            if regroup_paused:
                destination = None
            elif regroup_active:
                destination = squad_regroup_destination(
                    context,
                    position,
                    regroup_goal,
                    regroup_obstacles,
                )
            elif unit.id == leader.id:
                if squad_attack_now:
                    destination = combat_approach_step(
                        context,
                        position,
                        squad_target_position,
                        static_obstacles | context.visible_enemy_unit_cells,
                    )
                else:
                    destination = combat_move_toward(
                        context,
                        position,
                        mission_goal,
                        static_obstacles | context.visible_enemy_unit_cells,
                    )
            elif unit.unit_type is UnitType.RANGER:
                if (
                    squad_attack_now
                    and memory.assault_target_kind == "CORE"
                    and squad_target_position is not None
                ):
                    destination = ranger_attack_destination(
                        context,
                        position,
                        squad_target_position,
                        static_obstacles | context.visible_enemy_unit_cells,
                    )
                if destination is None:
                    destination = squad_ranger_follow_destination(
                        context,
                        position,
                        planned_leader_position,
                        mission_goal,
                        static_obstacles | context.visible_enemy_unit_cells,
                    )
            elif manhattan(position, planned_leader_position) > SQUAD_FOLLOW_DISTANCE:
                destination = combat_move_toward(
                    context,
                    position,
                    planned_leader_position,
                    (static_obstacles | context.visible_enemy_unit_cells)
                    - {planned_leader_position},
                )
            elif squad_attack_now:
                destination = combat_approach_step(
                    context,
                    position,
                    squad_target_position,
                    static_obstacles | context.visible_enemy_unit_cells,
                )
                if (
                    destination is not None
                    and manhattan(destination, planned_leader_position)
                    > SQUAD_FORMATION_RADIUS
                ):
                    destination = None

            if destination is not None and destination != position:
                direction = direction_between(position, destination)
                if direction is not None:
                    unit.move(direction)
                    occupied.add(destination)
                    if unit.id == leader.id:
                        planned_leader_position = destination
                    actions.append(
                        f"{str(unit.id)[:8]} {mission_label} "
                        f"team={squad.squad_id} {direction.value}"
                    )
                    continue
            unit.wait()
            occupied.add(position)
            actions.append(
                f"{str(unit.id)[:8]} {mission_label}-hold team={squad.squad_id}"
            )


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

    for vanguard_index, vanguard in enumerate(context.vanguards):
        if vanguard.id in context.reserved_combat_ids:
            continue
        position: Pos = tuple(vanguard.position)
        occupied.discard(position)
        is_home_guard = vanguard.id in context.home_squad_ids

        if context.spawn_clearing and not context.home_combat_targets:
            destination = core_clear_destination(
                context,
                position,
                vanguard_index + len(context.workers),
            )
            if destination is not None:
                direction = direction_between(position, destination)
                if direction is not None:
                    vanguard.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(vanguard.id)[:8]} spawn-clear {direction.value}"
                    )
                    continue

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

            home_response_target = min(
                context.roam_home_response_targets,
                key=lambda enemy: (
                    manhattan(position, tuple(enemy.position)),
                    str(enemy.id),
                ),
                default=None,
            )
            if home_response_target is not None:
                target_position = tuple(home_response_target.position)
                if manhattan(position, target_position) == 1:
                    direction = direction_between(position, target_position)
                    if direction is not None:
                        vanguard.sweep(direction)
                        occupied.add(position)
                        actions.append(
                            f"{str(vanguard.id)[:8]} roam-home-response-sweep "
                            f"{direction.value}"
                        )
                        continue
                destination = combat_approach_step(
                    context,
                    position,
                    target_position,
                    combat_navigation_obstacles,
                )
                if destination is not None:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        vanguard.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(vanguard.id)[:8]} roam-home-response "
                            f"{direction.value}"
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

            if (
                plan_idle_core_egress(
                    context,
                    vanguard,
                    combat_navigation_obstacles,
                    "vanguard-heal",
                )
                or plan_idle_healing(
                    context,
                    vanguard,
                    combat_navigation_obstacles,
                    "vanguard-heal",
                )
            ):
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

        if (
            plan_idle_core_egress(
                context,
                vanguard,
                combat_navigation_obstacles,
                "guard-heal",
            )
            or plan_idle_healing(
                context,
                vanguard,
                combat_navigation_obstacles,
                "guard-heal",
            )
        ):
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
        and vanguard.id not in context.reserved_combat_ids
    )
    roaming_rangers = tuple(
        ranger
        for ranger in context.rangers
        if ranger.id != memory.home_ranger_id
        and ranger.id not in context.reserved_combat_ids
    )
    follow_assignments = memory.assign_ranger_follow_targets(
        roaming_rangers,
        roaming_vanguards,
    )
    vanguard_positions = planned_vanguard_positions(context, roaming_vanguards)

    for ranger_index, ranger in enumerate(context.rangers):
        if ranger.id in context.reserved_combat_ids:
            continue
        position: Pos = tuple(ranger.position)
        occupied.discard(position)
        visible_entity_cells = {
            tuple(enemy.position) for enemy in context.turn.visible_enemies
        }
        is_home_guard = ranger.id in context.home_squad_ids

        if context.spawn_clearing and not context.home_combat_targets:
            destination = core_clear_destination(
                context,
                position,
                ranger_index + len(context.workers) + len(context.vanguards),
            )
            if destination is not None:
                direction = direction_between(position, destination)
                if direction is not None:
                    ranger.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(ranger.id)[:8]} spawn-clear {direction.value}"
                    )
                    continue

        adjacent_vanguards = tuple(
            enemy
            for enemy in context.combat_enemies
            if enemy.unit_type is UnitType.VANGUARD
            and manhattan(position, tuple(enemy.position)) == 1
        )
        if adjacent_vanguards:
            destination = choose_flee_step(
                position,
                (tuple(enemy.position) for enemy in adjacent_vanguards),
                context.core_pos,
                memory.known_obstacles | context.known_enemy_core_cells,
                occupied.occupied_cells() | context.visible_enemy_unit_cells,
                True,
            )
            if destination is not None:
                direction = direction_between(position, destination)
                if direction is not None:
                    ranger.move(direction)
                    occupied.add(destination)
                    actions.append(
                        f"{str(ranger.id)[:8]} ranger-disengage {direction.value}"
                    )
                    continue

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

            home_response_target = min(
                context.roam_home_response_targets,
                key=lambda enemy: (
                    manhattan(position, tuple(enemy.position)),
                    str(enemy.id),
                ),
                default=None,
            )
            if home_response_target is not None:
                target_position = tuple(home_response_target.position)
                if clear_ranger_shot(
                    position,
                    target_position,
                    ranger_shot_blockers,
                ):
                    ranger.shoot(home_response_target)
                    occupied.add(position)
                    actions.append(
                        f"{str(ranger.id)[:8]} roam-home-response-shoot"
                    )
                    continue
                destination = roam_ranger_response_step(
                    context,
                    position,
                    target_position,
                    ranger_navigation_obstacles | context.visible_enemy_unit_cells,
                )
                if destination is not None:
                    direction = direction_between(position, destination)
                    if direction is not None:
                        ranger.move(direction)
                        occupied.add(destination)
                        actions.append(
                            f"{str(ranger.id)[:8]} roam-home-response "
                            f"{direction.value}"
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

            if (
                plan_idle_core_egress(
                    context,
                    ranger,
                    ranger_navigation_obstacles | context.visible_enemy_unit_cells,
                    "ranger-heal",
                )
                or plan_idle_healing(
                    context,
                    ranger,
                    ranger_navigation_obstacles | context.visible_enemy_unit_cells,
                    "ranger-heal",
                )
            ):
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

        if (
            plan_idle_core_egress(
                context,
                ranger,
                ranger_navigation_obstacles | context.visible_enemy_unit_cells,
                "guard-heal",
            )
            or plan_idle_healing(
                context,
                ranger,
                ranger_navigation_obstacles | context.visible_enemy_unit_cells,
                "guard-heal",
            )
        ):
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
    if context.spawn_clearing:
        actions.append(f"core hold spawn-clear until={memory.spawn_clear_until}")
        return
    core_available = (
        turn.core.view.state is CoreState.NORMAL
        and context.core_pos not in context.occupied
    )
    if not core_available or turn.state.population >= MAX_AUTO_POPULATION:
        return
    available_resources = context.healing_resources

    # 控制模式：固定四个 Worker，其余人口严格补成 2V1R 小队。
    if mode == "control":
        spawn_type: UnitType | None = None
        spawn_reason = ""
        if len(context.workers) < TARGET_WORKERS_CONTROL and available_resources >= 5:
            spawn_type = UnitType.WORKER
            spawn_reason = (
                f"expand Workers {len(context.workers) + 1}/"
                f"{TARGET_WORKERS_CONTROL}"
            )
        else:
            home_squad = next(
                (squad for squad in context.combat_squads if squad.squad_id == 0),
                CombatSquad(0, (), ()),
            )
            incomplete = (
                home_squad
                if not home_squad.complete
                else next(
                    (squad for squad in context.combat_squads if not squad.complete),
                    None,
                )
            )
            if incomplete is None:
                next_squad_id = max(
                    (squad.squad_id for squad in context.combat_squads),
                    default=-1,
                ) + 1
                missing_type = UnitType.VANGUARD
                squad_id = next_squad_id
            elif len(incomplete.vanguard_ids) < SQUAD_VANGUARDS:
                missing_type = UnitType.VANGUARD
                squad_id = incomplete.squad_id
            else:
                missing_type = UnitType.RANGER
                squad_id = incomplete.squad_id
            cost = 10 if missing_type is UnitType.VANGUARD else 12
            if available_resources >= cost:
                spawn_type = missing_type
                spawn_reason = (
                    f"fill squad={squad_id} "
                    f"{len(incomplete.vanguard_ids) if incomplete else 0}V:"
                    f"{len(incomplete.ranger_ids) if incomplete else 0}R"
                )
        if spawn_type is not None:
            turn.core.spawn(spawn_type)
            actions.append(f"core spawn {spawn_type.value} ({spawn_reason})")
        return

    # 采集模式也不再自动超过四个 Worker。
    if mode == "harvest":
        spawned_worker = False
        if len(context.workers) < TARGET_WORKERS_CONTROL:
            worker_threshold = 5
            if available_resources >= worker_threshold:
                turn.core.spawn(UnitType.WORKER)
                spawned_worker = True
                actions.append(
                    f"core spawn WORKER (reserve={available_resources - 5})"
                )
        if not spawned_worker:
            needs_capacity = turn.resource_capacity < target
            defense_is_thin = bool(context.home_combat_targets) and (
                not context.vanguards
                or len(context.home_combat_targets)
                > len(context.rangers) + len(context.vanguards)
            )
            if available_resources >= 10 and (needs_capacity or defense_is_thin):
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
    memory.observe_spawn_blocks(turn.events, turn.tick)

    if mode == "harvest" and turn.resources >= target:
        return actions, True
    if turn.state.status is not PlayerStatus.ACTIVE or turn.core is None:
        return actions, False

    core_pos: Pos = tuple(turn.core.position)
    workers = sorted(turn.workers, key=lambda worker: str(worker.id))
    vanguards = sorted(turn.vanguards, key=lambda unit: str(unit.id))
    rangers = sorted(turn.rangers, key=lambda unit: str(unit.id))
    core_changed = memory.sync_core_position(core_pos)
    sectors_changed = memory.prune_unit_state(
        workers,
        vanguards,
        rangers,
        turn.tick,
    )
    squads_changed = memory.sync_combat_squads(
        vanguards,
        rangers,
        core_pos,
    )
    combat_squads = memory.combat_squads(vanguards, rangers)
    home_squad = next(
        (squad for squad in combat_squads if squad.squad_id == 0),
        None,
    )
    home_squad_ids = home_squad.unit_ids if home_squad else set()
    field_combat_ids = set().union(
        *(
            squad.unit_ids
            for squad in combat_squads
            if squad.squad_id != 0
        ),
    ) if any(squad.squad_id != 0 for squad in combat_squads) else set()
    reserved_combat_ids = set(field_combat_ids)
    sectors_before = dict(memory.worker_sector)
    memory.sync_worker_sectors(workers)
    sectors_changed = sectors_changed or memory.worker_sector != sectors_before
    outer_scout_active = (
        mode == "control"
        and turn.state.population >= MAX_AUTO_POPULATION
        and turn.resources >= OUTER_SCOUT_RESOURCE_THRESHOLD
    )
    if memory.sync_outer_scout_mode(outer_scout_active):
        actions.append(
            "workers outer-scout-active"
            if outer_scout_active
            else "workers outer-scout-complete"
        )
    memory.observe_worker_harvests(turn.events, workers)
    memory.sync_ranger_coverage(bool(turn.rangers), workers)
    friendly_positions = tuple(tuple(unit.position) for unit in turn.units)
    vision_sources = friendly_vision_sources(turn.core, turn.units)
    visible_resources: set[Pos] = {
        tuple(position)
        for position in turn.resource_cells
        if chebyshev(core_pos, tuple(position)) <= RESOURCE_MEMORY_RADIUS
    }
    resources_changed = memory.prune_resources_outside(
        core_pos,
        RESOURCE_MEMORY_RADIUS,
    )
    obstacle_resource_conflicts = memory.known_resources & memory.known_obstacles
    resources_changed = bool(obstacle_resource_conflicts) or resources_changed
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
    assault_changed = memory.sync_assault_target(
        turn.visible_enemies,
        core_pos,
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
    roam_home_response_targets = tuple(
        enemy
        for enemy in safe_enemy_units
        if enemy.unit_type in {UnitType.VANGUARD, UnitType.RANGER}
        and manhattan(core_pos, tuple(enemy.position))
        <= ROAM_HOME_RESPONSE_RADIUS
    )
    home_enemy_units = tuple(
        enemy
        for enemy in safe_enemy_units
        if manhattan(core_pos, tuple(enemy.position)) <= HOME_ENGAGE_RADIUS
    )

    # 资源任务跨 Tick 保留；只有空载、无资源任务且不在撤退的 Worker 才空闲。
    occupied = FriendlyOccupancy(tuple(unit.position) for unit in turn.units)
    resource_assignments = (
        {}
        if outer_scout_active
        else memory.assign_resource_targets(
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
    )
    roaming_combat_units = tuple(
        unit
        for unit in (*vanguards, *rangers)
        if unit.id in field_combat_ids
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
        safe_enemy_units=safe_enemy_units,
        combat_enemies=combat_enemies,
        home_combat_targets=home_combat_targets,
        roam_home_response_targets=roam_home_response_targets,
        home_enemy_units=home_enemy_units,
        navigation_obstacles=navigation_obstacles,
        danger_cells=danger_cells,
        threat_positions=threat_positions,
        occupied=occupied,
        resource_assignments=resource_assignments,
        reserved_combat_ids=reserved_combat_ids,
        combat_squads=combat_squads,
        home_squad_ids=home_squad_ids,
        field_combat_ids=field_combat_ids,
        assault_target=next(
            (
                enemy
                for enemy in turn.visible_enemies
                if enemy.id == memory.assault_target_id
            ),
            None,
        ),
        roaming_combat_units=roaming_combat_units,
        roam_is_aggressive=roam_is_aggressive,
        trap_obstacles=trap_obstacles,
        roam_target_track=roam_target_track,
        roam_target_enemy=roam_target_enemy,
        roam_target_id=roam_target_id,
        blocker_worker_id=blocker_worker_id,
        spawn_clearing=turn.tick < memory.spawn_clear_until,
        outer_scout_active=outer_scout_active,
        healing_resources=turn.resources,
        actions=actions,
    )
    plan_workers(context)
    plan_field_squads(context)
    plan_vanguards(context)
    plan_rangers(context)
    plan_core_production(context, mode, target)

    if (
        core_changed
        or resources_changed
        or squads_changed
        or sectors_changed
        or enemy_cores_changed
        or assault_changed
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
