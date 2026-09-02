"""Collision-aware path planning for the high-level bot API."""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass

from .errors import ProtoBotError
from .physics.geometry import AABB, Vec3
from .physics.world import CollisionWorld


class PathNotFound(ProtoBotError):
    """No walkable path was found within the configured search budget."""


class NavigationTimeout(ProtoBotError, TimeoutError):
    """A navigation task did not reach its target before its deadline."""


@dataclass(frozen=True, slots=True)
class PathWaypoint:
    """One node in a planned route.

    ``operation`` names the edge operation used to reach this node from the
    previous node: ``walk``, ``jump``, ``fly``, or ``vclip``.  The legacy
    ``jump``/``vclip`` flags remain available for callers that constructed
    waypoints before the explicit operation field was added.
    """

    position: Vec3
    jump: bool = False
    vclip: bool = False
    operation: str = ""

    def __post_init__(self) -> None:
        operation = self.operation.strip().lower() if self.operation else ""
        if not operation:
            if self.vclip:
                operation = "vclip"
            elif self.jump:
                operation = "jump"
            else:
                operation = "fly"
        if operation not in {"walk", "jump", "fly", "vclip"}:
            raise ValueError(
                "waypoint operation must be one of walk, jump, fly, or vclip"
            )
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "jump", self.jump or operation == "jump")
        object.__setattr__(self, "vclip", self.vclip or operation == "vclip")

    @property
    def action(self) -> str:
        """Alias for :attr:`operation` used by node-oriented clients."""

        return self.operation


# Node-oriented name for callers that prefer graph terminology.
PathNode = PathWaypoint


@dataclass(frozen=True, slots=True)
class NavigationPath:
    """An immutable route plus useful search diagnostics."""

    waypoints: tuple[PathWaypoint, ...]
    explored_nodes: int
    cost: float

    def __len__(self) -> int:
        return len(self.waypoints)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.waypoints)

    @property
    def nodes(self) -> tuple[PathWaypoint, ...]:
        """The planned nodes, named explicitly for graph-oriented callers."""

        return self.waypoints

    @property
    def operations(self) -> tuple[str, ...]:
        """Operations for each node, in path order."""

        return tuple(node.operation for node in self.waypoints)


@dataclass(frozen=True, slots=True)
class _GridNode:
    x: int
    z: int
    y: float

    @property
    def position(self) -> Vec3:
        return Vec3(self.x + 0.5, self.y, self.z + 0.5)

    @property
    def key(self) -> tuple[int, int, int]:
        # Official collision shapes are quantized much more coarsely than this.
        # The fixed key also keeps equivalent floating-point shape tops merged.
        return self.x, self.z, round(self.y * 1_000_000)


