"""Release metadata and the packet IDs needed by the core state machine."""

from __future__ import annotations

from dataclasses import dataclass

from protobot.errors import UnsupportedVersion


@dataclass(frozen=True, slots=True)
class PlayPacketIds:
    clientbound_add_entity: int
    clientbound_custom_payload: int
    clientbound_container_close: int
    clientbound_container_set_content: int
    clientbound_container_set_slot: int
    clientbound_open_screen: int
    clientbound_remove_mob_effect: int
    clientbound_remove_entities: int
    clientbound_block_update: int
    clientbound_chunk_batch_finished: int
    clientbound_chunk_data: int
    clientbound_disconnect: int
    clientbound_game_event: int
    clientbound_keep_alive: int
    clientbound_login: int
    clientbound_move_vehicle: int
    clientbound_move_entity_pos: int
    clientbound_move_entity_pos_rot: int
    clientbound_move_entity_rot: int
    clientbound_ping: int
    clientbound_player_abilities: int
    clientbound_player_chat: int
    clientbound_profileless_chat: int
    clientbound_position: int
    clientbound_respawn: int
    clientbound_section_blocks_update: int
    clientbound_set_entity_data: int
    clientbound_set_entity_motion: int
    clientbound_set_equipment: int
    clientbound_set_passengers: int
    clientbound_set_player_inventory: int
    clientbound_start_configuration: int
    clientbound_system_chat: int
    clientbound_teleport_entity: int
    clientbound_transfer: int
    clientbound_unload_chunk: int
    clientbound_update_attributes: int
    clientbound_update_mob_effect: int
    serverbound_chunk_batch_received: int
    serverbound_chat: int
    serverbound_chat_command: int
    serverbound_custom_payload: int
    serverbound_container_click: int
    serverbound_container_close: int
    serverbound_configuration_acknowledged: int
    serverbound_keep_alive: int
    serverbound_move_vehicle: int
    serverbound_paddle_boat: int
    serverbound_position: int
    serverbound_position_look: int
    serverbound_look: int
    serverbound_flying: int
    serverbound_player_abilities: int
    serverbound_player_command: int
    serverbound_player_input: int
    serverbound_player_loaded: int
    serverbound_pong: int
    serverbound_set_carried_item: int
    serverbound_tick_end: int
    serverbound_use_item: int
    serverbound_teleport_confirm: int = 0
    #: 位置型音效包。0 = 这个版本的 ID 未经核实，依赖它的功能应自行降级，
    #: 而不是拿一个推断值去解析（解错了会误判）。
    clientbound_sound: int = 0
    #: 死亡与重生。同样以 0 表示「本版本未核实」——核心解析会跳过 0，
    #: 避免把真正的 0x00 包错当成死亡信号。
    clientbound_set_health: int = 0
    clientbound_player_combat_kill: int = 0
    serverbound_client_command: int = 0


@dataclass(frozen=True, slots=True)
class VersionSpec:
    name: str
    protocol: int
    data_version: int
    packets: PlayPacketIds
    item_identifiers: tuple[tuple[int, str], ...] = ()
    hard_collision_entities: tuple[tuple[int, float, float], ...] = ()
    boat_entities: tuple[int, ...] = ()
    chest_boat_entities: tuple[int, ...] = ()
    raft_entities: tuple[int, ...] = ()

    def item_identifier(self, registry_id: int) -> str | None:
        """Resolve an item ID using this exact release's static registry."""

        for item_id, identifier in self.item_identifiers:
            if item_id == registry_id:
                return identifier
        return None

    def hard_collision_dimensions(self, registry_id: int) -> tuple[float, float] | None:
        for entity_id, width, height in self.hard_collision_entities:
            if entity_id == registry_id:
                return width, height
        return None

    def boat_kind(self, registry_id: int) -> str | None:
        """Return the source-level AbstractBoat variant for an entity ID."""

        if registry_id not in self.boat_entities:
            return None
        chest = registry_id in self.chest_boat_entities
        raft = registry_id in self.raft_entities
        if raft:
            return "chest_raft" if chest else "raft"
        return "chest_boat" if chest else "boat"


