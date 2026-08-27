"""Public API for ProtoBot."""

from .auth import (
    MinecraftProfile,
    authorization_code_login,
    authorization_url,
    device_code_login,
    join_session_server,
    minecraft_sha1_digest,
    refresh_login,
)
from .client import AttributeModifierUpdate, AttributeUpdate, Bot, connect
from .errors import (
    AuthenticationError,
    ConnectionClosed,
    LoginRejected,
    OnlineModeRequired,
    ProtocolError,
    UnsupportedVersion,
)
from . import log
from .modlist import (
    ChannelFlow,
    ChannelSpec,
    Loader,
    ModListAdapter,
    PayloadResponse,
    make_adapter,
)
from .navigation import (
    NavigationPath,
    NavigationTimeout,
    Pathfinder,
    PathNotFound,
    PathWaypoint,
)
from .physics import (
    AABB,
    AIR,
    DEFAULT_BLOCK,
    BlockProperties,
    BoatPhysicsEngine,
    MovementInput,
    PhysicsAttributes,
    PhysicsEngine,
    PhysicsState,
    StaticCollisionWorld,
    StatusEffect,
    Vec3,
)
from .plugin import Plugin, PluginError, PluginManager, PluginWatcher
from .protocol.versions import SUPPORTED_VERSIONS, VersionSpec, get_version
from .session import BotContainer, BotSession, SessionConfig
from .srv import resolve_minecraft_srv
from .text import plain_text
from .tui import ProtoBotApp, StdoutProxy, classify_submission, tui_enabled
from .state import (
    ContainerState,
    EntityMetadataValue,
    EntityState,
    EquipmentSlot,
    ItemStack,
    PlayerAbilities,
)
from .world import BlockStateDefinition, BlockStateRegistry, World

__all__ = [
    "AABB",
    "AIR",
    "DEFAULT_BLOCK",
    "SUPPORTED_VERSIONS",
    "AttributeModifierUpdate",
    "AttributeUpdate",
    "AuthenticationError",
    "BlockProperties",
    "BlockStateDefinition",
    "BlockStateRegistry",
    "BoatPhysicsEngine",
    "Bot",
    "BotContainer",
    "BotSession",
    "ChannelFlow",
    "ChannelSpec",
    "ConnectionClosed",
    "ContainerState",
    "EntityMetadataValue",
    "EntityState",
    "EquipmentSlot",
    "ItemStack",
    "Loader",
    "LoginRejected",
    "ModListAdapter",
    "MovementInput",
    "NavigationPath",
    "NavigationTimeout",
    "OnlineModeRequired",
    "PathNotFound",
    "PathWaypoint",
    "Pathfinder",
    "PayloadResponse",
    "PhysicsAttributes",
    "PhysicsEngine",
    "PhysicsState",
    "PlayerAbilities",
    "Plugin",
    "PluginError",
    "PluginManager",
    "PluginWatcher",
    "ProtoBotApp",
    "ProtocolError",
    "SessionConfig",
    "StaticCollisionWorld",
    "StdoutProxy",
    "StatusEffect",
    "UnsupportedVersion",
    "Vec3",
    "VersionSpec",
    "World",
    "classify_submission",
    "connect",
    "get_version",
    "make_adapter",
    "plain_text",
    "resolve_minecraft_srv",
    "minecraft_sha1_digest",
    "MinecraftProfile",
    "authorization_code_login",
    "authorization_url",
    "device_code_login",
    "join_session_server",
    "log",
    "refresh_login",
    "tui_enabled",
]
