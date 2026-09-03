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
- **事件总线**——可订阅聊天、区块、实体、容器、血量/死亡、玩家加入/退出与原始数据包事件。
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
    await bot.fly_to(50.0, 90.0, -30.0)                 # 碰撞感知的三维飞行

    await bot.close()

asyncio.run(main())
```

`fly_to` 默认使用原版飞行物理，但会屏蔽 abilities 发包。它只临时开启本地
物理状态，`player.abilities` 仍保持服务端快照。旧版参数 `force_flight` 和
`bypass_permission` 仍可传入，但不会检查权限，也不会发送 abilities 包。

可选的 `no_fall` 插件会把所有玩家和载具移动包的 on-ground 位强制改为
`false`。它只修改线上包，本地物理仍记录真实落地状态。插件被发现时默认开启，
可编辑 `plugins/no_fall.json`，也可以使用 LLM 工具 `no_fall_status` 和
`no_fall_set` 切换。

飞行寻路默认使用滚动即时规划：每次只规划前方一小段，飞行过程中根据最新坐标
和区块快照继续规划。传入 `realtime=False` 可改为一次性规划整条路径，或用
`planning_horizon` 调整分段长度（默认 8 格）。

`timeout` 限制整次飞行操作的总时间，包括等待区块、寻路、预取下一段和移动；
`planning_timeout` 限制每次后台 A* 规划的最长等待时间（默认
`min(timeout, 10)` 秒）。任一超时都会抛出 `NavigationTimeout`，并清理未完成的
预取任务和本地飞行状态。

客户端只拥有服务器当前下发的区块。`get_status` 会显示已加载区块边界和估算半径；
实际范围由服务器 `view_distance` 以及区块发送/卸载策略共同决定。

如果服务器或代理会踢出长时间悬停的玩家，可以在本地配置 anti-kick：

```yaml
navigation:
  anti_kick: true
  anti_kick_interval: 1.0
```

Packet 模式会按 Meteor 的逻辑，在周期到达时只把移动包里的 Y 改为
`lastPacketY - 0.03130`，本地预测坐标不会被改动。它可能降低空中挂机被踢的概率，
但不能授予服务器飞行权限，也不能绕过服务器端移动校验。它不会额外创建心跳包；
飞行寻路每个物理 tick 也会把朝向和位置合并成一个 `PositionRotation` 包，避免重复
移动包导致反作弊过快判定。

服务器主动断开时，日志会显示解析后的踢出原因和截断后的原始数据：
`[disconnect] server kick reason=... payload_hex=...`。网络或协议异常也会记录异常类型
和具体原因。

飞行寻路默认开启 VClip。上下穿墙距离仍由本地配置限制（单位为方块）：

```yaml
navigation:
  vclip: true
  vclip_up_limit: 3.0
  vclip_down_limit: 2.0
```

也可以只对某次调用开启：

```python
await bot.fly_to(50.0, 10.0, -30.0, vclip=True)
```

VClip 只放宽连续竖直段，横向穿墙仍然禁止；上下限制分别统计一次连续穿墙的距离。
寻路阶段的半格节点只用于精确搜索，执行时会把同一连续竖直段合并为一个终点 VClip
动作，因此一段穿墙只发送一个位置包，不会每半格重复发送。

### 事件订阅

```python
import asyncio
from protobot import connect, plain_text

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

    # 谁在线、什么时候变化。进服时的整份名单走 player_list，
    # 所以 join/leave 真的就是有人来了、有人走了。
    @bot.on("player_join")
    async def on_join(entry):
        print("加入:", entry.name, "->", bot.online_players)

    @bot.on("player_leave")
    async def on_leave(entry):
        print("退出:", entry.name)

    # 自己的生死：death 带死亡消息组件（血量归零那一路为 None），
    # 死后会一直停在死亡界面，直到有人调用 bot.respawn()——
    # plugins/respawn.py 会替你做这件事。
    @bot.on("death")
    async def on_death(message):
        print("死亡:", plain_text(message) if message else "?")

    @bot.on("respawn")
    async def on_respawn(session):
        print("重生于", session.dimension_name)

    await bot.send_message("hello from ProtoBot")
    await bot.send_command("say hello from ProtoBot")
    await asyncio.sleep(5)
    await bot.close()

