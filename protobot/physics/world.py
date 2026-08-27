"""Collision-world interface and an in-memory implementation for tests/automation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from .geometry import AABB, Vec3
from .math import f32

FLOW_FACE_WEST = 1 << 0
FLOW_FACE_EAST = 1 << 1
FLOW_FACE_NORTH = 1 << 2
FLOW_FACE_SOUTH = 1 << 3
ALL_HORIZONTAL_FLOW_FACES = (
    FLOW_FACE_WEST | FLOW_FACE_EAST | FLOW_FACE_NORTH | FLOW_FACE_SOUTH
)
_HORIZONTAL_DIRECTIONS = (
    (0, -1, FLOW_FACE_NORTH),
    (1, 0, FLOW_FACE_EAST),
    (0, 1, FLOW_FACE_SOUTH),
    (-1, 0, FLOW_FACE_WEST),
)


@dataclass(frozen=True, slots=True)
class BlockProperties:
    friction: float = f32(0.6)
    speed_factor: float = 1.0
    jump_factor: float = 1.0
    climbable: bool = False
    fluid: str | None = None
    # The table stores FluidState.getOwnHeight(). A same-fluid state directly
    # above raises the effective height to one block at query time.
    fluid_height: float = 0.0
    # ``None`` derives FlowingFluid.getFlow from neighboring states. Supplying
    # a vector remains an explicit override for custom or modded fluids.
    fluid_flow: tuple[float, float, float] | None = None
    fluid_falling: bool = False
    blocks_motion: bool = False
    # Bit mask of horizontal faces for FlowingFluid.isSolidFace. Official
    # tables pre-exclude ice, matching vanilla's special case.
    fluid_sturdy_faces: int = 0
    scaffolding: bool = False
    bounce_restitution: float = 0.0
    suppress_bounce: bool = False
    # ``None`` denotes a non-bubble block. Bubble columns use ``False`` for
    # the soul-sand upward stream and ``True`` for the magma drag-down stream.
    bubble_column_drag_down: bool | None = None
    powder_snow: bool = False
    stuck_speed_multiplier: tuple[float, float, float] | None = None
    weaving_stuck_speed_multiplier: tuple[float, float, float] | None = None
    honey_block: bool = False
    slime_block: bool = False
    boat_ignored_friction: bool = False


AIR = BlockProperties(friction=f32(0.6))
DEFAULT_BLOCK = BlockProperties(
    blocks_motion=True,
    fluid_sturdy_faces=ALL_HORIZONTAL_FLOW_FACES,
)
FULL_BLOCK = AABB(0.0, 0.0, 0.0, 1.0, 1.0, 1.0)


class CollisionWorld(Protocol):
    def collision_boxes(self, query: AABB) -> list[AABB]: ...

    def block_collision_boxes(self, x: int, y: int, z: int) -> tuple[AABB, ...]: ...

    def block_properties(self, x: int, y: int, z: int) -> BlockProperties: ...

    def no_collision(self, query: AABB) -> bool: ...

    def entity_collision_boxes(self, query: AABB) -> list[AABB]: ...


def fluid_height(world: CollisionWorld, x: int, y: int, z: int) -> float:
    """Return ``FluidState.getHeight`` for the state at the given position."""

    properties = world.block_properties(x, y, z)
    if properties.fluid is None or properties.fluid_height <= 0.0:
        return 0.0
    above = world.block_properties(x, y + 1, z)
    if above.fluid == properties.fluid:
        return 1.0
    return properties.fluid_height


def fluid_flow(world: CollisionWorld, x: int, y: int, z: int) -> Vec3:
    """Port ``FlowingFluid.getFlow`` using the decoded neighboring grid."""

    current = world.block_properties(x, y, z)
    fluid = current.fluid
    if fluid is None or current.fluid_height <= 0.0:
        return Vec3()
    if current.fluid_flow is not None:
        return Vec3(*current.fluid_flow)

    flow_x = 0.0
    flow_z = 0.0
    current_height = f32(current.fluid_height)
    full_fluid_height = f32(8.0 / 9.0)
    for step_x, step_z, _face in _HORIZONTAL_DIRECTIONS:
        neighbor = world.block_properties(x + step_x, y, z + step_z)
        # affectsFlow accepts empty fluid states and states in the same fluid
        # family, but rejects a different non-empty fluid.
        if neighbor.fluid is not None and neighbor.fluid != fluid:
            continue
        neighbor_height = f32(neighbor.fluid_height if neighbor.fluid == fluid else 0.0)
        distance = 0.0
        if neighbor_height == 0.0:
            below = world.block_properties(x + step_x, y - 1, z + step_z)
            if (
                not neighbor.blocks_motion
                and below.fluid == fluid
                and below.fluid_height > 0.0
            ):
                neighbor_height = f32(below.fluid_height)
                distance = f32(
                    current_height - f32(neighbor_height - full_fluid_height)
                )
        elif neighbor_height > 0.0:
            distance = f32(current_height - neighbor_height)
        if distance == 0.0:
            continue
        flow_x += float(f32(float(step_x) * distance))
        flow_z += float(f32(float(step_z) * distance))

    flow = Vec3(flow_x, 0.0, flow_z)
    if current.fluid_falling:
        for step_x, step_z, face in _HORIZONTAL_DIRECTIONS:
            neighbor = world.block_properties(x + step_x, y, z + step_z)
            above_neighbor = world.block_properties(x + step_x, y + 1, z + step_z)
            neighbor_solid = (
                neighbor.fluid != fluid and bool(neighbor.fluid_sturdy_faces & face)
            )
            above_solid = (
                above_neighbor.fluid != fluid
                and bool(above_neighbor.fluid_sturdy_faces & face)
            )
            if neighbor_solid or above_solid:
                flow = flow.normalized() + Vec3(0.0, -6.0, 0.0)
                break
    return flow.normalized()


class StaticCollisionWorld:
    """Sparse blocks with arbitrary per-block collision boxes."""

    def __init__(self) -> None:
        self._blocks: dict[tuple[int, int, int], tuple[tuple[AABB, ...], BlockProperties]] = {}
        self._entity_boxes: dict[int, AABB] = {}

    def add_block(
        self,
        x: int,
        y: int,
        z: int,
        *,
        shapes: tuple[AABB, ...] = (FULL_BLOCK,),
        properties: BlockProperties = DEFAULT_BLOCK,
    ) -> None:
        self._blocks[(x, y, z)] = (shapes, properties)

    def remove_block(self, x: int, y: int, z: int) -> None:
        self._blocks.pop((x, y, z), None)

    def set_entity_collision_box(self, entity_id: int, box: AABB) -> None:
        self._entity_boxes[entity_id] = box

    def remove_entity_collision_box(self, entity_id: int) -> None:
        self._entity_boxes.pop(entity_id, None)

    def fill(
        self,
        start: tuple[int, int, int],
        end: tuple[int, int, int],
        *,
        properties: BlockProperties = DEFAULT_BLOCK,
    ) -> None:
        for x in range(start[0], end[0]):
            for y in range(start[1], end[1]):
                for z in range(start[2], end[2]):
                    self.add_block(x, y, z, properties=properties)

    def collision_boxes(self, query: AABB) -> list[AABB]:
        result: list[AABB] = []
        min_x, max_x = math.floor(query.min_x), math.floor(query.max_x + 1.0)
        min_y, max_y = math.floor(query.min_y), math.floor(query.max_y + 1.0)
        min_z, max_z = math.floor(query.min_z), math.floor(query.max_z + 1.0)
        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                for z in range(min_z, max_z + 1):
                    entry = self._blocks.get((x, y, z))
                    if entry is None:
                        continue
                    for shape in entry[0]:
                        world_shape = shape.move(x, y, z)
                        if world_shape.intersects(query):
                            result.append(world_shape)
        return result

    def block_collision_boxes(self, x: int, y: int, z: int) -> tuple[AABB, ...]:
        entry = self._blocks.get((x, y, z))
        if entry is None:
            return ()
        return tuple(shape.move(x, y, z) for shape in entry[0])

    def block_properties(self, x: int, y: int, z: int) -> BlockProperties:
        entry = self._blocks.get((x, y, z))
        return AIR if entry is None else entry[1]

    def fluid_height(self, x: int, y: int, z: int) -> float:
        return fluid_height(self, x, y, z)

    def fluid_flow(self, x: int, y: int, z: int) -> Vec3:
        return fluid_flow(self, x, y, z)

    def no_collision(self, query: AABB) -> bool:
        return not self.collision_boxes(query) and not self.entity_collision_boxes(query)

    def entity_collision_boxes(self, query: AABB) -> list[AABB]:
        return [box for box in self._entity_boxes.values() if box.intersects(query)]
