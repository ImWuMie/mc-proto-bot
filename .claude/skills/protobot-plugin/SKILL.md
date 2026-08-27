---
name: protobot-plugin
description: >-
  ProtoBot 插件开发指南：Plugin 基类契约（name/dependencies）、事件清单与签名、
  重连下 self.bot 的更替规则、热更新语义、可用 Bot API 与写作约束。
  当用户要求编写、修改、调试 plugins/ 目录下的插件，或询问插件系统用法时使用本指南。
---

# ProtoBot 插件编写指南

编写 `plugins/*.py` 插件时严格遵守本契约。所有事实以 `protobot/plugin.py`、
`protobot/session.py`、`protobot/client.py`、`protobot/text.py` 为准。

## 最小骨架

```python
from protobot import Plugin, plain_text

class MyPlugin(Plugin):
    name = "my_plugin"                 # 必填，全仓唯一
    dependencies = ("chat_logger",)    # 可选：前置插件，必须先于本插件加载

    def __init__(self):
        super().__init__()
        self.subscribe("player_chat", self._on_player_chat)   # bot 协议事件
        self.subscribe_session("session_ready", self._on_ready)  # 会话生命周期事件

    async def _on_player_chat(self, sender, name, message, chat_type_id, target):
        if plain_text(message).startswith("hey,claude"):
            await self.bot.send_message("1")
```

## 事件清单（签名以 client.py 的 emit 调用为准）

### 会话生命周期事件（`subscribe_session`，挂在 session 自有总线上）

| 事件 | 参数 |
| --- | --- |
| `session_start` | () |
| `session_connecting` | (attempt: int) |
| `session_ready` | (bot: Bot) — 连接成功、插件已绑定后 |
| `session_disconnected` | (reason: str \| None, attempt: int) |
| `session_stop` | () |

### Bot 协议事件（`subscribe`，挂在 bot.events 上，与 `bot.on` 同名同参）

聊天：
- `system_chat` (component, overlay) — component 为解码后的 NBT（str/dict/list）
- `player_chat` (sender_uuid\|None, name, message, chat_type_id\|None, target_name\|None)
  — profileless 包 sender 为 None；渲染用 `plain_text()`

世界/区块：
- `world` (WorldSessionState) · `respawn` (WorldSessionState)
- `world_ready` (world) · `chunk` (Chunk) · `chunk_unload` (chunk_x, chunk_z)
- `chunk_batch` (batch_size) · `section_blocks_update` (updates)
- `block_update` (x, y, z, state_id)

实体：
- `entity_add` (EntityState) · `entity_move` (entity_id, entity\|None)
- `entity_teleport` (entity_id, entity\|None, relative) · `entities_remove` (ids, removed)
- `entity_motion` (entity_id, velocity, entity\|None) · `entity_data` (entity_id, updates, entity\|None)
- `equipment` (entity_id, updates) · `passengers` (vehicle_id, passenger_ids)
- `effect_update` (entity_id, effect_id, identifier, effect) · `effect_remove` (...)

容器/物品栏：
- `inventory` (slot, item) · `container_open` (ContainerState)
- `container_content` (ContainerState) · `container_slot` (ContainerState, slot)
- `container_close` (ContainerState)

状态/其他：
- `position` (PlayerState) · `abilities` (PlayerAbilities) · `game_mode` (int)
- `game_event` (event_id, value) · `attributes` (entity_id, updates)
- `login` (bot) · `ready` (bot) · `reconfiguration` (bot) · `transfer` (host, port)
- `error` (BaseException) · `close` (reason: str\|None)
- `packet` (RawPacket) · `packet:{state}:{id}` (RawPacket) — 原始包，兜底用
- `path` (NavigationPath, attempt) · `gliding_collision` (damage)

`login_plugin_request`/`cookie_request`/`configuration_payload`/`mod_payload`/
`play_payload`/`registry` 是配置阶段/模组相关事件，一般插件无需处理。

## 铁律（违反会出真 bug）

1. **handler 异常已被框架隔离**：`subscribe`/`subscribe_session` 包装了每个 handler，
   异常只会打印 `[插件] <name> 处理事件时出错` + 回溯，**不会**导致掉线。
   因此不要在 handler 里吞异常或自己包 try/except（会掩盖问题）。
   不要重新抛出 `CancelledError` 之外的东西指望框架处理。
2. **`self.bot` 每次调用时重读**：掉线重连会创建全新的 Bot 对象，框架自动把订阅
   重绑到新 bot。缓存 `self.bot` 会拿到已关闭的旧对象；`self.bot` 在退避间隙为
   None。需要按连接初始化的状态放 `on_bot_ready()`（每只 bot 一次，在绑定后调用）。
3. **`on_enable` 里创建的任务自己管**：跨重连存活，必须在 `on_disable` 里取消
   （框架绝不强制取消插件任务）。`on_enable`/`on_disable` 每进程各一次。
4. **热重载 = 新实例**：保存文件即热重载（`[plugins] watch`，默认开），旧实例
   `on_disable`、新实例 `on_enable` 都会再跑一次；删除文件 = 热关闭（依赖它的插件
   一并关闭）。**模块级全局变量不跨重载保留**（每次导入用新模块名）——需要持久
   状态请写文件。语法错误/依赖缺失的重载会被拒绝，旧插件继续运行。
5. **依赖关系只声明 name**：框架按 Kahn 拓扑排序（字典序确定），环/缺失依赖会
   报错拒绝加载。被 `[plugins] disabled` 禁用的插件，其依赖者会连带禁用并提示。
6. **零第三方依赖**：插件只能 import 标准库与 `protobot`；插件文件之间不能互相
   import（插件目录不在 sys.path 上）。文件 UTF-8，中文注释与中文控制台输出
   （沿用 `[标签]` 风格）符合仓库惯例。
7. **聊天发送限制**：`send_message()` 上限 256 字符且不带签名（
   enforce-secure-profile 的服务器会丢弃）；命令走 `send_command()` 不受影响。

## 插件可用的 Bot API（public）

- 聊天：`await self.bot.send_message(text)` / `await self.bot.send_command(cmd)`（自动去 `/`）
- 移动：`await self.bot.tick(MovementInput())`（一个 20Hz 物理 tick）、
  `walk_to(x, z, sprint=False)`、`navigate_to(x, z, sprint=False)`（A*）、
  `set_flying(flag)`、`start_gliding()`
- 交互：`click_container(slot, ...)`、`close_container()`、`use_item()`、
  `select_hotbar_slot(slot)`
- 状态读取：`bot.player`（PlayerState：x/y/z、生命、yaw/pitch）、`bot.world`（区块）、
  `bot.entities`、`bot.containers`、`bot.session`、`bot.username`、`bot.uuid`、
  `bot.closed`（asyncio.Event）、`bot.disconnect_reason`
- 文本：`from protobot.text import plain_text`（str/dict/list 组件 → 纯文本，
  处理 translate+fallback 与服务器插件的空键 `{'': '123'}` 怪癖）

## 交付前检查清单

- [ ] `name` 非空且唯一；`dependencies` 里只放真实存在的插件名，无环
- [ ] 订阅在 `__init__`（或 `on_enable`）里完成；事件名与参数签名对上表
- [ ] handler 里对 `self.bot` 判空或只在 `on_bot_ready` 之后访问
- [ ] 没有 import 第三方库、没有插件间互相 import
- [ ] 有跨重连任务的，在 `on_disable` 里取消
- [ ] `uv run pytest tests/test_plugin.py` 通过；新逻辑有回归测试
  （参考 tests/test_plugin.py 的临时目录 + FakeBot 模式）