class Pathfinder:
    """A* planner over the exact collision boxes exposed by a world.

    Nodes are safe player-feet positions on collision-shape top faces.  Edges
    model ordinary horizontal travel, one-block jumps, and bounded falls.  The
    planner deliberately does not mutate the world or simulate packets; Bot's
    :meth:`navigate_to` method executes the returned route through normal 20 Hz
    physics ticks.
    """

    _CARDINAL = ((1, 0), (-1, 0), (0, 1), (0, -1))
    _DIAGONAL = ((1, 1), (1, -1), (-1, 1), (-1, -1))

    def __init__(
        self,
        world: CollisionWorld,
        *,
        player_width: float = 0.6,
        player_height: float = 1.8,
        step_height: float = 0.6,
        max_jump_height: float = 1.25,
        max_drop: float = 3.0,
        allow_diagonal: bool = True,
    ) -> None:
        values = (player_width, player_height, step_height, max_jump_height, max_drop)
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            raise ValueError("pathfinder dimensions and movement limits must be positive")
        if max_jump_height < step_height:
            raise ValueError("max_jump_height cannot be lower than step_height")
        self.world = world
        self.player_width = player_width
        self.player_height = player_height
        self.step_height = step_height
        self.max_jump_height = max_jump_height
        self.max_drop = max_drop
        self.allow_diagonal = allow_diagonal

    def find_path(
        self,
        start: Vec3,
        target: Vec3,
        *,
        match_target_y: bool = True,
        target_y_tolerance: float = 0.51,
        max_nodes: int = 4096,
    ) -> NavigationPath:
        """Find a collision-safe route from ``start`` to ``target``.

        ``match_target_y=False`` makes the first reachable standing surface in
        the target X/Z cell acceptable.  This is useful when the caller knows a
        horizontal destination but not the terrain's exact top face.
        """

        if max_nodes <= 0:
            raise ValueError("max_nodes must be positive")
        if not math.isfinite(target_y_tolerance) or target_y_tolerance < 0.0:
            raise ValueError("target_y_tolerance must be finite and non-negative")
        if not self._finite_vec(start) or not self._finite_vec(target):
            raise ValueError("path endpoints must contain finite coordinates")

        start_node = self._start_node(start)
        goal_x, goal_z = math.floor(target.x), math.floor(target.z)
        serial = itertools.count()
        frontier: list[tuple[float, int, tuple[int, int, int, int, int]]] = []
        nodes = {start_node.key: start_node}
        costs = {start_node.key: 0.0}
        parents: dict[tuple[int, int, int], tuple[int, int, int]] = {}
        operations: dict[tuple[int, int, int], str] = {}
        heapq.heappush(
            frontier,
            (
                self._heuristic(start_node, target, match_target_y),
                next(serial),
                start_node.key,
            ),
        )

        explored = 0
        goal: _GridNode | None = None
        while frontier and explored < max_nodes:
            _, _, current_key = heapq.heappop(frontier)
            current = nodes[current_key]
            explored += 1
            if self._is_goal(
                current,
                goal_x,
                goal_z,
                target.y,
                match_target_y,
                target_y_tolerance,
            ):
                goal = current
                break

            for neighbor, requires_jump, edge_cost in self._neighbors(current):
                key = neighbor.key
                new_cost = costs[current_key] + edge_cost
                if new_cost >= costs.get(key, math.inf):
                    continue
                costs[key] = new_cost
                nodes[key] = neighbor
                parents[key] = current_key
                operations[key] = "jump" if requires_jump else "walk"
                priority = new_cost + self._heuristic(neighbor, target, match_target_y)
                heapq.heappush(frontier, (priority, next(serial), key))

        if goal is None:
            raise PathNotFound(
                f"no path to ({target.x:.3f}, {target.y:.3f}, {target.z:.3f}) "
                f"after exploring {explored} nodes"
            )

        chain: list[_GridNode] = []
        key = goal.key
        while key != start_node.key:
            chain.append(nodes[key])
            key = parents[key]
        chain.reverse()
        waypoints: list[PathWaypoint] = []
        # The search node is the cell center, while a spawned player may be
        # anywhere in that cell.  Anchor off-center starts before following
        # grid edges so the first edge is checked from the actual body.
        anchor = start_node.position
        if (
            self._body_clear(start.x, start.y, start.z)
            and (start - anchor).length_squared > 1.0e-8
        ):
            anchor_jump = anchor.y > start.y + self.step_height + 1.0e-7
            if not self._transition_clear(start, anchor, anchor_jump):
                raise PathNotFound("the starting position cannot reach its path grid cell")
            waypoints.append(
                PathWaypoint(anchor, anchor_jump, operation="jump" if anchor_jump else "walk")
            )
        for node in chain:
            operation = operations[node.key]
            waypoints.append(
                PathWaypoint(
                    node.position,
                    operation=operation,
                )
            )

        # Finish at the caller's exact horizontal coordinate when the body can
        # occupy it.  Grid centers remain the fallback near obstructed edges.
        exact = Vec3(target.x, goal.y, target.z)
        previous = start if not waypoints else waypoints[-1].position
        exact_jump = exact.y > previous.y + self.step_height + 1.0e-7
        if (
            self._body_clear(exact.x, exact.y, exact.z)
            and self._supported(exact.x, exact.y, exact.z)
            and self._transition_clear(previous, exact, exact_jump)
            and self._horizontal_distance_squared(previous, exact) > 1.0e-8
        ):
            waypoints.append(
                PathWaypoint(exact, exact_jump, operation="jump" if exact_jump else "walk")
            )

        return NavigationPath(tuple(waypoints), explored, costs[goal.key])

    def _start_node(self, start: Vec3) -> _GridNode:
        x, z = math.floor(start.x), math.floor(start.z)
        if self._body_clear(start.x, start.y, start.z) and self._supported(
            start.x, start.y, start.z
        ):
            return _GridNode(x, z, start.y)
        surfaces = self._surface_heights(x, z, start.y)
        if not surfaces:
            raise PathNotFound("the starting cell has no safe standing surface")
        # Prefer the surface directly below an airborne or slightly corrected
        # spawn, then the nearest surface if the player starts inside geometry.
        below = [surface for surface in surfaces if surface <= start.y + 1.0e-6]
        y = max(below) if below else min(surfaces, key=lambda surface: abs(surface - start.y))
        return _GridNode(x, z, y)

    def _neighbors(self, current: _GridNode):  # type: ignore[no-untyped-def]
        offsets = self._CARDINAL + (self._DIAGONAL if self.allow_diagonal else ())
        current_position = current.position
        for dx, dz in offsets:
            x, z = current.x + dx, current.z + dz
            for y in self._surface_heights(x, z, current.y):
                delta_y = y - current.y
                if delta_y > self.max_jump_height + 1.0e-7:
                    continue
                if delta_y < -self.max_drop - 1.0e-7:
                    continue
                requires_jump = delta_y > self.step_height + 1.0e-7
                position = Vec3(x + 0.5, y, z + 0.5)
                if not self._transition_clear(current_position, position, requires_jump):
                    continue
                horizontal = math.sqrt(dx * dx + dz * dz)
                edge_cost = horizontal + abs(delta_y) * 0.35 + (0.35 if requires_jump else 0.0)
                yield _GridNode(x, z, y), requires_jump, edge_cost

    def _surface_heights(self, x: int, z: int, reference_y: float) -> tuple[float, ...]:
        center_x, center_z = x + 0.5, z + 0.5
        half = self.player_width / 2.0 - 1.0e-6
        low = reference_y - self.max_drop - 1.0
        high = reference_y + self.max_jump_height + self.player_height + 1.0
        query = AABB(
            center_x - half,
            low,
            center_z - half,
            center_x + half,
            high,
            center_z + half,
        )
        surfaces = {
            box.max_y
            for box in self.world.collision_boxes(query)
            if reference_y - self.max_drop - 1.0e-7
            <= box.max_y
            <= reference_y + self.max_jump_height + 1.0e-7
            and self._body_clear(center_x, box.max_y, center_z)
            and self._supported(center_x, box.max_y, center_z)
        }
        return tuple(sorted(surfaces, key=lambda y: (abs(y - reference_y), y)))

    def _body_clear(self, x: float, y: float, z: float) -> bool:
        half = self.player_width / 2.0
        epsilon = 1.0e-7
        return self.world.no_collision(
            AABB(
                x - half + epsilon,
                y + epsilon,
                z - half + epsilon,
                x + half - epsilon,
                y + self.player_height - epsilon,
                z + half - epsilon,
            )
        )

    def _supported(self, x: float, y: float, z: float) -> bool:
        half = self.player_width / 2.0 - 1.0e-6
        probe = AABB(x - half, y - 0.05, z - half, x + half, y + 1.0e-7, z + half)
        return any(abs(box.max_y - y) <= 1.0e-6 for box in self.world.collision_boxes(probe))

    def _transition_clear(self, start: Vec3, end: Vec3, requires_jump: bool) -> bool:
        delta_y = end.y - start.y
        if requires_jump:
            # A normal jump rises beside the obstacle before horizontal motion
            # clears its upper face.  Splitting the geometric check this way
            # accepts a vanilla one-block jump without accepting wall phasing.
            if not self._vertical_clear(start.x, start.z, start.y, end.y):
                return False
            travel_y = end.y
        elif delta_y < -1.0e-7:
            # Walking off a ledge keeps the old Y until the support is cleared,
            # then gravity resolves the fall at the destination column.
            travel_y = start.y
            if not self._vertical_clear(end.x, end.z, start.y, end.y):
                return False
        else:
            travel_y = end.y

        if not self._horizontal_clear(start.x, start.z, end.x, end.z, travel_y):
            return False
        if start.x != end.x and start.z != end.z:
            # At least one axis order must fit the player's full width.  This
            # prevents an A* diagonal from cutting through a blocked corner.
            x_first = self._horizontal_clear(start.x, start.z, end.x, start.z, travel_y)
            x_first = x_first and self._horizontal_clear(
                end.x, start.z, end.x, end.z, travel_y
            )
            z_first = self._horizontal_clear(start.x, start.z, start.x, end.z, travel_y)
            z_first = z_first and self._horizontal_clear(
                start.x, end.z, end.x, end.z, travel_y
            )
            if not (x_first or z_first):
                return False
        return True

    def _horizontal_clear(
        self,
        start_x: float,
        start_z: float,
        end_x: float,
        end_z: float,
        y: float,
    ) -> bool:
        distance = math.hypot(end_x - start_x, end_z - start_z)
        steps = max(1, math.ceil(distance * 8.0))
        for index in range(steps + 1):
            fraction = index / steps
            x = start_x + (end_x - start_x) * fraction
            z = start_z + (end_z - start_z) * fraction
            if not self._body_clear(x, y, z):
                return False
        return True

    def _vertical_clear(
        self,
        x: float,
        z: float,
        start_y: float,
        end_y: float,
    ) -> bool:
        distance = abs(end_y - start_y)
        steps = max(1, math.ceil(distance * 8.0))
        for index in range(steps + 1):
            fraction = index / steps
            if not self._body_clear(x, start_y + (end_y - start_y) * fraction, z):
                return False
        return True

    @staticmethod
    def _is_goal(
        node: _GridNode,
        goal_x: int,
        goal_z: int,
        goal_y: float,
        match_target_y: bool,
        tolerance: float,
    ) -> bool:
        return node.x == goal_x and node.z == goal_z and (
            not match_target_y or abs(node.y - goal_y) <= tolerance
        )

    @staticmethod
    def _heuristic(node: _GridNode, target: Vec3, match_target_y: bool) -> float:
        horizontal = math.hypot(node.x + 0.5 - target.x, node.z + 0.5 - target.z)
        vertical = abs(node.y - target.y) * 0.35 if match_target_y else 0.0
        return horizontal + vertical

    @staticmethod
    def _horizontal_distance_squared(first: Vec3, second: Vec3) -> float:
        return (first.x - second.x) ** 2 + (first.z - second.z) ** 2

    @staticmethod
    def _finite_vec(value: Vec3) -> bool:
        return all(math.isfinite(component) for component in (value.x, value.y, value.z))


