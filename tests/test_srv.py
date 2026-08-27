"""Regression tests for Minecraft SRV resolution.

Vanilla clients look up ``_minecraft._tcp.<host>`` when no port is typed; most
public servers rely on it, and connecting without it lands on nothing and the
server closes the socket.
"""

from __future__ import annotations

import struct
import unittest
from unittest.mock import AsyncMock, patch

from protobot import srv
from protobot.client import Bot


def _name(labels: str) -> bytes:
    out = bytearray()
    for label in labels.split("."):
        out.append(len(label))
        out += label.encode("ascii")
    out.append(0)
    return bytes(out)


def _srv_response(
    transaction_id: int,
    records: list[tuple[int, int, int, str]],
    *,
    rcode: int = 0,
) -> bytes:
    question = _name("_minecraft._tcp.example.com") + struct.pack(">HH", 33, 1)
    body = bytearray()
    for priority, weight, port, target in records:
        rdata = struct.pack(">HHH", priority, weight, port) + _name(target)
        body += _name("_minecraft._tcp.example.com")
        body += struct.pack(">HHIH", 33, 1, 300, len(rdata))
        body += rdata
    header = struct.pack(
        ">HHHHHH", transaction_id, 0x8180 | rcode, 1, len(records), 0, 0
    )
    return header + question + bytes(body)


class NameCodecTest(unittest.TestCase):
    def test_encodes_labels_with_length_prefixes(self) -> None:
        self.assertEqual(
            srv._encode_name("_minecraft._tcp.example.com"),
            _name("_minecraft._tcp.example.com"),
        )

    def test_trailing_dot_is_ignored(self) -> None:
        self.assertEqual(srv._encode_name("example.com."), srv._encode_name("example.com"))

    def test_rejects_oversized_label(self) -> None:
        with self.assertRaises(ValueError):
            srv._encode_name("a" * 64 + ".com")

    def test_decodes_a_plain_name(self) -> None:
        raw = _name("mc.example.com")
        name, end = srv._decode_name(raw, 0)
        self.assertEqual(name, "mc.example.com")
        self.assertEqual(end, len(raw))

    def test_follows_compression_pointers(self) -> None:
        base = _name("example.com")
        # "mc" followed by a pointer back to offset 0
        compressed = b"\x02mc" + struct.pack(">H", 0xC000)
        payload = base + compressed
        name, end = srv._decode_name(payload, len(base))
        self.assertEqual(name, "mc.example.com")
        self.assertEqual(end, len(payload))

    def test_rejects_a_pointer_loop(self) -> None:
        payload = struct.pack(">H", 0xC000)
        with self.assertRaises(ValueError):
            srv._decode_name(payload, 0)

    def test_rejects_a_truncated_name(self) -> None:
        with self.assertRaises(ValueError):
            srv._decode_name(b"\x05ab", 0)


class ParseResponseTest(unittest.TestCase):
    def test_parses_records(self) -> None:
        payload = _srv_response(0x1234, [(10, 5, 22100, "backend.example.com")])
        self.assertEqual(
            srv.parse_srv_response(payload, 0x1234),
            [(10, 5, 22100, "backend.example.com")],
        )

    def test_rejects_transaction_id_mismatch(self) -> None:
        payload = _srv_response(0x1234, [(10, 5, 25565, "a.example.com")])
        with self.assertRaises(ValueError):
            srv.parse_srv_response(payload, 0x9999)

    def test_nxdomain_yields_no_records(self) -> None:
        payload = _srv_response(0x1234, [], rcode=3)
        self.assertEqual(srv.parse_srv_response(payload, 0x1234), [])

    def test_rejects_a_short_response(self) -> None:
        with self.assertRaises(ValueError):
            srv.parse_srv_response(b"\x12\x34", 0x1234)


