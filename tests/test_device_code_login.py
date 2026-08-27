"""Regression tests for the Microsoft device-code login flow.

These cover the OAuth wiring only and deliberately avoid the optional
``cryptography`` extra, so they run on a base install.
"""

from __future__ import annotations

import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from protobot import auth
from protobot.errors import AuthenticationError

PROFILE_HEX = "0123456789abcdef0123456789abcdef"


def _quiet(user_code: str, verification_uri: str) -> None:
    """Prompt callback that keeps test output clean."""


class FakeEndpoints:
    """Stands in for the Microsoft/Xbox/Minecraft HTTP endpoints."""

    def __init__(
        self,
        *,
        pending_polls: int = 0,
        xbl_accepts: str = "t=",
        interval: float = 0,
    ) -> None:
        self.pending_polls = pending_polls
        self.xbl_accepts = xbl_accepts
        self.interval = interval
        self.form_calls: list[tuple[str, dict[str, str]]] = []
        self.json_calls: list[tuple[str, dict]] = []
        self.get_calls: list[str] = []
        self.xbl_tickets: list[str] = []

    @property
    def urls(self) -> list[str]:
        return (
            [url for url, _ in self.form_calls]
            + [url for url, _ in self.json_calls]
            + list(self.get_calls)
        )

    def post_form(self, url: str, params: dict[str, str]) -> tuple[int, dict]:
        self.form_calls.append((url, params))
        if url == auth._MSA_DEVICE_CODE_URL:
            return 200, {
                "device_code": "DEVICE-CODE",
                "user_code": "ABCD1234",
                "verification_uri": "https://www.microsoft.com/link",
                "interval": self.interval,
                "expires_in": 900,
            }
        if url == auth._MSA_TOKEN_URL:
            if params.get("grant_type") == "refresh_token":
                return 200, {
                    "access_token": "ms-token-refreshed",
                    "refresh_token": "refresh-token-2",
                }
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return 400, {"error": "authorization_pending"}
            return 200, {"access_token": "ms-token", "refresh_token": "refresh-token-1"}
        raise AssertionError(f"unexpected form POST to {url}")

    def post_json(self, url: str, payload: dict, *, headers=None) -> tuple[int, dict]:
        self.json_calls.append((url, payload))
        if url == auth._XBL_AUTH_URL:
            ticket = payload["Properties"]["RpsTicket"]
            self.xbl_tickets.append(ticket)
            if not ticket.startswith(self.xbl_accepts):
                return 400, {"error": "wrong ticket format"}
            return 200, {"Token": "xbl-token", "DisplayClaims": {"xui": [{"uhs": "user-hash"}]}}
        if url == auth._XSTS_AUTH_URL:
            return 200, {"Token": "xsts-token"}
        if url == auth._MC_LOGIN_URL:
            return 200, {"access_token": "mc-token", "expires_in": 86400}
        raise AssertionError(f"unexpected JSON POST to {url}")

    def get_json(self, url: str, *, headers=None) -> tuple[int, dict]:
        self.get_calls.append(url)
        if url == auth._MC_PROFILE_URL:
            return 200, {"id": PROFILE_HEX, "name": "Steve"}
        raise AssertionError(f"unexpected GET to {url}")

    def install(self):
        return (
            patch.object(auth, "_http_post_form", self.post_form),
            patch.object(auth, "_http_post_json", self.post_json),
            patch.object(auth, "_http_get_json", self.get_json),
        )


