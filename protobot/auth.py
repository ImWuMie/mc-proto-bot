"""Authentication and encryption helpers for Minecraft online-mode."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import AuthenticationError, OnlineModeRequired

# Public client ID of the Minecraft launcher. It belongs to the legacy
# Microsoft-account (MSA) endpoints and supports the *authorization-code* flow
# without any app registration of your own.
#
# It cannot be used for the device-code flow: the MSA device endpoint hands out
# a device code for any scope, but the later token poll fails with "The user
# could not be authenticated or user interaction is required" because this
# client can never be granted the requested scope. Device code needs an
# application you registered in Azure AD -- see ``device_code_login``.
DEFAULT_MICROSOFT_CLIENT_ID = "00000000402b5328"

# Legacy MSA endpoints (authorization-code flow, no registration required).
_MSA_AUTHORIZE_URL = "https://login.live.com/oauth20_authorize.srf"
_MSA_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
_MSA_REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"
_MSA_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"

# Azure AD v2.0 endpoints (device-code flow, needs your own registered app).
_AAD_DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
_AAD_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
_AAD_SCOPE = "XboxLive.signin offline_access"

_DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

_XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
_MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
_MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

# Refresh a little early so a token cannot expire mid-handshake.
_TOKEN_EXPIRY_MARGIN = 60.0


def minecraft_sha1_digest(*data: bytes | str) -> str:
    """Compute the two's-complement hexadecimal SHA-1 digest used by Minecraft.

    Minecraft interprets the 160-bit SHA-1 output as a signed big-endian integer.
    Negative values are formatted with a leading '-' sign without leading zeros.
    """
    hasher = hashlib.sha1()
    for item in data:
        if isinstance(item, str):
            hasher.update(item.encode("utf-8"))
        else:
            hasher.update(item)
    digest = hasher.digest()
    value = int.from_bytes(digest, byteorder="big", signed=True)
    if value < 0:
        return f"-{-value:x}"
    return f"{value:x}"


def _ensure_cryptography():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        return {
            "hashes": hashes,
            "serialization": serialization,
            "padding": padding,
            "Cipher": Cipher,
            "algorithms": algorithms,
            "modes": modes,
        }
    except ImportError as error:
        raise OnlineModeRequired(
            "Online mode encryption requires the 'cryptography' package. "
            "Install it via: pip install protobot[online] (or pip install cryptography)"
        ) from error


def rsa_encrypt(der_public_key: bytes, data: bytes) -> bytes:
    """Encrypt data with an X.509 SubjectPublicKeyInfo DER public key using RSA PKCS#1 v1.5."""
    crypto = _ensure_cryptography()
    public_key = crypto["serialization"].load_der_public_key(der_public_key)
    return public_key.encrypt(data, crypto["padding"].PKCS1v15())


class StreamCipher:
    """Continuous AES-128 CFB8 stream cipher for bidirectional packet encryption."""

    def __init__(self, shared_secret: bytes) -> None:
        if len(shared_secret) != 16:
            raise ValueError(f"AES-128 key must be 16 bytes, got {len(shared_secret)}")
        crypto = _ensure_cryptography()
        cipher_factory = crypto["Cipher"]
        algo = crypto["algorithms"].AES(shared_secret)
        # CFB8 mode for Minecraft 8-bit stream cipher (handles cryptography >= 42 and >= 49)
        try:
            from cryptography.hazmat.decrepit.ciphers.modes import CFB8 as DecrepitCFB8
            mode = DecrepitCFB8(shared_secret)
        except ImportError:
            if hasattr(crypto["modes"], "CFB8"):
                mode = crypto["modes"].CFB8(shared_secret)
            else:
                mode = crypto["modes"].CFB(shared_secret)

        self._encryptor = cipher_factory(algo, mode).encryptor()
        self._decryptor = cipher_factory(algo, mode).decryptor()

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt a continuous slice of outgoing bytes."""
        if not data:
            return b""
        return self._encryptor.update(data)

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt a continuous slice of incoming bytes."""
        if not data:
            return b""
        return self._decryptor.update(data)