class SelectTargetTest(unittest.TestCase):
    def test_lowest_priority_wins(self) -> None:
        records = [
            (20, 10, 30000, "high.example.com"),
            (10, 10, 22100, "low.example.com"),
        ]
        self.assertEqual(srv.select_srv_target(records), ("low.example.com", 22100))

    def test_higher_weight_breaks_priority_ties(self) -> None:
        records = [
            (10, 1, 30000, "light.example.com"),
            (10, 99, 22100, "heavy.example.com"),
        ]
        self.assertEqual(srv.select_srv_target(records), ("heavy.example.com", 22100))

    def test_strips_the_trailing_dot(self) -> None:
        self.assertEqual(
            srv.select_srv_target([(10, 10, 22100, "backend.example.com.")]),
            ("backend.example.com", 22100),
        )

    def test_root_target_means_service_unavailable(self) -> None:
        self.assertIsNone(srv.select_srv_target([(0, 0, 25565, ".")]))

    def test_zero_port_is_ignored(self) -> None:
        self.assertIsNone(srv.select_srv_target([(10, 10, 0, "backend.example.com")]))

    def test_no_records_yields_none(self) -> None:
        self.assertIsNone(srv.select_srv_target([]))


class ResolveTest(unittest.TestCase):
    def test_ip_literals_are_never_looked_up(self) -> None:
        for literal in ("127.0.0.1", "198.18.0.24", "::1"):
            self.assertIsNone(srv.resolve_minecraft_srv(literal))

    def test_empty_host_yields_none(self) -> None:
        self.assertIsNone(srv.resolve_minecraft_srv(""))

    def test_queries_the_minecraft_service_name(self) -> None:
        seen: list[str] = []

        def fake_query(name):
            seen.append(name)
            return [(10, 10, 22100, "backend.example.com")]

        with patch.object(srv.sys, "platform", "win32"), \
             patch.object(srv, "_query_windows", fake_query):
            self.assertEqual(
                srv.resolve_minecraft_srv("example.com"), ("backend.example.com", 22100)
            )
        self.assertEqual(seen, ["_minecraft._tcp.example.com"])

    def test_resolver_failure_falls_back_to_none(self) -> None:
        def boom(name):
            raise OSError("no resolver")

        with patch.object(srv.sys, "platform", "win32"), \
             patch.object(srv, "_query_windows", boom):
            self.assertIsNone(srv.resolve_minecraft_srv("example.com"))


class BotEndpointResolutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_port_follows_the_srv_record(self) -> None:
        bot = Bot("example.com")
        with patch(
            "protobot.client.resolve_minecraft_srv",
            return_value=("backend.example.com", 22100),
        ):
            self.assertEqual(
                await bot._resolve_endpoint("example.com", 25565),
                ("backend.example.com", 22100),
            )

    async def test_explicit_port_skips_the_lookup(self) -> None:
        """Vanilla only resolves SRV when the player typed no port."""
        bot = Bot("example.com", port=25566)
        lookup = AsyncMock()
        with patch("protobot.client.resolve_minecraft_srv", lookup):
            self.assertEqual(
                await bot._resolve_endpoint("example.com", 25566), ("example.com", 25566)
            )
        lookup.assert_not_called()

    async def test_resolve_srv_false_skips_the_lookup(self) -> None:
        bot = Bot("example.com", resolve_srv=False)
        lookup = AsyncMock()
        with patch("protobot.client.resolve_minecraft_srv", lookup):
            self.assertEqual(
                await bot._resolve_endpoint("example.com", 25565), ("example.com", 25565)
            )
        lookup.assert_not_called()

    async def test_missing_record_keeps_the_original_address(self) -> None:
        bot = Bot("example.com")
        with patch("protobot.client.resolve_minecraft_srv", return_value=None):
            self.assertEqual(
                await bot._resolve_endpoint("example.com", 25565), ("example.com", 25565)
            )

    async def test_emits_an_event_when_redirected(self) -> None:
        bot = Bot("example.com")
        seen: list[tuple[str, str, int]] = []
        bot.on("srv_resolved", lambda original, host, port: seen.append((original, host, port)))
        with patch(
            "protobot.client.resolve_minecraft_srv",
            return_value=("backend.example.com", 22100),
        ):
            await bot._resolve_endpoint("example.com", 25565)
        self.assertEqual(seen, [("example.com", "backend.example.com", 22100)])

    async def test_rejects_a_non_bool_flag(self) -> None:
        with self.assertRaises(TypeError):
            Bot("example.com", resolve_srv="yes")


if __name__ == "__main__":
    unittest.main()
