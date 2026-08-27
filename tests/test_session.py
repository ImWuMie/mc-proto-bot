"""Tests for the bot session (reconnect policy) and the container."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from protobot.events import EventBus
from protobot.plugin import Plugin, PluginManager
from protobot.session import BotContainer, BotSession, SessionConfig


class FakeBot:
    """Minimal Bot stand-in: what BotSession touches."""

    def __init__(self, *, closed: bool = False, reason: str | None = None) -> None:
        self.closed = asyncio.Event()
        if closed:
            self.closed.set()
        self.events = EventBus()
        self.disconnect_reason = reason
        self.username = "FakeBot"
        self.player = SimpleNamespace(x=0.0, y=64.0, z=0.0)
        self.world = SimpleNamespace(chunks={})
        self.tick_count = 0
        self.close_count = 0

    async def tick(self, movement=None) -> None:
        self.tick_count += 1

    async def close(self) -> None:
        self.close_count += 1

    async def wait_world(self, *, timeout: float) -> SimpleNamespace:
        return self.world


class FakeConnector:
    """Scripted connector: outcomes repeat the last entry once exhausted."""

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls: list[tuple] = []

    async def __call__(self, config, username, access_token, profile_uuid):
        self.calls.append((config, username, access_token, profile_uuid))
        outcome = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class EventRecorder:
    def __init__(self, session: BotSession) -> None:
        self.log: list[tuple] = []
        for name in (
            "session_start", "session_connecting", "session_ready",
            "session_disconnected", "session_stop",
        ):
            session.events.on(name, self._record(name))

    def _record(self, name):
        def handler(*args):
            self.log.append((name,) + args)

        return handler

    def names(self) -> list[str]:
        return [entry[0] for entry in self.log]


def wait_for(predicate, timeout: float = 2.0) -> asyncio.Future:
    async def poll() -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while not predicate():
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("timed out waiting for condition")
            await asyncio.sleep(0.005)

    return asyncio.create_task(poll())


def make_session(*, config=None, connector, credentials=None, manager=None) -> BotSession:
    config = config or SessionConfig(
        host="127.0.0.1", reconnect_delay=0.01, reconnect_max_attempts=None
    )

    async def creds():
        return "FakeBot", None, None

    return BotSession(
        config,
        credentials=credentials or creds,
        connector=connector,
        plugin_manager=manager,
        tick_interval=0.001,
        heartbeat_ticks=10**9,  # never print heartbeats in tests
        wait_world_timeout=0.1,
    )


class SessionConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        config = SessionConfig("example.com")
        self.assertEqual(config.port, 25565)
        self.assertEqual(config.version, "26.2")
        self.assertTrue(config.online_mode)
        self.assertEqual(config.reconnect_delay, 5.0)
        self.assertIsNone(config.reconnect_max_attempts)

    def test_invalid_port(self) -> None:
        with self.assertRaises(ValueError):
            SessionConfig("example.com", port=0)
        with self.assertRaises(ValueError):
            SessionConfig("example.com", port=70000)

    def test_invalid_delays(self) -> None:
        with self.assertRaises(ValueError):
            SessionConfig("example.com", reconnect_delay=0)
        with self.assertRaises(ValueError):
            SessionConfig("example.com", reconnect_delay=-1)
        with self.assertRaises(ValueError):
            SessionConfig("example.com", connect_timeout=0)
        with self.assertRaises(ValueError):
            SessionConfig("example.com", reconnect_max_attempts=-1)


class ReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_until_the_connector_succeeds(self) -> None:
        bot = FakeBot()
        connector = FakeConnector([ConnectionError("refused"), bot])
        session = make_session(connector=connector)
        recorder = EventRecorder(session)

        task = asyncio.create_task(session.run())
        await wait_for(lambda: ("session_ready" in recorder.names()))
        session.request_stop()
        await task

        self.assertEqual(len(connector.calls), 2)
        self.assertEqual(recorder.names(), [
            "session_start",
            "session_connecting",  # attempt 1 fails
            "session_connecting",  # attempt 2 succeeds
            "session_ready",
            "session_disconnected", "session_stop",
        ])
        self.assertEqual(recorder.log[1][1], 1)  # connecting attempt 1
        self.assertEqual(recorder.log[2][1], 2)  # connecting attempt 2
        self.assertEqual(recorder.log[4][2], 2)  # disconnected attempt 2

    async def test_reconnect_disabled_stops_after_one_failure(self) -> None:
        connector = FakeConnector([ConnectionError("refused")])
        config = SessionConfig("127.0.0.1", reconnect=False, reconnect_delay=0.01)
        session = make_session(config=config, connector=connector)
        recorder = EventRecorder(session)

        await session.run()
        self.assertEqual(len(connector.calls), 1)
        self.assertEqual(recorder.names(), [
            "session_start", "session_connecting", "session_stop",
        ])

    async def test_max_attempts_caps_the_number_of_connects(self) -> None:
        connector = FakeConnector([ConnectionError("refused")])
        config = SessionConfig(
            "127.0.0.1", reconnect_delay=0.01, reconnect_max_attempts=3
        )
        session = make_session(config=config, connector=connector)

        await session.run()
        self.assertEqual(len(connector.calls), 3)

    async def test_credentials_are_resolved_every_attempt(self) -> None:
        bot = FakeBot()
        connector = FakeConnector([ConnectionError("refused"), bot])
        credential_calls = 0

        async def creds():
            nonlocal credential_calls
            credential_calls += 1
            return "FakeBot", None, None

        session = make_session(connector=connector, credentials=creds)
        task = asyncio.create_task(session.run())
        await wait_for(lambda: credential_calls >= 2)
        session.request_stop()
        await task
        self.assertEqual(credential_calls, 2)

    async def test_systemexit_from_credentials_propagates(self) -> None:
        async def creds():
            raise SystemExit("no credentials")

        session = make_session(connector=FakeConnector([]), credentials=creds)
        with self.assertRaises(SystemExit):
            await session.run()

    async def test_tick_loop_runs_and_close_happens_exactly_once(self) -> None:
        bot = FakeBot()
        session = make_session(connector=FakeConnector([bot]))
        task = asyncio.create_task(session.run())
        await wait_for(lambda: bot.tick_count > 0)
        session.request_stop()
        await task
        self.assertEqual(bot.close_count, 1)

    async def test_session_can_be_restarted_after_a_stop(self) -> None:
        bot1, bot2 = FakeBot(), FakeBot()
        connector = FakeConnector([bot1, bot2])
        session = make_session(connector=connector)

        task = asyncio.create_task(session.run())
        await wait_for(lambda: len(connector.calls) == 1 and bot1.tick_count > 0)
        session.request_stop()
        await task
        self.assertEqual(bot1.close_count, 1)

        # A second run clears the stop flag and connects again.
        task = asyncio.create_task(session.run())
        await wait_for(lambda: len(connector.calls) == 2 and bot2.tick_count > 0)
        session.request_stop()
        await task
        self.assertEqual(bot2.close_count, 1)


class ReconnectBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_plugins_rebind_to_each_new_bot(self) -> None:
        class Pinger(Plugin):
            name = "pinger"

            def __init__(self) -> None:
                super().__init__()
                self.pings = 0
                self.subscribe("ping", self._on_ping)

            async def _on_ping(self, value) -> None:
                self.pings += 1

        plugin = Pinger()
        manager = PluginManager([])
        manager._plugins = {"pinger": plugin}
        manager._order = [plugin]

        bot1 = FakeBot(closed=True, reason="test disconnect")
        bot2 = FakeBot()
        connector = FakeConnector([bot1, bot2])
        session = make_session(connector=connector, manager=manager)
        recorder = EventRecorder(session)

        task = asyncio.create_task(session.run())
        await wait_for(lambda: recorder.names().count("session_ready") >= 2)

        # bot1's handlers were unbound when it closed; bot2's are live.
        await bot1.events.emit("ping", 1)
        await bot2.events.emit("ping", 2)
        self.assertEqual(plugin.pings, 1)
        self.assertIs(plugin.bot, bot2)

        session.request_stop()
        await task
        self.assertIsNone(plugin.bot)  # unbound at teardown
        self.assertEqual(bot1.close_count, 1)
        self.assertEqual(bot2.close_count, 1)


class ContainerTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_enables_and_disables_plugins(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.enabled = False
                self.disabled = False

            async def enable_all(self) -> None:
                self.enabled = True

            async def disable_all(self) -> None:
                self.disabled = True

        manager = FakeManager()
        connector = FakeConnector([ConnectionError("refused")])
        config = SessionConfig("127.0.0.1", reconnect=False, reconnect_delay=0.01)
        container = BotContainer(plugin_manager=manager)
        container.add_session("default", make_session(config=config, connector=connector))

        await container.run()
        self.assertTrue(manager.enabled)
        self.assertTrue(manager.disabled)

    async def test_duplicate_session_names_are_rejected(self) -> None:
        container = BotContainer()
        session = make_session(connector=FakeConnector([]))
        container.add_session("default", session)
        with self.assertRaises(ValueError):
            container.add_session("default", session)

    async def test_cancellation_closes_the_bot_gracefully(self) -> None:
        bot = FakeBot()
        container = BotContainer(plugin_manager=PluginManager([]))
        container.add_session(
            "default", make_session(connector=FakeConnector([bot]))
        )
        task = asyncio.create_task(container.run())
        await wait_for(lambda: bot.tick_count > 0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(bot.close_count, 1)  # closed before the container gave up


if __name__ == "__main__":
    unittest.main()
