"""Public API for ProtoBot."""

__version__ = "1.2.0"

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
from .plugin import (
    ExposedFunction,
    Plugin,
    PluginError,
    PluginManager,
    PluginWatcher,
)
from .protocol.versions import SUPPORTED_VERSIONS, VersionSpec, get_version
from .session import BotContainer, BotSession, SessionConfig
from .settings import PluginSettings, deep_merge
from .srv import resolve_minecraft_srv
from .text import format_translation, plain_text
from .translations import (
    TRANSLATIONS,
    load_translations,
    register_translations,
)
from .tui import ProtoBotApp, StdoutProxy, classify_submission, tui_enabled
from .state import (
    ContainerState,
    EntityMetadataValue,
    EntityState,
    EquipmentSlot,
    ItemStack,
    PlayerAbilities,
    PlayerListEntry,
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
    "ExposedFunction",
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
    "PlayerListEntry",
    "Plugin",
    "PluginError",
    "PluginManager",
    "PluginSettings",
    "PluginWatcher",
    "ProtoBotApp",
    "ProtocolError",
    "SessionConfig",
    "StaticCollisionWorld",
    "StdoutProxy",
    "StatusEffect",
    "TRANSLATIONS",
    "UnsupportedVersion",
    "__version__",
    "Vec3",
    "VersionSpec",
    "World",
    "classify_submission",
    "connect",
    "deep_merge",
    "format_translation",
    "get_version",
    "load_translations",
    "make_adapter",
    "plain_text",
    "register_translations",
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
