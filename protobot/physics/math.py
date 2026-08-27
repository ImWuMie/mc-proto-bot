"""Java-compatible numeric helpers used by the physics port."""

from __future__ import annotations

import math
import struct
from functools import lru_cache

_FLOAT = struct.Struct(">f")
_SIN_SCALE = 10430.378350470453
_DEGREES_TO_RADIANS = None


def f32(value: float) -> float:
    """Round a Python float exactly as a Java ``float`` assignment does."""
    return _FLOAT.unpack(_FLOAT.pack(value))[0]


_DEGREES_TO_RADIANS = f32(f32(math.pi) / 180.0)


@lru_cache(maxsize=65536)
def _sin_table(index: int) -> float:
    return f32(math.sin(index / _SIN_SCALE))


def minecraft_sin(radians: float) -> float:
    index = int(radians * _SIN_SCALE) & 0xFFFF
    return _sin_table(index)


def minecraft_cos(radians: float) -> float:
    index = int(radians * _SIN_SCALE + 16384.0) & 0xFFFF
    return _sin_table(index)


def yaw_sin_cos(yaw: float) -> tuple[float, float]:
    radians = f32(f32(yaw) * _DEGREES_TO_RADIANS)
    return minecraft_sin(radians), minecraft_cos(radians)


def look_y_from_pitch(pitch: float) -> float:
    """Return the Y component of Minecraft's view vector for a pitch angle."""

    radians = f32(f32(-pitch) * _DEGREES_TO_RADIANS)
    return minecraft_sin(radians)


def look_vector_components(yaw: float, pitch: float) -> tuple[float, float, float]:
    """Return Minecraft's float-table view vector as three doubles."""

    pitch_radians = f32(f32(pitch) * _DEGREES_TO_RADIANS)
    yaw_radians = f32(f32(-yaw) * _DEGREES_TO_RADIANS)
    yaw_cos = minecraft_cos(yaw_radians)
    yaw_sin = minecraft_sin(yaw_radians)
    pitch_cos = minecraft_cos(pitch_radians)
    pitch_sin = minecraft_sin(pitch_radians)
    return yaw_sin * pitch_cos, -pitch_sin, yaw_cos * pitch_cos


def modified_friction(friction: float, modifier: float) -> float:
    inner = f32(f32(1.0) - f32(friction))
    scaled = f32(inner * f32(modifier))
    return min(1.0, max(0.0, f32(f32(1.0) - scaled)))