def _http_post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any] | str]:
    body = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "User-Agent": "ProtoBot"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15.0) as response:
            status = response.status
            raw = response.read()
            if not raw:
                return status, {}
            try:
                return status, json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = raw.decode("utf-8", errors="replace")
        return error.code, parsed
    except urllib.error.URLError as error:
        raise AuthenticationError(f"HTTP request to {url} failed: {error}") from error


def _http_get_json(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any] | str]:
    req_headers = {"User-Agent": "ProtoBot"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15.0) as response:
            status = response.status
            raw = response.read()
            if not raw:
                return status, {}
            try:
                return status, json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = raw.decode("utf-8", errors="replace")
        return error.code, parsed
    except urllib.error.URLError as error:
        raise AuthenticationError(f"HTTP request to {url} failed: {error}") from error


def _http_post_form(url: str, params: dict[str, str]) -> tuple[int, dict[str, Any] | str]:
    """POST ``application/x-www-form-urlencoded`` data, as the OAuth endpoints require."""

    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ProtoBot",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as response:
            status = response.status
            raw = response.read()
            if not raw:
                return status, {}
            try:
                return status, json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = raw.decode("utf-8", errors="replace")
        return error.code, parsed
    except urllib.error.URLError as error:
        raise AuthenticationError(f"HTTP request to {url} failed: {error}") from error


async def join_session_server(
    access_token: str,
    selected_profile: uuid.UUID | str,
    server_hash: str,
    session_server_url: str = "https://sessionserver.mojang.com",
) -> None:
    """Inform Mojang (or authlib-injector) session server that the client is joining.

    Sends POST /session/minecraft/join with accessToken, selectedProfile, and serverId.
    """
    if isinstance(selected_profile, uuid.UUID):
        profile_hex = selected_profile.hex
    else:
        profile_hex = str(selected_profile).replace("-", "")

    endpoint = f"{session_server_url.rstrip('/')}/session/minecraft/join"
    payload = {
        "accessToken": access_token,
        "selectedProfile": profile_hex,
        "serverId": server_hash,
    }

    status, response = await asyncio.to_thread(_http_post_json, endpoint, payload)
    if status != 204 and status != 200:
        error_message = response.get("errorMessage", response) if isinstance(response, dict) else response
        raise AuthenticationError(
            f"Mojang session join failed with HTTP status {status}: {error_message}"
        )


@dataclass(frozen=True, slots=True)
class MinecraftProfile:
    id: uuid.UUID
    name: str
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0

    @property
    def expired(self) -> bool:
        """Whether the Minecraft access token is at or near its expiry.

        Caches written before expiry tracking existed carry ``expires_at == 0``
        and are treated as expired, which forces one refresh instead of a
        confusing session-server rejection at login time.
        """

        return time.time() >= self.expires_at - _TOKEN_EXPIRY_MARGIN


async def _authenticate_xbox_live(ms_access_token: str) -> tuple[str, str]:
    """Exchange a Microsoft access token for an Xbox Live token and user hash.

    The ``RpsTicket`` prefix depends on which endpoint minted the token: legacy
    MSA (``MBI_SSL``) tickets use ``t=`` while Azure AD tickets use ``d=``. Both
    are attempted so a caller supplying their own AAD application still works.
    """

    failure: tuple[int, Any] | None = None
    for ticket in (f"t={ms_access_token}", f"d={ms_access_token}"):
        payload = {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": ticket,
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        }
        status, response = await asyncio.to_thread(
            _http_post_json, _XBL_AUTH_URL, payload, headers={"Accept": "application/json"}
        )
        if status == 200 and isinstance(response, dict):
            try:
                return response["Token"], response["DisplayClaims"]["xui"][0]["uhs"]
            except (KeyError, IndexError, TypeError) as error:
                raise AuthenticationError(
                    f"Xbox Live returned an unexpected response shape: {response}"
                ) from error
        failure = (status, response)

    status, response = failure if failure is not None else (0, "no attempt made")
    raise AuthenticationError(f"Xbox Live authentication failed (HTTP {status}): {response}")


