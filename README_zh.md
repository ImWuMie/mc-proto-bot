# ProtoBot

[English](README.md) | 简体中文

现代化的 Python 3.12+ Minecraft 协议客户端，同时支持**离线模式（offline-mode）**与**正版验证（online-mode，Mojang / 微软账号认证）**。

ProtoBot 直接基于 asyncio TCP 套接字实现完整的原版协议栈——握手、登录、配置、游戏四个阶段全部覆盖——离线使用保持零第三方依赖，正版验证模式通过可选的 `cryptography` 支持 RSA/AES-CFB8 流式加密。它内置确定性的客户端物理引擎（行走、疾跑、跳跃、潜行、船、旁观者飞行），基于精确碰撞箱的 A\* 寻路器，以及事件驱动的高层 `Bot` API。

> 🙏 **鸣谢** — 本项目的 Bot 核心与协议底座基于
> [ImAlexBlock](https://github.com/ImAlexBlock) 提供的工作。

## 功能特性

- **完整协议栈**——握手 → 登录 → 配置 → 游戏全流程，含 keep-alive、传送确认、区块解码与服务器转移，全部经过边界检查、行为确定。
- **正版与离线双支持**——完整支持 Mojang session-server 正版加密验证（RSA/AES-CFB8 8位流式加密）、微软 OAuth 交互登录（默认授权码流程，可选设备码流程）及离线模式。
- **SRV 记录解析**——像原版客户端一样查询 `_minecraft._tcp`，自动连到地址所发布的后端主机与端口。
- **多版本支持**——开箱支持 Minecraft `1.21.11`、`26.1`、`26.1.1`、`26.1.2`、`26.2`（内置各版本方块状态表）。
- **客户端物理**——20 Hz 确定性物理引擎，精确复刻原版移动逻辑，含船载物理与实体硬碰撞。
- **导航寻路**——基于解码后世界的 A\* 路径规划与执行，支持自动重规划。
- **模组加载器握手**——支持 Forge、NeoForge、Fabric 客户端模组声明，以及 Velocity modern forwarding。
- **事件总线**——可订阅聊天、区块、实体、容器与原始数据包事件。
- **插件系统与统一 CLI**——`plugins/` 目录自动发现插件，支持前置插件依赖、拓扑排序加载、异常隔离、热加载/热重载/热关闭；`protobot login|run|plugins|setup` 一个命令搞定授权、连服与插件管理，掉线自动重连。
- **诊断 CLI**——针对本地服务器的在线回归检查与移动轨迹采集。

## 安装

需要 Python 3.12+。

```bash
# 离线模式（零第三方依赖）
python -m pip install -e .

# 包含正版验证支持
python -m pip install -e ".[online]"

# 包含 TUI 界面支持
python -m pip install -e ".[tui]"

# 或使用 uv
uv sync --extra online --extra tui
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

    # 玩家消息与服务器广播走不同的数据包：签名消息走 player_chat 包，
    # 未签名消息走 profileless 包。两者都会被解码并从这里发出。
    @bot.on("player_chat")
    async def on_player_chat(sender_uuid, name, message, chat_type_id, target_name):
        print("玩家:", name, "说", message)

    @bot.on("close")
    async def on_close(reason):
        print("断开连接:", reason)

    await bot.send_message("hello from ProtoBot")
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
此时重新授权即可。命令行工具就是这么做的（见下一节的 `protobot login` 与
`protobot run`）：授权一次，之后自动续期反复连服。

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
表示 MSA，其他表示 Azure AD）；`azure_ad=True/False` 可强制指定。`protobot login`
会把这一点记录到缓存里，`protobot run` 便能正确续期。

## 机器人 CLI 与插件系统

除了作为库使用，ProtoBot 还带一个统一的命令行入口，把原来的授权脚本与启动
脚本合并成了子命令：

```bash
protobot login     # 微软正版账号授权（设备码流程，凭据自动缓存）
protobot run       # 连接服务器、运行插件、掉线自动重连
protobot plugins   # 列出已发现的插件与加载顺序
protobot setup     # 重新进入交互式配置向导
```

**首次启动**任何子命令时，如果本地没有 `config.yaml`，会先进入交互式配置
向导：依次选择登录方式（离线/正版）、服务器地址、协议版本，然后自动写入
配置：

```yaml
server:
  host: "wolfx.jp"
  port: 25565
  version: "26.2"
login:
  mode: online              # online | offline
  offline_username: "ProtoBot"
session:
  reconnect: true           # 掉线自动重连
  reconnect_delay: 5.0      # 重连间隔（秒），默认每 5 秒一次
  reconnect_max_attempts: null   # 可选：最大重连次数（null = 无限）
plugins:
  directory: "plugins"      # 相对本配置文件所在目录
  disabled: []              # 例: ["hello_reply"]
  watch: true               # 监视插件目录，文件变化即热加载/热重载/热关闭
```

正版登录的凭据缓存在配置文件旁边的 `auth_cache.json`，`login` 与 `run`
在任意工作目录下都指向同一份。仓库根目录的 `run_bot.py` 是给 PyCharm
右键运行的薄壳，等价于 `protobot run`。

### 编写插件

插件是 `Plugin` 的子类，放在 `plugins/` 目录下（`[plugins] directory` 可改）。
每个插件声明 `name` 与可选的前置插件 `dependencies`，框架按依赖拓扑排序加载：

```python
from protobot import Plugin, plain_text

class HelloReply(Plugin):
    name = "hello_reply"
    dependencies = ("chat_logger",)   # 前置插件：必须先于本插件加载

    def __init__(self):
        super().__init__()
        # 注册 bot 协议事件（异常会被框架隔离，不会打断连接）
        self.subscribe("player_chat", self._on_player_chat)
        # 注册会话生命周期事件
        self.subscribe_session("session_disconnected", self._on_disconnect)

    async def _on_player_chat(self, sender, name, message, chat_type_id, target):
        if plain_text(message).startswith("hey,claude"):
            await self.bot.send_message("1")
```

要点：

- **事件注册**：`subscribe()` 注册 bot 协议事件（`player_chat`、
  `system_chat`、`world`、`entity_add`……与 `bot.on` 同名同参）；
  `subscribe_session()` 注册会话生命周期事件（`session_start`、
  `session_connecting`、`session_ready`、`session_disconnected`、
  `session_stop`）。handler 抛出的异常只会打印回溯，**不会**导致掉线。
- **`self.bot` 每次调用时重读**：掉线重连会生成全新的 Bot 对象，框架在
  重连后自动把订阅重新绑定到新 bot；缓存 bot 引用会拿到已关闭的旧对象。
  每只 bot 就绪时会调用一次 `on_bot_ready()`，适合放按连接初始化的状态。
- **生命周期**：`on_enable()` / `on_disable()` 每个进程各一次；
  `on_enable` 里创建的任务跨重连存活，请在 `on_disable` 里自行取消
  （框架绝不强制取消插件任务）。
- **热更新**：`plugins.watch = true`（默认）时，编辑 `plugins/` 下的文件
  保存即热重载——新增文件热加载、修改文件热重载、删除文件热关闭。语法
  错误或依赖缺失的重载会被拒绝，**旧插件继续运行**，不会打断在线 bot。
- **配置开关**：`[plugins] disabled = ["hello_reply"]` 可禁用插件，依赖
  被禁用插件的插件会被一并禁用并提示。

### LLM 智能体插件（llm_agent）

`plugins/llm_agent.py` 把机器人变成游戏内 LLM 智能体（类 Hermes Agent）：
维护 **agent 对话上下文**，通过 `read_chat` 工具查询最近 **200 条聊天记录**
（可按玩家/关键词/系统广播过滤），并用 OpenAI 兼容的 function calling 执行
动作——发消息、执行命令、行走/寻路、转头（绝对/相对）、查询玩家位置、
查看游戏状态、自检运行状态（`get_system_info`：模型、上下文占用、连接时长、
插件与任务数）、开关/修改(patch)/读取插件源码、编写新插件、管理定时任务，
以及维护**按服务器分开的 Markdown 记忆**（`MEMORY.md`，可多文件）并自主更新。

**首次运行**会自动生成 `plugins/llm_agent.json`——填好端点与密钥后保存一次
`llm_agent.py` 触发热重载即可：

```json
{
  "llm": {
    "base_url": "https://api.openai.com/v1",   // 任意 OpenAI 兼容端点
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "max_tokens": 1000000,          // 模型上下文窗口
    "compact_reserve_ratio": 0.05   // 预留 5%，超出后自动压缩旧对话
  },
  "reply": {
    "all": false,               // true = 回应每条聊天
    "name_mention": true,       // 聊天包含自己名字时回应
    "prefix": "hey,claude",     // 特殊前缀（留空 "" 表示不使用）
    "keywords": ["claude"]      // 额外关键词触发（忽略大小写）
  },
  "admins": ["你的名字"],       // 只有管理员能写插件/开关插件（[] = 不限制）
  "system_prompt": "...",       // 可选，覆盖内置提示词
  "history_limit": 200,         // read_chat 保留的聊天条数
  "memory_dir": "llm_agent_memory",
  "generated_dir": "../plugins_llm"
}
```

- **记忆**按服务器存放在 `llm_agent_memory/<host>_<port>/MEMORY.md`，智能体
  通过 `read_memory` / `save_memory` / `write_memory` / `clear_memory` 自主
  维护，每次对话都会带上。
- **管理工具**（`write_plugin`、`set_plugin`）仅限 `admins` 名单；生成的插件
  写入独立的 `plugins_llm/` 目录（与手工 `plugins/` 分开），重启后自动恢复。
- **私聊**：形如 `[玩家 -> me] ...` 的系统私聊消息总是触发回复；发送者参与
  管理员权限判定。
- `llm_agent.json`、记忆目录与 `plugins_llm/` 已加入 .gitignore——设置文件
  含 api_key，切勿提交到版本库。

### 定时任务插件（scheduler）

`plugins/scheduler.py` 按计划自动发送聊天或执行服务器命令。任务存放在
`plugins/scheduler.json`（首次运行生成，内含一个默认禁用的示例）：

```json
{
  "tasks": [
    {"name": "晚间问候", "time": "18:00", "action": "chat",
     "text": "晚上好！", "enabled": true},
    {"name": "清理提醒", "interval": 1800, "action": "command",
     "text": "say 该清理掉落物啦", "enabled": true}
  ]
}
```

- `interval`（秒，最小 5）循环执行；`time`（`HH:MM` 24 小时本地时间）每天
  执行一次——两者至少给一个。`action` 为 `chat` 或 `command`；
  `enabled: false` 暂停该任务。
- 文件改动后 5 秒内自动重新加载，无需重启或热重载插件。未连接服务器时到期
  任务顺延，不会丢失。
- **LLM 智能体可以直接管理这些任务**：`schedule_list`、`schedule_add`、
  `schedule_set`、`schedule_remove`、`schedule_run`（立即执行一次），除
  `schedule_list` 外都仅限管理员。也就是说在游戏里说「每 30 分钟提醒大家
  吃饭」就能建好任务，说「把提醒取消」就能删掉。

### 全屏 TUI 界面（可选）

`protobot run` 支持 Claude Code 风格的全屏界面：上方滚动日志区（会话与
插件的全部输出）、紧贴输入框上方的三栏状态条（bot 名字 / 坐标 / 服务器·
版本·模式·连接时长）、**底部输入框**。界面基于
[Textual](https://github.com/Textualize/textual)，作为可选依赖安装，核心
仍然零依赖：

```bash
uv sync --extra tui              # 或 pip install -e ".[tui]"
uv run protobot run              # 在真终端（Windows Terminal 等）中运行
```

输入框支持三种输入，输入 `.` 时自动弹出命令提示下拉框：

| 输入 | 行为 |
| --- | --- |
| 普通文本 | 发送聊天消息 |
| `/命令` | 执行服务器命令（如 `/say hi`） |
| `.run` | 启动 bot（进入界面后 bot 默认不启动） |
| `.stop` | 停止 bot（保持界面） |
| `.plugins` | 列出已加载插件 |
| `.help` | 显示可用命令 |

- **真终端**（Windows Terminal / VS Code 终端 / macOS / Linux）自动启用
  全屏界面，**Ctrl+C 退出**。
- **PyCharm 控制台、管道、CI** 自动降级为普通逐行日志（此时 bot 照常
  自动启动，无需 `.run`）；未安装 extra 时会打印一次提示并同样降级。
- 配置开关：`[tui] enabled = false` 可彻底关闭（默认 `true`）。
- **插件日志**：TUI 运行期间 Textual 会吞掉 `print()` 输出——插件应使用
  `from protobot import log` 的 `log.info/warn/error/debug(...)`（调用格式
  与 `print` 相同），这些输出会自动进入日志区；非 TUI 模式下降级为普通
  print。

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
| `plugin.py` | 插件框架：目录发现、依赖拓扑排序、异常隔离、热加载/热重载/热关闭 |
| `session.py` | `BotSession` 会话（重连循环）与 `BotContainer` 容器 |
| `text.py` | 聊天组件转纯文本（`plain_text`） |
| `config.py` | 零依赖的 YAML 子集编解码（`config.yaml`） |
| `cli_app.py` | 统一 CLI：`protobot login|run|plugins|setup` |
| `tui.py` | Textual 全屏 TUI（可选 `tui` extra）与普通日志降级 |
| `data/` | 内置各版本方块状态表 |
| `cli.py` | 诊断控制台命令 |
| `plugins/` | 示例插件（chat_logger、llm_agent、scheduler） |
| `config.yaml` | 本地配置文件（首次启动向导生成，不入库） |

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
- **发送的聊天消息不带签名。** `send_message()` 在不强制安全聊天的服务器（大多数插件服）上可用。若服务器开启 `enforce-secure-profile=true`，消息会被丢弃或拒绝——签名需要账号本地的聊天密钥对，只有 access token 的机器人无法取得。命令不受影响：`send_command()` 走的是普通命令包。
- 物理预测以原版 26.2 默认值为基准；重度定制移动反作弊的服务器仍可能发出位置纠正。
- `python -m compileall .` 是固定的语法检查；单元测试位于 `tests/` 目录下。

## 许可证

版权归仓库所有者所有。
