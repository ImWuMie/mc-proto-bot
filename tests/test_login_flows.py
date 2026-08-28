"""Regression tests for the Microsoft sign-in flows.

These cover the OAuth wiring only and deliberately avoid the optional
``cryptography`` extra, so they run on a base install.
"""

from __future__ import annotations

import time
import unittest
import urllib.parse
import uuid
from unittest.mock import AsyncMock, patch

from protobot import auth
from protobot.errors import AuthenticationError

PROFILE_HEX = "0123456789abcdef0123456789abcdef"
AZURE_CLIENT_ID = "11111111-2222-3333-4444-555555555555"
REDIRECT = "https://login.live.com/oauth20_desktop.srf"
MSA_TOKEN = "https://login.live.com/oauth20_token.srf"
AAD_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"


def _quiet_device_prompt(user_code: str, verification_uri: str) -> None:
    """Device-code prompt callback that keeps test output clean."""


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
        if url in (auth._MSA_DEVICE_CODE_URL, auth._AAD_DEVICE_CODE_URL):
            return 200, {
                "device_code": "DEVICE-CODE",
                "user_code": "ABCD1234",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": self.interval,
                "expires_in": 900,
            }
        if url in (auth._MSA_TOKEN_URL, auth._AAD_TOKEN_URL):
            grant = params.get("grant_type")
            if grant == "refresh_token":
                return 200, {
                    "access_token": "ms-token-refreshed",
                    "refresh_token": "refresh-token-2",
                }
            if grant == "authorization_code":
                return 200, {"access_token": "ms-token", "refresh_token": "refresh-token-1"}
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

    def install(self, test: unittest.TestCase) -> None:
        for target, replacement in (
            ("_http_post_form", self.post_form),
            ("_http_post_json", self.post_json),
            ("_http_get_json", self.get_json),
        ):
            patcher = patch.object(auth, target, replacement)
            patcher.start()
            test.addCleanup(patcher.stop)


class EndpointFamilyTest(unittest.TestCase):
    """The launcher client ID lives on MSA; anything else is an Azure app."""

    def test_default_client_infers_msa(self) -> None:
        self.assertFalse(auth._uses_azure_ad(auth.DEFAULT_MICROSOFT_CLIENT_ID, None))

    def test_other_client_infers_azure(self) -> None:
        self.assertTrue(auth._uses_azure_ad(AZURE_CLIENT_ID, None))

    def test_explicit_choice_wins(self) -> None:
        self.assertTrue(auth._uses_azure_ad(auth.DEFAULT_MICROSOFT_CLIENT_ID, True))
        self.assertFalse(auth._uses_azure_ad(AZURE_CLIENT_ID, False))


class AuthorizationUrlTest(unittest.TestCase):
    def test_targets_the_msa_authorize_endpoint(self) -> None:
        split = urllib.parse.urlsplit(auth.authorization_url())
        self.assertEqual(
            f"{split.scheme}://{split.netloc}{split.path}",
            "https://login.live.com/oauth20_authorize.srf",
        )
        params = urllib.parse.parse_qs(split.query)
        self.assertEqual(params["client_id"], [auth.DEFAULT_MICROSOFT_CLIENT_ID])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["redirect_uri"], [REDIRECT])
        self.assertEqual(params["scope"], ["service::user.auth.xboxlive.com::MBI_SSL"])

    def test_honours_a_custom_client_and_redirect(self) -> None:
        url = auth.authorization_url("my-client", redirect_uri="http://localhost:9000/cb")
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(params["client_id"], ["my-client"])
        self.assertEqual(params["redirect_uri"], ["http://localhost:9000/cb"])


