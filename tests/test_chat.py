from __future__ import annotations

import base64
import unittest
import uuid
from unittest.mock import patch

from protobot.auth import fetch_player_certificate
from protobot.client import Bot
from protobot.errors import OnlineModeRequired
from protobot.protocol import PacketReader, PacketWriter
from protobot.protocol.connection import ConnectionState


def _anonymous_nbt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return b"\x08" + len(raw).to_bytes(2, "big") + raw


def _player_chat_packet(signature: bytes, *, content: str = "hello") -> bytes:
    return (
        PacketWriter()
        .write_varint(0)  # global index
        .write_uuid(uuid.UUID("12345678-1234-5678-1234-567812345678"))
        .write_varint(0)  # per-sender index
        .write_bool(True)
        .write_raw(signature)
        .write_string(content, max_chars=256)
        .write_long(1_700_000_000_000)
        .write_long(1234)
        .write_varint(0)  # packed last-seen signatures
        .write_bool(False)  # no unsigned content
        .write_varint(0)  # pass-through filter mask
        .write_varint(1)  # chat type registry id 0
        .write_raw(_anonymous_nbt_string("Alice"))
        .write_bool(False)  # no target name
        .to_bytes()
    )


def _read_chat_update(payload: bytes) -> tuple[int, bytes, int, bytes | None]:
    reader = PacketReader(payload)
    reader.read_string(max_chars=256)
    reader.read_long()
    reader.read_long()
    signature = reader.read_raw(256) if reader.read_bool() else None
    offset = reader.read_varint()
    acknowledged = reader.read_raw(3)
    checksum = reader.read_unsigned_byte()
    reader.expect_end()
    return offset, acknowledged, checksum, signature


