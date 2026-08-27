"""ProtoBot 主启动脚本。

在 PyCharm 中直接右键 -> Run 'run_bot' 即可启动！
"""

import asyncio
import json
import uuid
from pathlib import Path

from protobot import MinecraftProfile, MovementInput, connect, refresh_login

# ======================== 配置区域 ========================
# 服务器地址与端口
SERVER_HOST = "wolfx.jp"
SERVER_PORT = 25565

# 服务器协议版本：支持 "1.21.11", "26.1", "26.1.1", "26.1.2", "26.2"
SERVER_VERSION = "26.2"

# 是否启用正版验证模式 (online-mode=true 的服务器需要设为 True)
ONLINE_MODE = True

# 离线模式下的备用用户名（ONLINE_MODE=False 时生效）
OFFLINE_USERNAME = "ProtoBot"
# =========================================================

CACHE_FILE = Path(__file__).parent / "auth_cache.json"


def _plain_text(component) -> str:
    """Extract readable text from a decoded chat component (str/dict/list)."""

    if isinstance(component, str):
        return component
    if isinstance(component, list):
        return "".join(_plain_text(item) for item in component)
    if isinstance(component, dict):
        parts: list[str] = []
        if "text" in component:
            parts.append(str(component["text"]))
        if "translate" in component:
            parts.append(str(component["translate"]))
        for key in ("with", "extra"):
            if key in component:
                parts.append(_plain_text(component[key]))
        return "".join(parts)
    return str(component)


def _save_profile(profile: MinecraftProfile, refresh_options: dict) -> None:
    CACHE_FILE.write_text(
        json.dumps(
            {
                "name": profile.name,
                "uuid": str(profile.id),
                "access_token": profile.access_token,
                "refresh_token": profile.refresh_token,
                "expires_at": profile.expires_at,
                "azure_ad": refresh_options.get("azure_ad", False),
                "client_id": refresh_options.get("client_id"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _load_profile() -> tuple[MinecraftProfile, dict] | None:
    """读取本地缓存的正版凭据；缺失或损坏时返回 None。

    同时返回续期所需的参数：设备码流程签发的令牌必须回到 Azure AD 端点续期，
    授权码流程签发的必须回到 MSA 端点，两者不能混用。
    """
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        profile = MinecraftProfile(
            id=uuid.UUID(data["uuid"]),
            name=data["name"],
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=float(data.get("expires_at", 0.0)),
        )
    except (OSError, ValueError, KeyError) as error:
        print(f"[提示] 本地凭据缓存损坏，将重新发起登录 ({error})")
        return None

    refresh_options: dict = {}
    if data.get("azure_ad") and data.get("client_id"):
        refresh_options = {"client_id": data["client_id"], "azure_ad": True}
    return profile, refresh_options


async def get_credentials() -> tuple[str, str | None, uuid.UUID | None]:
    """获取连接凭据（正版或离线）。

    正版模式下优先复用本地缓存；令牌过期时用 refresh token 自动续期。
    续期不可用时提示重新运行 login.py（授权需要浏览器交互，不在本脚本内进行）。
    """
    if not ONLINE_MODE:
        return OFFLINE_USERNAME, None, None

    loaded = _load_profile()
    if loaded is None:
        raise SystemExit(
            "[错误] 未找到正版凭据缓存。请先运行 login.py 完成一次微软账号授权。"
        )
    profile, refresh_options = loaded

    if not profile.expired:
        print(f"[凭据] 已从本地缓存读取正版账号: {profile.name}")
        return profile.name, profile.access_token, profile.id

    if not profile.refresh_token:
        raise SystemExit(
            "[错误] 缓存令牌已过期且没有续期令牌，请重新运行 login.py 授权。"
        )

    print(f"[凭据] 缓存令牌已过期，正在为 {profile.name} 自动续期...")
    try:
        profile = await refresh_login(profile.refresh_token, **refresh_options)
    except Exception as error:
        raise SystemExit(
            f"[错误] 自动续期失败，请重新运行 login.py 授权。原因: {error}"
        ) from error

    _save_profile(profile, refresh_options)
    print(f"[凭据] 续期成功: {profile.name}")
    return profile.name, profile.access_token, profile.id



async def main() -> None:
    print("=" * 60)
    print("           ProtoBot 机器人启动器")
    print("=" * 60)

    username, access_token, profile_uuid = await get_credentials()

    print(f"\n[连接] 正在连接服务器 {SERVER_HOST}:{SERVER_PORT} (版本: {SERVER_VERSION}) ...")
    print(f"[模式] {'正版验证 (Online Mode)' if access_token else '离线模式 (Offline Mode)'}")

    try:
        bot = await connect(
            SERVER_HOST,
            port=SERVER_PORT,
            username=username,
            version=SERVER_VERSION,
            access_token=access_token,
            profile_uuid=profile_uuid,
            timeout=30.0,
        )
    except Exception as error:
        print(f"\n[错误] 连接服务器失败: {error}")
        return

    print(f"\n[成功] 机器人已进入游戏！")
    print(f"       玩家名: {bot.username}")
    print(f"       UUID: {bot.uuid}")
    print(f"       出生位置: X={bot.player.x:.2f}, Y={bot.player.y:.2f}, Z={bot.player.z:.2f}")

    # 注册事件监听器
    @bot.on("system_chat")
    async def on_system_chat(comp, overlay):
        print(f"[聊天/系统] {_plain_text(comp)}")

    @bot.on("player_chat")
    async def on_player_chat(sender_uuid, name, message, chat_type_id, target_name):
        print(f"[聊天] {_plain_text(name)}: {_plain_text(message)}")

    @bot.on("close")
    async def on_close(reason):
        print(f"\n[提示] 与服务器的连接已断开: {reason}")

    print("\n[等待] 正在加载世界区块...")
    try:
        await bot.wait_world(timeout=20.0)
        print(f"[世界] 已加载 {len(bot.world.chunks)} 个区块！")
    except Exception:
        print("[世界] 等待区块超时，继续保持在线...")

    # 保持机器人在线循环
    print("\n[运行中] 机器人正在运行 (按 Ctrl + C 可退出)...")
    try:
        tick_count = 0
        while not bot.closed.is_set():
            # 基础 20 Hz 心跳 tick，维持本地物理模拟与发包
            await bot.tick(MovementInput())
            await asyncio.sleep(0.05)
            tick_count += 1
            if tick_count % 200 == 0:  # 每 10 秒打印一次当前状态
                pos = bot.player
                print(f"[心跳] 当前坐标: X={pos.x:.1f}, Y={pos.y:.1f}, Z={pos.z:.1f} | 在线中")
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n[退出] 正在正常退出并关闭连接...")
    finally:
        await bot.close()
        print("[完成] 机器人已安全退出。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