class ExtractAuthorizationCodeTest(unittest.TestCase):
    def test_extracts_from_full_redirect_url(self) -> None:
        pasted = f"{REDIRECT}?code=M.C5_BAY.2.U.abcdef&lc=2052"
        self.assertEqual(auth.extract_authorization_code(pasted), "M.C5_BAY.2.U.abcdef")

    def test_accepts_a_bare_code(self) -> None:
        self.assertEqual(
            auth.extract_authorization_code("  M.C5_BAY.2.U.xyz  "), "M.C5_BAY.2.U.xyz"
        )

    def test_reports_an_error_carried_in_the_redirect(self) -> None:
        pasted = f"{REDIRECT}?error=access_denied&error_description=User+cancelled"
        with self.assertRaises(AuthenticationError) as ctx:
            auth.extract_authorization_code(pasted)
        self.assertIn("User cancelled", str(ctx.exception))

    def test_rejects_a_url_without_a_code(self) -> None:
        with self.assertRaises(AuthenticationError) as ctx:
            auth.extract_authorization_code("https://login.live.com/oauth20_authorize.srf?x=1")
        self.assertIn("no authorization code", str(ctx.exception))

    def test_rejects_empty_input(self) -> None:
        with self.assertRaises(AuthenticationError):
            auth.extract_authorization_code("   ")


class AuthorizationCodeLoginTest(unittest.IsolatedAsyncioTestCase):
    async def test_exchanges_code_at_the_msa_token_endpoint(self) -> None:
        fake = FakeEndpoints()
        fake.install(self)
        prompts: list[str] = []

        profile = await auth.authorization_code_login(
            prompt_callback=lambda url: (prompts.append(url), f"{REDIRECT}?code=THE-CODE")[1]
        )

        self.assertIn("login.live.com/oauth20_authorize.srf", prompts[0])
        url, params = fake.form_calls[0]
        self.assertEqual(url, MSA_TOKEN)
        self.assertEqual(params["grant_type"], "authorization_code")
        self.assertEqual(params["code"], "THE-CODE")
        self.assertEqual(params["redirect_uri"], REDIRECT)

        self.assertEqual(profile.name, "Steve")
        self.assertEqual(profile.id, uuid.UUID(hex=PROFILE_HEX))
        self.assertEqual(profile.access_token, "mc-token")
        self.assertEqual(profile.refresh_token, "refresh-token-1")
        self.assertFalse(profile.expired)

    async def test_never_contacts_azure_ad(self) -> None:
        fake = FakeEndpoints()
        fake.install(self)
        await auth.authorization_code_login(
            prompt_callback=lambda url: f"{REDIRECT}?code=THE-CODE"
        )
        for url in fake.urls:
            self.assertNotIn("login.microsoftonline.com", url)

    async def test_rejected_code_raises_with_guidance(self) -> None:
        def post_form(url, params):
            return 400, {
                "error": "invalid_grant",
                "error_description": "The provided value for the 'code' parameter is not valid.",
            }

        with patch.object(auth, "_http_post_form", post_form):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth.authorization_code_login(
                    prompt_callback=lambda url: f"{REDIRECT}?code=STALE"
                )
        message = str(ctx.exception)
        self.assertIn("code exchange failed", message)
        self.assertIn("single-use", message)


