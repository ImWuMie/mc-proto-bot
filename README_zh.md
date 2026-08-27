# ProtoBot

[English](README.md) | 简体中文

现代化的 Python 3.12+ Minecraft 协议客户端，同时支持**离线模式（offline-mode）**与**正版验证（online-mode，Mojang / 微软账号认证）**。

ProtoBot 直接基于 asyncio TCP 套接字实现完整的原版协议栈——握手、登录、配置、游戏四个阶段全部覆盖——离线使用保持零第三方依赖，正版验证模式通过可选的 `cryptography` 支持 RSA/AES-CFB8 流式加密。它内置确定性的客户端物理引擎（行走、疾跑、跳跃、潜行、船、旁观者飞行），基于精确碰撞箱的 A\* 寻路器，以及事件驱动的高层 `Bot` API。

## 功能特性

- **完整协议栈**——握手 → 登录 → 配置 → 游戏全流程，含 keep-alive、传送确认、区块解码与服务器转移，全部经过边界检查、行为确定。
- **正版与离线双支持**——完整支持 Mojang session-server 正版加密验证（RSA/AES-CFB8 8位流式加密）、微软 OAuth 交互登录（默认授权码流程，可选设备码流程）及离线模式。
- **SRV 记录解析**——像原版客户端一样查询 `_minecraft._tcp`，自动连到地址所发布的后端主机与端口。
- **多版本支持**——开箱支持 Minecraft `1.21.11`、`26.1`、`26.1.1`、`26.1.2`、`26.2`（内置各版本方块状态表）。
- **客户端物理**——20 Hz 确定性物理引擎，精确复刻原版移动逻辑，含船载物理与实体硬碰撞。
- **导航寻路**——基于解码后世界的 A\* 路径规划与执行，支持自动重规划。
- **模组加载器握手**——支持 Forge、NeoForge、Fabric 客户端模组声明，以及 Velocity modern forwarding。
- **事件总线**——可订阅聊天、区块、实体、容器与原始数据包事件。
- **诊断 CLI**——针对本地服务器的在线回归检查与移动轨迹采集。

## 安装

需要 Python 3.12+。

```bash
# 离线模式（零第三方依赖）
python -m pip install -e .

# 包含正版验证支持
python -m pip install -e ".[online]"

# 或使用 uv
uv sync --extra online
```

## 快速开始

最简单的入口是 `connect()` 辅助函数，它会返回一个已完成出生的机器人：

```python
import asyncio
from protobot import connect, MovementInput

async def main():
    bot = await connect("127.0.0.1", username="MyBot", version="26.2")
    print(f"已登录 {bot.username}，位置 ({bot.player.x:.1f}, {bot.player.y:.1f}, {bot.player.z:.1f})")

    # 通过物理引擎向前走 40 个 tick
    for _ in range(40):
        await bot.tick(MovementInput(forward=1.0))
        await asyncio.sleep(0.05)

    await bot.close()

asyncio.run(main())
```

### 行走与寻路

```python
import asyncio
from protobot import connect

async def main():
    bot = await connect("127.0.0.1", username="MyBot")

    # 等待第一个区块解码完成，然后走动
    await bot.wait_world()
    await bot.walk_to(10.5, 20.5, sprint=True)          # 直线行走
    await bot.navigate_to(50.0, -30.0, sprint=True)     # A* 路线 + 自动重规划

    await bot.close()

asyncio.run(main())
```

### 事件订阅

```python
import asyncio
from protobot import connect

async def main():
    bot = await connect("127.0.0.1", username="MyBot")

    @bot.on("system_chat")
    async def on_chat(component, overlay):
        print("聊天:", component)

    @bot.on("close")
    async def on_close(reason):
        print("断开连接:", reason)

    await bot.send_command("say hello from ProtoBot")
    await asyncio.sleep(5)
    await bot.close()

asyncio.run(main())
```

### 服务器地址与 SRV 记录

多数公开服务器通过 `_minecraft._tcp` SRV 记录发布真实的后端主机与端口，也就是说
玩家输入的地址往往并不是实际连接的地址。`connect()` 会像原版客户端那样跟随这类
记录——仅在未指定端口时生效，显式端口始终优先：

```python
bot = await connect("play.example.com")                     # 跟随 SRV 记录
bot = await connect("play.example.com", port=25565)          # 显式端口：按原样连接
bot = await connect("play.example.com", resolve_srv=False)   # 完全不查 SRV
```

缺少这一步时，只在 SRV 目标上监听的服务器会接受你在输入地址上的 TCP 连接
（或者你连到的其实是一个 DNS 占位地址），随后直接关闭，表现为
`ConnectionClosed: server closed the connection`。可以订阅 `srv_resolved` 观察
重定向，或读取 `bot.connected_host` / `bot.connected_port` 查看实际拨号的地址：

```python
@bot.on("srv_resolved")
def on_srv(original, host, port):
    print(f"{original} -> {host}:{port}")
```

如需单独使用，`resolve_minecraft_srv(host)` 也已导出。它走操作系统解析器（因此
分离解析与 VPN DNS 都能生效），对 IP 字面量、无记录、解析器不可达等情况返回
`None`，表示"直接连接即可"。

### 正版验证登录（Mojang / 微软账号）

直接传入 `access_token` 与 `profile_uuid`：

```python
import asyncio
from protobot import connect

async def main():
    bot = await connect(
        "mc.example.com",
        username="PlayerName",
        access_token="<你的_minecraft_access_token>",
        profile_uuid="<你的_player_uuid>",
        version="26.2",
    )
    print("正版连接成功:", bot.username, bot.uuid)
    await bot.close()

asyncio.run(main())
```

