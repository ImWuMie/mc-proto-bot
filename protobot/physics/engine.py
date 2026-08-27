"""Python port of the vanilla 26.2 player travel and entity collision path."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import AABB, Vec3, collide_with_boxes
from .math import (
    f32,
    look_vector_components,
    look_y_from_pitch,
    minecraft_sin,
    modified_friction,
    yaw_sin_cos,
)
from .world import BlockProperties, CollisionWorld, fluid_flow, fluid_height


@dataclass(frozen=True, slots=True)
class MovementInput:
    forward: float = 0.0
    strafe: float = 0.0
    jump: bool = False
    sneak: bool = False
    sprint: bool = False
    crawl: bool = False

    def __post_init__(self) -> None:
        if not -1.0 <= self.forward <= 1.0 or not -1.0 <= self.strafe <= 1.0:
            raise ValueError("forward and strafe inputs must be between -1 and 1")


@dataclass(frozen=True, slots=True)
class PhysicsAttributes:
    # These are the vanilla player defaults in 26.2.  Keep the values as
    # single-precision numbers where the game stores them as ``float``;
    # calculations below still use Python doubles just like the JVM does for
    # entity positions/velocities.
    movement_speed: float = f32(0.1)
    jump_strength: float = f32(0.42)
    # GRAVITY is a double-valued attribute in vanilla (unlike jump strength),
    # so retain the exact 0.08 constant rather than rounding it to float32.
    gravity: float = 0.08
    step_height: float = f32(0.6)
    air_drag_modifier: float = f32(1.0)
    friction_modifier: float = f32(1.0)
    water_movement_efficiency: float = f32(0.0)
    movement_efficiency: float = f32(0.0)
    sneaking_speed: float = f32(0.3)
    fluid_jump_threshold: float = f32(0.4)
    lava_flow_scale: float = 0.0023333333333333335
    bounciness: float = 0.0


@dataclass(frozen=True, slots=True)
class StatusEffect:
    """Client-visible status effect data used by movement prediction."""

    amplifier: int = 0
    duration: int = -1
    ambient: bool = False
    show_particles: bool = True
    show_icon: bool = True
    keep_fading: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.amplifier <= 255:
            raise ValueError("status-effect amplifier must be between 0 and 255")
        if self.duration < -1:
            raise ValueError("status-effect duration must be -1 or non-negative")


@dataclass(slots=True)
class PhysicsState:
    position: Vec3 = field(default_factory=Vec3)
    velocity: Vec3 = field(default_factory=Vec3)
    yaw: float = 0.0
    pitch: float = 0.0
    width: float = f32(0.6)
    height: float = f32(1.8)
    pose: str = "standing"
    on_ground: bool = False
    horizontal_collision: bool = False
    vertical_collision: bool = False
    crouching: bool = False
    sprinting: bool = False
    swimming: bool = False
    in_water: bool = False
    in_lava: bool = False
    on_climbable: bool = False
    fall_distance: float = 0.0
    # ``crouching`` describes the actual pose, not merely the shift key.  The
    # latter is retained separately as ``sneaking`` because vanilla uses the
    # key state for edge protection even while the player is forced into the
    # low (visually swimming/crawling) pose by a ceiling.
    sneaking: bool = False
    water_height: float = 0.0
    lava_height: float = 0.0
    water_flow: Vec3 = field(default_factory=Vec3)
    lava_flow: Vec3 = field(default_factory=Vec3)
    eye_in_water: bool = False
    eye_in_lava: bool = False
    on_scaffolding: bool = False
    in_powder_snow: bool = False
    was_in_powder_snow: bool = False
    wearing_leather_boots: bool = False
    stuck_speed_multiplier: Vec3 = field(default_factory=Vec3)
    jump_cooldown: int = 0
    allow_flying: bool = False
    flying: bool = False
    fly_speed: float = f32(0.05)
    spectator: bool = False
    gliding: bool = False
    gliding_ticks: int = 0
    gliding_collision_damage: float = 0.0
    status_effects: dict[str, StatusEffect] = field(default_factory=dict)
    attribute_values: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def _identifier(value: str) -> str:
        if not value:
            raise ValueError("identifier cannot be empty")
        return value if ":" in value else f"minecraft:{value}"

    def set_status_effect(
        self,
        identifier: str,
        *,
        amplifier: int = 0,
        duration: int = -1,
        ambient: bool = False,
        show_particles: bool = True,
        show_icon: bool = True,
        keep_fading: bool = False,
    ) -> StatusEffect:
        effect = StatusEffect(
            amplifier=amplifier,
            duration=duration,
            ambient=ambient,
            show_particles=show_particles,
            show_icon=show_icon,
            keep_fading=keep_fading,
        )
        self.status_effects[self._identifier(identifier)] = effect
        return effect

    def remove_status_effect(self, identifier: str) -> StatusEffect | None:
        return self.status_effects.pop(self._identifier(identifier), None)

    def status_effect(self, identifier: str) -> StatusEffect | None:
        return self.status_effects.get(self._identifier(identifier))

    def set_attribute(self, identifier: str, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("attribute value must be finite")
        self.attribute_values[self._identifier(identifier)] = float(value)

    def clear_attribute(self, identifier: str) -> None:
        self.attribute_values.pop(self._identifier(identifier), None)

    @property
    def bounding_box(self) -> AABB:
        half_width = self.width / 2.0
        return AABB(
            self.position.x - half_width,
            self.position.y,
            self.position.z - half_width,
            self.position.x + half_width,
            self.position.y + self.height,
            self.position.z + half_width,
        )

    @property
    def eye_height(self) -> float:
        """Eye height for the current vanilla player pose."""

        if self.pose in ("swimming", "crawling", "gliding"):
            # Player dimensions use a 0.4 eye offset for the swimming pose.
            return f32(0.4)
        if self.pose == "crouching":
            return f32(1.27)
        return f32(1.62)


class PhysicsEngine:
    """Advance vanilla-like player state by one 20 Hz game tick.

    The implemented path covers walking, sprinting, sneaking at edges, gravity,
    jumping, full-box/arbitrary-box collision, stepping, water/lava travel, and
    climbable velocity constraints. Chunk decoding and pose negotiation live above
    this deterministic core.
    """

    def __init__(self, attributes: PhysicsAttributes | None = None) -> None:
        self.attributes = attributes or PhysicsAttributes()

    def tick(
        self,
        state: PhysicsState,
        controls: MovementInput,
        world: CollisionWorld,
    ) -> PhysicsState:
        # ``LivingEntity.aiStep`` first discards tiny residual movement, then
        # applies the packet's key state and updates fluid/swimming flags before
        # deciding the pose.  Doing this in the same order matters at water
        # surfaces: a player whose feet are wet but whose eyes are above water
        # must not enter the swimming pose merely because sprint is held.
        self._zero_small_velocity(state)
        state.gliding_collision_damage = 0.0
        state.was_in_powder_snow = state.in_powder_snow
        state.in_powder_snow = False
        if state.jump_cooldown > 0:
            state.jump_cooldown -= 1
        state.sneaking = controls.sneak
        state.sprinting = controls.sprint and not (controls.sneak or controls.crawl)
        self._update_environment(state, world, apply_currents=True)
        if state.flying:
            # Player.isAffectedByFluids and Player.isSwimming both return false
            # while abilities.flying is set. Player.aiStep also resets fall
            # distance before delegating to LivingEntity.
            state.swimming = False
            state.gliding = False
            state.fall_distance = 0.0
        else:
            self._update_swimming(state, controls, world)

        if state.gliding and (
            state.on_ground or self._has_effect(state, "levitation")
        ):
            # LivingEntity.tickGliding clears the flag when canGlide fails.
            state.gliding = False
        if state.gliding and state.velocity.y > -0.5 and state.fall_distance > 1.0:
            state.fall_distance = 1.0

        # ClientPlayerEntity applies vertical creative/spectator input before
        # LivingEntity's jump and travel phases.
        if state.flying:
            vertical_input = int(controls.jump) - int(controls.sneak)
            if vertical_input:
                state.velocity = state.velocity + Vec3(
                    0.0,
                    vertical_input * f32(state.fly_speed) * f32(3.0),
                    0.0,
                )

        # ClientPlayer.tickMovement applies the shift-key water impulse before
        # LivingEntity handles jumping. Holding jump and sneak therefore
        # cancels the two +/-0.04 impulses instead of selecting one of them.
        if state.in_water and state.sneaking and not state.flying:
            state.velocity = state.velocity + Vec3(0.0, -f32(0.04), 0.0)

        if controls.jump and not state.flying:
            self._handle_jump(state, world)
        else:
            # Releasing jump clears LivingEntity.noJumpDelay immediately.
            state.jump_cooldown = 0

        movement_input = self._player_movement_input(state, controls)

        # LivingEntity resets fall distance for both effects immediately before
        # travel. This is observable even when levitation is moving downward.
        if self._has_effect(state, "slow_falling") or self._has_effect(state, "levitation"):
            state.fall_distance = 0.0

        if state.swimming:
            look_y = look_y_from_pitch(state.pitch)
            multiplier = 0.085 if look_y < -0.2 else 0.06
            # Player.travel applies the vertical look correction while looking
            # down, while jumping, or when the block around y+0.9 is fluid.  It
            # intentionally does not require the eye itself to be submerged.
            if look_y <= 0.0 or controls.jump or self._head_in_fluid(state, world):
                velocity = state.velocity
                state.velocity = velocity + Vec3(
                    0.0,
                    (look_y - velocity.y) * multiplier,
                    0.0,
                )

        travel_start = state.position
        if state.flying:
            self._travel_flying(state, movement_input, world, controls.jump)
        elif state.in_water:
            self._travel_in_water(state, movement_input, world)
        elif state.in_lava:
            self._travel_in_lava(state, movement_input, world)
        elif state.gliding:
            self._travel_gliding(state, movement_input, world, controls.jump)
        else:
            self._travel_in_air(state, movement_input, world, controls.jump)

        self._apply_bubble_column_effects(state, world, travel_start)
        self._apply_inside_block_effects(state, world, travel_start)

        if state.on_ground and state.flying and not state.spectator:
            # LocalPlayer turns ordinary creative flight off after landing;
            # spectator flight is locked and remains enabled.
            state.flying = False
        state.gliding_ticks = state.gliding_ticks + 1 if state.gliding else 0
        self._update_environment(state, world)
        # Player.updatePlayerPose runs after LivingEntity.aiStep/travel.  This
        # one-tick ordering affects collision dimensions when swimming starts
        # or ends and is observable to movement checks at tight openings.
        self._refresh_pose(state, controls, world)
        return state

    @staticmethod
    def _zero_small_velocity(state: PhysicsState) -> None:
        """Mirror LivingEntity.aiStep's packet-noise suppression.

        For players Mojang snaps horizontal movement below ``sqrt(9e-6)`` and
        vertical movement below ``0.003`` to zero before applying input.  It is
        not just cosmetic: without this, a grounded bot can retain a tiny
        horizontal drift forever and repeatedly fail edge/collision checks.
        """

        velocity = state.velocity
        horizontal_x = velocity.x
        horizontal_z = velocity.z
        if velocity.horizontal_length_squared < 9.0e-6:
            horizontal_x = 0.0
            horizontal_z = 0.0
        vertical = 0.0 if abs(velocity.y) < 0.003 else velocity.y
        state.velocity = Vec3(horizontal_x, vertical, horizontal_z)

    def _player_movement_input(
        self,
        state: PhysicsState,
        controls: MovementInput,
    ) -> Vec3:
        """Port ClientPlayerEntity.applyMovementSpeedFactors.

        ``MovementInput`` represents the raw key/analog state. Vanilla first
        applies its fixed 0.98 factor, then pose slowdown, and finally restores
        full diagonal reach without letting the vector exceed unit length.
        """

        strafe = f32(f32(controls.strafe) * f32(0.98))
        forward = f32(f32(controls.forward) * f32(0.98))
        if (
            controls.crawl
            or (state.sneaking and not state.flying)
            or state.pose in ("crouching", "crawling")
        ):
            slowdown = f32(self._attribute(state, "sneaking_speed"))
            strafe = f32(strafe * slowdown)
            forward = f32(forward * slowdown)

        length_squared = f32(f32(strafe * strafe) + f32(forward * forward))
        length = f32(math.sqrt(length_squared))
        if length <= 0.0:
            return Vec3()
        reciprocal = f32(f32(1.0) / length)
        normalized_strafe = f32(strafe * reciprocal)
        normalized_forward = f32(forward * reciprocal)
        absolute_strafe = abs(normalized_strafe)
        absolute_forward = abs(normalized_forward)
        high = max(absolute_strafe, absolute_forward)
        low = min(absolute_strafe, absolute_forward)
        ratio = f32(low / high)
        directional = f32(math.sqrt(f32(f32(1.0) + f32(ratio * ratio))))
        magnitude = min(f32(length * directional), f32(1.0))
        return Vec3(
            f32(normalized_strafe * magnitude),
            0.0,
            f32(normalized_forward * magnitude),
        )

    def _attribute(self, state: PhysicsState, name: str) -> float:
        identifier = name if ":" in name else f"minecraft:{name}"
        return state.attribute_values.get(identifier, getattr(self.attributes, name))

    @staticmethod
    def _effect(state: PhysicsState, name: str) -> StatusEffect | None:
        identifier = name if ":" in name else f"minecraft:{name}"
        return state.status_effects.get(identifier)

    @staticmethod
    def _has_effect(state: PhysicsState, name: str) -> bool:
        return PhysicsEngine._effect(state, name) is not None

    @staticmethod
    def _effect_amplifier(state: PhysicsState, name: str) -> int | None:
        effect = PhysicsEngine._effect(state, name)
        return None if effect is None else effect.amplifier

    def _handle_jump(self, state: PhysicsState, world: CollisionWorld) -> None:
        """Apply the same fluid/ground jump priority as LivingEntity.aiStep."""

        fluid_jump_threshold = self._fluid_jump_threshold(state)
        water_shallow = state.in_water and state.water_height <= fluid_jump_threshold
        if state.in_water and (not state.on_ground or state.water_height > fluid_jump_threshold):
            state.velocity = state.velocity + Vec3(0.0, f32(0.04), 0.0)
            return
        if state.in_lava and not (state.on_ground and state.lava_height <= fluid_jump_threshold):
            state.velocity = state.velocity + Vec3(0.0, f32(0.04), 0.0)
            return
        if (state.on_ground or water_shallow) and state.jump_cooldown == 0:
            self._jump_from_ground(state, world)
            # Vanilla's noJumpDelay prevents a held jump key from repeatedly
            # launching the player when a fluid edge or slab toggles onGround.
            state.jump_cooldown = 10

    def _fluid_jump_threshold(self, state: PhysicsState) -> float:
        # Entity.getFluidJumpThreshold returns zero only for unusually tiny
        # entities; the player swimming eye height is exactly 0.4 and therefore
        # retains the normal 0.4 threshold.
        return 0.0 if state.eye_height < 0.4 else f32(self.attributes.fluid_jump_threshold)

    def _effective_gravity(self, state: PhysicsState) -> float:
        gravity = self._attribute(state, "gravity")
        if state.velocity.y <= 0.0 and self._has_effect(state, "slow_falling"):
            return min(gravity, 0.01)
        return gravity

    def _travel_in_air(
        self,
        state: PhysicsState,
        movement_input: Vec3,
        world: CollisionWorld,
        jumping: bool,
    ) -> None:
        below = self._block_below(state)
        block = world.block_properties(*below)
        friction = (
            modified_friction(block.friction, self._attribute(state, "friction_modifier"))
            if state.on_ground
            else f32(1.0)
        )
        speed = self._friction_influenced_speed(state, friction)
        state.velocity = state.velocity + self._input_vector(movement_input, speed, state.yaw)
        state.velocity = self._handle_climbable(state, state.velocity)
        self._move(state, state.velocity, world)
        movement = state.velocity
        if (state.horizontal_collision or jumping) and (
            state.on_climbable
            or (state.was_in_powder_snow and state.wearing_leather_boots)
        ):
            movement = Vec3(movement.x, 0.2, movement.z)

        movement_y = movement.y
        levitation = self._effect_amplifier(state, "levitation")
        if levitation is None:
            movement_y -= self._effective_gravity(state)
        else:
            movement_y += (0.05 * (levitation + 1) - movement.y) * 0.2
        air_drag_modifier = self._attribute(state, "air_drag_modifier")
        air_drag = modified_friction(f32(0.91), air_drag_modifier)
        horizontal_friction = f32(friction * air_drag)
        vertical_friction = modified_friction(f32(0.98), air_drag_modifier)
        state.velocity = Vec3(
            movement.x * horizontal_friction,
            movement_y * vertical_friction,
            movement.z * horizontal_friction,
        )

    def _travel_flying(
        self,
        state: PhysicsState,
        movement_input: Vec3,
        world: CollisionWorld,
        jumping: bool,
    ) -> None:
        """Port Player.travel's abilities.flying wrapper around air travel."""

        original_vertical_velocity = state.velocity.y
        # Player.isAffectedByFluids returns false while flying, so super.travel
        # always selects LivingEntity.travelInAir even inside water or lava.
        self._travel_in_air(state, movement_input, world, jumping)
        movement = state.velocity
        state.velocity = Vec3(
            movement.x,
            original_vertical_velocity * 0.6,
            movement.z,
        )

    def _travel_gliding(
        self,
        state: PhysicsState,
        movement_input: Vec3,
        world: CollisionWorld,
        jumping: bool,
    ) -> None:
        """Port LivingEntity.travelGliding and its wall-impact calculation."""

        if state.on_climbable:
            self._travel_in_air(state, movement_input, world, jumping)
            state.gliding = False
            return
        old_horizontal_speed = math.sqrt(state.velocity.horizontal_length_squared)
        state.velocity = self._calculate_gliding_velocity(state)
        self._move(state, state.velocity, world)
        new_horizontal_speed = math.sqrt(state.velocity.horizontal_length_squared)
        damage = f32((old_horizontal_speed - new_horizontal_speed) * 10.0 - 3.0)
        if state.horizontal_collision and damage > 0.0:
            state.gliding_collision_damage = damage

    def _calculate_gliding_velocity(self, state: PhysicsState) -> Vec3:
        movement = state.velocity
        look = Vec3(*look_vector_components(state.yaw, state.pitch))
        look_horizontal = math.sqrt(look.x * look.x + look.z * look.z)
        movement_horizontal = math.sqrt(movement.horizontal_length_squared)
        gravity = self._effective_gravity(state)
        pitch_radians = f32(f32(state.pitch) * f32(f32(math.pi) / 180.0))
        lift = math.cos(pitch_radians) ** 2
        movement = movement + Vec3(0.0, gravity * (-1.0 + lift * 0.75), 0.0)
        if movement.y < 0.0 and look_horizontal > 0.0:
            converted = movement.y * -0.1 * lift
            movement = movement + Vec3(
                look.x * converted / look_horizontal,
                converted,
                look.z * converted / look_horizontal,
            )
        if pitch_radians < 0.0 and look_horizontal > 0.0:
            converted = movement_horizontal * -minecraft_sin(pitch_radians) * 0.04
            movement = movement + Vec3(
                -look.x * converted / look_horizontal,
                converted * 3.2,
                -look.z * converted / look_horizontal,
            )
        if look_horizontal > 0.0:
            movement = movement + Vec3(
                (look.x / look_horizontal * movement_horizontal - movement.x) * 0.1,
                0.0,
                (look.z / look_horizontal * movement_horizontal - movement.z) * 0.1,
            )
        return movement.multiply(f32(0.99), f32(0.98), f32(0.99))

    def _travel_in_water(
        self,
        state: PhysicsState,
        movement_input: Vec3,
        world: CollisionWorld,
    ) -> None:
        falling = state.velocity.y <= 0.0
        gravity = self._effective_gravity(state)
        old_y = state.position.y
        slowdown = f32(0.9) if state.sprinting else f32(0.8)
        speed = f32(0.02)
        efficiency = f32(self._attribute(state, "water_movement_efficiency"))
        if not state.on_ground:
            efficiency = f32(efficiency * f32(0.5))
        if efficiency > 0.0:
            slowdown = f32(slowdown + f32((f32(0.54600006) - slowdown) * efficiency))
            entity_speed = self._movement_speed(state)
            speed = f32(speed + f32((entity_speed - speed) * efficiency))
        if self._has_effect(state, "dolphins_grace"):
            slowdown = f32(0.96)
        state.velocity = state.velocity + self._input_vector(movement_input, speed, state.yaw)
        self._move(state, state.velocity, world)
        movement = state.velocity
        if state.horizontal_collision and state.on_climbable:
            movement = Vec3(movement.x, 0.2, movement.z)
        movement = movement.multiply(slowdown, f32(0.8), slowdown)
        state.velocity = self._fluid_falling_adjusted(state, gravity, falling, movement)
        self._jump_out_of_fluid(state, old_y, world)

    def _travel_in_lava(
        self,
        state: PhysicsState,
        movement_input: Vec3,
        world: CollisionWorld,
    ) -> None:
        falling = state.velocity.y <= 0.0
        gravity = self._effective_gravity(state)
        old_y = state.position.y
        state.velocity = state.velocity + self._input_vector(movement_input, f32(0.02), state.yaw)
        self._move(state, state.velocity, world)
        # In vanilla shallow lava follows the water-style vertical drag and
        # gravity compensation; only deep lava uses the all-axis 0.5 scale.
        if state.lava_height <= self._fluid_jump_threshold(state):
            movement = state.velocity.multiply(0.5, f32(0.8), 0.5)
            state.velocity = self._fluid_falling_adjusted(state, gravity, falling, movement)
        else:
            state.velocity = state.velocity.scale(0.5)
        if gravity != 0.0:
            state.velocity = state.velocity + Vec3(0.0, -gravity / 4.0, 0.0)
        self._jump_out_of_fluid(state, old_y, world)

    def _move(self, state: PhysicsState, requested: Vec3, world: CollisionWorld) -> Vec3:
        if state.spectator:
            state.position = state.position + requested
            state.horizontal_collision = False
            state.vertical_collision = False
            state.on_ground = False
            return requested
        if state.stuck_speed_multiplier.length_squared > 1.0e-7:
            requested = requested.multiply(
                state.stuck_speed_multiplier.x,
                state.stuck_speed_multiplier.y,
                state.stuck_speed_multiplier.z,
            )
            state.stuck_speed_multiplier = Vec3()
            state.velocity = Vec3()
        requested = self._maybe_back_off_from_edge(state, requested, world)
        bounding_box = state.bounding_box
        boxes = self._entity_collision_boxes(
            state,
            world,
            bounding_box.expand_towards(requested),
        )
        movement = collide_with_boxes(requested, bounding_box, boxes)
        step_x_collision = requested.x != movement.x
        y_collision = requested.y != movement.y
        step_z_collision = requested.z != movement.z
        # Entity.move only refreshes onGround when there was vertical
        # movement.  A horizontal-only tick on a slab therefore keeps the
        # previous onGround flag; unconditionally clearing it here causes a
        # one-tick airborne state and changes friction/jump behavior.
        on_ground_after_collision = (
            y_collision and requested.y < 0.0 if requested.y != 0.0 else state.on_ground
        )

        if (
            self._attribute(state, "step_height") > 0.0
            and (on_ground_after_collision or state.on_ground)
            and (step_x_collision or step_z_collision)
        ):
            movement = self._try_step(
                state,
                requested,
                movement,
                bounding_box,
                boxes,
                on_ground_after_collision,
                world,
            )
            y_collision = requested.y != movement.y
            on_ground_after_collision = (
                y_collision and requested.y < 0.0 if requested.y != 0.0 else state.on_ground
            )

        # Entity.collide uses exact comparison when deciding whether to try a
        # step, but Entity.move uses Mth.equal (epsilon 1e-5) for the published
        # horizontalCollision flag and restitution path.
        x_collision = abs(requested.x - movement.x) >= 1.0e-5
        z_collision = abs(requested.z - movement.z) >= 1.0e-5
        state.position = state.position + movement
        state.horizontal_collision = x_collision or z_collision
        if requested.y != 0.0:
            state.vertical_collision = y_collision
        state.on_ground = on_ground_after_collision
        current_velocity = state.velocity
        state.velocity = self._restitute_after_collision(
            current_velocity,
            movement,
            x_collision,
            y_collision,
            z_collision,
            requested,
            state,
            world,
        )

        effect_block = world.block_properties(
            math.floor(state.position.x),
            math.floor(state.position.y - f32(0.2)),
            math.floor(state.position.z),
        )
        if (
            effect_block.slime_block
            and abs(state.velocity.y) < 0.1
            and not state.sneaking
        ):
            horizontal_scale = 0.4 + abs(state.velocity.y) * 0.2
            state.velocity = state.velocity.multiply(
                horizontal_scale,
                1.0,
                horizontal_scale,
            )

        if state.flying or state.gliding:
            speed_factor = f32(1.0)
        else:
            block = self._block_speed_properties(state, world)
            efficiency = f32(self._attribute(state, "movement_efficiency"))
            speed_factor = f32(
                f32(block.speed_factor)
                + f32(efficiency * f32(f32(1.0) - f32(block.speed_factor)))
            )
        state.velocity = state.velocity.multiply(speed_factor, 1.0, speed_factor)
        if state.on_ground:
            state.fall_distance = 0.0
        elif movement.y < 0.0:
            state.fall_distance -= movement.y
        return movement

    def _restitute_after_collision(
        self,
        current_velocity: Vec3,
        movement: Vec3,
        x_collision: bool,
        y_collision: bool,
        z_collision: bool,
        requested: Vec3,
        state: PhysicsState,
        world: CollisionWorld,
    ) -> Vec3:
        """Port Entity.restituteMovementAfterCollisions.

        Players default to zero bounciness, so ordinary walls/floors retain
        the familiar zeroed component.  Exposing the attribute and block
        restitution keeps slime/bed/custom entity trajectories faithful too.
        """

        restitution = 0.0 if state.sneaking else f32(self._attribute(state, "bounciness"))
        result = current_velocity
        if x_collision:
            result = result.with_axis("x", -current_velocity.x * restitution)
        if z_collision:
            result = result.with_axis("z", -current_velocity.z * restitution)
        if not y_collision:
            return result

        if requested.y < 0.0:
            block = self._block_speed_properties(state, world)
            gravity = self._effective_gravity(state)
            if (
                -current_velocity.y < gravity
                or state.sneaking
                or block.suppress_bounce
            ):
                restitution = 0.0
            else:
                restitution = max(restitution, f32(block.bounce_restitution))
        if restitution <= 0.0 or current_velocity.y == 0.0:
            return result.with_axis("y", 0.0)

        portion = movement.y / current_velocity.y
        air_drag = modified_friction(
            f32(0.98), self._attribute(state, "air_drag_modifier")
        )
        effective_drag = 1.0 + portion * (air_drag - 1.0)
        gravity_compensation = portion * self._effective_gravity(state)
        new_y = (gravity_compensation - current_velocity.y) * effective_drag * restitution
        return result.with_axis("y", new_y)

    def _try_step(
        self,
        state: PhysicsState,
        requested: Vec3,
        clipped: Vec3,
        bounding_box: AABB,
        existing_boxes: list[AABB],
        collided_down: bool,
        world: CollisionWorld,
    ) -> Vec3:
        grounded = bounding_box.move(0.0, clipped.y, 0.0) if collided_down else bounding_box
        step_height = self._attribute(state, "step_height")
        query = grounded.expand_towards(Vec3(requested.x, step_height, requested.z))
        if not collided_down:
            query = query.expand_towards(Vec3(0.0, -1.0e-5, 0.0))
        boxes = existing_boxes + [
            box
            for box in self._entity_collision_boxes(state, world, query)
            if box not in existing_boxes
        ]
        skipped_height = f32(clipped.y)
        candidates: set[float] = set()
        for box in boxes:
            for coordinate in box.y_coordinates:
                relative = f32(coordinate - grounded.min_y)
                if relative < 0.0 or relative == skipped_height:
                    continue
                if relative > step_height:
                    break
                candidates.add(relative)
        for height in sorted(candidates):
            candidate = collide_with_boxes(Vec3(requested.x, height, requested.z), grounded, boxes)
            if candidate.horizontal_length_squared > clipped.horizontal_length_squared:
                distance_to_ground = bounding_box.min_y - grounded.min_y
                return candidate - Vec3(0.0, distance_to_ground, 0.0)
        return clipped

    def _maybe_back_off_from_edge(
        self,
        state: PhysicsState,
        movement: Vec3,
        world: CollisionWorld,
    ) -> Vec3:
        # Player.maybeBackOffFromEdge also protects a player who is already
        # falling a short distance (for example after walking off a slab), as
        # long as there is still ground within the remaining step height.
        if (
            state.flying
            or not (state.sneaking or state.crouching)
            or movement.y > 0.0
            or not self._is_above_ground(state, world)
        ):
            return movement
        max_down = self._attribute(state, "step_height")
        x, z = movement.x, movement.z
        step_x = math.copysign(0.05, x) if x else 0.0
        step_z = math.copysign(0.05, z) if z else 0.0
        while x != 0.0 and self._can_fall(state, world, x, 0.0, max_down):
            x = 0.0 if abs(x) <= 0.05 else x - step_x
        while z != 0.0 and self._can_fall(state, world, 0.0, z, max_down):
            z = 0.0 if abs(z) <= 0.05 else z - step_z
        while x != 0.0 and z != 0.0 and self._can_fall(state, world, x, z, max_down):
            x = 0.0 if abs(x) <= 0.05 else x - step_x
            z = 0.0 if abs(z) <= 0.05 else z - step_z
        return Vec3(x, movement.y, z)

    def _is_above_ground(self, state: PhysicsState, world: CollisionWorld) -> bool:
        max_down = self._attribute(state, "step_height")
        if state.on_ground:
            return True
        if state.fall_distance >= max_down:
            return False
        return not self._can_fall(
            state,
            world,
            0.0,
            0.0,
            max_down - state.fall_distance,
        )

    @staticmethod
    def _can_fall(
        state: PhysicsState,
        world: CollisionWorld,
        delta_x: float,
        delta_z: float,
        height: float,
    ) -> bool:
        box = state.bounding_box
        query = AABB(
            box.min_x + 1.0e-7 + delta_x,
            box.min_y - height - 1.0e-7,
            box.min_z + 1.0e-7 + delta_z,
            box.max_x - 1.0e-7 + delta_x,
            box.min_y,
            box.max_z - 1.0e-7 + delta_z,
        )
        return PhysicsEngine._no_entity_collision(state, world, query)

    @staticmethod
    def _entity_collision_boxes(
        state: PhysicsState,
        world: CollisionWorld,
        query: AABB,
    ) -> list[AABB]:
        """Return block collision shapes using the player's collision context.

        Powder snow is the only vanilla player-relevant block whose shape is
        dynamic in the supported state tables. Its first branch deliberately
        ignores the player's position: once fall distance exceeds 2.5 blocks,
        Minecraft exposes a 0.9-high shape. Leather boots expose the normal
        full-block shape only while the player's feet are above the block and
        the shift key is not held.
        """

        boxes = world.collision_boxes(query)
        for box in world.entity_collision_boxes(query):
            if box not in boxes:
                boxes.append(box)
        for x in range(math.floor(query.min_x), math.floor(query.max_x) + 1):
            for y in range(math.floor(query.min_y), math.floor(query.max_y) + 1):
                for z in range(math.floor(query.min_z), math.floor(query.max_z) + 1):
                    if not world.block_properties(x, y, z).powder_snow:
                        continue
                    if state.fall_distance > 2.5:
                        height = f32(0.9)
                    elif (
                        state.wearing_leather_boots
                        and not state.sneaking
                        and state.position.y > y + 1.0 - f32(1.0e-5)
                    ):
                        height = 1.0
                    else:
                        continue
                    shape = AABB(x, y, z, x + 1.0, y + height, z + 1.0)
                    if shape.intersects(query) and shape not in boxes:
                        boxes.append(shape)
        return boxes

    @staticmethod
    def _no_entity_collision(
        state: PhysicsState,
        world: CollisionWorld,
        query: AABB,
    ) -> bool:
        return not PhysicsEngine._entity_collision_boxes(state, world, query)

    def _jump_from_ground(self, state: PhysicsState, world: CollisionWorld) -> None:
        jump_factor = self._block_jump_factor(state, world)
        jump_power = f32(f32(self._attribute(state, "jump_strength")) * f32(jump_factor))
        jump_boost = self._effect_amplifier(state, "jump_boost")
        if jump_boost is not None:
            boost = f32(f32(0.1) * f32(float(jump_boost) + f32(1.0)))
            jump_power = f32(jump_power + boost)
        if jump_power <= 1.0e-5:
            return
        state.velocity = Vec3(
            state.velocity.x,
            max(jump_power, state.velocity.y),
            state.velocity.z,
        )
        if state.sprinting:
            sin_yaw, cos_yaw = yaw_sin_cos(state.yaw)
            state.velocity = state.velocity + Vec3(-sin_yaw * 0.2, 0.0, cos_yaw * 0.2)

    def _friction_influenced_speed(self, state: PhysicsState, friction: float) -> float:
        if not state.on_ground:
            if state.flying:
                multiplier = f32(2.0) if state.sprinting else f32(1.0)
                return f32(f32(state.fly_speed) * multiplier)
            return f32(0.025999999) if state.sprinting else f32(0.02)
        speed = self._movement_speed(state)
        if friction > 0.6:
            squared = f32(friction * friction)
            cubed = f32(squared * friction)
            factor = f32(f32(0.21600002) / cubed)
            return f32(speed * factor)
        return speed

    def _movement_speed(self, state: PhysicsState) -> float:
        speed = f32(self._attribute(state, "movement_speed"))
        if state.sprinting:
            speed = f32(speed * (1.0 + f32(0.3)))
        return speed

    @staticmethod
    def _block_jump_factor(state: PhysicsState, world: CollisionWorld) -> float:
        """Return vanilla ``getBlockJumpFactor`` for the player's support."""

        here = world.block_properties(
            math.floor(state.position.x),
            math.floor(state.position.y),
            math.floor(state.position.z),
        )
        if here.jump_factor != 1.0:
            return f32(here.jump_factor)
        below = world.block_properties(*PhysicsEngine._block_below(state))
        return f32(below.jump_factor)

    @staticmethod
    def _block_speed_properties(state: PhysicsState, world: CollisionWorld) -> BlockProperties:
        """Return vanilla ``getBlockSpeedFactor``'s effective block.

        Water and bubble columns use the block at the player's feet; for other
        blocks a neutral factor delegates to the supporting block below.
        """

        here = world.block_properties(
            math.floor(state.position.x),
            math.floor(state.position.y),
            math.floor(state.position.z),
        )
        # Vanilla special-cases water/bubble-column blocks only; lava still
        # delegates a neutral speed factor to the supporting block.
        if here.fluid == "water" or here.speed_factor != 1.0:
            return here
        return world.block_properties(*PhysicsEngine._block_below(state))

    @staticmethod
    def _input_vector(input_vector: Vec3, speed: float, yaw: float) -> Vec3:
        length = input_vector.length_squared
        if length < 1.0e-7:
            return Vec3()
        movement = (input_vector.normalized() if length > 1.0 else input_vector).scale(speed)
        sin_yaw, cos_yaw = yaw_sin_cos(yaw)
        return Vec3(
            movement.x * cos_yaw - movement.z * sin_yaw,
            movement.y,
            movement.z * cos_yaw + movement.x * sin_yaw,
        )

    @staticmethod
    def _handle_climbable(state: PhysicsState, movement: Vec3) -> Vec3:
        if not state.on_climbable or state.flying:
            return movement
        state.fall_distance = 0.0
        y = max(movement.y, -f32(0.15))
        if y < 0.0 and state.sneaking and not state.on_scaffolding:
            y = 0.0
        return Vec3(
            min(f32(0.15), max(-f32(0.15), movement.x)),
            y,
            min(f32(0.15), max(-f32(0.15), movement.z)),
        )

    def _fluid_falling_adjusted(
        self,
        state: PhysicsState,
        gravity: float,
        falling: bool,
        movement: Vec3,
    ) -> Vec3:
        if gravity != 0.0 and not state.sprinting:
            if (
                falling
                and abs(movement.y - 0.005) >= 0.003
                and abs(movement.y - gravity / 16.0) < 0.003
            ):
                y = -0.003
            else:
                y = movement.y - gravity / 16.0
            return Vec3(movement.x, y, movement.z)
        return movement

    @staticmethod
    def _jump_out_of_fluid(
        state: PhysicsState,
        old_y: float,
        world: CollisionWorld,
    ) -> None:
        movement = state.velocity
        query = state.bounding_box.move(
            movement.x,
            movement.y + f32(0.6) - state.position.y + old_y,
            movement.z,
        )
        if state.horizontal_collision and PhysicsEngine._no_entity_collision(
            state, world, query
        ):
            state.velocity = Vec3(movement.x, f32(0.3), movement.z)

    @staticmethod
    def _block_below(state: PhysicsState) -> tuple[int, int, int]:
        return (
            math.floor(state.position.x),
            math.floor(state.position.y - f32(0.500001)),
            math.floor(state.position.z),
        )

    @staticmethod
    def _head_in_fluid(state: PhysicsState, world: CollisionWorld) -> bool:
        """Return whether the block at player y+0.9 contains any fluid.

        This deliberately follows Player.travel's block-state check instead of
        ``eye_in_water``: a swimming player's look-vector buoyancy is enabled
        by a fluid block just below the eyes as well as by a submerged eye.
        """

        properties = world.block_properties(
            math.floor(state.position.x),
            math.floor(state.position.y + 0.9),
            math.floor(state.position.z),
        )
        return properties.fluid is not None

    @staticmethod
    def _fluid_interaction(
        state: PhysicsState,
        world: CollisionWorld,
        fluid: str,
    ) -> tuple[float, bool, Vec3]:
        """Approximate EntityFluidInteraction's height/eye tracker.

        Mojang scans every block touched by the entity's fluid-interaction box,
        taking the greatest ``fluidTop - boundingBox.minY``.  Looking only at
        the feet block (the former implementation) misses swimming through a
        column, partial water levels, and lava surrounding the torso.
        """

        raw_box = state.bounding_box
        # Entity.getFluidInteractionBox() deflates every face by 0.001.  The
        # tracker still measures height relative to the original entity minY.
        margin = 0.001
        box = AABB(
            raw_box.min_x + margin,
            raw_box.min_y + margin,
            raw_box.min_z + margin,
            raw_box.max_x - margin,
            raw_box.max_y - margin,
            raw_box.max_z - margin,
        )
        x0, x1 = math.floor(box.min_x), math.ceil(box.max_x) - 1
        y0, y1 = math.floor(box.min_y), math.ceil(box.max_y) - 1
        z0, z1 = math.floor(box.min_z), math.ceil(box.max_z) - 1
        eye_x = math.floor(state.position.x)
        eye_y = state.position.y + state.eye_height
        eye_z = math.floor(state.position.z)
        height = 0.0
        eye_inside = False
        accumulated_flow = Vec3()
        flow_count = 0
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for z in range(z0, z1 + 1):
                    properties = world.block_properties(x, y, z)
                    if properties.fluid != fluid or properties.fluid_height <= 0.0:
                        continue
                    bottom = float(y)
                    top = bottom + float(fluid_height(world, x, y, z))
                    # EntityFluidInteraction accepts equality at the lower
                    # face, matching the server's inclusive fluid check.
                    if top < box.min_y:
                        continue
                    height = max(height, top - raw_box.min_y)
                    if x == eye_x and z == eye_z and eye_y >= bottom and eye_y <= top:
                        eye_inside = True
                    flow = fluid_flow(world, x, y, z)
                    if height < 0.4:
                        flow = flow.scale(height)
                    accumulated_flow = accumulated_flow + flow
                    flow_count += 1
        if flow_count:
            accumulated_flow = accumulated_flow.scale(1.0 / flow_count)
        return max(0.0, height), eye_inside, accumulated_flow

    def _update_environment(
        self,
        state: PhysicsState,
        world: CollisionWorld,
        *,
        apply_currents: bool = False,
    ) -> None:
        water_height, eye_water, water_flow = PhysicsEngine._fluid_interaction(
            state, world, "water"
        )
        lava_height, eye_lava, lava_flow = PhysicsEngine._fluid_interaction(state, world, "lava")
        state.water_height = water_height
        state.lava_height = lava_height
        state.water_flow = water_flow
        state.lava_flow = lava_flow
        state.eye_in_water = eye_water
        state.eye_in_lava = eye_lava
        state.in_water = water_height > 0.0
        state.in_lava = lava_height > 0.0

        # LivingEntity.onClimbable checks only getInBlockState(), i.e. the
        # player's blockPosition, not every ladder touched by the body.
        in_block = world.block_properties(
            math.floor(state.position.x),
            math.floor(state.position.y),
            math.floor(state.position.z),
        )
        state.on_climbable = in_block.climbable
        state.on_scaffolding = in_block.scaffolding
        if state.in_water:
            # EntityFluidInteraction.resetFallDistance() runs every tick in
            # water before travel.
            state.fall_distance = 0.0
        if apply_currents:
            # Entity.baseTick halves accumulated fall distance in lava once per
            # tick (the final environment refresh must not halve it again).
            if state.in_lava:
                state.fall_distance *= 0.5
            self._apply_fluid_currents(state)

    def _apply_fluid_currents(self, state: PhysicsState) -> None:
        """Apply EntityFluidInteraction's averaged fluid impulses."""

        if state.flying:
            return
        velocity = state.velocity
        for flow, scale in (
            (state.water_flow, 0.014),
            (state.lava_flow, self.attributes.lava_flow_scale),
        ):
            if flow.length_squared < 1.0e-5:
                continue
            impulse = flow.scale(scale)
            if (
                abs(velocity.x) < 0.003
                and abs(velocity.z) < 0.003
                and impulse.length_squared**0.5 < 0.0045
            ):
                impulse = impulse.normalized().scale(0.0045)
            velocity = velocity + impulse
        state.velocity = velocity

    @staticmethod
    def _apply_bubble_column_effects(
        state: PhysicsState,
        world: CollisionWorld,
        start_position: Vec3,
    ) -> None:
        """Apply BubbleColumnBlock's post-travel velocity callback.

        LivingEntity applies inside-block effects after travel. For ordinary
        sub-block movement Mojang only marks blocks intersecting the final,
        deflated player box as ``inside``. A movement longer than 0.99999
        blocks instead enables callbacks for every block swept by that box.
        """

        if state.flying:
            return
        for x, y, z, properties in PhysicsEngine._blocks_touched_by_movement(
            state, world, start_position
        ):
            drag_down = properties.bubble_column_drag_down
            if drag_down is None:
                continue
            above_properties = world.block_properties(x, y + 1, z)
            inner = 1.0e-7
            above_interior = AABB(
                x + inner,
                y + 1.0 + inner,
                z + inner,
                x + 1.0 - inner,
                y + 2.0 - inner,
                z + 1.0 - inner,
            )
            surface = (
                world.no_collision(above_interior)
                and above_properties.fluid is None
            )
            velocity = state.velocity
            if surface:
                vertical = (
                    max(-0.9, velocity.y - 0.03)
                    if drag_down
                    else min(1.8, velocity.y + 0.1)
                )
            else:
                vertical = (
                    max(-0.3, velocity.y - 0.03)
                    if drag_down
                    else min(0.7, velocity.y + 0.06)
                )
                state.fall_distance = 0.0
            state.velocity = Vec3(velocity.x, vertical, velocity.z)

    @staticmethod
    def _apply_inside_block_effects(
        state: PhysicsState,
        world: CollisionWorld,
        start_position: Vec3,
    ) -> None:
        """Apply movement-relevant vanilla ``entityInside`` callbacks."""

        in_block = world.block_properties(
            math.floor(state.position.x),
            math.floor(state.position.y),
            math.floor(state.position.z),
        )
        for x, y, z, properties in PhysicsEngine._blocks_touched_by_movement(
            state, world, start_position
        ):
            multiplier = properties.stuck_speed_multiplier
            if (
                properties.weaving_stuck_speed_multiplier is not None
                and PhysicsEngine._has_effect(state, "weaving")
            ):
                multiplier = properties.weaving_stuck_speed_multiplier
            if properties.powder_snow:
                state.in_powder_snow = True
                if in_block.powder_snow:
                    multiplier = (f32(0.9), 1.5, f32(0.9))
            if multiplier is not None:
                state.fall_distance = 0.0
                state.stuck_speed_multiplier = Vec3(*multiplier)
            if properties.honey_block:
                PhysicsEngine._apply_honey_slide(state, x, y, z)

    @staticmethod
    def _apply_honey_slide(state: PhysicsState, x: int, y: int, z: int) -> None:
        if state.on_ground or state.position.y > y + 0.9375 - 1.0e-7:
            return
        velocity = state.velocity
        old_delta_y = velocity.y / f32(0.98) + 0.08
        if old_delta_y >= -0.08:
            return
        overlap_distance = 0.4375 + state.width / 2.0
        dx = abs(x + 0.5 - state.position.x)
        dz = abs(z + 0.5 - state.position.z)
        if dx + 1.0e-7 <= overlap_distance and dz + 1.0e-7 <= overlap_distance:
            return
        if old_delta_y < -0.13:
            horizontal_scale = -0.05 / old_delta_y
            velocity = velocity.multiply(horizontal_scale, 1.0, horizontal_scale)
        state.velocity = Vec3(
            velocity.x,
            (-0.05 - 0.08) * f32(0.98),
            velocity.z,
        )
        state.fall_distance = 0.0

    @staticmethod
    def _blocks_touched_by_movement(
        state: PhysicsState,
        world: CollisionWorld,
        start_position: Vec3,
    ) -> list[tuple[int, int, int, BlockProperties]]:
        """Return block cells receiving vanilla inside-block callbacks."""

        end_position = state.position
        displacement = end_position - start_position
        check_sweep = displacement.length_squared > 0.99999 * 0.99999
        epsilon = 1.0e-5
        half_width = state.width / 2.0 - epsilon
        height = state.height - 2.0 * epsilon
        if half_width <= 0.0 or height <= 0.0:
            return []

        min_x = math.floor(min(start_position.x, end_position.x) - half_width)
        max_x = math.ceil(max(start_position.x, end_position.x) + half_width) - 1
        min_y = math.floor(min(start_position.y, end_position.y) + epsilon)
        max_y = math.ceil(max(start_position.y, end_position.y) + state.height - epsilon) - 1
        min_z = math.floor(min(start_position.z, end_position.z) - half_width)
        max_z = math.ceil(max(start_position.z, end_position.z) + half_width) - 1

        touched: list[tuple[float, int, int, int, BlockProperties]] = []
        final_box = AABB(
            end_position.x - half_width,
            end_position.y + epsilon,
            end_position.z - half_width,
            end_position.x + half_width,
            end_position.y + state.height - epsilon,
            end_position.z + half_width,
        )
        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                for z in range(min_z, max_z + 1):
                    properties = world.block_properties(x, y, z)
                    block_box = AABB(x, y, z, x + 1.0, y + 1.0, z + 1.0)
                    if final_box.intersects(block_box):
                        entry = 1.0
                    elif check_sweep:
                        entry = PhysicsEngine._swept_player_block_entry(
                            start_position,
                            displacement,
                            state.width,
                            state.height,
                            block_box,
                            epsilon,
                        )
                        if entry is None:
                            continue
                    else:
                        continue
                    touched.append((entry, x, y, z, properties))
        return [
            (x, y, z, properties)
            for _, x, y, z, properties in sorted(
                touched,
                key=lambda item: item[:4],
            )
        ]

    @staticmethod
    def _swept_player_block_entry(
        start: Vec3,
        displacement: Vec3,
        width: float,
        height: float,
        block: AABB,
        epsilon: float,
    ) -> float | None:
        """Return the first segment time at which the moving player touches a block."""

        half_width = width / 2.0 - epsilon
        target = AABB(
            block.min_x - half_width,
            block.min_y - height + epsilon,
            block.min_z - half_width,
            block.max_x + half_width,
            block.max_y - epsilon,
            block.max_z + half_width,
        )
        entry, leave = 0.0, 1.0
        for axis in ("x", "y", "z"):
            origin = getattr(start, axis)
            delta = getattr(displacement, axis)
            lower = target.min(axis)
            upper = target.max(axis)
            if delta == 0.0:
                if origin <= lower or origin >= upper:
                    return None
                continue
            axis_entry = (lower - origin) / delta
            axis_leave = (upper - origin) / delta
            if axis_entry > axis_leave:
                axis_entry, axis_leave = axis_leave, axis_entry
            entry = max(entry, axis_entry)
            leave = min(leave, axis_leave)
            if entry >= leave:
                return None
        return entry if 0.0 <= entry <= 1.0 else None

    @staticmethod
    def _update_swimming(
        state: PhysicsState,
        controls: MovementInput,
        world: CollisionWorld,
    ) -> None:
        """Implement Entity.updateSwimming's two different transitions.

        Once swimming, sprinting + any water contact keeps the flag alive.  A
        non-swimming player may enter it only when sprinting while submerged
        (eye in water) and the block at ``blockPosition`` is water.  Feet-only
        contact must remain a standing/wading pose.
        """

        if state.flying or state.spectator:
            state.swimming = False
        elif state.swimming:
            state.swimming = state.sprinting and state.in_water
        else:
            block = world.block_properties(
                math.floor(state.position.x),
                math.floor(state.position.y),
                math.floor(state.position.z),
            )
            state.swimming = (
                state.sprinting and state.eye_in_water and state.in_water and block.fluid == "water"
            )

    @staticmethod
    def _refresh_pose(
        state: PhysicsState,
        controls: MovementInput,
        world: CollisionWorld,
    ) -> None:
        """Apply the player dimensions used by standing/crouching/swimming.

        Minecraft keeps the feet anchored while refreshing dimensions.  A
        crouched player that cannot expand to standing height remains low even
        after the sneak key is released; this is what makes crawling through a
        one-block gap deterministic instead of teleporting the bounding box.
        """

        # Player.updatePlayerPose returns without changing anything if the
        # universal swimming-sized fallback itself is blocked.  This guard is
        # important when a server teleports a player into a tight collision.
        if not state.spectator and not PhysicsEngine._pose_fits(state, "swimming", world):
            return

        if state.swimming:
            desired = "swimming"
        elif state.gliding:
            desired = "gliding"
        elif controls.crawl:
            desired = "crawling"
        elif controls.sneak and not state.flying:
            desired = "crouching"
        else:
            desired = "standing"

        # Vanilla tests the desired pose at the current feet position and, if
        # it does not fit, falls back to crouching and finally the low swimming
        # pose.  ``crawling`` is our explicit name for that visually-swimming,
        # non-fluid fallback; it must not set the swimming movement flag.
        if not PhysicsEngine._pose_fits(state, desired, world):
            if desired != "crouching" and PhysicsEngine._pose_fits(state, "crouching", world):
                desired = "crouching"
            elif PhysicsEngine._pose_fits(state, "crawling", world):
                desired = "crawling"
            else:
                # Keep the old dimensions if even the low pose is blocked;
                # this mirrors Entity.refreshDimensions' refusal to move an
                # intersecting player.
                desired = state.pose

        heights = {
            "standing": f32(1.8),
            "crouching": f32(1.5),
            "crawling": f32(0.6),
            "swimming": f32(0.6),
            "gliding": f32(0.6),
        }
        state.pose = desired
        state.height = heights[desired]
        state.crouching = desired == "crouching"

    @staticmethod
    def _pose_fits(
        state: PhysicsState,
        pose: str,
        world: CollisionWorld,
    ) -> bool:
        heights = {
            "standing": f32(1.8),
            "crouching": f32(1.5),
            "crawling": f32(0.6),
            "swimming": f32(0.6),
            "gliding": f32(0.6),
        }
        height = heights[pose]
        half_width = state.width / 2.0
        # Player.canPlayerFitWithinBlocksAndEntitiesWhen deflates by 1e-7.
        epsilon = 1.0e-7
        box = AABB(
            state.position.x - half_width + epsilon,
            state.position.y + epsilon,
            state.position.z - half_width + epsilon,
            state.position.x + half_width - epsilon,
            state.position.y + height - epsilon,
            state.position.z + half_width - epsilon,
        )
        return PhysicsEngine._no_entity_collision(state, world, box)
