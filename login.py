"""微软正版账号登录脚本。

在 PyCharm 中直接右键运行本文件即可。

默认流程（无需任何注册）：
    脚本打印一个链接和一个 8 位验证码 -> 浏览器打开链接（验证码已预填）
    -> 登录并**一路点到最后的确认页** -> 脚本自动继续，无需回到控制台粘贴。

    注意：这一步必须完整走完到确认页。如果中途关掉窗口或没点最终确认，
    微软会返回「用户须重新登录并授予该客户端访问权限」，此时重跑本脚本即可。

如果设备码流程反复失败，有两个备选：

  A. 授权码流程（同样无需注册）——把 USE_AUTH_CODE 改成 True。
     缺点：登录完要把浏览器地址栏复制回来粘贴，且微软会显示一个
     「Microsoft 绝不会要求你复制或分享此 URL」的反钓鱼提示页。

  B. 用自己的 Azure 应用走设备码（微软官方支持该授权的路径）——
     把应用 ID 填到 AZURE_CLIENT_ID。注册免费，约 3 分钟：
       1. https://portal.azure.com -> Microsoft Entra ID -> 应用注册 -> 新注册
       2. 「支持的账户类型」选「仅个人 Microsoft 账户」
       3. 创建后进入「身份验证」，打开「允许公共客户端流」并保存
       4. 复制「概述」页的「应用程序(客户端) ID」填到下面
"""

import asyncio
import json
import webbrowser
from pathlib import Path

from protobot import authorization_code_login, authorization_url, device_code_login

# 备选 A：改为 True 走授权码流程（需要复制粘贴地址）
USE_AUTH_CODE = False

# 备选 B：填入自己的 Azure 应用 ID
AZURE_CLIENT_ID = ""

CACHE_FILE = Path(__file__).parent / "auth_cache.json"


def _open_browser(url: str) -> None:
    try:
        if webbrowser.open(url):
            print("    （已尝试自动打开浏览器）")
    except Exception:
        pass


def show_device_code(user_code: str, verification_uri: str) -> None:
    print(f"\n[1] 请在浏览器中打开（验证码已预填）：\n\n    {verification_uri}\n")
    _open_browser(verification_uri)
    print(f"[2] 验证码： {user_code}")
    print("    如果页面没有预填，手动输入上面这串即可。\n")
    print("[3] 登录后请**一路点到最后的确认页**，不要提前关闭窗口。")
    print("    完成后本脚本会自动继续，无需回到这里操作。\n")
    print("[..] 正在等待你在浏览器中完成授权（最多 15 分钟）...")


def prompt_for_code(url: str) -> str:
    print("\n[1] 请在浏览器中打开下面的链接并登录：\n")
    print(f"    {url}\n")
    _open_browser(url)
    print("[2] 登录完成后，微软会显示一个提示页，大意是：")
    print('    「你已进入一个通常不会显示的页面。Microsoft 绝不会要求你复制或分享此 URL」')
    print("    这是反钓鱼提示。你要粘贴到的是本机上的这个脚本，令牌不会外传。\n")
    print("[3] 请把浏览器地址栏里**整条**地址复制下来，它形如：")
    print("    https://login.live.com/oauth20_desktop.srf?code=M.C5xx...&lc=2052\n")
    return input("[4] 粘贴回跳地址（或只粘贴 code 部分）后回车： ")


async def main() -> None:
    print("=" * 60)
    print("      ProtoBot 微软正版账号授权向导")
    print("=" * 60)

    if USE_AUTH_CODE:
        print("[方式] 授权码流程（需复制粘贴地址）")
        profile = await authorization_code_login(prompt_callback=prompt_for_code)
        client_id = None
        azure_ad = False
    elif AZURE_CLIENT_ID:
        print("[方式] 设备码流程（使用你的 Azure 应用）")
        profile = await device_code_login(
            AZURE_CLIENT_ID, prompt_callback=show_device_code
        )
        client_id = AZURE_CLIENT_ID
        azure_ad = True
    else:
        print("[方式] 设备码流程（输验证码，无需注册）")
        profile = await device_code_login(prompt_callback=show_device_code)
        client_id = None
        azure_ad = False

    cache_data = {
        "name": profile.name,
        "uuid": str(profile.id),
        "access_token": profile.access_token,
        "refresh_token": profile.refresh_token,
        "expires_at": profile.expires_at,
        "azure_ad": azure_ad,
        "client_id": client_id,
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
    """供排查使用：只打印授权码流程的登录链接。"""
    print(authorization_url())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[取消] 已中断授权。")