class MsaDeviceCodeLoginTest(unittest.IsolatedAsyncioTestCase):
    """Default device-code login needs no registration and uses MSA."""

    async def _login(self, fake: FakeEndpoints, **kwargs):
        fake.install(self)
        kwargs.setdefault("prompt_callback", _quiet_device_prompt)
        return await auth.device_code_login(**kwargs)

    async def test_uses_msa_endpoints(self) -> None:
        fake = FakeEndpoints()
        await self._login(fake)

        device_url, device_params = fake.form_calls[0]
        self.assertEqual(device_url, "https://login.live.com/oauth20_connect.srf")
        self.assertEqual(device_params["scope"], "service::user.auth.xboxlive.com::MBI_SSL")
        self.assertEqual(device_params["response_type"], "device_code")
        self.assertEqual(device_params["client_id"], auth.DEFAULT_MICROSOFT_CLIENT_ID)

        token_url, token_params = fake.form_calls[1]
        self.assertEqual(token_url, MSA_TOKEN)
        self.assertEqual(token_params["grant_type"], auth._DEVICE_CODE_GRANT)
        self.assertEqual(token_params["device_code"], "DEVICE-CODE")

        for url in fake.urls:
            self.assertNotIn("login.microsoftonline.com", url)

    async def test_verification_uri_prefills_the_code(self) -> None:
        """microsoft.com/link redirects to remoteconnect; link the code directly."""
        seen: list[tuple[str, str]] = []
        fake = FakeEndpoints()
        await self._login(fake, prompt_callback=lambda code, uri: seen.append((code, uri)))
        user_code, uri = seen[0]
        self.assertEqual(user_code, "ABCD1234")
        self.assertEqual(
            uri, "https://login.live.com/oauth20_remoteconnect.srf?otc=ABCD1234"
        )

    async def test_returns_profile_with_refresh_token_and_expiry(self) -> None:
        fake = FakeEndpoints()
        before = time.time()
        profile = await self._login(fake)
        self.assertEqual(profile.access_token, "mc-token")
        self.assertEqual(profile.refresh_token, "refresh-token-1")
        self.assertGreaterEqual(profile.expires_at, before + 86400 - 5)

    async def test_polls_until_user_authorizes(self) -> None:
        fake = FakeEndpoints(pending_polls=3)
        profile = await self._login(fake)
        self.assertEqual(profile.access_token, "mc-token")
        # one device-code request plus four token polls
        self.assertEqual(len(fake.form_calls), 5)

    async def test_incomplete_signin_explains_what_to_do(self) -> None:
        """invalid_grant here means the browser sign-in did not finish."""

        def post_form(url, params):
            if url == auth._MSA_DEVICE_CODE_URL:
                return 200, {
                    "device_code": "D",
                    "user_code": "U",
                    "interval": 0,
                    "expires_in": 900,
                }
            return 400, {
                "error": "invalid_grant",
                "error_description": "The user could not be authenticated.",
            }

        with patch.object(auth, "_http_post_form", post_form):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth.device_code_login(prompt_callback=_quiet_device_prompt)
        message = str(ctx.exception)
        self.assertIn("did not finish", message)
        self.assertIn("authorization_code_login", message)

    async def test_rejects_azure_ad_with_the_launcher_client_id(self) -> None:
        calls: list[str] = []
        with patch.object(auth, "_http_post_form", lambda url, params: calls.append(url)):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth.device_code_login(azure_ad=True)
        self.assertIn("AADSTS700016", str(ctx.exception))
        self.assertEqual(calls, [], "must refuse before making a request")


class AzureDeviceCodeLoginTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_azure_endpoints_for_a_registered_app(self) -> None:
        fake = FakeEndpoints()
        fake.install(self)
        await auth.device_code_login(
            AZURE_CLIENT_ID, prompt_callback=_quiet_device_prompt
        )

        device_url, device_params = fake.form_calls[0]
        self.assertEqual(
            device_url,
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode",
        )
        self.assertEqual(device_params["scope"], "XboxLive.signin offline_access")
        self.assertNotIn("response_type", device_params)
        self.assertEqual(fake.form_calls[1][0], AAD_TOKEN)

    async def test_uses_the_server_supplied_verification_uri(self) -> None:
        seen: list[str] = []
        fake = FakeEndpoints()
        fake.install(self)
        await auth.device_code_login(
            AZURE_CLIENT_ID, prompt_callback=lambda code, uri: seen.append(uri)
        )
        self.assertEqual(seen, ["https://microsoft.com/devicelogin"])

    async def test_device_code_request_failure_raises(self) -> None:
        aadsts = {
            "error": "unauthorized_client",
            "error_description": "AADSTS700016: Application with identifier ... not found",
        }
        with patch.object(auth, "_http_post_form", lambda url, params: (400, aadsts)):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth.device_code_login(
                    AZURE_CLIENT_ID, prompt_callback=_quiet_device_prompt
                )
        self.assertIn("device code request failed", str(ctx.exception))
        self.assertIn("AADSTS700016", str(ctx.exception))

    async def _poll_error(self, error: str) -> str:
        def post_form(url, params):
            if url == auth._AAD_DEVICE_CODE_URL:
                return 200, {
                    "device_code": "D",
                    "user_code": "U",
                    "interval": 0,
                    "expires_in": 900,
                }
            return 400, {"error": error}

        with patch.object(auth, "_http_post_form", post_form):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth.device_code_login(
                    AZURE_CLIENT_ID, prompt_callback=_quiet_device_prompt
                )
        return str(ctx.exception)

    async def test_declined_login_raises(self) -> None:
        self.assertIn("declined", await self._poll_error("authorization_declined"))

    async def test_expired_device_code_raises(self) -> None:
        self.assertIn("expired", await self._poll_error("expired_token"))

    async def test_slow_down_backs_off_and_retries(self) -> None:
        state = {"slowed": False}

        def post_form(url, params):
            if url == auth._AAD_DEVICE_CODE_URL:
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
            profile = await auth.device_code_login(
                AZURE_CLIENT_ID, prompt_callback=_quiet_device_prompt
            )
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
    async def _xerr(self, code: int) -> str:
        def post_json(url, payload, *, headers=None):
            return 401, {"XErr": code}

        with patch.object(auth, "_http_post_json", post_json):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth._authorize_xsts("xbl-token")
        return str(ctx.exception)

    async def test_missing_xbox_account_is_explained(self) -> None:
        self.assertIn("no Xbox profile", await self._xerr(2148916233))

    async def test_region_block_is_explained(self) -> None:
        self.assertIn("region", await self._xerr(2148916235))

    async def test_child_account_is_explained(self) -> None:
        self.assertIn("child account", await self._xerr(2148916238))


class MinecraftProfileFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_unowned_game_is_explained(self) -> None:
        fake = FakeEndpoints()

        def get_json(url, *, headers=None):
            return 404, {"path": "/minecraft/profile"}

        with patch.object(auth, "_http_post_json", fake.post_json), \
             patch.object(auth, "_http_get_json", get_json):
            with self.assertRaises(AuthenticationError) as ctx:
                await auth._minecraft_profile("ms-token", None)
        self.assertIn("does not own Minecraft", str(ctx.exception))


class RefreshLoginTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_client_refreshes_against_msa(self) -> None:
        fake = FakeEndpoints()
        fake.install(self)
        profile = await auth.refresh_login("refresh-token-1")

        url, params = fake.form_calls[0]
        self.assertEqual(url, MSA_TOKEN)
        self.assertEqual(params["grant_type"], "refresh_token")
        self.assertEqual(params["refresh_token"], "refresh-token-1")
        # login.live.com 的续期同样要 scope：少了它返回 HTTP 400
        # "The provided request must include a 'scope' input parameter."
        self.assertEqual(params["scope"], "service::user.auth.xboxlive.com::MBI_SSL")
        self.assertEqual(profile.refresh_token, "refresh-token-2")

    async def test_refresh_scope_matches_the_login_scope(self) -> None:
        # 续期的 scope 必须和当初签发时用的一致，否则微软拒收
        fake = FakeEndpoints()
        fake.install(self)
        await auth.device_code_login(prompt_callback=_quiet_device_prompt)
        login_scope = fake.form_calls[0][1]["scope"]
        fake.form_calls.clear()
        await auth.refresh_login("refresh-token-1")
        self.assertEqual(fake.form_calls[0][1]["scope"], login_scope)

    async def test_azure_client_refreshes_against_azure(self) -> None:
        fake = FakeEndpoints()
        fake.install(self)
        await auth.refresh_login("refresh-token-1", AZURE_CLIENT_ID)

        url, params = fake.form_calls[0]
        self.assertEqual(url, AAD_TOKEN)
        self.assertEqual(params["scope"], "XboxLive.signin offline_access")

    async def test_explicit_flag_overrides_inference(self) -> None:
        fake = FakeEndpoints()
        fake.install(self)
        await auth.refresh_login("refresh-token-1", AZURE_CLIENT_ID, azure_ad=False)
        self.assertEqual(fake.form_calls[0][0], MSA_TOKEN)

    async def test_refresh_keeps_old_token_when_none_returned(self) -> None:
        fake = FakeEndpoints()
        with patch.object(auth, "_http_post_form", lambda url, params: (200, {"access_token": "ms"})), \
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
