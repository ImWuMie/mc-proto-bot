"""控制台聊天记录插件：打印系统消息与玩家消息。

事件签名（协议 774-776）：
  system_chat(component, overlay)
  player_chat(sender_uuid, name, message, chat_type_id, target_name)
"""

from protobot import Plugin, log, plain_text


class ChatLogger(Plugin):
    name = "chat_logger"

    def __init__(self) -> None:
        super().__init__()
        self.subscribe("system_chat", self._on_system_chat)
        self.subscribe("player_chat", self._on_player_chat)

    async def _on_system_chat(self, component, overlay) -> None:
        log.info("[sys]", plain_text(component))

    async def _on_player_chat(
        self, sender_uuid, name, message, chat_type_id, target_name
    ) -> None:
        log.info("[chat]", plain_text(name), ":", plain_text(message))