_BOAT_ENTITIES = (
        0,
        1,
        8,
        9,
        12,
        13,
        23,
        24,
        33,
        34,
        74,
        75,
        81,
        82,
        89,
        90,
        94,
        95,
        125,
        126,
)

_CHEST_BOAT_ENTITIES = (1, 8, 13, 24, 34, 75, 82, 90, 95, 126)
_RAFT_ENTITIES = (8, 9)

_HARD_COLLISION_ENTITIES = (
    *((entity_id, 1.375, 0.5625) for entity_id in _BOAT_ENTITIES),
    (58, 4.0, 4.0),
    (112, 1.0, 1.0),
)


_PLAY_774 = PlayPacketIds(
    clientbound_add_entity=0x01,
    clientbound_custom_payload=0x18,
    clientbound_container_close=0x11,
    clientbound_container_set_content=0x12,
    clientbound_container_set_slot=0x14,
    clientbound_open_screen=0x39,
    clientbound_profileless_chat=0x21,
    clientbound_remove_mob_effect=0x4C,
    clientbound_remove_entities=0x4B,
    clientbound_block_update=0x08,
    clientbound_chunk_batch_finished=0x0B,
    clientbound_chunk_data=0x2C,
    clientbound_disconnect=0x20,
    clientbound_game_event=0x26,
    clientbound_keep_alive=0x2B,
    clientbound_login=0x30,
    clientbound_move_vehicle=0x37,
    clientbound_move_entity_pos=0x33,
    clientbound_move_entity_pos_rot=0x34,
    clientbound_move_entity_rot=0x36,
    clientbound_ping=0x3B,
    clientbound_player_abilities=0x3E,
    clientbound_player_chat=0x3F,
    clientbound_position=0x46,
    clientbound_respawn=0x50,
    clientbound_section_blocks_update=0x52,
    clientbound_set_entity_data=0x61,
    clientbound_set_entity_motion=0x63,
    clientbound_set_equipment=0x64,
    clientbound_set_passengers=0x69,
    clientbound_set_player_inventory=0x6A,
    clientbound_start_configuration=0x74,
    clientbound_system_chat=0x77,
    clientbound_teleport_entity=0x7B,
    clientbound_transfer=0x7F,
    clientbound_unload_chunk=0x25,
    clientbound_update_attributes=0x81,
    clientbound_update_mob_effect=0x82,
    serverbound_chunk_batch_received=0x0A,
    serverbound_chat=0x08,
    serverbound_chat_command=0x06,
    serverbound_custom_payload=0x15,
    serverbound_container_click=0x11,
    serverbound_container_close=0x12,
    serverbound_configuration_acknowledged=0x0F,
    serverbound_keep_alive=0x1B,
    serverbound_move_vehicle=0x21,
    serverbound_paddle_boat=0x22,
    serverbound_position=0x1D,
    serverbound_position_look=0x1E,
    serverbound_look=0x1F,
    serverbound_flying=0x20,
    serverbound_player_abilities=0x27,
    serverbound_player_command=0x29,
    serverbound_player_input=0x2A,
    serverbound_player_loaded=0x2B,
    serverbound_pong=0x2C,
    serverbound_set_carried_item=0x34,
    serverbound_tick_end=0x0C,
    serverbound_use_item=0x40,
)