async def _authorize_xsts(xbl_token: str) -> str:
    """Trade an Xbox Live token for an XSTS token scoped to Minecraft services."""

    payload = {
        "Properties": {
            "SandboxId": "RETAIL",
            "UserTokens": [xbl_token],
        },
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    }
    status, response = await asyncio.to_thread(
        _http_post_json, _XSTS_AUTH_URL, payload, headers={"Accept": "application/json"}
    )
    if status == 200 and isinstance(response, dict) and "Token" in response:
        return response["Token"]

    err_code = response.get("XErr") if isinstance(response, dict) else None
    if err_code == 2148916233:
        raise AuthenticationError(
            "This Microsoft account has no Xbox profile. Sign in once at "
            "https://www.xbox.com to create one, then retry."
        )
    if err_code == 2148916235:
        raise AuthenticationError("Xbox Live is not available in this account's region.")
    if err_code == 2148916238:
        raise AuthenticationError(
            "This is a child account and needs to be added to a family by an adult."
        )
    raise AuthenticationError(
        f"XSTS authorization failed (HTTP {status}, XErr {err_code}): {response}"
    )


async def _minecraft_profile(
    ms_access_token: str,
    refresh_token: str | None,
) -> MinecraftProfile:
    """Run the Xbox Live -> XSTS -> Minecraft chain and fetch the player profile."""

    xbl_token, user_hash = await _authenticate_xbox_live(ms_access_token)
    xsts_token = await _authorize_xsts(xbl_token)

    status, mc_res = await asyncio.to_thread(
        _http_post_json,
        _MC_LOGIN_URL,
        {"identityToken": f"XBL3.0 x={user_hash};{xsts_token}"},
        headers={"Accept": "application/json"},
    )
    if status != 200 or not isinstance(mc_res, dict) or "access_token" not in mc_res:
        raise AuthenticationError(
            f"Minecraft Services login failed (HTTP {status}): {mc_res}"
        )
    mc_access_token = mc_res["access_token"]
    expires_at = time.time() + float(mc_res.get("expires_in", 86400))

    status, prof_res = await asyncio.to_thread(
        _http_get_json,
        _MC_PROFILE_URL,
        headers={"Authorization": f"Bearer {mc_access_token}", "Accept": "application/json"},
    )
    if status == 404:
        raise AuthenticationError(
            "This account does not own Minecraft: Java Edition (no profile exists). "
            "Game Pass accounts must launch the game once to create a profile."
        )
    if status != 200 or not isinstance(prof_res, dict) or "id" not in prof_res:
        raise AuthenticationError(
            f"Failed to fetch Minecraft profile (HTTP {status}): {prof_res}"
        )

    return MinecraftProfile(
        id=uuid.UUID(hex=prof_res["id"]),
        name=prof_res["name"],
        access_token=mc_access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )


def authorization_url(
    client_id: str = DEFAULT_MICROSOFT_CLIENT_ID,
    *,
    redirect_uri: str = _MSA_REDIRECT_URI,
) -> str:
    """Build the Microsoft sign-in URL for the authorization-code flow."""

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": _MSA_SCOPE,
        }
    )
    return f"{_MSA_AUTHORIZE_URL}?{query}"