class DeviceCodeLoginTest(unittest.IsolatedAsyncioTestCase):
    async def _login(self, fake: FakeEndpoints, **kwargs):
        patches = fake.install()
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in patches])
        kwargs.setdefault("prompt_callback", _quiet)
        return await auth.device_code_login(**kwargs)

    async def test_uses_msa_endpoints_not_azure_ad(self) -> None:
        """The legacy launcher client ID only works against login.live.com.

        Posting it to login.microsoftonline.com fails with AADSTS700016, which
        made the whole flow unusable.
        """
        fake = FakeEndpoints()
        await self._login(fake)

        device_url, device_params = fake.form_calls[0]
        self.assertEqual(device_url, "https://login.live.com/oauth20_connect.srf")
        self.assertEqual(device_params["scope"], "service::user.auth.xboxlive.com::MBI_SSL")
        self.assertEqual(device_params["response_type"], "device_code")
        self.assertEqual(device_params["client_id"], auth.DEFAULT_MICROSOFT_CLIENT_ID)

        token_url, token_params = fake.form_calls[1]
        self.assertEqual(token_url, "https://login.live.com/oauth20_token.srf")
        self.assertEqual(token_params["grant_type"], auth._DEVICE_CODE_GRANT)
        self.assertEqual(token_params["device_code"], "DEVICE-CODE")

        for url in fake.urls:
            self.assertNotIn("login.microsoftonline.com", url)

    async def test_returns_profile_with_refresh_token_and_expiry(self) -> None:
        fake = FakeEndpoints()
        before = time.time()
        profile = await self._login(fake)

        self.assertEqual(profile.name, "Steve")
        self.assertEqual(profile.id, uuid.UUID(hex=PROFILE_HEX))
        self.assertEqual(profile.access_token, "mc-token")
        self.assertEqual(profile.refresh_token, "refresh-token-1")
        self.assertGreaterEqual(profile.expires_at, before + 86400 - 5)
        self.assertFalse(profile.expired)

    async def test_prompt_callback_receives_code_and_uri(self) -> None:
        seen: list[tuple[str, str]] = []
        fake = FakeEndpoints()
        await self._login(fake, prompt_callback=lambda code, uri: seen.append((code, uri)))
        self.assertEqual(seen, [("ABCD1234", "https://www.microsoft.com/link")])

    async def test_polls_until_user_authorizes(self) -> None:
        fake = FakeEndpoints(pending_polls=3)
        profile = await self._login(fake)
        self.assertEqual(profile.access_token, "mc-token")
        # one device-code request plus four token polls
        self.assertEqual(len(fake.form_calls), 5)

    async def test_device_code_request_failure_raises(self) -> None:
        aadsts = {
            "error": "unauthorized_client",
            "error_description": "AADSTS700016: Application with identifier ... not found",
        }
        with patch.object(auth, "_http_post_form", lambda url, params: (400, aadsts)):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth.device_code_login(prompt_callback=_quiet)
        self.assertIn("device code request failed", str(ctx.exception))
        self.assertIn("AADSTS700016", str(ctx.exception))

    async def test_declined_login_raises(self) -> None:
        def post_form(url, params):
            if url == auth._MSA_DEVICE_CODE_URL:
                return 200, {
                    "device_code": "D",
                    "user_code": "U",
                    "interval": 0,
                    "expires_in": 900,
                }
            return 400, {"error": "authorization_declined"}

        with patch.object(auth, "_http_post_form", post_form):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth.device_code_login(prompt_callback=_quiet)
        self.assertIn("declined", str(ctx.exception))

    async def test_expired_device_code_raises(self) -> None:
        def post_form(url, params):
            if url == auth._MSA_DEVICE_CODE_URL:
                return 200, {
                    "device_code": "D",
                    "user_code": "U",
                    "interval": 0,
                    "expires_in": 900,
                }
            return 400, {"error": "expired_token"}

        with patch.object(auth, "_http_post_form", post_form):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth.device_code_login(prompt_callback=_quiet)
        self.assertIn("expired", str(ctx.exception))

    async def test_slow_down_backs_off_and_retries(self) -> None:
        state = {"slowed": False}

        def post_form(url, params):
            if url == auth._MSA_DEVICE_CODE_URL:
                return 200, {
                    "device_code": "D",
                    "user_code": "U",
                    "interval": 0,
                    "expires_in": 900,
                }
            if not state["slowed"]:
                state["slowed"] = True
                return 400, {"error": "slow_down"}
            return 200, {"access_token": "ms-token"}

        fake = FakeEndpoints()
        with patch.object(auth, "_http_post_form", post_form), \
             patch.object(auth, "_http_post_json", fake.post_json), \
             patch.object(auth, "_http_get_json", fake.get_json), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            profile = await auth.device_code_login(prompt_callback=_quiet)
        self.assertEqual(profile.access_token, "mc-token")
        self.assertTrue(state["slowed"])


