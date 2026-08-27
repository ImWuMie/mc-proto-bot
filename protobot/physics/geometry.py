"""Immutable geometry matching Minecraft's Vec3 and AABB operations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .math import f32

Axis = Literal["x", "y", "z"]
COLLISION_EPSILON = 1.0e-7


@dataclass(frozen=True, slots=True)
class Vec3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __add__(self, other: Vec3) -> Vec3:
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: Vec3) -> Vec3:
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def scale(self, factor: float) -> Vec3:
        return Vec3(self.x * factor, self.y * factor, self.z * factor)

    def multiply(self, x: float, y: float, z: float) -> Vec3:
        return Vec3(self.x * x, self.y * y, self.z * z)

    @property
    def length_squared(self) -> float:
        return self.x * self.x + self.y * self.y + self.z * self.z

    @property
    def horizontal_length_squared(self) -> float:
        return self.x * self.x + self.z * self.z

    def normalized(self) -> Vec3:
        length = math.sqrt(self.length_squared)
        if length < f32(1.0e-5):
            return Vec3()
        return self.scale(1.0 / length)

    def get(self, axis: Axis) -> float:
        return getattr(self, axis)

    def with_axis(self, axis: Axis, value: float) -> Vec3:
        if axis == "x":
            return Vec3(value, self.y, self.z)
        if axis == "y":
            return Vec3(self.x, value, self.z)
        return Vec3(self.x, self.y, value)


@dataclass(frozen=True, slots=True)
class AABB:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    def __post_init__(self) -> None:
        if self.min_x > self.max_x or self.min_y > self.max_y or self.min_z > self.max_z:
            raise ValueError("AABB minimum cannot exceed maximum")

    def move(self, x: float | Vec3, y: float = 0.0, z: float = 0.0) -> AABB:
        if isinstance(x, Vec3):
            x, y, z = x.x, x.y, x.z
        return AABB(
            self.min_x + x,
            self.min_y + y,
            self.min_z + z,
            self.max_x + x,
            self.max_y + y,
            self.max_z + z,
        )

    def expand_towards(self, movement: Vec3) -> AABB:
        return AABB(
            self.min_x + min(movement.x, 0.0),
            self.min_y + min(movement.y, 0.0),
            self.min_z + min(movement.z, 0.0),
            self.max_x + max(movement.x, 0.0),
            self.max_y + max(movement.y, 0.0),
            self.max_z + max(movement.z, 0.0),
        )

    def intersects(self, other: AABB) -> bool:
        return (
            self.max_x > other.min_x
            and self.min_x < other.max_x
            and self.max_y > other.min_y
            and self.min_y < other.max_y
            and self.max_z > other.min_z
            and self.min_z < other.max_z
        )

    def min(self, axis: Axis) -> float:
        return getattr(self, f"min_{axis}")

    def max(self, axis: Axis) -> float:
        return getattr(self, f"max_{axis}")

    @property
    def y_coordinates(self) -> tuple[float, float]:
        return self.min_y, self.max_y


def axis_step_order(movement: Vec3) -> tuple[Axis, Axis, Axis]:
    return ("y", "z", "x") if abs(movement.x) < abs(movement.z) else ("y", "x", "z")


def clip_axis(moving: AABB, collider: AABB, axis: Axis, distance: float) -> float:
    if abs(distance) < COLLISION_EPSILON:
        return 0.0
    other_axes = ("y", "z") if axis == "x" else (("x", "z") if axis == "y" else ("x", "y"))
    for other in other_axes:
        if (
            moving.max(other) <= collider.min(other) + COLLISION_EPSILON
            or moving.min(other) >= collider.max(other) - COLLISION_EPSILON
        ):
            return distance
    if distance > 0.0:
        # VoxelShape.collide ignores a shape that already overlaps the moving
        # box along the movement axis (its search starts at the next voxel
        # face).  Clipping to a negative ``gap`` here would incorrectly push a
        # player backwards when spawned inside a block or during a pose change.
        if moving.max(axis) > collider.min(axis) + COLLISION_EPSILON:
            return distance
        gap = collider.min(axis) - moving.max(axis)
        if gap >= -COLLISION_EPSILON:
            return min(distance, gap)
    elif distance < 0.0:
        if moving.min(axis) < collider.max(axis) - COLLISION_EPSILON:
            return distance
        gap = collider.max(axis) - moving.min(axis)
        if gap <= COLLISION_EPSILON:
            return max(distance, gap)
    return distance


def collide_with_boxes(movement: Vec3, bounding_box: AABB, boxes: list[AABB]) -> Vec3:
    if not boxes:
        return movement
    resolved = Vec3()
    for axis in axis_step_order(movement):
        distance = movement.get(axis)
        if distance == 0.0:
            continue
        moved_box = bounding_box.move(resolved)
        for collider in boxes:
            if abs(distance) < COLLISION_EPSILON:
                distance = 0.0
                break
            distance = clip_axis(moved_box, collider, axis, distance)
        resolved = resolved.with_axis(axis, distance)
    return resolved
