"""ProtoBot 统一命令行入口：``protobot login|run|plugins|setup``。

首次启动（配置文件不存在）时进入交互式配置向导：登录方式 -> 服务器地址 ->
协议版本，随后把配置写入本地 ``config.yaml``。授权凭据缓存始终存放在配置
文件旁边（``auth_cache.json``），因此 ``login`` 与 ``run`` 在任意工作目录
下都指向同一份。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from . import (
    MinecraftProfile,
    authorization_code_login,
    device_code_login,
    refresh_login,
)
from .config import load_config, save_config
from .log import info, set_sink
from .plugin import PluginError, PluginManager, PluginWatcher
from .protocol.versions import SUPPORTED_VERSIONS
from .session import BotContainer, BotSession, SessionConfig
from .tui import ProtoBotApp, StdoutProxy, tui_enabled

DEFAULT_CONFIG = Path("config.yaml")

__all__ = [
    "get_credentials",
    "list_plugins",
    "load_plugin_config",
    "load_profile",
    "load_session_config",
    "load_tui_config",
    "main",
    "run_bot_session",
    "run_login",
    "save_profile",
]


# ======================== 配置加载 ========================


def load_session_config(data: dict) -> SessionConfig:
    """从 config.yaml 的字典构建 SessionConfig（非法值抛中文 ValueError）。"""
    server = data.get("server", {})
    login = data.get("login", {})
    session = data.get("session", {})
    if not isinstance(server, dict) or not server.get("host"):
        raise ValueError("配置缺少 server.host（服务器地址）")

    mode = login.get("mode", "online") if isinstance(login, dict) else "online"
    if mode not in ("online", "offline"):
        raise ValueError(f"login.mode 必须是 online 或 offline，得到 {mode!r}")
    offline_username = str(login.get("offline_username", "ProtoBot")) \
        if isinstance(login, dict) else "ProtoBot"

    def _get(section: dict, key: str, default):
        return section[key] if isinstance(section, dict) and key in section else default

    return SessionConfig(
        host=str(server["host"]),
        port=int(_get(server, "port", 25565)),
        version=str(_get(server, "version", "26.2")),
        online_mode=mode == "online",
        offline_username=offline_username,
        reconnect=bool(_get(session, "reconnect", True)),
        reconnect_delay=float(_get(session, "reconnect_delay", 5.0)),
        reconnect_max_attempts=_get(session, "reconnect_max_attempts", None),
        connect_timeout=float(_get(session, "connect_timeout", 30.0)),
    )


@dataclass(frozen=True)
class PluginConfig:
    directory: Path
    disabled: tuple[str, ...] = ()
    watch: bool = True


def load_plugin_config(data: dict, base_dir: Path) -> PluginConfig:
    """从 config.yaml 读取 [plugins] 段；相对目录按配置文件所在目录解析。"""
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}
    directory = Path(str(plugins.get("directory", "plugins")))
    if not directory.is_absolute():
        directory = base_dir / directory
    disabled = plugins.get("disabled", []) or []
    if isinstance(disabled, str):
        disabled = [disabled]
    return PluginConfig(
        directory=directory,
        disabled=tuple(str(name) for name in disabled),
        watch=bool(plugins.get("watch", True)),
    )


def load_tui_config(data: dict) -> bool:
    """从 config.yaml 读取 [tui] enabled（缺省 True）。"""
    tui = data.get("tui", {})
    return bool(tui.get("enabled", True)) if isinstance(tui, dict) else True


# ======================== 凭据缓存（与旧 login.py / run_bot.py 同格式） ========================


def save_profile(
    cache_file: Path, profile: MinecraftProfile, refresh_options: dict
) -> None:
    cache_file.write_text(
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


def load_profile(cache_file: Path) -> tuple[MinecraftProfile, dict] | None:
    """读取本地缓存的正版凭据；缺失或损坏时返回 None。

    同时返回续期所需的参数：设备码流程签发的令牌必须回到 Azure AD 端点续期，
    授权码流程签发的必须回到 MSA 端点，两者不能混用。
    """
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        profile = MinecraftProfile(
            id=uuid.UUID(data["uuid"]),
            name=data["name"],
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=float(data.get("expires_at", 0.0)),
        )
    except (OSError, ValueError, KeyError) as error:
        info(f"[提示] 本地凭据缓存损坏，将重新发起登录 ({error})")
        return None

    refresh_options: dict = {}
    if data.get("azure_ad") and data.get("client_id"):
        refresh_options = {"client_id": data["client_id"], "azure_ad": True}
    return profile, refresh_options


async def get_credentials(
    cache_file: Path, *, online_mode: bool, offline_username: str
) -> tuple[str, str | None, uuid.UUID | None]:
    """获取连接凭据（正版或离线）。

    正版模式下优先复用本地缓存；令牌过期时用 refresh token 自动续期。
    续期不可用时提示重新运行 ``protobot login``。
    """
    if not online_mode:
        return offline_username, None, None

    loaded = load_profile(cache_file)
    if loaded is None:
        raise SystemExit(
            "[错误] 未找到正版凭据缓存。请先运行 protobot login 完成一次微软账号授权。"
        )
    profile, refresh_options = loaded

    if not profile.expired:
        info(f"[凭据] 已从本地缓存读取正版账号: {profile.name}")
        return profile.name, profile.access_token, profile.id

    if not profile.refresh_token:
        raise SystemExit(
            "[错误] 缓存令牌已过期且没有续期令牌，请重新运行 protobot login 授权。"
        )

    info(f"[凭据] 缓存令牌已过期，正在为 {profile.name} 自动续期...")
    try:
        profile = await refresh_login(profile.refresh_token, **refresh_options)
    except Exception as error:
        raise SystemExit(
            f"[错误] 自动续期失败，请重新运行 protobot login 授权。原因: {error}"
        ) from error

    save_profile(cache_file, profile, refresh_options)
    info(f"[凭据] 续期成功: {profile.name}")
    return profile.name, profile.access_token, profile.id


# ======================== 登录交互（自 login.py 迁入） ========================


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


# ======================== 首次启动配置向导 ========================


def _parse_address(value: str) -> tuple[str, int]:
    """解析 ``host`` 或 ``host:port``（不支持 IPv6 字面量）。"""
    if ":" in value:
        host, _, port_text = value.rpartition(":")
        if not host:
            raise ValueError("缺少主机名")
        try:
            port = int(port_text)
        except ValueError:
            raise ValueError(f"端口无效: {port_text!r}") from None
    else:
        host, port = value, 25565
    if not 1 <= port <= 65535:
        raise ValueError(f"端口必须在 1-65535 之间，得到 {port}")
    return host, port


def run_setup(config_path: Path) -> int:
    """交互式配置向导：登录方式 -> 服务器 -> 版本，写入 config.yaml。"""
    print("=" * 60)
    print("        ProtoBot 首次配置向导")
    print("=" * 60)
    print("（按 Enter 接受默认值；随时 Ctrl+C 退出）\n")

    # [1/3] 登录方式
    while True:
        choice = input(
            "[1/3] 选择登录方式: [1] 离线登录  [2] 正版登录 (默认 2): "
        ).strip() or "2"
        if choice == "1":
            mode = "offline"
        elif choice == "2":
            mode = "online"
        else:
            print("    无效输入，请输入 1 或 2。")
            continue
        break
    offline_username = "ProtoBot"
    if mode == "offline":
        name = input("[ ] 离线用户名 (默认 ProtoBot): ").strip() or "ProtoBot"
        offline_username = name[:16] or "ProtoBot"

    # [2/3] 服务器地址
    while True:
        address = input(
            "[2/3] 服务器地址 host[:port] (默认端口 25565): "
        ).strip()
        if not address:
            print("    地址不能为空。")
            continue
        try:
            host, port = _parse_address(address)
        except ValueError as error:
            print(f"    {error}")
            continue
        break

    # [3/3] 协议版本
    versions = list(SUPPORTED_VERSIONS)
    print("[3/3] 选择协议版本:")
    for index, version in enumerate(versions, start=1):
        marker = " (默认)" if version == "26.2" else ""
        print(f"    [{index}] {version}{marker}")
    while True:
        choice = input("[ ] 请输入序号 (默认 26.2): ").strip() or "26.2"
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(versions):
                version = versions[index - 1]
                break
        if choice in versions:
            version = choice
            break
        print(f"    无效输入，请输入 1-{len(versions)} 或版本号。")

    data = {
        "server": {"host": host, "port": port, "version": version},
        "login": {"mode": mode, "offline_username": offline_username},
        "session": {
            "reconnect": True,
            "reconnect_delay": 5.0,
            "reconnect_max_attempts": None,
        },
        "plugins": {"directory": "plugins", "disabled": [], "watch": True},
        "tui": {"enabled": True},
    }
    save_config(config_path, data)

    print("\n" + "=" * 60)
    print("【配置完成！】")
    print(f"登录方式: {'正版登录' if mode == 'online' else '离线登录'}")
    print(f"服务器: {host}:{port}  版本: {version}")
    print(f"配置文件已保存到: {config_path}")
    print("=" * 60)
    print("下一步:")
    if mode == "online":
        print("  1. 运行 protobot login 完成一次微软账号授权")
    print("  2. 运行 protobot run（或在 PyCharm 中右键运行 run_bot.py）连服")
    return 0


# ======================== 子命令 ========================


async def run_login(args: argparse.Namespace) -> int:
    print("=" * 60)
    print("      ProtoBot 微软正版账号授权向导")
    print("=" * 60)

    if args.auth_code:
        print("[方式] 授权码流程（需复制粘贴地址）")
        profile = await authorization_code_login(prompt_callback=prompt_for_code)
        client_id = None
        azure_ad = False
    elif args.azure_client_id:
        print("[方式] 设备码流程（使用你的 Azure 应用）")
        profile = await device_code_login(
            args.azure_client_id, prompt_callback=show_device_code
        )
        client_id = args.azure_client_id
        azure_ad = True
    else:
        print("[方式] 设备码流程（输验证码，无需注册）")
        profile = await device_code_login(prompt_callback=show_device_code)
        client_id = None
        azure_ad = False

    cache_file = args.config.with_name("auth_cache.json")
    save_profile(cache_file, profile, {"azure_ad": azure_ad, "client_id": client_id})

    print("\n" + "=" * 60)
    print("【登录成功！】")
    print(f"玩家昵称: {profile.name}")
    print(f"玩家 UUID: {profile.id}")
    print(f"凭据已自动保存到: {cache_file.name}")
    if profile.refresh_token:
        print("已保存续期令牌，后续连接会自动刷新，无需重复授权。")
    else:
        print("提示: 本次未获得续期令牌，令牌过期后需要重新运行本命令。")
    print("注意: 该文件包含账号访问令牌，请勿分享或提交到版本库。")
    print("现在你可以运行 protobot run（或在 PyCharm 中右键运行 run_bot.py）连服了！")
    print("=" * 60)
    return 0


async def run_bot_session(args: argparse.Namespace) -> int:
    config_path = args.config
    try:
        data = load_config(config_path)
    except OSError:
        print(f"[错误] 找不到配置文件: {config_path}。可用 --config 指定路径。")
        return 2
    except ValueError as error:
        print(f"[错误] {error}")
        return 2

    try:
        session_config = load_session_config(data)
        plugin_config = load_plugin_config(data, config_path.parent)
    except ValueError as error:
        print(f"[错误] {error}")
        return 2

    manager = PluginManager(
        [plugin_config.directory], disabled=plugin_config.disabled
    )
    try:
        manager.discover()
    except PluginError as error:
        print(error)
        return 1
    if manager.load_order():
        print(
            f"[插件] 已加载 {len(manager.load_order())} 个插件: "
            + ", ".join(plugin.name for plugin in manager.load_order())
        )

    print("=" * 60)
    print("           ProtoBot 机器人启动器")
    print("=" * 60)

    cache_file = config_path.with_name("auth_cache.json")
    session = BotSession(
        session_config,
        credentials=lambda: get_credentials(
            cache_file,
            online_mode=session_config.online_mode,
            offline_username=session_config.offline_username,
        ),
        plugin_manager=manager,
    )

    watcher = PluginWatcher(manager) if plugin_config.watch else None
    watcher_task: asyncio.Task | None = None
    if watcher is not None:
        watcher_task = asyncio.create_task(
            watcher.run(), name="protobot-plugin-watcher"
        )
        print("[插件] 热更新监视已启动（编辑 plugins/ 下的文件即生效）。")
    try:
        if tui_enabled(load_tui_config(data)):
            # 单事件循环：Textual App 与 session 任务共存。Textual 运行期会
            # 捕获 stdout（print 会丢失），因此 protobot.log 的 sink 直连
            # 代理队列进入日志区；会话由界面内的 .run 命令启动，UI 退出
            # （Ctrl+C）→ request_stop + 等待会话优雅结束。
            proxy = StdoutProxy()
            app = ProtoBotApp(session, manager, proxy)
            await manager.enable_all()
            set_sink(lambda line: proxy.write(line + "\n"))
            try:
                await app.run_async()
            finally:
                session.request_stop()
                if app.session_task is not None:
                    await app.session_task
                set_sink(None)  # 恢复 print 路由
                await manager.disable_all()
        else:
            container = BotContainer(plugin_manager=manager)
            container.add_session("default", session)
            await container.run()
    finally:
        if watcher is not None:
            watcher.request_stop()
            if watcher_task is not None:
                await watcher_task
    return 0


def list_plugins(args: argparse.Namespace) -> int:
    config_path = args.config
    data: dict = {}
    if config_path.exists():
        try:
            data = load_config(config_path)
        except ValueError as error:
            print(f"[错误] {error}")
            return 2
    plugin_config = load_plugin_config(data, config_path.parent)

    manager = PluginManager(
        [plugin_config.directory], disabled=plugin_config.disabled
    )
    try:
        manager.discover()
    except PluginError as error:
        print(error)
        return 1

    if not manager.plugins:
        print("[插件] 未发现插件。")
        return 0

    config_disabled = set(plugin_config.disabled)
    print("[插件] 已发现插件:")
    for name in sorted(manager.plugins):
        plugin = manager.plugins[name]
        source = manager.source_of(name)
        if name in config_disabled:
            status = "禁用（配置）"
        elif name in manager.disabled_names():
            status = "禁用（依赖被禁用）"
        else:
            status = "启用"
        deps = ", ".join(plugin.dependencies) or "无"
        print(f"  {name:<24s} 依赖: {deps:<28s} {status}  ({source})")
    order = [plugin.name for plugin in manager.load_order()]
    print(f"[插件] 加载顺序: {' -> '.join(order) if order else '(无)'}")
    return 0


# ======================== 入口 ========================


def _root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protobot", description="ProtoBot 机器人命令行工具"
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{login,run,plugins,setup}"
    )

    login_parser = sub.add_parser("login", help="微软正版账号授权并保存凭据")
    login_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help="配置文件路径（用于推导凭据缓存位置，默认 config.yaml）",
    )
    login_parser.add_argument(
        "--auth-code", action="store_true", help="改用授权码流程（需复制粘贴地址）"
    )
    login_parser.add_argument(
        "--azure-client-id", default="", help="使用自己的 Azure 应用 ID 走设备码"
    )

    run_parser = sub.add_parser("run", help="连接服务器并运行插件")
    run_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help="配置文件路径（默认 config.yaml）",
    )

    plugins_parser = sub.add_parser("plugins", help="列出已发现的插件")
    plugins_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help="配置文件路径（默认 config.yaml）",
    )

    setup_parser = sub.add_parser("setup", help="重新进入交互式配置向导")
    setup_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help="配置文件路径（默认 config.yaml）",
    )
    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "setup":
        return run_setup(args.config)
    if not args.config.exists():
        print("[提示] 未找到配置文件，进入首次配置向导...\n")
        code = run_setup(args.config)
        if code != 0:
            return code
    if args.command == "login":
        return await run_login(args)
    if args.command == "run":
        return await run_bot_session(args)
    return list_plugins(args)


def main(argv: list[str] | None = None) -> int:
    """Console-script 入口：``protobot login|run|plugins|setup``。

    argv 缺省为 ``sys.argv[1:]``；run_bot.py 薄壳直接调用 ``main(["run", ...])``。
    """
    parser = _root_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print("\n[退出] 已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
