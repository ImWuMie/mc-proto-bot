"""ProtoBot 主启动脚本。

在 PyCharm 中直接右键 -> Run 'run_bot' 即可启动！
"""

import asyncio
import json
import uuid
from pathlib import Path

from protobot import MovementInput, connect, device_code_login

# ======================== 配置区域 ========================
# 服务器地址与端口
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 25565

# 服务器协议版本：支持 "1.21.11", "26.1", "26.1.1", "26.1.2", "26.2"
SERVER_VERSION = "26.2"

# 是否启用正版验证模式 (online-mode=true 的服务器需要设为 True)
ONLINE_MODE = True

# 离线模式下的备用用户名（ONLINE_MODE=False 时生效）
OFFLINE_USERNAME = "ProtoBot"
# =========================================================

CACHE_FILE = Path(__file__).parent / "auth_cache.json"


async def get_credentials() -> tuple[str, str | None, uuid.UUID | None]:
    """获取连接凭据（正版或离线）。"""
    if not ONLINE_MODE:
        return OFFLINE_USERNAME, None, None

    # 优先从本地缓存读取
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            name = data["name"]
            token = data["access_token"]
            player_uuid = uuid.UUID(data["uuid"])
            print(f"[凭据] 已从本地缓存读取正版账号: {name}")
            return name, token, player_uuid
        except Exception as e:
            print(f"[提示] 本地凭据缓存损坏，重新发起登录 ({e})")

    # 首次运行发起微软登录
    print("\n[认证] 未检测到有效凭据，正在发起微软正版登录...")
    profile = await device_code_login()
    cache_data = {
        "name": profile.name,
        "uuid": str(profile.id),
        "access_token": profile.access_token,
    }
    CACHE_FILE.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")
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
        print(f"[聊天/系统] {comp}")

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
