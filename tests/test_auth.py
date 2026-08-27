"""Unit tests for Minecraft online-mode authentication and encryption."""

from __future__ import annotations

import asyncio
import secrets
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ImportError as error:  # pragma: no cover - depends on the install extra
    raise unittest.SkipTest(
        "online-mode tests require the optional 'cryptography' extra "
        "(uv sync --extra online)"
    ) from error

from protobot.auth import (
    StreamCipher,
    join_session_server,
    minecraft_sha1_digest,
    rsa_encrypt,
)
from protobot.client import Bot
from protobot.errors import AuthenticationError, OnlineModeRequired
from protobot.protocol.codec import PacketReader, PacketWriter
from protobot.protocol.connection import ConnectionState, ProtocolConnection


class MinecraftSha1DigestTest(unittest.TestCase):
    def test_known_vectors(self) -> None:
        # Standard Notchian / Minecraft test vectors
        # "Notch" -> 4ed1f46bbe04bc756bcb17c0c7ce3e4632f06a48
        self.assertEqual(
            minecraft_sha1_digest("Notch"),
            "4ed1f46bbe04bc756bcb17c0c7ce3e4632f06a48",
        )
        # "jeb_" -> -7c9d5b0044c130109a5d7b5fb5c317c02b4e28c1
        self.assertEqual(
            minecraft_sha1_digest("jeb_"),
            "-7c9d5b0044c130109a5d7b5fb5c317c02b4e28c1",
        )
        # "simon" -> 88e16a1019277b15d58faf0541e11910eb756f6
        self.assertEqual(
            minecraft_sha1_digest("simon"),
            "88e16a1019277b15d58faf0541e11910eb756f6",
        )

    def test_multi_part_digest(self) -> None:
        server_id = "test_server"
        shared_secret = b"\x01\x02\x03\x04" * 4
        public_key = b"\xAA\xBB\xCC\xDD" * 8
        combined = server_id.encode("utf-8") + shared_secret + public_key
        self.assertEqual(
            minecraft_sha1_digest(server_id, shared_secret, public_key),
            minecraft_sha1_digest(combined),
        )


class RsaEncryptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=1024,
        )
        self.public_der = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def test_rsa_encrypt_roundtrip(self) -> None:
        data = b"hello_minecraft_shared_secret_16"
        encrypted = rsa_encrypt(self.public_der, data)
        decrypted = self.private_key.decrypt(encrypted, padding.PKCS1v15())
        self.assertEqual(decrypted, data)


class StreamCipherTest(unittest.TestCase):
    def test_cfb8_stream_roundtrip(self) -> None:
        secret = secrets.token_bytes(16)
        cipher_send = StreamCipher(secret)
        cipher_recv = StreamCipher(secret)

        chunks = [
            b"first small packet",
            b"\x00\x01\x02\x03\x04" * 50,
            b"single byte by byte",
        ]
        for chunk in chunks:
            encrypted = cipher_send.encrypt(chunk)
            decrypted = cipher_recv.decrypt(encrypted)
            self.assertEqual(decrypted, chunk)

    def test_byte_by_byte_decryption_state(self) -> None:
        secret = secrets.token_bytes(16)
        cipher_send = StreamCipher(secret)
        cipher_recv = StreamCipher(secret)

        message = b"continuous stream of bytes 123456789"
        encrypted = cipher_send.encrypt(message)

        # Decrypt byte-by-byte
        decrypted = bytearray()
        for i in range(len(encrypted)):
            decrypted.extend(cipher_recv.decrypt(bytes([encrypted[i]])))
        self.assertEqual(bytes(decrypted), message)

    def test_rejects_wrong_key_length(self) -> None:
        with self.assertRaises(ValueError):
            StreamCipher(secrets.token_bytes(15))