_PLAY_775_776 = PlayPacketIds(
    clientbound_sound=0x75,  # 116/117/119 = sound_entity/sound/stop_sound
    clientbound_set_health=0x68,  # 104: Float 血量, VarInt 饱食度, Float 饱和度
    clientbound_player_combat_kill=0x44,  # 68: VarInt 实体 ID + 死亡消息组件
    serverbound_client_command=0x0C,  # 12: 单个 VarInt，0 = perform respawn
    clientbound_add_entity=0x01,
    clientbound_custom_payload=0x18,
    clientbound_container_close=0x11,
    clientbound_container_set_content=0x12,
    clientbound_container_set_slot=0x14,
    clientbound_open_screen=0x3B,
    clientbound_profileless_chat=0x21,
    clientbound_remove_mob_effect=0x4E,
    clientbound_remove_entities=0x4D,
    clientbound_block_update=0x08,
    clientbound_chunk_batch_finished=0x0B,
    clientbound_chunk_data=0x2D,
    clientbound_disconnect=0x20,
    clientbound_game_event=0x26,
    clientbound_keep_alive=0x2C,
    clientbound_login=0x31,
    clientbound_move_vehicle=0x39,
    clientbound_move_entity_pos=0x35,
    clientbound_move_entity_pos_rot=0x36,
    clientbound_move_entity_rot=0x38,
    clientbound_ping=0x3D,
    clientbound_player_abilities=0x40,
    clientbound_player_chat=0x41,
    clientbound_position=0x48,
    clientbound_respawn=0x52,
    clientbound_section_blocks_update=0x54,
    clientbound_set_entity_data=0x63,
    clientbound_set_entity_motion=0x65,
    clientbound_set_equipment=0x66,
    clientbound_set_passengers=0x6B,
    clientbound_set_player_inventory=0x6C,
    clientbound_start_configuration=0x76,
    clientbound_system_chat=0x79,
    clientbound_teleport_entity=0x7D,
    clientbound_transfer=0x81,
    clientbound_unload_chunk=0x25,
    clientbound_update_attributes=0x83,
    clientbound_update_mob_effect=0x84,
    serverbound_chunk_batch_received=0x0B,
    serverbound_chat=0x09,
    serverbound_chat_command=0x07,
    serverbound_custom_payload=0x16,
    serverbound_container_click=0x12,
    serverbound_container_close=0x13,
    serverbound_configuration_acknowledged=0x10,
    serverbound_keep_alive=0x1C,
    serverbound_move_vehicle=0x22,
    serverbound_paddle_boat=0x23,
    serverbound_position=0x1E,
    serverbound_position_look=0x1F,
    serverbound_look=0x20,
    serverbound_flying=0x21,
    serverbound_player_abilities=0x28,
    serverbound_player_command=0x2A,
    serverbound_player_input=0x2B,
    serverbound_player_loaded=0x2C,
    serverbound_pong=0x2D,
    serverbound_set_carried_item=0x35,
    serverbound_tick_end=0x0D,
    serverbound_use_item=0x43,
)

SUPPORTED_VERSIONS: dict[str, VersionSpec] = {
    "1.21.11": VersionSpec(
        "1.21.11",
        774,
        4671,
        _PLAY_774,
        ((862, "minecraft:elytra"), (957, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
    "26.1": VersionSpec(
        "26.1",
        775,
        4786,
        _PLAY_775_776,
        ((863, "minecraft:elytra"), (958, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
    "26.1.1": VersionSpec(
        "26.1.1",
        775,
        4788,
        _PLAY_775_776,
        ((863, "minecraft:elytra"), (958, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
    "26.1.2": VersionSpec(
        "26.1.2",
        775,
        4790,
        _PLAY_775_776,
        ((863, "minecraft:elytra"), (958, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
    "26.2": VersionSpec(
        "26.2",
        776,
        4903,
        _PLAY_775_776,
        ((890, "minecraft:elytra"), (985, "minecraft:leather_boots")),
        _HARD_COLLISION_ENTITIES,
        _BOAT_ENTITIES,
        _CHEST_BOAT_ENTITIES,
        _RAFT_ENTITIES,
    ),
}


def get_version(version: str | VersionSpec) -> VersionSpec:
    if isinstance(version, VersionSpec):
        return version
    try:
        return SUPPORTED_VERSIONS[version]
    except KeyError as error:
        supported = ", ".join(SUPPORTED_VERSIONS)
        raise UnsupportedVersion(
            f"unsupported Minecraft version {version!r}; use {supported}"
        ) from error
