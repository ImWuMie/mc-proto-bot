# ProtoBot

[English](README.md) | 简体中文

现代化的 Python 3.12+ Minecraft 协议客户端，同时支持**离线模式（offline-mode）**与**正版验证（online-mode，Mojang / 微软账号认证）**。

ProtoBot 直接基于 asyncio TCP 套接字实现完整的原版协议栈——握手、登录、配置、游戏四个阶段全部覆盖——离线使用保持零第三方依赖，正版验证模式通过可选的 `cryptography` 支持 RSA/AES-CFB8 流式加密。它内置确定性的客户端物理引擎（行走、疾跑、跳跃、潜行、船、旁观者飞行），基于精确碰撞箱的 A\* 寻路器，以及事件驱动的高层 `Bot` API。

## 功能特性

- **完整协议栈**——握手 → 登录 → 配置 → 游戏全流程，含 keep-alive、传送确认、区块解码与服务器转移，全部经过边界检查、行为确定。
- **正版与离线双支持**——完整支持 Mojang session-server 正版加密验证（RSA/AES-CFB8 8位流式加密）、微软 OAuth 交互登录（默认授权码流程，可选设备码流程）及离线模式。
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

或使用交互式微软账号登录。默认走**授权码流程**，配合公开的启动器 client ID
即可，无需你自己注册 Azure 应用：

```python
import asyncio
from protobot import authorization_code_login, connect

async def main():
    # 打印微软登录链接，然后要求粘贴回跳地址
    profile = await authorization_code_login()

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

也可以传入自己的提示函数以接入 GUI，它收到登录 URL、返回用户粘贴的内容：

```python
profile = await authorization_code_login(prompt_callback=my_prompt)
```

登录完成后，微软会在回跳页显示一个反钓鱼提示页：「你已进入一个通常不会显示的
页面。Microsoft 绝不会要求你复制或分享此 URL」。该提示针对的是"骗你把地址转发
给他人"的钓鱼手法；粘贴到本机脚本里，令牌不会离开你的电脑。不过这终究不是理想
模式，而回环地址（`http://localhost:...`）在公开启动器 client ID 上会被拒绝
（返回 `invalid_request`），因此**彻底免去复制粘贴的办法是用自己的应用走设备码
流程**。

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

### 设备码登录（需要你自己的 Azure 应用）

设备码是体验最好的流程——在浏览器里输入一个短验证码即可，无需复制任何东西，
也不会出现上面的提示页——但微软只对在 Azure AD 注册过的应用开放它。公开的
启动器 client ID **无法**完成该流程：微软会先发出设备码，随后拒绝发放令牌并提示
「用户需要重新登录或需要用户交互」。因此 `device_code_login()` 要求显式传入
`client_id`，而不是等用户输完验证码才失败。

注册是免费的，几分钟即可：在 [Azure 门户](https://portal.azure.com) 进入
*Microsoft Entra ID → 应用注册 → 新注册*，账户类型选「仅个人 Microsoft 账户」，
创建后在*身份验证*页打开「允许公共客户端流」。

```python
profile = await device_code_login("<你的 Azure 应用 ID>")
...
profile = await refresh_login(profile.refresh_token, "<你的 Azure 应用 ID>", azure_ad=True)
```

续期必须回到签发该令牌的那一套端点，这正是 `azure_ad=True` 的作用；`login.py`
会把这一点记录到缓存里，`run_bot.py` 便能自动选对端点续期。

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