class _RecordingCertificate:
    def __init__(self) -> None:
        self.public_key_der = b"public-key"
        self.key_signature = b"mojang-signature"
        self.expires_at_ms = 4_000_000_000_000
        self.refreshed_after_ms = 3_000_000_000_000
        self.payloads: list[bytes] = []

    @property
    def expired(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        self.payloads.append(payload)
        return bytes([0xA5]) * 256


class ChatAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self) -> Bot:
        bot = Bot("localhost", username="ChatTest", version="26.2")
        bot.state = ConnectionState.PLAY
        return bot

    async def test_immediate_reply_acknowledges_triggering_message(self) -> None:
        bot = self.make_bot()
        sent: list[tuple[int, bytes]] = []

        async def capture(packet_id: int, payload: bytes = b"") -> None:
            sent.append((packet_id, payload))

        async def reply(*_args: object) -> None:
            await bot.send_message("reply")

        bot.send_raw = capture  # type: ignore[method-assign]
        bot.on("player_chat", reply)

        await bot._handle_player_chat(_player_chat_packet(bytes([1]) * 256))

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], bot.version.packets.serverbound_chat)
        self.assertEqual(
            _read_chat_update(sent[0][1]),
            (1, b"\x00\x00\x08", 32, None),
        )

    async def test_large_pending_window_sends_standalone_ack(self) -> None:
        bot = self.make_bot()
        sent: list[tuple[int, bytes]] = []

        async def capture(packet_id: int, payload: bytes = b"") -> None:
            sent.append((packet_id, payload))

        bot.send_raw = capture  # type: ignore[method-assign]
        for number in range(65):
            signature = number.to_bytes(256, "big")
            await bot._handle_player_chat(_player_chat_packet(signature))

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], bot.version.packets.serverbound_chat_ack)
        ack = PacketReader(sent[0][1])
        self.assertEqual(ack.read_varint(), 65)
        ack.expect_end()

        await bot.send_message("after ack")
        self.assertEqual(
            _read_chat_update(sent[-1][1]),
            (0, b"\xff\xff\x0f", 11, None),
        )

    async def test_unsigned_player_chat_does_not_advance_window(self) -> None:
        bot = self.make_bot()
        payload = bytearray(_player_chat_packet(bytes(256)))
        signature_flag = 1 + 16 + 1
        payload[signature_flag] = 0
        del payload[signature_flag + 1 : signature_flag + 257]

        await bot._handle_player_chat(bytes(payload))
        self.assertEqual(bot._last_seen_messages.offset, 0)

    async def test_signed_message_uses_vanilla_signature_payload(self) -> None:
        bot = self.make_bot()
        sent: list[tuple[int, bytes]] = []
        certificate = _RecordingCertificate()
        bot._chat_certificate = certificate  # type: ignore[assignment]
        bot._chat_session_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        bot._last_seen_messages.add_pending(bytes([3]) * 256)

        async def capture(packet_id: int, payload: bytes = b"") -> None:
            sent.append((packet_id, payload))

        bot.send_raw = capture  # type: ignore[method-assign]
        with patch("protobot.client.time.time", return_value=1_700_000_000.125), patch(
            "protobot.client.secrets.randbits", return_value=0x0102030405060708
        ):
            await bot.send_message("signed")

        offset, bits, checksum, signature = _read_chat_update(sent[0][1])
        self.assertEqual((offset, bits, signature), (1, b"\x00\x00\x08", bytes([0xA5]) * 256))
        self.assertNotEqual(checksum, 0)

        signed = PacketReader(certificate.payloads[0])
        self.assertEqual(signed.read_int(), 1)
        self.assertEqual(signed.read_uuid(), bot.uuid)
        self.assertEqual(signed.read_uuid(), bot._chat_session_id)
        self.assertEqual(signed.read_int(), 0)
        self.assertEqual(signed.read_unsigned_long(), 0x0102030405060708)
        self.assertEqual(signed.read_long(), 1_700_000_000)
        self.assertEqual(signed.read_int(), len(b"signed"))
        self.assertEqual(signed.read_raw(len(b"signed")), b"signed")
        self.assertEqual(signed.read_int(), 1)
        self.assertEqual(signed.read_raw(256), bytes([3]) * 256)
        signed.expect_end()
        self.assertEqual(bot._chat_message_index, 1)

    async def test_chat_session_registration_packet(self) -> None:
        bot = self.make_bot()
        bot._authenticated_connection = True
        bot._chat_certificate = _RecordingCertificate()  # type: ignore[assignment]
        sent: list[tuple[int, bytes]] = []

        async def capture(packet_id: int, payload: bytes = b"") -> None:
            sent.append((packet_id, payload))

        bot.send_raw = capture  # type: ignore[method-assign]
        await bot._activate_chat_session()

        self.assertEqual(sent[0][0], bot.version.packets.serverbound_chat_session_update)
        reader = PacketReader(sent[0][1])
        self.assertEqual(reader.read_uuid(), bot._chat_session_id)
        self.assertEqual(reader.read_long(), 4_000_000_000_000)
        self.assertEqual(reader.read_bytes(), b"public-key")
        self.assertEqual(reader.read_bytes(), b"mojang-signature")
        reader.expect_end()

    async def test_enforced_secure_chat_never_falls_back_to_unsigned(self) -> None:
        bot = self.make_bot()
        bot.session.enforces_secure_chat = True

        with self.assertRaisesRegex(OnlineModeRequired, "no Mojang player certificate"):
            await bot.send_message("must be signed")


class PlayerCertificateTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_player_certificate_parses_response(self) -> None:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest("cryptography extra is not installed")

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_der = private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        response = {
            "keyPair": {
                # Mojang's endpoint uses these PKCS#1-looking labels around
                # PKCS#8/SPKI DER, which strict PEM loaders reject.
                "privateKey": "-----BEGIN RSA PRIVATE KEY-----\n"
                + base64.b64encode(private_der).decode("ascii")
                + "\n-----END RSA PRIVATE KEY-----\n",
                "publicKey": "-----BEGIN RSA PUBLIC KEY-----\n"
                + base64.b64encode(public_der).decode("ascii")
                + "\n-----END RSA PUBLIC KEY-----\n",
            },
            "publicKeySignatureV2": "c2lnbmF0dXJl",
            "expiresAt": "2099-01-01T00:00:00Z",
            "refreshedAfter": "2098-12-31T23:00:00Z",
        }
        with patch("protobot.auth._http_post_empty_json", return_value=(200, response)):
            certificate = await fetch_player_certificate("token")

        self.assertEqual(certificate.key_signature, b"signature")
        signature = certificate.sign(b"message")
        self.assertEqual(len(signature), 256)
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        private_key.public_key().verify(
            signature,
            b"message",
            padding.PKCS1v15(),
            hashes.SHA256(),
        )


if __name__ == "__main__":
    unittest.main()