def extract_authorization_code(pasted: str) -> str:
    """Pull the ``code`` out of the redirect URL the browser landed on.

    Accepts either the full ``https://login.live.com/oauth20_desktop.srf?code=...``
    URL or a bare code, since users paste both. A redirect carrying ``error``
    instead of ``code`` is reported with Microsoft's own description.
    """

    pasted = pasted.strip()
    if not pasted:
        raise AuthenticationError("No authorization code was provided.")

    is_url = pasted.startswith(("http://", "https://"))
    if is_url or "?" in pasted or "code=" in pasted or "error=" in pasted:
        query = urllib.parse.urlsplit(pasted).query
        if not query:
            query = pasted.partition("?")[2] or pasted
        values = urllib.parse.parse_qs(query)
        if "error" in values:
            description = values.get("error_description", values["error"])[0]
            raise AuthenticationError(f"Microsoft denied the sign-in: {description}")
        codes = values.get("code")
        if codes and codes[0]:
            return codes[0]
        raise AuthenticationError(
            "That URL carries no authorization code. Sign in fully, then copy the "
            "address bar once it points at login.live.com/oauth20_desktop.srf."
        )

    return pasted


def _default_code_prompt(url: str) -> str:
    print("\n[ProtoBot Auth] Open this URL in your browser and sign in:")
    print(f"  {url}")
    print(
        "\n[ProtoBot Auth] After signing in the page will look blank. Copy the "
        "whole address bar\n                (it starts with "
        "https://login.live.com/oauth20_desktop.srf?code=) and paste it here."
    )
    return input("\n[ProtoBot Auth] Redirect URL (or just the code): ")


async def authorization_code_login(
    client_id: str = DEFAULT_MICROSOFT_CLIENT_ID,
    *,
    redirect_uri: str = _MSA_REDIRECT_URI,
    prompt_callback: Callable[[str], str] | None = None,
) -> MinecraftProfile:
    """Sign in to a Minecraft account with the Microsoft authorization-code flow.

    This is the flow that works with the public launcher client ID, so it needs
    no Azure application of your own. The caller is shown a URL, signs in with a
    browser, and hands back the redirect URL it lands on.

    Args:
        client_id: Microsoft client ID. The default public launcher ID is fine.
        redirect_uri: Must match the one registered for ``client_id``.
        prompt_callback: Receives the sign-in URL and returns the redirect URL
            (or bare code) the user copied. If None, prompts on stdin.

    Returns:
        A :class:`MinecraftProfile` with the player UUID, username, Minecraft
        bearer token, and a refresh token usable with :func:`refresh_login`.
    """

    url = authorization_url(client_id, redirect_uri=redirect_uri)
    prompt = prompt_callback or _default_code_prompt
    pasted = await asyncio.to_thread(prompt, url)
    code = extract_authorization_code(pasted)

    status, token_res = await asyncio.to_thread(
        _http_post_form,
        _MSA_TOKEN_URL,
        {
            "client_id": client_id,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "scope": _MSA_SCOPE,
        },
    )
    if status != 200 or not isinstance(token_res, dict) or "access_token" not in token_res:
        description = (
            token_res.get("error_description", token_res.get("error"))
            if isinstance(token_res, dict)
            else token_res
        )
        raise AuthenticationError(
            f"Microsoft code exchange failed (HTTP {status}): {description}. "
            "Authorization codes are single-use and expire quickly — sign in again."
        )

    return await _minecraft_profile(
        token_res["access_token"], token_res.get("refresh_token")
    )