class SessionServerJoinTest(unittest.IsolatedAsyncioTestCase):
    @patch("protobot.auth._http_post_json")
    async def test_join_success_204(self, mock_post: MagicMock) -> None:
        mock_post.return_value = (204, {})
        user_uuid = uuid.uuid4()
        await join_session_server("token123", user_uuid, "hash456")
        mock_post.assert_called_once_with(
            "https://sessionserver.mojang.com/session/minecraft/join",
            {
                "accessToken": "token123",
                "selectedProfile": user_uuid.hex,
                "serverId": "hash456",
            },
        )

    @patch("protobot.auth._http_post_json")
    async def test_join_failure_403(self, mock_post: MagicMock) -> None:
        mock_post.return_value = (403, {"errorMessage": "Invalid token."})
        with self.assertRaises(AuthenticationError) as ctx:
            await join_session_server("bad_token", uuid.uuid4(), "hash")
        self.assertIn("Invalid token", str(ctx.exception))


class ConnectionEncryptionIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_packets_over_stream(self) -> None:
        from protobot.protocol.framing import encode_frame

        reader = asyncio.StreamReader()
        client_conn = ProtocolConnection()
        shared_secret = secrets.token_bytes(16)

        # Build an incoming encrypted frame exactly as a server would.
        cipher_server = StreamCipher(shared_secret)
        raw_packet_payload = b"hello_world"
        plaintext_frame = encode_frame(0x02, raw_packet_payload, None)
        encrypted_frame = cipher_server.encrypt(plaintext_frame)

        reader.feed_data(encrypted_frame)
        reader.feed_eof()

        client_conn.reader = reader
        client_conn.enable_encryption(shared_secret)

        packet = await client_conn.receive_packet(ConnectionState.LOGIN)
        self.assertEqual(packet.packet_id, 0x02)
        self.assertEqual(packet.payload, raw_packet_payload)


class ClientEncryptionHandlerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=1024,
        )
        self.public_der = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @patch("protobot.auth.join_session_server", new_callable=AsyncMock)
    async def test_handle_encryption_request_online(self, mock_join: AsyncMock) -> None:
        token = "valid_access_token"
        account_uuid = uuid.uuid4()
        bot = Bot("127.0.0.1", username="Player1", access_token=token, profile_uuid=account_uuid)
        bot._connection.send_packet = AsyncMock()

        # Build server Encryption Request (0x01)
        verify_token = b"verify1234"
        req_payload = (
            PacketWriter()
            .write_string("server_hash_id")
            .write_bytes(self.public_der)
            .write_bytes(verify_token)
            .write_bool(True)
            .to_bytes()
        )

        await bot._handle_encryption_request(req_payload)

        # Verify join_session_server was called
        mock_join.assert_called_once()
        # Verify Encryption Response (0x01) was sent
        bot._connection.send_packet.assert_called_once()
        call_args = bot._connection.send_packet.call_args[0]
        self.assertEqual(call_args[0], 0x01)

        # Verify decrypted payload contains valid secrets
        resp_reader = PacketReader(call_args[1])
        enc_secret = resp_reader.read_bytes()
        enc_token = resp_reader.read_bytes()
        dec_secret = self.private_key.decrypt(enc_secret, padding.PKCS1v15())
        dec_token = self.private_key.decrypt(enc_token, padding.PKCS1v15())
        self.assertEqual(len(dec_secret), 16)
        self.assertEqual(dec_token, verify_token)
        self.assertIsNotNone(bot._connection._cipher)

    async def test_handle_encryption_request_missing_token_raises(self) -> None:
        bot = Bot("127.0.0.1", username="Player1")  # no access token
        req_payload = (
            PacketWriter()
            .write_string("")
            .write_bytes(self.public_der)
            .write_bytes(b"token")
            .write_bool(True)
            .to_bytes()
        )
        with self.assertRaises(OnlineModeRequired):
            await bot._handle_encryption_request(req_payload)


if __name__ == "__main__":
    unittest.main()
