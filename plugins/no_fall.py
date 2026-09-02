"""Force the on-ground bit off in outgoing movement packets.

This plugin changes only the wire flag. The local physics predictor continues
to track its real landing and fall-distance state, so enabling the plugin does
not create a movement loop or send extra packets.
"""

from __future__ import annotations

import asyncio

from protobot import Plugin, PluginSettings, log

DEFAULT_SETTINGS: dict = {
    "enabled": True,
}

RELOAD_INTERVAL = 5.0


class NoFall(Plugin):
    name = "no_fall"

    def __init__(self) -> None:
        super().__init__()
        self._config: PluginSettings | None = None
        self._settings: dict = dict(DEFAULT_SETTINGS)
        self._reload_task: asyncio.Task | None = None
        self.expose(
            "status",
            self._service_status,
            description=(
                "Show whether no-fall is enabled. When enabled, every player "
                "and vehicle movement packet carries on_ground=false."
            ),
            llm=True,
        )
        self.expose(
            "set",
            self._service_set,
            description="Enable or disable the no-fall movement packet flag.",
            parameters={
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "description": "Force on_ground=false in outgoing movement packets",
                    }
                },
                "required": ["enabled"],
            },
            llm=True,
            admin=True,
        )

    async def on_enable(self) -> None:
        self._config = self.settings_file(
            "no_fall.json",
            DEFAULT_SETTINGS,
            label="no_fall",
            normalize=self._normalize,
        )
        self._settings = self._config.load()
        self._apply()
        self._reload_task = asyncio.create_task(
            self._reload_loop(), name="protobot-no-fall-settings"
        )
        log.info(
            f"[no_fall] {'enabled' if self._settings['enabled'] else 'loaded but off'} "
            f"(settings: {self._config.path})"
        )

    async def on_disable(self) -> None:
        task = self._reload_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._reload_task = None
        bot = self.bot
        if bot is not None:
            bot.set_no_fall(False)
        log.info("[no_fall] stopped")

    async def on_bot_ready(self) -> None:
        """Apply the setting to each fresh connection after reconnect."""

        self._apply()

    def _apply(self) -> None:
        bot = self.bot
        if bot is None:
            return
        enabled = bool(self._settings.get("enabled", True))
        bot.set_no_fall(enabled)
        log.debug(f"[no_fall] outgoing on_ground flag {'forced false' if enabled else 'restored'}")

    async def _reload_loop(self) -> None:
        while True:
            await asyncio.sleep(RELOAD_INTERVAL)
            try:
                if self._config is not None and self._config.reload_if_changed():
                    self._settings = self._config.data
                    self._apply()
                    log.info(
                        f"[no_fall] settings reloaded: "
                        f"{'enabled' if self._settings['enabled'] else 'disabled'}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error(f"[no_fall] failed to read settings: {error!r}")

    async def _service_status(self) -> str:
        enabled = bool(self._settings.get("enabled", True))
        bot = self.bot
        connection = "connected" if bot is not None and not bot.closed.is_set() else "disconnected"
        return f"No-fall is {'on' if enabled else 'off'}; {connection}"

    async def _service_set(self, enabled: bool) -> str:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        if self._config is None:
            return "No-fall settings are not loaded"
        error = self._config.patch({"enabled": enabled})
        self._settings = self._config.data
        self._settings["enabled"] = enabled
        self._apply()
        if error:
            log.warn(f"[no_fall] could not save settings: {error}")
            return f"No-fall {'enabled' if enabled else 'disabled'} in memory; save failed: {error}"
        return f"No-fall {'enabled' if enabled else 'disabled'}"

    @staticmethod
    def _normalize(merged: dict) -> dict:
        merged["enabled"] = bool(merged.get("enabled", True))
        return merged
