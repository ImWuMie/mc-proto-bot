"""微软正版账号登录脚本。

在 PyCharm 中直接右键运行本文件即可。

本脚本支持两种登录方式：

1. 授权码流程（默认，无需任何注册）
   打印一个微软登录链接 -> 你在浏览器里登录 -> 把地址栏复制回来粘贴。
   注意：登录完微软会显示一个提示页，写着「你已进入一个通常不会显示的页面，
   Microsoft 绝不会要求你复制或分享此 URL」。这是微软的反钓鱼提示，因为骗子
   会用「把地址发给我」的话术骗取令牌。这里你粘贴的目标是本机上的这个脚本，
   令牌不会离开你的电脑，所以是安全的——但如果你不想每次都看到这个页面，
   请改用下面的方式 2。

2. 设备码流程（推荐，需要一次性注册自己的 Azure 应用）
   在浏览器里输入一个 8 位验证码即可，不需要复制粘贴地址，也不会出现上述提示页。
   注册步骤（免费，约 3 分钟）：
     a. 打开 https://portal.azure.com -> Microsoft Entra ID -> 应用注册 -> 新注册
     b. 名称随意；「支持的账户类型」选择「仅个人 Microsoft 账户」
     c. 创建后进入「身份验证」，把「允许公共客户端流」(Allow public client flows)
        打开并保存
     d. 复制「概述」页的「应用程序(客户端) ID」，填到下面的 AZURE_CLIENT_ID
"""

import asyncio
import json
import webbrowser
from pathlib import Path

from protobot import authorization_code_login, authorization_url, device_code_login

# 填入自己的 Azure 应用 ID 即启用设备码流程；留空则走授权码流程。
AZURE_CLIENT_ID = ""

CACHE_FILE = Path(__file__).parent / "auth_cache.json"


def _open_browser(url: str) -> None:
    try:
        if webbrowser.open(url):
            print("    （已尝试自动打开浏览器）\n")
    except Exception:
        pass


def prompt_for_code(url: str) -> str:
    """打印登录链接，等待用户粘贴回跳地址。"""
    print("\n[1] 请在浏览器中打开下面的链接并登录你的微软账号：\n")
    print(f"    {url}\n")
    _open_browser(url)
    print("[2] 登录完成后，微软可能显示一个提示页，内容大意是：")
    print('    「你已进入一个通常不会显示的页面。Microsoft 绝不会要求你复制或分享此 URL」')
    print("    这是正常的反钓鱼提示。你要粘贴到的是本机上的这个脚本，令牌不会外传。")
    print("    （不想每次看到它，可按文件开头说明改用设备码流程。）\n")
    print("[3] 请把浏览器地址栏里**整条**地址复制下来，它形如：")
    print("    https://login.live.com/oauth20_desktop.srf?code=M.C5xx...&lc=2052\n")
    return input("[4] 粘贴回跳地址（或只粘贴 code 部分）后回车： ")


def show_device_code(user_code: str, verification_uri: str) -> None:
    print(f"\n[1] 请在浏览器中打开： {verification_uri}")
    _open_browser(verification_uri)
    print(f"[2] 输入验证码： {user_code}")
    print("[3] 在浏览器里完成授权，本脚本会自动继续（无需回到这里操作）...\n")


async def main() -> None:
    print("=" * 60)
    print("      ProtoBot 微软正版账号授权向导")
    print("=" * 60)

    if AZURE_CLIENT_ID:
        print("[方式] 设备码流程（使用你的 Azure 应用）")
        profile = await device_code_login(
            AZURE_CLIENT_ID, prompt_callback=show_device_code
        )
    else:
        print("[方式] 授权码流程（无需注册；如需免复制粘贴请见文件开头说明）")
        profile = await authorization_code_login(prompt_callback=prompt_for_code)

    cache_data = {
        "name": profile.name,
        "uuid": str(profile.id),
        "access_token": profile.access_token,
        "refresh_token": profile.refresh_token,
        "expires_at": profile.expires_at,
        "azure_ad": bool(AZURE_CLIENT_ID),
        "client_id": AZURE_CLIENT_ID or None,
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