@dataclass(frozen=True, slots=True)
class _FlightNode:
    """A half-block resolution node used by :class:`FlightPathfinder`."""

    x: int
    y2: int
    z: int
    clip_depth: int = 0
    clip_direction: int = 0

    @property
    def position(self) -> Vec3:
        return Vec3(self.x + 0.5, self.y2 / 2.0, self.z + 0.5)

    @property
    def key(self) -> tuple[int, int, int, int, int]:
        return self.x, self.y2, self.z, self.clip_depth, self.clip_direction


class FlightPathfinder:
    """A* planner for collision-aware free-flight routes.

    Flight has no support or step constraints: every node only needs enough
    empty volume for the player's body.  Half-block vertical nodes keep the
    route useful for common creative-flight targets while still bounding the
    search to a finite neighborhood around the request.
    """

    _CARDINAL = (
        (1, 0, 0),
        (-1, 0, 0),
        (0, 1, 0),
        (0, -1, 0),
        (0, 0, 1),
        (0, 0, -1),
    )
    _DIAGONAL = tuple(
        (dx, dy, dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if (dx, dy, dz) != (0, 0, 0)
        and sum(value != 0 for value in (dx, dy, dz)) >= 2
    )

    def __init__(
        self,
        world: CollisionWorld,
        *,
        player_width: float = 0.6,
        player_height: float = 1.8,
        allow_diagonal: bool = True,
        vclip: bool = True,
        vclip_up_limit: float = 3.0,
        vclip_down_limit: float = 2.0,
    ) -> None:
        if not all(
            math.isfinite(value) and value > 0.0
            for value in (player_width, player_height)
        ):
            raise ValueError("flight dimensions must be positive")
        self.world = world
        self.player_width = player_width
        self.player_height = player_height
        self.allow_diagonal = allow_diagonal
        if not isinstance(vclip, bool):
            raise TypeError("vclip must be a bool")
        if not all(
            math.isfinite(value) and value >= 0.0
            for value in (vclip_up_limit, vclip_down_limit)
        ):
            raise ValueError("VClip limits must be finite and non-negative")
        self.vclip = vclip
        self.vclip_up_limit = vclip_up_limit
        self.vclip_down_limit = vclip_down_limit

    def find_path(
        self,
        start: Vec3,
        target: Vec3,
        *,
        target_y_tolerance: float = 0.51,
        max_nodes: int = 8192,
    ) -> NavigationPath:
        """Find a route through empty volume from ``start`` to ``target``."""

        if max_nodes <= 0:
            raise ValueError("max_nodes must be positive")
        if not math.isfinite(target_y_tolerance) or target_y_tolerance < 0.0:
            raise ValueError("target_y_tolerance must be finite and non-negative")
        if not self._finite_vec(start) or not self._finite_vec(target):
            raise ValueError("path endpoints must contain finite coordinates")
        start_obstructed = not self._body_clear(start.x, start.y, start.z)
        if start_obstructed and not self.vclip:
            raise PathNotFound("the starting flight volume is obstructed")

        start_node = _FlightNode(
            math.floor(start.x),
            round(start.y * 2.0),
            math.floor(start.z),
            # A previous VClip may leave the rolling-planner start inside a
            # wall.  Seed a clipped state so the first vertical clear node can
            # exit the volume instead of rejecting the whole replan.
            clip_depth=1 if start_obstructed else 0,
        )
        goal_x, goal_z = math.floor(target.x), math.floor(target.z)
        goal_y2 = round(target.y * 2.0)
        target_clear = self._body_clear(target.x, target.y, target.z)
        # A finite envelope prevents an unreachable target from expanding the
        # entire infinite airspace.  The margin permits routes around a wall.
        margin = 16
        max_delta = max(
            abs(start_node.x - goal_x),
            abs(start_node.y2 - goal_y2) // 2,
            abs(start_node.z - goal_z),
        ) + margin
        serial = itertools.count()
        frontier: list[tuple[float, int, tuple[int, int, int, int, int]]] = []
        nodes = {start_node.key: start_node}
        costs = {start_node.key: 0.0}
        parents: dict[tuple[int, int, int, int, int], tuple[int, int, int, int, int]] = {}
        operations: dict[tuple[int, int, int, int, int], str] = {}
        heapq.heappush(frontier, (self._heuristic(start_node, target), next(serial), start_node.key))
        explored = 0
        goal: _FlightNode | None = None
        while frontier and explored < max_nodes:
            _, _, current_key = heapq.heappop(frontier)
            current = nodes[current_key]
            explored += 1
            if (
                current.x == goal_x
                and current.z == goal_z
                and abs(current.y2 / 2.0 - target.y) <= target_y_tolerance
                # Never finish a normal flight target while the planner's
                # body is still inside a block.  Clipped terminal nodes are
                # valid only when the requested target itself is obstructed.
                and (not target_clear or self._body_clear(
                    current.position.x,
                    current.position.y,
                    current.position.z,
                ))
            ):
                goal = current
                break
            offsets = self._CARDINAL + (self._DIAGONAL if self.allow_diagonal else ())
            for dx, dy, dz in offsets:
                position = Vec3(
                    current.x + dx + 0.5,
                    (current.y2 + dy) / 2.0,
                    current.z + dz + 0.5,
                )
                body_clear = self._body_clear(position.x, position.y, position.z)
                current_clear = self._body_clear(current.position.x, current.position.y, current.position.z)
                vertical = dx == 0 and dz == 0 and dy != 0
                clip_depth = 0
                clip_direction = 0
                vclip_edge = False
                if not body_clear:
                    if not self.vclip or not vertical:
                        continue
                    direction = 1 if dy > 0 else -1
                    if current.clip_direction not in (0, direction):
                        continue
                    clip_depth = current.clip_depth + 1
                    limit = self.vclip_up_limit if direction > 0 else self.vclip_down_limit
                    if clip_depth * 0.5 > limit + 1.0e-7:
                        continue
                    clip_direction = direction
                    vclip_edge = True
                elif current.clip_depth and vertical and body_clear and not current_clear:
                    # Exiting a clipped volume is still a VClip edge, but the
                    # accumulated depth must reset once the body is clear.
                    clip_depth = 0
                    clip_direction = 0
                    vclip_edge = True
                neighbor = _FlightNode(
                    current.x + dx,
                    current.y2 + dy,
                    current.z + dz,
                    clip_depth,
                    clip_direction,
                )
                if (
                    abs(neighbor.x - start_node.x) > max_delta
                    or abs(neighbor.z - start_node.z) > max_delta
                    or abs(neighbor.y2 / 2.0 - start.y) > max_delta
                ):
                    continue
                position = neighbor.position
                if not body_clear and not vclip_edge:
                    continue
                if not self._segment_clear(current.position, position) and not vclip_edge:
                    continue
                key = neighbor.key
                new_cost = costs[current_key] + math.sqrt(dx * dx + (dy * 0.5) ** 2 + dz * dz)
                if new_cost >= costs.get(key, math.inf):
                    continue
                costs[key] = new_cost
                nodes[key] = neighbor
                parents[key] = current_key
                operations[key] = "vclip" if vclip_edge else "fly"
                heapq.heappush(
                    frontier,
                    (new_cost + self._heuristic(neighbor, target), next(serial), key),
                )

        if goal is None:
            raise PathNotFound(
                f"no flight path to ({target.x:.3f}, {target.y:.3f}, {target.z:.3f}) "
                f"after exploring {explored} nodes"
            )
        chain: list[_FlightNode] = []
        key = goal.key
        while key != start_node.key:
            chain.append(nodes[key])
            key = parents[key]
        chain.reverse()
        waypoints: list[PathWaypoint] = []
        anchor = start_node.position
        if (start - anchor).length_squared > 1.0e-8:
            if not self._segment_clear(start, anchor):
                raise PathNotFound("the starting position cannot reach its flight grid cell")
            waypoints.append(PathWaypoint(anchor, operation="fly"))
        waypoints.extend(
            PathWaypoint(
                node.position,
                vclip=operations[node.key] == "vclip",
                operation=operations[node.key],
            )
            for node in chain
        )
        exact = target
        previous = chain[-1].position if chain else start
        if (
            self._body_clear(exact.x, exact.y, exact.z)
            and self._segment_clear(previous, exact)
            and (exact - previous).length_squared > 1.0e-8
        ):
            waypoints.append(PathWaypoint(exact, operation="fly"))
        waypoints = self._merge_vclip_waypoints(waypoints)
        return NavigationPath(
            tuple(self._simplify_waypoints(waypoints, start=start)),
            explored,
            costs[goal.key],
        )

    plan = find_path

    def _body_clear(self, x: float, y: float, z: float) -> bool:
        half = self.player_width / 2.0
        epsilon = 1.0e-7
        return self.world.no_collision(
            AABB(
                x - half + epsilon,
                y + epsilon,
                z - half + epsilon,
                x + half - epsilon,
                y + self.player_height - epsilon,
                z + half - epsilon,
            )
        )

    def _segment_clear(self, start: Vec3, end: Vec3) -> bool:
        distance = math.sqrt((end - start).length_squared)
        steps = max(1, math.ceil(distance * 8.0))
        for index in range(steps + 1):
            fraction = index / steps
            point = Vec3(
                start.x + (end.x - start.x) * fraction,
                start.y + (end.y - start.y) * fraction,
                start.z + (end.z - start.z) * fraction,
            )
            if not self._body_clear(point.x, point.y, point.z):
                return False
        return True

    @staticmethod
    def _merge_vclip_waypoints(waypoints: list[PathWaypoint]) -> list[PathWaypoint]:
        """Coalesce one continuous vertical VClip passage into one action.

        The A* grid uses half-block nodes so it can search precisely around
        collision boundaries.  Sending every one of those nodes is unnecessary
        and produces a burst of position packets.  A VClip action is already a
        one-shot position move, so consecutive nodes on the same vertical line
        and in the same direction can safely use the last node as their single
        endpoint.  Direction changes and any intervening flight node remain
        separate actions.
        """

        if len(waypoints) < 2:
            return waypoints
        merged: list[PathWaypoint] = []
        vclip_direction = 0
        epsilon = 1.0e-7
        for waypoint in waypoints:
            if not merged or waypoint.operation != "vclip":
                merged.append(waypoint)
                vclip_direction = 0
                continue
            previous = merged[-1]
            if previous.operation != "vclip":
                merged.append(waypoint)
                vclip_direction = 0
                continue
            delta_y = waypoint.position.y - previous.position.y
            direction = 1 if delta_y > epsilon else (-1 if delta_y < -epsilon else 0)
            same_vertical_line = (
                abs(waypoint.position.x - previous.position.x) <= epsilon
                and abs(waypoint.position.z - previous.position.z) <= epsilon
            )
            if same_vertical_line and direction and (
                vclip_direction in (0, direction)
            ):
                merged[-1] = waypoint
                vclip_direction = direction
                continue
            merged.append(waypoint)
            vclip_direction = direction
        return merged

    def _simplify_waypoints(
        self,
        waypoints: list[PathWaypoint],
        *,
        start: Vec3 | None = None,
    ) -> list[PathWaypoint]:
        """Drop redundant grid corners when a longer segment is clear.

        The raw half-block A* chain is useful for search but makes the flight
        executor toggle vertical input at every node.  Greedy line-of-sight
        compression keeps VClip boundaries intact while producing stable,
        meaningful steering points.
        """

        if len(waypoints) < 3:
            return waypoints
        result: list[PathWaypoint] = []
        index = 0
        origin = start if start is not None else waypoints[0].position
        while index < len(waypoints):
            current_position = origin
            farthest = index
            for candidate in range(index + 1, len(waypoints)):
                if any(
                    item.operation == "vclip"
                    for item in waypoints[index : candidate + 1]
                ):
                    break
                if not self._segment_clear(
                    current_position,
                    waypoints[candidate].position,
                ):
                    break
                farthest = candidate
            result.append(waypoints[farthest])
            origin = waypoints[farthest].position
            index = farthest + 1
        return result

    @staticmethod
    def _heuristic(node: _FlightNode, target: Vec3) -> float:
        position = node.position
        return math.sqrt(
            (position.x - target.x) ** 2
            + (position.y - target.y) ** 2
            + (position.z - target.z) ** 2
        )

    @staticmethod
    def _finite_vec(value: Vec3) -> bool:
        return all(math.isfinite(component) for component in (value.x, value.y, value.z))
