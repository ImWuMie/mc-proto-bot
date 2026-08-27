"""Source-faithful local physics for client-authoritative vanilla boats."""

from __future__ import annotations

import math
from typing import Protocol

from .engine import MovementInput
from .geometry import AABB, Vec3, collide_with_boxes
from .math import f32, minecraft_cos, minecraft_sin
from .world import CollisionWorld, fluid_height


class BoatEntityState(Protocol):
    x: float
    y: float
    z: float
    velocity_x: float
    velocity_y: float
    velocity_z: float
    yaw: float
    on_ground: bool
    horizontal_collision: bool
    vertical_collision: bool
    boat_status: str
    boat_old_status: str
    boat_water_level: float
    boat_land_friction: float
    boat_last_y_velocity: float
    boat_delta_rotation: float
    boat_left_paddle: bool
    boat_right_paddle: bool


class BoatPhysicsEngine:
    """Port of 26.2 ``AbstractBoat`` movement used by the local controller."""

    width = 1.375
    height = 0.5625
    gravity = 0.04

    def tick(
        self,
        state: BoatEntityState,
        controls: MovementInput,
        world: CollisionWorld,
    ) -> None:
        state.boat_old_status = state.boat_status
        state.boat_status = self._get_status(state, world)
        self._float_boat(state, world)
        self._control_boat(state, controls)
        self._move(state, world)

    def bounding_box(self, state: BoatEntityState) -> AABB:
        half_width = self.width / 2.0
        return AABB(
            state.x - half_width,
            state.y,
            state.z - half_width,
            state.x + half_width,
            state.y + self.height,
            state.z + half_width,
        )

    def _get_status(self, state: BoatEntityState, world: CollisionWorld) -> str:
        underwater = self._underwater_status(state, world)
        if underwater is not None:
            state.boat_water_level = self.bounding_box(state).max_y
            return underwater
        if self._check_in_water(state, world):
            return "in_water"
        friction = self._ground_friction(state, world)
        if friction > 0.0:
            state.boat_land_friction = friction
            return "on_land"
        return "in_air"

    def _underwater_status(
        self,
        state: BoatEntityState,
        world: CollisionWorld,
    ) -> str | None:
        box = self.bounding_box(state)
        max_y = box.max_y + 0.001
        under_source = False
        for x in range(math.floor(box.min_x), math.ceil(box.max_x)):
            for y in range(math.floor(box.max_y), math.ceil(max_y)):
                for z in range(math.floor(box.min_z), math.ceil(box.max_z)):
                    properties = world.block_properties(x, y, z)
                    if properties.fluid != "water":
                        continue
                    if max_y >= y + fluid_height(world, x, y, z):
                        continue
                    if properties.fluid_height >= f32(8.0 / 9.0):
                        under_source = True
                    else:
                        return "under_flowing_water"
        return "under_water" if under_source else None

    def _check_in_water(self, state: BoatEntityState, world: CollisionWorld) -> bool:
        box = self.bounding_box(state)
        state.boat_water_level = -float("inf")
        in_water = False
        for x in range(math.floor(box.min_x), math.ceil(box.max_x)):
            for y in range(math.floor(box.min_y), math.ceil(box.min_y + 0.001)):
                for z in range(math.floor(box.min_z), math.ceil(box.max_z)):
                    if world.block_properties(x, y, z).fluid != "water":
                        continue
                    surface = y + fluid_height(world, x, y, z)
                    state.boat_water_level = max(state.boat_water_level, surface)
                    in_water |= box.min_y < surface
        return in_water

    def _ground_friction(self, state: BoatEntityState, world: CollisionWorld) -> float:
        box = self.bounding_box(state)
        contact = AABB(
            box.min_x,
            box.min_y - 0.001,
            box.min_z,
            box.max_x,
            box.min_y,
            box.max_z,
        )
        x0, x1 = math.floor(contact.min_x) - 1, math.ceil(contact.max_x) + 1
        y0, y1 = math.floor(contact.min_y) - 1, math.ceil(contact.max_y) + 1
        z0, z1 = math.floor(contact.min_z) - 1, math.ceil(contact.max_z) + 1
        friction = f32(0.0)
        count = 0
        for x in range(x0, x1):
            for z in range(z0, z1):
                edges = int(x in (x0, x1 - 1)) + int(z in (z0, z1 - 1))
                if edges == 2:
                    continue
                for y in range(y0, y1):
                    if edges > 0 and y in (y0, y1 - 1):
                        continue
                    properties = world.block_properties(x, y, z)
                    if properties.boat_ignored_friction:
                        continue
                    if not any(
                        shape.intersects(contact)
                        for shape in world.block_collision_boxes(x, y, z)
                    ):
                        continue
                    friction = f32(friction + properties.friction)
                    count += 1
        return float("nan") if count == 0 else f32(friction / float(count))

    def _water_level_above(
        self,
        state: BoatEntityState,
        world: CollisionWorld,
    ) -> float:
        box = self.bounding_box(state)
        min_y = math.floor(box.max_y)
        max_y = math.ceil(box.max_y - state.boat_last_y_velocity)
        for y in range(min_y, max_y):
            block_height = f32(0.0)
            full = False
            for x in range(math.floor(box.min_x), math.ceil(box.max_x)):
                for z in range(math.floor(box.min_z), math.ceil(box.max_z)):
                    if world.block_properties(x, y, z).fluid == "water":
                        block_height = f32(
                            max(block_height, fluid_height(world, x, y, z))
                        )
                    if block_height >= 1.0:
                        full = True
                        break
                if full:
                    break
            if block_height < 1.0:
                return float(y) + block_height
        return float(max_y + 1)

    def _float_boat(self, state: BoatEntityState, world: CollisionWorld) -> None:
        vertical_speed = -self.gravity
        buoyancy = 0.0
        inverse_friction = f32(0.05)
        if (
            state.boat_old_status == "in_air"
            and state.boat_status not in ("in_air", "on_land")
        ):
            state.boat_water_level = state.y + self.height
            target_y = self._water_level_above(state, world) - self.height + 0.101
            moved = self.bounding_box(state).move(0.0, target_y - state.y, 0.0)
            if world.no_collision(moved):
                state.y = target_y
                state.velocity_y = 0.0
                state.boat_last_y_velocity = 0.0
            state.boat_status = "in_water"
            return

        if state.boat_status == "in_water":
            buoyancy = (state.boat_water_level - state.y) / self.height
            inverse_friction = f32(0.9)
        elif state.boat_status == "under_flowing_water":
            vertical_speed = -7.0e-4
            inverse_friction = f32(0.9)
        elif state.boat_status == "under_water":
            buoyancy = f32(0.01)
            inverse_friction = f32(0.45)
        elif state.boat_status == "in_air":
            inverse_friction = f32(0.9)
        elif state.boat_status == "on_land":
            inverse_friction = state.boat_land_friction
            state.boat_land_friction = f32(state.boat_land_friction / f32(2.0))

        state.velocity_x *= inverse_friction
        state.velocity_y += vertical_speed
        state.velocity_z *= inverse_friction
        state.boat_delta_rotation = f32(
            state.boat_delta_rotation * inverse_friction
        )
        if buoyancy > 0.0:
            state.velocity_y = (
                state.velocity_y + buoyancy * (self.gravity / 0.65)
            ) * 0.75

    def _control_boat(self, state: BoatEntityState, controls: MovementInput) -> None:
        input_left = controls.strafe < 0.0
        input_right = controls.strafe > 0.0
        input_up = controls.forward > 0.0
        input_down = controls.forward < 0.0
        acceleration = f32(0.0)
        if input_left:
            state.boat_delta_rotation = f32(state.boat_delta_rotation - f32(1.0))
        if input_right:
            state.boat_delta_rotation = f32(state.boat_delta_rotation + f32(1.0))
        if input_right != input_left and not input_up and not input_down:
            acceleration = f32(acceleration + f32(0.005))
        state.yaw = f32(state.yaw + state.boat_delta_rotation)
        if input_up:
            acceleration = f32(acceleration + f32(0.04))
        if input_down:
            acceleration = f32(acceleration - f32(0.005))
        radians = f32(f32(-state.yaw) * f32(f32(math.pi) / f32(180.0)))
        state.velocity_x += float(f32(minecraft_sin(radians) * acceleration))
        state.velocity_z += float(f32(minecraft_cos(radians) * acceleration))
        state.boat_left_paddle = (input_right and not input_left) or input_up
        state.boat_right_paddle = (input_left and not input_right) or input_up

    def _move(self, state: BoatEntityState, world: CollisionWorld) -> None:
        requested = Vec3(state.velocity_x, state.velocity_y, state.velocity_z)
        box = self.bounding_box(state)
        query = box.expand_towards(requested)
        collisions = world.collision_boxes(query)
        collisions.extend(world.entity_collision_boxes(query))
        resolved = collide_with_boxes(requested, box, collisions)
        state.x += resolved.x
        state.y += resolved.y
        state.z += resolved.z
        x_collision = abs(requested.x - resolved.x) >= 1.0e-5
        z_collision = abs(requested.z - resolved.z) >= 1.0e-5
        state.horizontal_collision = x_collision or z_collision
        state.vertical_collision = requested.y != resolved.y
        state.on_ground = state.vertical_collision and requested.y < 0.0
        state.boat_last_y_velocity = state.velocity_y

        if x_collision:
            state.velocity_x = 0.0
        if z_collision:
            state.velocity_z = 0.0
        if state.vertical_collision:
            state.velocity_y = self._vertical_restitution(
                state,
                world,
                requested.y,
                resolved.y,
            )
        speed_factor = self._block_speed_factor(state, world)
        state.velocity_x *= speed_factor
        state.velocity_z *= speed_factor

    def _vertical_restitution(
        self,
        state: BoatEntityState,
        world: CollisionWorld,
        requested_y: float,
        resolved_y: float,
    ) -> float:
        if not state.on_ground:
            return 0.0
        effect = world.block_properties(
            math.floor(state.x),
            math.floor(state.y - 0.2),
            math.floor(state.z),
        )
        restitution = f32(effect.bounce_restitution * f32(0.8))
        if (
            -requested_y < self.gravity
            or effect.suppress_bounce
            or restitution <= 0.0
        ):
            return 0.0
        portion = resolved_y / requested_y
        gravity_compensation = portion * self.gravity
        return (gravity_compensation - requested_y) * restitution

    @staticmethod
    def _block_speed_factor(
        state: BoatEntityState,
        world: CollisionWorld,
    ) -> float:
        x, y, z = math.floor(state.x), math.floor(state.y), math.floor(state.z)
        here = world.block_properties(x, y, z)
        if here.speed_factor != 1.0:
            return here.speed_factor
        return world.block_properties(x, math.floor(state.y - 0.500001), z).speed_factor
