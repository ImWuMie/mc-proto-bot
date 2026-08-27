"""微软正版账号登录脚本。

在 PyCharm 中直接右键运行本文件即可。

流程：脚本打印一个微软登录链接 -> 你在浏览器里登录 -> 登录完页面会变成空白，
把地址栏整条复制回来粘贴到控制台。这一步只需做一次，之后 run_bot.py 会用
refresh token 自动续期。

（这里用的是授权码流程，而不是设备码流程：设备码需要你自己在 Azure AD
注册应用，而授权码流程配合公开的启动器 client ID 即可，无需任何注册。）
"""

import asyncio
import json
import webbrowser
from pathlib import Path

from protobot import authorization_code_login, authorization_url

CACHE_FILE = Path(__file__).parent / "auth_cache.json"


def prompt_for_code(url: str) -> str:
    """打印登录链接并尝试自动打开浏览器，然后等待用户粘贴回跳地址。"""
    print("\n[1] 请在浏览器中打开下面的链接并登录你的微软账号：\n")
    print(f"    {url}\n")
    try:
        if webbrowser.open(url):
            print("    （已尝试自动打开浏览器）\n")
    except Exception:
        pass
    print("[2] 登录完成后页面会显示空白，这是正常的。")
    print("    请把浏览器地址栏里**整条**地址复制下来，它形如：")
    print("    https://login.live.com/oauth20_desktop.srf?code=M.C5xx...&lc=2052\n")
    return input("[3] 粘贴回跳地址（或只粘贴 code 部分）后回车： ")


async def main() -> None:
    print("=" * 60)
    print("      ProtoBot 微软正版账号授权向导")
    print("=" * 60)

    profile = await authorization_code_login(prompt_callback=prompt_for_code)

    cache_data = {
        "name": profile.name,
        "uuid": str(profile.id),
        "access_token": profile.access_token,
        "refresh_token": profile.refresh_token,
        "expires_at": profile.expires_at,
    }
    CACHE_FILE.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print("【登录成功！】")
    print(f"玩家昵称: {profile.name}")
    print(f"玩家 UUID: {profile.id}")
    print(f"凭据已自动保存到: {CACHE_FILE.name}")
    if profile.refresh_token:
        print("已保存续期令牌，后续连接会自动刷新，无需重复授权。")
    else:
        print("提示: 本次未获得续期令牌，令牌过期后需要重新运行本脚本。")
    print("注意: 该文件包含账号访问令牌，请勿分享或提交到版本库。")
    print("现在你可以直接在 PyCharm 中右键运行 run_bot.py 连服了！")
    print("=" * 60)


def print_login_url() -> None:
    """供排查使用：只打印登录链接，不走完整流程。"""
    print(authorization_url())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[取消] 已中断授权。")