class XboxLiveTicketTest(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_msa_ticket_prefix(self) -> None:
        fake = FakeEndpoints(xbl_accepts="t=")
        with patch.object(auth, "_http_post_json", fake.post_json):
            token, user_hash = await auth._authenticate_xbox_live("ms-token")
        self.assertEqual(token, "xbl-token")
        self.assertEqual(user_hash, "user-hash")
        self.assertEqual(fake.xbl_tickets, ["t=ms-token"])

    async def test_falls_back_to_azure_ad_ticket_prefix(self) -> None:
        fake = FakeEndpoints(xbl_accepts="d=")
        with patch.object(auth, "_http_post_json", fake.post_json):
            token, _ = await auth._authenticate_xbox_live("ms-token")
        self.assertEqual(token, "xbl-token")
        self.assertEqual(fake.xbl_tickets, ["t=ms-token", "d=ms-token"])

    async def test_raises_when_both_prefixes_rejected(self) -> None:
        fake = FakeEndpoints(xbl_accepts="never")
        with patch.object(auth, "_http_post_json", fake.post_json):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth._authenticate_xbox_live("ms-token")
        self.assertIn("Xbox Live authentication failed", str(ctx.exception))
        self.assertEqual(len(fake.xbl_tickets), 2)


class XstsErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_xbox_account_is_explained(self) -> None:
        def post_json(url, payload, *, headers=None):
            return 401, {"XErr": 2148916233}

        with patch.object(auth, "_http_post_json", post_json):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth._authorize_xsts("xbl-token")
        self.assertIn("no Xbox profile", str(ctx.exception))

    async def test_child_account_is_explained(self) -> None:
        def post_json(url, payload, *, headers=None):
            return 401, {"XErr": 2148916238}

        with patch.object(auth, "_http_post_json", post_json):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth._authorize_xsts("xbl-token")
        self.assertIn("child account", str(ctx.exception))


class RefreshLoginTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_uses_refresh_grant_and_rotates_token(self) -> None:
        fake = FakeEndpoints()
        with patch.object(auth, "_http_post_form", fake.post_form), \
             patch.object(auth, "_http_post_json", fake.post_json), \
             patch.object(auth, "_http_get_json", fake.get_json):
            profile = await auth.refresh_login("refresh-token-1")

        url, params = fake.form_calls[0]
        self.assertEqual(url, "https://login.live.com/oauth20_token.srf")
        self.assertEqual(params["grant_type"], "refresh_token")
        self.assertEqual(params["refresh_token"], "refresh-token-1")
        self.assertEqual(profile.access_token, "mc-token")
        self.assertEqual(profile.refresh_token, "refresh-token-2")

    async def test_refresh_keeps_old_token_when_none_returned(self) -> None:
        def post_form(url, params):
            return 200, {"access_token": "ms-token"}

        fake = FakeEndpoints()
        with patch.object(auth, "_http_post_form", post_form), \
             patch.object(auth, "_http_post_json", fake.post_json), \
             patch.object(auth, "_http_get_json", fake.get_json):
            profile = await auth.refresh_login("still-valid")
        self.assertEqual(profile.refresh_token, "still-valid")

    async def test_revoked_refresh_token_raises(self) -> None:
        def post_form(url, params):
            return 400, {"error": "invalid_grant", "error_description": "Token revoked."}

        with patch.object(auth, "_http_post_form", post_form):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth.refresh_login("revoked")
        self.assertIn("Token revoked", str(ctx.exception))


class ProfileExpiryTest(unittest.TestCase):
    def _profile(self, expires_at: float) -> auth.MinecraftProfile:
        return auth.MinecraftProfile(
            id=uuid.uuid4(),
            name="Steve",
            access_token="token",
            refresh_token="refresh",
            expires_at=expires_at,
        )

    def test_fresh_token_is_not_expired(self) -> None:
        self.assertFalse(self._profile(time.time() + 3600).expired)

    def test_past_token_is_expired(self) -> None:
        self.assertTrue(self._profile(time.time() - 1).expired)

    def test_token_inside_safety_margin_is_expired(self) -> None:
        self.assertTrue(self._profile(time.time() + 5).expired)

    def test_legacy_cache_without_expiry_is_expired(self) -> None:
        """Caches written before expiry tracking must trigger a refresh."""
        legacy = auth.MinecraftProfile(id=uuid.uuid4(), name="Steve", access_token="token")
        self.assertTrue(legacy.expired)
        self.assertIsNone(legacy.refresh_token)


if __name__ == "__main__":
    unittest.main()
