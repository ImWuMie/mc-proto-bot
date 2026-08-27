"""微软正版账号登录脚本。

在 PyCharm 中直接右键运行本文件即可。
首次运行会提示打开 https://microsoft.com/link 并输入 8 位验证码。
登录成功后会自动将 Token 缓存到本地 auth_cache.json 中，后续连接无需重复认证。
"""

import asyncio
import json
from pathlib import Path

from protobot import device_code_login

CACHE_FILE = Path(__file__).parent / "auth_cache.json"


async def main() -> None:
    print("=" * 60)
    print("      ProtoBot 微软正版账号授权向导")
    print("=" * 60)

    # 1. 调起微软设备代码流
    profile = await device_code_login()

    # 2. 缓存凭证到本地
    cache_data = {
        "name": profile.name,
        "uuid": str(profile.id),
        "access_token": profile.access_token,
    }
    CACHE_FILE.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print("【登录成功！】")
    print(f"玩家昵称: {profile.name}")
    print(f"玩家 UUID: {profile.id}")
    print(f"凭据已自动保存到: {CACHE_FILE.name}")
    print("现在你可以直接在 PyCharm 中右键运行 run_bot.py 连服了！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
