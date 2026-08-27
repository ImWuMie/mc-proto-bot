"""Client-side state models."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import IntEnum


class EquipmentSlot(IntEnum):
    """Wire ordinals used by the clientbound equipment packet."""

    MAINHAND = 0
    OFFHAND = 1
    FEET = 2
    LEGS = 3
    CHEST = 4
    HEAD = 5
    BODY = 6
    SADDLE = 7


@dataclass(frozen=True, slots=True)
class ItemStack:
    """Safely decoded identity and removal-only component patch."""

    count: int = 0
    item_id: int | None = None
    identifier: str | None = None
    removed_component_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("item count cannot be negative")
        if self.count and (self.item_id is None or self.item_id < 0):
            raise ValueError("non-empty item stacks require a registry ID")
        if not self.count and self.item_id is not None:
            raise ValueError("empty item stacks cannot have a registry ID")
        if any(component_id < 0 for component_id in self.removed_component_ids):
            raise ValueError("component registry IDs cannot be negative")

    @property
    def empty(self) -> bool:
        return self.count == 0


@dataclass(slots=True)
class ContainerState:
    """Latest server snapshot for one open container.

    Item data is kept as the exact protocol bytes after the slot header. This
    makes custom GUI components observable without pretending to understand
    application-specific component registries.
    """

    container_id: int
    menu_type_id: int | None = None
    state_id: int = 0
    title_data: bytes = b""
    slots: dict[int, bytes] = field(default_factory=dict)
    carried_item_data: bytes = b""
    content_data: bytes = b""
    is_open: bool = True


@dataclass(frozen=True, slots=True)
class EntityMetadataValue:
    """One decoded SynchedEntityData value keyed by its accessor index."""

    serializer_id: int
    value: object


@dataclass(slots=True)
class EntityState:
    """Latest protocol position, motion, mount, and equipment for one entity."""

    entity_id: int
    entity_uuid: uuid.UUID | None = None
    type_id: int | None = None
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    velocity_z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    head_yaw: float = 0.0
    data: int = 0
    on_ground: bool = False
    collision_width: float | None = None
    collision_height: float | None = None
    vehicle_id: int | None = None
    passenger_ids: tuple[int, ...] = ()
    metadata: dict[int, EntityMetadataValue] = field(default_factory=dict)
    shulker_attach_face: int = 0
    shulker_peek: int = 0
    shulker_current_peek: float = 0.0
    shulker_previous_peek: float = 0.0
    boat_status: str = "in_air"
    boat_old_status: str = "in_air"
    boat_water_level: float = 0.0
    boat_land_friction: float = 0.0
    boat_last_y_velocity: float = 0.0
    boat_delta_rotation: float = 0.0
    boat_left_paddle: bool = False
    boat_right_paddle: bool = False
    horizontal_collision: bool = False
    vertical_collision: bool = False
    equipment: dict[EquipmentSlot, ItemStack] = field(default_factory=dict)

    @property
    def position(self) -> tuple[float, float, float]:
        return self.x, self.y, self.z

    @property
    def velocity(self) -> tuple[float, float, float]:
        return self.velocity_x, self.velocity_y, self.velocity_z


@dataclass(slots=True)
class PlayerAbilities:
    """The six values carried by the clientbound player-abilities packet."""

    invulnerable: bool = False
    flying: bool = False
    allow_flying: bool = False
    instant_build: bool = False
    fly_speed: float = 0.05
    walk_speed: float = 0.1


@dataclass(slots=True)
class PlayerState:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    velocity_z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    on_ground: bool = False
    horizontal_collision: bool = False
    loaded: bool = False
    pose: str = "standing"
    crouching: bool = False
    swimming: bool = False
    gliding: bool = False
    selected_hotbar_slot: int = 0
    vehicle_id: int | None = None
    passenger_ids: tuple[int, ...] = ()
    metadata: dict[int, EntityMetadataValue] = field(default_factory=dict)
    abilities: PlayerAbilities = field(default_factory=PlayerAbilities)
    inventory: dict[int, ItemStack] = field(default_factory=dict)
    equipment: dict[EquipmentSlot, ItemStack] = field(default_factory=dict)

    @property
    def position(self) -> tuple[float, float, float]:
        return self.x, self.y, self.z

    @property
    def velocity(self) -> tuple[float, float, float]:
        return self.velocity_x, self.velocity_y, self.velocity_z

    @property
    def wearing_elytra(self) -> bool:
        chest = self.equipment.get(EquipmentSlot.CHEST)
        return chest is not None and chest.identifier == "minecraft:elytra"


@dataclass(slots=True)
class WorldSessionState:
    entity_id: int | None = None
    dimension_type_id: int | None = None
    dimension_name: str | None = None
    game_mode: int = 0
    previous_game_mode: int = -1
    hardcore: bool = False
    is_debug: bool = False
    is_flat: bool = False
    view_distance: int = 0
    simulation_distance: int = 0
    sea_level: int = 63
    online_mode: bool = False
    enforces_secure_chat: bool = False