asyncio.run(main())
```

聊天组件里装的是翻译键而不是句子，`plain_text()` 会用内置的 `en_us` 模式
把它**格式化**出来：`{"translate": "chat.type.text", "with": ["Steve", "hi"]}`
渲染成 `<Steve> hi`，原版全套死亡消息也都在表里。服务器自带的 `fallback`
优先于内置表；表里没有的键会连同参数一起显示，而不是把内容丢掉。服务器自定义
的键用 `register_translations({...})` 补，或者用 `load_translations()` 直接读
一份 Mojang 的 `en_us.json`。

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
tui:
  enabled: true             # 真终端下启用全屏界面
  autostart: true           # 凭据齐全时启动即连服
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
- **暴露能力**：`self.expose("name", handler, llm=True)` 把函数发布为
  `"<插件>.<名字>"`。其他插件用 `await self.call("fishing.status")` 调用；
  标了 `llm=True` 还会自动进入 LLM 智能体的工具表（工具名 `fishing_status`，
  用 `description` 与 JSON Schema 的 `parameters` 描述），`admin=True` 则让
  智能体对非管理员拒绝该工具。插件禁用或热重载时暴露会被撤回，不会调到旧
  实例；handler 抛出的异常会传给调用方而不是被吞掉。

### LLM 智能体插件（llm_agent）

`plugins/llm_agent.py` 把机器人变成游戏内 LLM 智能体（类 Hermes Agent）：
维护 **agent 对话上下文**，通过 `read_chat` 工具查询最近 **200 条聊天记录**
（可按玩家/关键词/系统广播过滤），并用 OpenAI 兼容的 function calling 执行
动作——发消息、执行命令、行走/寻路、转头（绝对/相对）、查询玩家位置、
查看游戏状态、自检运行状态（`get_system_info`：模型、上下文占用、连接时长、
插件与任务数）、开关/修改(patch)/读取/删除插件、编写新插件、管理定时任务、
用仅管理员可调用的 `start_bot` 启动配置中的 bot 会话、把聊天交给一个什么都没有的
**副 AI**，以及维护**按服务器分开的 Markdown 记忆**
（`MEMORY.md`，可多文件）并自主更新。

**首次运行**会自动生成 `plugins/llm_agent.json`——填好端点与密钥后保存一次
`llm_agent.py` 触发热重载即可：

```json
{
  "llm": {
    "base_url": "https://api.openai.com/v1",   // 任意 OpenAI 兼容端点
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "max_tokens": 1000000,          // 模型上下文窗口
    "compact_reserve_ratio": 0.05,  // 预留 5%，超出后自动压缩旧对话
    "system_blocks": true,          // 系统提示词按块发送（content 数组）
    "cache_control": false          // 给最后一块打 {"type":"ephemeral"} 缓存标记
  },
  "speaker": {                  // 可选的副 AI：专门用来回聊天
    "enabled": false,
    "base_url": "",             // 留空 = 与上面同一个端点
    "api_key": "",              // 留空 = 用上面的 key
    "model": "",                // 留空 = 用上面的 model
    "max_tokens": 300,          // 一条回复的生成上限（不是上下文窗口）
    "temperature": 1.0
  },
  "reply": {
    "all": false,               // true = 回应每条聊天
    "name_mention": true,       // 聊天包含自己名字时回应
    "prefix": "hey,claude",     // 特殊前缀（留空 "" 表示不使用）
    "keywords": ["claude"],     // 额外关键词触发（忽略大小写）
    "attention_seconds": 15,    // 回复后对该玩家的持续注意窗口（秒，0 关闭，默认 0）
    "duplicate_window": 10      // 同一玩家的同一句话，这么多秒内只处理一次
  },
  "admins": ["你的名字"],       // 只有管理员能写插件/开关插件（[] = 不限制）
  "system_prompt": "...",       // 可选，覆盖内置提示词
  "history_limit": 200,         // read_chat 保留的聊天条数
  "persona_file": "llm_agent_persona.md",
  "skills_dir": "../.claude/skills",
  "memory_dir": "llm_agent_memory",
  "generated_dir": "../plugins_llm",
  "qq": {                       // 可选的 QQ 机器人桥接（protobot[qq]）
    "enabled": false,
    "appid": "",                // 来自 QQ 开放平台
    "token": "",
    "sandbox": false,           // bot 在沙箱环境时设为 true
    "admin_ids": [],            // 视为管理员的 QQ openid（[] = 无）
    "trust_players": []         // 允许其发言转发给你的 MC 玩家名
  }
}
```

- **QQ 桥接**（`qq`，默认关闭；需要 `pip install protobot[qq]`，portable 已内置）——同一个
  agent 接进 QQ：C2C 私聊与群内 @ 消息像其他触发一样进队列，回复走 QQ 发回，
  而不是发到 Minecraft 聊天。即使没连服务器也能工作。需要 QQ 开放平台的
  appid/token，在 `llm_agent.json` 里配 `enabled`、`appid`、`token`（约 3 秒
  生效）。请求者名字形如 `[QQ] <openid>`；QQ 用户**默认不是**管理员，只有
  openid 在 `admin_ids` 里才算——`admins` 留空放开限制不会把权限漏给 QQ 上
  的陌生人。bot 还会**记住**每个联系过它的用户/群（首次遇到时 openid 会以
  「QQ contact learned」打进日志，之后可主动发消息），并暴露 `send_qq`
  （仅管理员）与 `qq_contacts`；把自己的 openid 填进 `admin_ids`，定时
  任务就能在 QQ 上 ping 你。列进 `trust_players` 的玩家，其发言也可以
  转发给你（`send_qq` 用 `to='owner'`）；他们不是管理员，够不到别的联系人。
- **副 AI 专管闲聊**（`speaker`，默认关闭）：打开后主 AI 多一个 `speak`
  工具——把某人说的那句话**原样**转给第二个模型，它回什么就发到聊天里。
  这个副 AI **什么都没有**：请求里只有一条 user 消息，没有系统提示词、没有
  人物预设、没有记忆、没有聊天记录、也不带工具表。这正是它的意义：便宜、
  不占主 AI 的上下文，而且聊天里的任何内容都不可能把它牵到别处去——因为它
  根本没有「别处」。代价是它对服务器一无所知，所以什么时候值得交给它由主 AI
  判断：需要查到的事实、需要用工具、需要看是谁在问的，主 AI 自己用
  `send_message` 回。留空的字段沿用主 AI 的端点/密钥/模型/超时，所以通常只
  需要把 `model` 指向一个小模型就够了；`max_tokens` 限制它一次能说多长。
  `speak` 失败会明确告诉主 AI「自己回」，而 `send_message` **不会**经过副 AI。
  `get_system_info` 会报是哪个模型在说话，但不报端点。
- **控制台直接对话**：TUI 里输入 `.llm 内容` 就是一次完整的 agent 回合，回复
  打印在日志区、**不发到游戏聊天**，工具照常可用（叫它去聊天里说话，它会用
  `send_message`）。控制台按**管理员**对待——能启动这个进程的人本来就能改配置
  文件，所以写插件之类的管理工具对它开放；但它的身份是个内部标记，玩家取不到
  这个名字，也就冒充不了。没装/禁用了 `llm_agent` 时命令会明确说不可用。
- **提示词缓存**：系统提示词按**稳定性从高到低**分块发送——整段静态提示词、
  技能清单、身份（名字与服务器）、人物预设、记忆、待办。里面不再放任何每次
  请求都变的东西：时间改由触发消息携带（`[HH:MM] <玩家>: 内容`）。这样系统
  提示词加上对话历史在两次请求之间**逐字节一致**，端点才可能命中缓存——以前
  「Current time」排在第二块，每秒都不一样，于是除了第一块之外全都白算，
  这就是命中率低的原因。只认字符串的端点把 `system_blocks` 设为 false；需要
  显式缓存断点的端点把 `cache_control` 设为 true（多数端点不需要，故默认关）。
  `get_system_info` 会报当前块数与标记状态。
- **持续注意**（默认关闭）：把 `attention_seconds` 设成秒数后，真的回复过某个
  玩家的话，他会进入这么长的注意窗口。窗口内他的后续发言即便没提到 bot 也会
  送进来，标记为 `(follow-up)`，由 LLM 判断这句是不是在跟自己说话——是就接着
  聊，话题已经转走就输出 `NO_REPLY` 保持安静。每次回复都会续上窗口；
  `NO_REPLY` 不会开窗口。注意窗口内每条发言都会产生一次 API 调用。
- **人物预设**：`plugins/llm_agent_persona.md`（首次运行生成模板）是自由撰写
  的 Markdown 角色设定——性格、经历、喜好、说话习惯。每次构建提示词时都会
  重读，因此**保存文件即生效**，无需重启也无需热重载插件。它只影响语气人格，
  不授予权限，也不能放宽信任规则。
- **写插件读技能**：写插件的权威契约是 `.claude/skills/protobot-plugin/SKILL.md`，
  智能体用 `list_skills` / `read_skill` 现读——系统提示词只留不可省的核心，
  详细规则不再内联（内联的那一份已经和框架漂移过一次）。技能目录可用
  `skills_dir` 指定。
- **插话**：写插件常要好几轮工具调用，这期间**同一个玩家**的新发言会并入
  正在跑的那一轮（标记 `(interjection)`），所以可以中途改主意、补要求；
  别人的发言仍排队走各自的回合，不会顺带获得本轮的管理员权限。
- **待办清单**：与记忆文件同目录的 `TODO.md` 是一份 Markdown 清单，由智能体
  用 `todo_add` / `todo_list` / `todo_done` / `todo_remove` 维护。未完成项会
  注入每次提示词，所以它答应过的事重启也不会忘；完成的项不再占用上下文。
  条目按**文字子串**定位而不是下标，匹配到多条会拒绝执行而不是瞎猜。
- **重复触发过滤**：同一个玩家的同一句话，若还排在队里、或
  `duplicate_window` 秒内（默认 10）刚处理过，直接丢掉。玩家连按回车、
  或几个触发条件同时命中，否则每一次都要花一次 API 调用。
- **记忆**按服务器存放在 `llm_agent_memory/<host>_<port>/MEMORY.md`，智能体
  通过 `read_memory` / `save_memory` / `write_memory` / `clear_memory` 自主
  维护，每次对话都会带上。
- **管理工具**（`write_plugin`、`patch_plugin`、`set_plugin`、`remove_plugin`）
  仅限 `admins` 名单；生成的插件写入独立的 `plugins_llm/` 目录（与手工
  `plugins/` 分开），重启后自动恢复。`remove_plugin` 会关掉插件并**删除源
  文件**，不可撤销（只想停掉用 `set_plugin` 的 `enabled: false`）；它拒绝删
  `llm_agent` 自己，否则智能体连自己一起没了。
- **私聊**：形如 `[玩家 -> me] ...` 的系统私聊消息总是触发回复；发送者参与
  管理员权限判定。
- `llm_agent.json`、记忆目录与 `plugins_llm/` 已加入 .gitignore——设置文件
  含 api_key，切勿提交到版本库。

### 定时任务插件（scheduler）

`plugins/scheduler.py` 按时间、游戏事件或状态条件自动发送聊天、执行服务器
命令。任务存放在 `plugins/scheduler.json`（首次运行生成，内含一个默认禁用的
示例）：

```json
{
  "tasks": [
    {"name": "晚间问候", "time": "18:00", "action": "chat",
     "text": "晚上好！", "enabled": true},
    {"name": "清理提醒", "interval": 1800, "action": "command",
     "text": "say 该清理掉落物啦", "enabled": true},
    {"name": "迎新", "event": "player_join", "action": "chat",
     "text": "欢迎 {player}！", "cooldown": 5, "enabled": true},
    {"name": "开门", "event": "player_chat", "match": "开门",
     "action": "command", "text": "say 来了来了", "enabled": true},
    {"name": "血量告警", "condition": "health < 8", "action": "remind",
     "text": "血量只剩 {health} 了，想想办法", "enabled": true}
  ]
}
```

- 四种触发方式可以组合，至少给一个：`interval`（秒，最小 5）循环执行；
  `time`（`HH:MM` 24 小时本地时间）每天一次；`event` 由游戏事件触发
  （`player_chat`、`system_chat`、`player_join`、`player_leave`、`death`、
  `respawn`）；`condition` 由状态条件触发。
- **`condition` 单独出现时是触发器**——条件由假变真的那一刻执行一次，而不是
  「条件为真就每秒来一遍」；**与 `interval`/`time`/`event` 同时出现时是开关**，
  到点或事件发生时条件不成立就跳过这一次。条件是比较式（`<`、`<=`、`>`、
  `>=`、`==`、`!=`）用 `and` 连接，变量有 `health`、`food`、`players`（tab
  列表人数）、`entities`、`x`、`y`、`z`、`dead`、`hour`、`minute`，例如
  `players > 4 and dead == false`。不支持 `or`，也不会 eval 任何代码：条件是
  被解析的，写错在建任务的那一刻就被拒绝，而不是等到执行时才炸。
- `cooldown` 是同一任务两次执行的最小间隔（秒）——迎新任务值得设一下，
  否则十个人同时进服就会发十条。`match` 只在事件内容（聊天内容、玩家名、
  死亡消息）包含该子串时才触发，两个聊天事件**必须**给它：不给的话任何人
  说任何话都会触发一次。`text` 里含着自己 `match` 的任务会被拒绝——那会
  一直触发自己；bot 也会忽略服务器回显的自己的话（按名字，以及按 10 秒内
  自己说过的内容）。
- `text` 支持占位符，执行时替换：`{player}`、`{message}`（死亡消息）、
  `{bot}`、`{health}`、`{food}`、`{players}`、`{x}`、`{y}`、`{z}`、`{hour}`、
  `{minute}`。其他花括号内容原样保留，命令语法不会被吃掉。
- `action` 为 `chat`（发聊天）、`command`（执行命令）或 `remind`（把内容交给
  LLM 智能体，由它决定说什么、做什么）；`enabled: false` 暂停该任务。
- 文件改动后 5 秒内自动重新加载，无需重启或热重载插件。未连接服务器时到期
  任务顺延，不会丢失。
- **LLM 智能体可以直接管理这些任务**：插件暴露了 `scheduler.list` / `add` /
  `set` / `remove` / `run`（立即执行一次）/ `status`，会自动成为智能体的工具，
  除 `list`、`status` 外都仅限管理员。在游戏里说「每 30 分钟提醒大家吃饭」
  就能建好任务，说「有人进服就打个招呼」「有人说开门就应一声」「血量低于 8
  就告诉我」同样能建出事件/条件任务。校验规则只写在插件里，所以文件、服务调用、智能体三条路径
  遵守同一套规则。
- **叫醒智能体**：`action: remind` 调用暴露出来的 `llm_agent.remind`，内容作为
  「提醒」进入 LLM 而不是被原样念出来——「每小时看看有没有人需要帮忙」这种任务
  就由它自己决定做什么、或者干脆不说话。提醒不携带管理员权限；没装智能体插件时
  任务会被跳过并提示。
- 协议 774（1.21.11）上玩家列表与 combat death 的包 ID 未经核实，所以那个
  版本上 `player_join`、`player_leave`、`death` 不会触发；聊天、
  `system_chat` 与 `respawn` 各版本都可用。

### 自动钓鱼插件（fishing）

`plugins/fishing.py` 循环执行抛竿 → 判定咬钩 → 收杆。三路信号任一命中即收杆，
按可靠性排序：

1. **咬钩音效**（最准，原版就是靠它提示玩家）——`entity.fishing_bobber.splash`
   由位置型音效包（协议 775/776 为 0x75 = 117）发出，坐标是**浮标所在处**，
   而甩竿/收杆的音效发在**玩家所在处**，所以「音效位置离浮标 `sound_radius`
   格内」既能认出咬钩，也能排除自己甩竿和别人钓鱼。音效数字 ID 逐版本变动且
   不由服务端下发（`minecraft:sound_event` 是内置注册表），因此这里**不硬编码**：
   第一次靠位置认出咬钩时把 ID 学下来，之后要求 ID 也匹配。包 ID 取自所连版本
   的包表——该版本的 ID 未经核实时（如 1.21.11）这一路直接关闭，而不是拿推断值
   去解析。也可用 `sound_id` / `sound_packet_id` 手动固定。
2. **向下速度**——`entity_motion` 报出浮标的向下速度。原版咬钩那一刻把浮标的
   Y 速度设成 `-0.4 × [0.6, 1.0]`（即 -0.24 ~ -0.4 格/tick）并同时播放溅水
   音效，所以这一路和音效路本是同一个瞬间的两种表现。
3. **位置下沉**——浮标从静止水面基准线被拽下超过 `bite_drop`。

浮标的认领：优先从服务端下发的 `minecraft:entity_type` 注册表里查
`minecraft:fishing_bobber`；查不到就把「抛竿后 `spawn_window` 秒内、
`spawn_radius` 格内新生成的实体」当作浮标，并**记住它的 type_id**，之后精确认领。
万一记错了（认成了掉落物、经验球，或注册表下标对不上），下一竿认领失败时会在
`spawn_window` 过后启用候选并**纠正 type_id**——否则一次记错就整场瞎，再也认不
出浮标。

第 2、3 路在浮标出现 `settle_delay` 秒后开始生效（默认 1.2 秒，抛出去到落水
就这么点时间）：朝下抛竿时初速度本身就是负的，不做门控会一抛竿就误判。这里
**故意不用**「连续几次位置更新几乎不动」来判定落稳——浮标停在水面上时服务端
根本不再发位置包，那种判定常常永远等不到，两路信号被永久门控住，全靠音效路
撑着；音效再有一点问题就整小时钓不上鱼。音效路不需要门控，只有真咬钩才响。

`water_check`（默认开）在浮标就位后查它脚下那两格是不是水，连续 5 次读到「不是
水」就立刻重抛，而不是干等满 `max_wait`。区块还没收到时判为「未知」，不会瞎重抛。

`max_wait` 秒没咬钩会收杆重抛，浮标被移除也会重抛。收杆到重抛之间只隔
`recast_delay`。

设置在 `plugins/fishing.json`（首次运行生成，改动 5 秒内自动重载），默认
**`enabled: false`**，改成 true 才开始钓；也可以直接让智能体开——插件暴露了
`fishing.start` / `fishing.stop` / `fishing.status`，在游戏里说「开始钓鱼」
即可（start/stop 仅管理员）。手持鱼竿需要你自己保证：协议栈拿不到物品名称，
插件无法校验手里到底是不是鱼竿。

### 自动重生插件（respawn）

`plugins/respawn.py` 让 bot 死了自己站起来。死亡后玩家会一直停在死亡界面上，
**只有客户端主动请求**才会重生——服务端从不自己动手，即便开了
`doImmediateRespawn` 也只是原版客户端不显示死亡界面而已。没有这一步，死掉的
bot 就一直躺着：物理不跑、走不动、也不说话。

死亡判定来自核心的 `death` 事件，它合并两路信号，**一次死亡只发一次**：

1. **Combat Death**（协议 775/776 的 0x44 = 68）——这个包的用途就是「让客户端
   弹出死亡界面」，所以服务端在玩家死亡时必然发它，也带着死亡消息（插件会记
   下来）。按自己的实体 ID 过滤，旁边别人死了不算。
2. **血量归零**（`set_health`，0x68 = 104）——兜底。服务端在 tick 边界补发、
   顺序不受协议保证，而且服务器插件可以缩放血量，所以它不单独作为依据。

重生动作是 Client Status（服务端方向 0x0C，载荷只有一个 VarInt，0 = perform
respawn），由 `await bot.respawn()` 发出。之后服务端回重生包 + 一次带 teleport
id 的位置同步；确认传送与补发 Player Loaded 由核心处理（重生处理会清掉
`player.loaded`）。插件把 `respawn` 事件当作确认，等不到就按 `max_retries` 重试。

设置在 `plugins/respawn.json`（首次运行生成，改动 5 秒内自动重载），**默认开启**：

```json
{
  "enabled": true,
  "delay": 1.0,
  "retry_delay": 2.0,
  "max_retries": 2,
  "announce": "",
  "return_to_death_point": false,
  "return_max_distance": 200.0
}
```

- `announce` 非空则重生后发这句聊天，留空不说话。
- `return_to_death_point` 打开后会寻路走回死亡坐标，除非重生点离死亡点超过
  `return_max_distance` 格。
- 插件暴露 `respawn.status`、`respawn.now`（跳过延迟立刻重生）与 `respawn.set`
  （后两个仅管理员），所以在游戏里问「你死了吗」、说「别自动重生了」都能生效；
  `get_status` 也会一并报血量、饱食度和是否正躺在死亡界面上。
- 协议 774（1.21.11）上这三个包 ID **未经核实**，`bot.respawn()` 会抛
  `UnsupportedVersion`，插件提示一次就停手，绝不拿推断值发包。

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
| `.run` | 启动 bot |
| `.stop` | 停止 bot（保持界面） |
| `.plugins` | 列出已加载插件 |
| `.llm 内容` | 把内容交给 LLM 智能体，回复打印在日志区（不发到游戏聊天） |
| `.help` | 显示可用命令 |
| ↑ / ↓ | 翻输入历史（正在打的那行会原样回来） |
| PageUp / PageDown | 翻日志。上翻会暂停「自动跟随最新」，新日志不再把视图拽回底部；翻回底部自动恢复跟随 |
| Ctrl+L | 直接回到最新一行并恢复跟随 |

- **翻日志靠键盘，不靠滚轮。** 滚轮要终端把鼠标事件转发进来，而多路复用器
  默认不转发：GNU `screen` 要在 `.screenrc` 里 `mousetrack on`，tmux 要
  `set -g mouse on`——所以 SSH + screen 下滚轮完全没反应。终端自己的回滚也翻
  不到日志，因为全屏应用跑在**备用屏缓冲**里，内容根本不进回滚缓冲区。
  PageUp/PageDown 是纯键盘序列，哪一层都不需要配置，所以这里用它们。
  在输入框里发任何东西也会自动回到最新一行。

- **自动启动**：配置齐全到能直接连（离线模式始终齐全；正版模式需要
  `protobot login` 缓存过、且令牌未过期或还能续期）时，会话会自己启动，
  `.run` 只在 `.stop` 之后或缺凭据时才需要。用 `[tui] autostart = false`
  可关闭。
- **真终端**（Windows Terminal / VS Code 终端 / macOS / Linux）自动启用
  全屏界面，**Ctrl+C 退出**。
- **PyCharm 控制台、管道、CI** 自动降级为普通逐行日志（bot 同样自动启动）；
  未安装 extra 时会打印一次提示并同样降级。
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
| `plugin.py` | 插件框架：目录发现、依赖拓扑排序、异常隔离、热加载/热重载/热关闭、`expose()` 服务 |
| `settings.py` | 插件伴生配置：默认值、深合并、mtime 热重载、单键写回 |
| `session.py` | `BotSession` 会话（重连循环）与 `BotContainer` 容器 |
| `text.py` | 聊天组件转纯文本（`plain_text`） |
| `translations.py` | 内置 `en_us` 翻译表，含 `register_translations` / `load_translations` |
| `config.py` | 零依赖的 YAML 子集编解码（`config.yaml`） |
| `cli_app.py` | 统一 CLI：`protobot login|run|plugins|setup` |
| `tui.py` | Textual 全屏 TUI（可选 `tui` extra）与普通日志降级 |
| `data/` | 内置各版本方块状态表 |
| `cli.py` | 诊断控制台命令 |
| `plugins/` | 示例插件（chat_logger、llm_agent、scheduler、fishing、no_fall、respawn） |
| `config.yaml` | 本地配置文件（首次启动向导生成，不入库） |

## 开发

```bash
uv sync --extra online          # 安装可选运行时依赖与开发工具
python -m compileall protobot plugins run_bot.py   # 快速语法检查
```

## 构建发布

版本号只有一处：`protobot/__init__.py` 里的 `__version__`，命令行会跟着显示：
`protobot --version`。一条命令构建整个发布：

```bash
uv sync --extra online --extra tui   # 一次性：装齐开发工具（含 PyInstaller）
python release.py                    # -> dist/ 下产出全部产物
```

一次发布包含两种形态：

- **`protobot-x.y.z.tar.gz` / `.whl`**——pip/uv 包。wheel 自带方块状态表与
  内置示例插件；`protobot setup` 会在配置旁生成一个初始 `plugins/` 目录，
  所以 pip 安装同样拥有完整的插件系统和示例。
- **`protobot-x.y.z-<平台>-portable.zip`**（windows-amd64 与
  linux-x86_64）——自包含构建：`protobot.exe` / `protobot` 自带整套
  Python 运行时，外加示例插件与说明文档。解压到任意位置，在终端里运行
  它即可——**什么都不用装，不需要安装 Python**。不带参数直接运行等同
  `protobot run`（双击就能启动 bot）；首次运行会先走一遍配置向导。

`release.py packages` / `release.py portable` 可以只构建其中一种。`dist/`
已加入 .gitignore。仓库里的 GitHub Actions 工作流会在推送 `v*` tag 时自动
构建并把产物挂到 GitHub Release 上，所以发布流程是：

```bash
git tag v1.0.0
git push origin main --tags
```

（推完 tag 后到 GitHub 给该 tag 创建 Release，workflow 会自动补上产物。）
离线模式在 wheel 里依然是零依赖——`[online]` 与 `[tui]` 始终是可选 extra，
导入 `protobot` 从不需要它们。

## 注意事项与限制

- **正版与离线模式。** 离线模式保持零第三方依赖。若需连接开启 `online-mode=true` 的正版服务器，请安装 `protobot[online]`（引入 `cryptography`）。
- **在线账号支持安全聊天。** ProtoBot 会获取账号的临时玩家证书、注册新的聊天会话、签署 `send_message()` 消息、维护 last-seen 确认窗口，并在连接期间刷新证书。离线模式仍使用无签名兼容路径；`send_command()` 继续使用普通命令包。
- 物理预测以原版 26.2 默认值为基准；重度定制移动反作弊的服务器仍可能发出位置纠正。
- `python -m compileall protobot plugins run_bot.py` 是固定的语法检查。

## 许可证

MIT，见 [LICENSE](LICENSE)。你可以自由使用、修改与再分发 ProtoBot，
包括用在闭源和商业项目里——只要保留版权与许可声明即可。
