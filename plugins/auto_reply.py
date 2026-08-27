"""自动回复示例插件：收到以 "hey,claude" 开头的玩家消息时回复 "1"。

演示要点：
  - 通过 subscribe() 注册事件（异常会被框架隔离，不会打断连接）
  - self.bot 每次调用时重读：掉线重连后 bot 对象会更换，不能缓存
  - 依赖其他插件时声明 dependencies = ("xxx",)，框架按拓扑序加载
"""

from protobot import Plugin, plain_text


class AutoReply(Plugin):
    name = "auto_reply"

    def __init__(self) -> None:
        super().__init__()
        self.subscribe("player_chat", self._on_player_chat)

    async def _on_player_chat(
        self, sender_uuid, name, message, chat_type_id, target_name
    ) -> None:
        if plain_text(message).startswith("hey,claude"):
            await self.bot.send_message("1")