或使用交互式微软账号登录。默认是 `device_code_login()`：打开一个**已预填验证码**
的链接，在浏览器里确认即可，无需复制任何东西回来。它配合公开的启动器 client ID
工作，不需要注册 Azure 应用：

```python
import asyncio
from protobot import connect, device_code_login

async def main():
    # 打印已预填验证码的链接，然后等待浏览器端授权
    profile = await device_code_login()

    bot = await connect(
        "mc.example.com",
        username=profile.name,
        access_token=profile.access_token,
        profile_uuid=profile.id,
        version="26.2",
    )
    print("已登录正版账号:", bot.username)
    await bot.close()

asyncio.run(main())
```

浏览器里的登录必须**一路走到最后的确认页**。中途放弃会让设备码保持未授权状态，
微软会在下一次轮询时返回「用户须重新登录或需要用户交互」——重试并把浏览器流程
走完即可。

Minecraft 访问令牌大约一天过期。登录同时会返回续期令牌（refresh token），
因此缓存的凭据可以自动续期，无需再次授权：

```python
from protobot import refresh_login

if profile.expired and profile.refresh_token:
    profile = await refresh_login(profile.refresh_token)
```

当续期令牌本身被吊销或过期时，`refresh_login` 会抛出 `AuthenticationError`，
此时重新授权即可。仓库自带的 `login.py` 与 `run_bot.py` 就是这么做的：
授权一次，之后自动续期反复连服。

### 备选一：授权码流程

`authorization_code_login()` 同样无需注册。它打印登录 URL，并接收浏览器回跳的地址：

```python
profile = await authorization_code_login()                       # 在 stdin 上询问
profile = await authorization_code_login(prompt_callback=my_ui)  # 或接入自己的 UI
```

微软会在回跳页显示反钓鱼提示：「你已进入一个通常不会显示的页面。Microsoft 绝不会
要求你复制或分享此 URL」。该提示针对的是"骗你把地址转发给他人"的手法；粘贴到本机
脚本里，令牌不会离开你的电脑。回环地址（`http://localhost:...`）本可免去复制，但在
公开启动器 client ID 上会被拒绝（`invalid_request`），所以优先用上面的设备码流程。

### 备选二：用自己的 Azure 应用走设备码

这是微软官方支持该授权的路径。传入 Azure 应用 ID 即自动切换到 Azure 端点：

```python
profile = await device_code_login("<你的 Azure 应用 ID>")
...
profile = await refresh_login(profile.refresh_token, "<你的 Azure 应用 ID>")
```

注册免费：在 [Azure 门户](https://portal.azure.com) 进入 *Microsoft Entra ID →
应用注册 → 新注册*，账户类型选「仅个人 Microsoft 账户」，创建后在*身份验证*页
打开「允许公共客户端流」。

续期必须回到签发该令牌的那一套端点。传入相同的 `client_id` 就会自动选对（启动器 ID
表示 MSA，其他表示 Azure AD）；`azure_ad=True/False` 可强制指定。`login.py` 会把
这一点记录到缓存里，`run_bot.py` 便能正确续期。

## 诊断 CLI

安装后会附带三个控制台命令：

```bash
# 对已运行的服务器执行登录、世界加载、keep-alive 及可选移动检查
# （也可用 --jar / --accept-eula 自动启动本地服务器 jar）
protobot-live-regression 127.0.0.1 --version 26.2 --movement-ticks 40

# 采集确定性移动轨迹（行走、疾跑、潜行）并写入 JSON 文件
protobot-movement-matrix 127.0.0.1 --output trace.json

# 将 Mojang 的 reports/blocks.json 转换为 ProtoBot 紧凑方块表
protobot-export-block-states reports/blocks.json --output data/blocks-26.2.json.gz --version 26.2
```

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `client.py` | `Bot` 高层 API 与 `connect()` |
| `auth.py` | Mojang 会话加入、RSA/AES-CFB8 加密、微软 OAuth 登录与令牌续期 |
| `protocol/` | 传输编解码、封帧、NBT、连接状态机、版本表 |
| `physics/` | 确定性移动引擎、碰撞几何、船载物理 |
| `navigation.py` | A\* 寻路器 |
| `srv.py` | 零依赖的 `_minecraft._tcp` SRV 查询 |
| `world.py` / `state.py` | 世界/区块解码、方块状态注册表、实体/物品栏状态 |
| `modlist.py` | Forge/NeoForge/Fabric 加载器适配、Velocity forwarding |
| `data/` | 内置各版本方块状态表 |
| `cli.py` | 诊断控制台命令 |

## 开发

```bash
uv sync --extra online          # 安装可选运行时依赖与开发工具
uv run python -m compileall .   # 快速语法检查
uv run pytest                   # 运行 tests/ 下的单元测试
```

测试基于标准库 `unittest` 编写，因此在不额外安装任何东西的情况下也能运行：

```bash
python -m unittest discover -s tests -t .
```

其中覆盖正版验证的测试在缺少可选依赖 `cryptography` 时会自动跳过。

## 注意事项与限制

- **正版与离线模式。** 离线模式保持零第三方依赖。若需连接开启 `online-mode=true` 的正版服务器，请安装 `protobot[online]`（引入 `cryptography`）。
- 物理预测以原版 26.2 默认值为基准；重度定制移动反作弊的服务器仍可能发出位置纠正。
- `python -m compileall .` 是固定的语法检查；单元测试位于 `tests/` 目录下。

## 许可证

版权归仓库所有者所有。
