"""Dynamic registries synchronized during the configuration state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ProtocolError
from .protocol.codec import PacketReader
from .protocol.nbt import read_anonymous_nbt


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    key: str
    value: Any | None


class RegistryStore:
    def __init__(self) -> None:
        self._registries: dict[str, tuple[RegistryEntry, ...]] = {}

    def apply_packet(self, payload: bytes) -> str:
        reader = PacketReader(payload)
        registry_id = reader.read_string(max_chars=32767)
        count = reader.read_varint()
        if count < 0 or count > 1 << 20:
            raise ProtocolError(f"invalid registry entry count {count}")
        entries: list[RegistryEntry] = []
        for _ in range(count):
            key = reader.read_string(max_chars=32767)
            value = read_anonymous_nbt(reader) if reader.read_bool() else None
            entries.append(RegistryEntry(key, value))
        reader.expect_end()
        self._registries[registry_id] = tuple(entries)
        return registry_id

    def get(self, registry_id: str) -> tuple[RegistryEntry, ...]:
        return self._registries.get(registry_id, ())

    def by_id(self, registry_id: str, protocol_id: int) -> RegistryEntry | None:
        entries = self.get(registry_id)
        return entries[protocol_id] if 0 <= protocol_id < len(entries) else None

    def dimension_bounds(self, protocol_id: int) -> tuple[int, int] | None:
        entry = self.by_id("minecraft:dimension_type", protocol_id)
        if entry is None or not isinstance(entry.value, dict):
            return None
        min_y, height = entry.value.get("min_y"), entry.value.get("height")
        if not isinstance(min_y, int) or not isinstance(height, int):
            return None
        if height <= 0 or height % 16:
            raise ProtocolError(f"dimension {entry.key} has invalid height {height}")
        return min_y, height
