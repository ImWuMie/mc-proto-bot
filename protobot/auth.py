"""Authentication and encryption helpers for Minecraft online-mode."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import AuthenticationError, OnlineModeRequired

# Microsoft Public OAuth Client ID for Minecraft Launcher
# Used widely across open-source Minecraft tools.
DEFAULT_MICROSOFT_CLIENT_ID = "00000000402b5328"


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


async def device_code_login(
    client_id: str = DEFAULT_MICROSOFT_CLIENT_ID,
    *,
    prompt_callback: Callable[[str, str], None] | None = None,
) -> MinecraftProfile:
    """Perform an interactive Microsoft Device Code OAuth flow to obtain a Minecraft Access Token.

    Args:
        client_id: Microsoft Azure App Client ID. Defaults to standard public Minecraft launcher ID.
        prompt_callback: Callable receiving ``(user_code, verification_uri)``. If None, prints to stdout.

    Returns:
        A :class:`MinecraftProfile` containing player UUID, username, and the Minecraft Bearer access token.
    """
    # 1. Request device code
    device_code_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "scope": "XboxLive.signin offline_access",
    }).encode("utf-8")
    req = urllib.request.Request(
        device_code_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "ProtoBot"},
        method="POST",
    )

    def _get_device_code():
        with urllib.request.urlopen(req, timeout=15.0) as res:
            return json.loads(res.read().decode("utf-8"))

    device_info = await asyncio.to_thread(_get_device_code)
    user_code = device_info["user_code"]
    verification_uri = device_info.get("verification_uri", "https://microsoft.com/link")
    device_code = device_info["device_code"]
    interval = device_info.get("interval", 5)
    expires_in = device_info.get("expires_in", 900)

    if prompt_callback is not None:
        prompt_callback(user_code, verification_uri)
    else:
        print(f"\n[ProtoBot Auth] Open: {verification_uri}")
        print(f"[ProtoBot Auth] Enter code: {user_code}\n")

    # 2. Poll for Microsoft OAuth token
    token_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    token_payload = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code,
    }).encode("utf-8")

    deadline = time.monotonic() + expires_in
    ms_access_token = None

    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        poll_req = urllib.request.Request(
            token_url,
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "ProtoBot"},
            method="POST",
        )
        try:
            def _poll():
                with urllib.request.urlopen(poll_req, timeout=15.0) as res:
                    return json.loads(res.read().decode("utf-8"))

            token_res = await asyncio.to_thread(_poll)
            if "access_token" in token_res:
                ms_access_token = token_res["access_token"]
                break
        except urllib.error.HTTPError as err:
            err_data = json.loads(err.read().decode("utf-8"))
            err_type = err_data.get("error")
            if err_type == "authorization_pending":
                continue
            if err_type == "slow_down":
                interval += 5
                continue
            raise AuthenticationError(f"Microsoft login failed: {err_data.get('error_description', err_type)}")

    if ms_access_token is None:
        raise AuthenticationError("Device code login timed out.")

    # 3. Authenticate with Xbox Live (user.auth.xboxlive.com)
    xbl_url = "https://user.auth.xboxlive.com/user/authenticate"
    xbl_payload = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={ms_access_token}",
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    status, xbl_res = await asyncio.to_thread(_http_post_json, xbl_url, xbl_payload)
    if status != 200 or not isinstance(xbl_res, dict):
        raise AuthenticationError(f"Xbox Live authentication failed: {xbl_res}")

    xbl_token = xbl_res["Token"]
    user_hash = xbl_res["DisplayClaims"]["xui"][0]["uhs"]

    # 4. Acquire XSTS token (xsts.auth.xboxlive.com)
    xsts_url = "https://xsts.auth.xboxlive.com/xsts/authorize"
    xsts_payload = {
        "Properties": {
            "SandboxId": "RETAIL",
            "UserTokens": [xbl_token],
        },
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    }
    status, xsts_res = await asyncio.to_thread(_http_post_json, xsts_url, xsts_payload)
    if status != 200 or not isinstance(xsts_res, dict):
        err_code = xsts_res.get("XErr", "Unknown") if isinstance(xsts_res, dict) else "Unknown"
        if err_code == 2148916233:
            raise AuthenticationError("Microsoft account has no Xbox account.")
        if err_code == 2148916238:
            raise AuthenticationError("Child account requires parental consent.")
        raise AuthenticationError(f"XSTS authorization failed (code {err_code}): {xsts_res}")

    xsts_token = xsts_res["Token"]

    # 5. Login with Xbox to Minecraft Services
    mc_login_url = "https://api.minecraftservices.com/authentication/login_with_xbox"
    mc_payload = {
        "identityToken": f"XBL3.0 x={user_hash};{xsts_token}",
    }
    status, mc_res = await asyncio.to_thread(_http_post_json, mc_login_url, mc_payload)
    if status != 200 or not isinstance(mc_res, dict):
        raise AuthenticationError(f"Minecraft Services login failed: {mc_res}")

    mc_access_token = mc_res["access_token"]

    # 6. Fetch Minecraft player profile
    profile_url = "https://api.minecraftservices.com/minecraft/profile"
    status, prof_res = await asyncio.to_thread(
        _http_get_json, profile_url, headers={"Authorization": f"Bearer {mc_access_token}"}
    )
    if status != 200 or not isinstance(prof_res, dict):
        raise AuthenticationError(f"Failed to fetch Minecraft profile (HTTP {status}): {prof_res}")

    profile_id = uuid.UUID(prof_res["id"])
    profile_name = prof_res["name"]

    return MinecraftProfile(id=profile_id, name=profile_name, access_token=mc_access_token)