async def device_code_login(
    client_id: str | None = None,
    *,
    prompt_callback: Callable[[str, str], None] | None = None,
) -> MinecraftProfile:
    """Perform an interactive Microsoft Device Code login for a Minecraft account.

    The device-code grant is only available to applications registered in Azure
    AD, so ``client_id`` is required: register a public client with the
    "Allow public client flows" option enabled and the ``XboxLive.signin``
    delegated permission. Use :func:`authorization_code_login` if you would
    rather not register anything.

    Args:
        client_id: Your Azure AD application (client) ID.
        prompt_callback: Callable receiving ``(user_code, verification_uri)``.
            If None, the code and URL are printed to stdout.

    Returns:
        A :class:`MinecraftProfile` with the player UUID, username, Minecraft
        bearer token, and a refresh token usable with
        ``refresh_login(..., azure_ad=True)``.
    """

    if not client_id or client_id == DEFAULT_MICROSOFT_CLIENT_ID:
        raise AuthenticationError(
            "device_code_login() requires your own Azure AD application ID. The "
            "public launcher client ID cannot complete the device-code grant: "
            "Microsoft issues a device code but then refuses the token with "
            "'user interaction is required'. Either pass client_id=<your Azure "
            "app id>, or call authorization_code_login(), which needs no "
            "registration."
        )

    # 1. Request a device code.
    status, device_info = await asyncio.to_thread(
        _http_post_form,
        _AAD_DEVICE_CODE_URL,
        {
            "client_id": client_id,
            "scope": _AAD_SCOPE,
        },
    )
    if status != 200 or not isinstance(device_info, dict) or "device_code" not in device_info:
        raise AuthenticationError(
            f"Microsoft device code request failed (HTTP {status}): {device_info}"
        )

    device_code = device_info["device_code"]
    user_code = device_info["user_code"]
    verification_uri = device_info.get("verification_uri", "https://microsoft.com/devicelogin")
    interval = float(device_info.get("interval", 5))
    expires_in = float(device_info.get("expires_in", 900))

    if prompt_callback is not None:
        prompt_callback(user_code, verification_uri)
    else:
        print(f"\n[ProtoBot Auth] Open: {verification_uri}")
        print(f"[ProtoBot Auth] Enter code: {user_code}\n")

    # 2. Poll until the user finishes authorizing in their browser.
    token_params = {
        "client_id": client_id,
        "device_code": device_code,
        "grant_type": _DEVICE_CODE_GRANT,
    }
    deadline = time.monotonic() + expires_in
    while True:
        await asyncio.sleep(interval)
        if time.monotonic() >= deadline:
            raise AuthenticationError(
                "Device code login timed out before the code was entered."
            )

        status, token_res = await asyncio.to_thread(
            _http_post_form, _AAD_TOKEN_URL, token_params
        )
        if status == 200 and isinstance(token_res, dict) and "access_token" in token_res:
            return await _minecraft_profile(
                token_res["access_token"], token_res.get("refresh_token")
            )

        error = token_res.get("error") if isinstance(token_res, dict) else None
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error in ("authorization_declined", "access_denied"):
            raise AuthenticationError("The Microsoft login was declined in the browser.")
        if error in ("expired_token", "code_expired"):
            raise AuthenticationError(
                "The device code expired before it was entered. Please retry."
            )
        description = (
            token_res.get("error_description", error)
            if isinstance(token_res, dict)
            else token_res
        )
        raise AuthenticationError(f"Microsoft login failed (HTTP {status}): {description}")



async def refresh_login(
    refresh_token: str,
    client_id: str = DEFAULT_MICROSOFT_CLIENT_ID,
    *,
    azure_ad: bool = False,
) -> MinecraftProfile:
    """Mint a fresh Minecraft token from a stored refresh token.

    Minecraft access tokens last about a day, so a cached credential needs this
    before it can be reused. Raises :class:`AuthenticationError` if the refresh
    token has itself been revoked or expired, in which case the caller should
    fall back to an interactive login.

    Args:
        refresh_token: The token stored alongside the previous login.
        client_id: The same client ID the refresh token was issued to.
        azure_ad: Set when the token came from :func:`device_code_login`, so the
            refresh goes to the Azure AD endpoint rather than the MSA one.
    """

    params = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if azure_ad:
        params["scope"] = _AAD_SCOPE
    token_url = _AAD_TOKEN_URL if azure_ad else _MSA_TOKEN_URL

    status, token_res = await asyncio.to_thread(_http_post_form, token_url, params)
    if status != 200 or not isinstance(token_res, dict) or "access_token" not in token_res:
        description = (
            token_res.get("error_description", token_res.get("error"))
            if isinstance(token_res, dict)
            else token_res
        )
        raise AuthenticationError(
            f"Microsoft token refresh failed (HTTP {status}): {description}"
        )

    return await _minecraft_profile(
        token_res["access_token"], token_res.get("refresh_token", refresh_token)
    )

