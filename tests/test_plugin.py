"""Tests for the plugin framework: discovery, ordering, and isolation."""

from __future__ import annotations

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from protobot.events import EventBus
from protobot.plugin import Plugin, PluginError, PluginManager, PluginWatcher


class FakeBot:
    """Minimal stand-in for Bot: plugins only touch ``events``."""

    def __init__(self) -> None:
        self.events = EventBus()


def write_plugin(directory: Path, filename: str, source: str) -> Path:
    file = directory / filename
    file.write_text(source, encoding="utf-8")
    return file


SIMPLE_PLUGIN = """\
from protobot.plugin import Plugin

class {class_name}(Plugin):
    name = {name!r}
    dependencies = {deps!r}
"""


class DiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = contextlib.ExitStack()
        self.directory = Path(self._tmp.enter_context(
            __import__("tempfile").TemporaryDirectory()
        ))

    def tearDown(self) -> None:
        self._tmp.close()

    def test_collects_plugins_from_sorted_files(self) -> None:
        write_plugin(self.directory, "b_plugin.py",
                     SIMPLE_PLUGIN.format(class_name="Bee", name="bee", deps=()))
        write_plugin(self.directory, "a_plugin.py",
                     SIMPLE_PLUGIN.format(class_name="Ant", name="ant", deps=()))
        manager = PluginManager([self.directory])
        manager.discover()
        self.assertEqual(sorted(manager.plugins), ["ant", "bee"])

    def test_one_file_may_define_several_plugins(self) -> None:
        source = SIMPLE_PLUGIN.format(class_name="A", name="a", deps=()) + \
            SIMPLE_PLUGIN.format(class_name="B", name="b", deps=("a",))
        write_plugin(self.directory, "multi.py", source)
        manager = PluginManager([self.directory])
        manager.discover()
        self.assertEqual(sorted(manager.plugins), ["a", "b"])

    def test_same_filename_in_two_directories_does_not_collide(self) -> None:
        other = Path(self._tmp.enter_context(
            __import__("tempfile").TemporaryDirectory()
        ))
        write_plugin(self.directory, "plugin.py",
                     SIMPLE_PLUGIN.format(class_name="A", name="a", deps=()))
        write_plugin(other, "plugin.py",
                     SIMPLE_PLUGIN.format(class_name="B", name="b", deps=()))
        manager = PluginManager([self.directory, other])
        manager.discover()
        self.assertEqual(sorted(manager.plugins), ["a", "b"])

    def test_missing_directories_yield_an_empty_set(self) -> None:
        manager = PluginManager([self.directory / "nope"])
        manager.discover()
        self.assertEqual(manager.plugins, {})

    def test_non_python_files_are_ignored(self) -> None:
        (self.directory / "readme.txt").write_text("not a plugin", encoding="utf-8")
        manager = PluginManager([self.directory])
        manager.discover()
        self.assertEqual(manager.plugins, {})

    def test_syntax_error_fails_with_file_path(self) -> None:
        write_plugin(self.directory, "broken.py", "def broken(:\n")
        manager = PluginManager([self.directory])
        with self.assertRaises(PluginError) as ctx:
            manager.discover()
        self.assertIn("加载失败", str(ctx.exception))
        self.assertIn("broken.py", str(ctx.exception))

    def test_import_error_fails_with_file_path(self) -> None:
        write_plugin(self.directory, "broken.py", "import no_such_module_xyz\n")
        manager = PluginManager([self.directory])
        with self.assertRaises(PluginError) as ctx:
            manager.discover()
        self.assertIn("加载失败", str(ctx.exception))
        self.assertIn("broken.py", str(ctx.exception))

    def test_plugin_without_name_fails(self) -> None:
        write_plugin(self.directory, "nameless.py", """\
from protobot.plugin import Plugin

class Nameless(Plugin):
    pass
""")
        manager = PluginManager([self.directory])
        with self.assertRaises(PluginError) as ctx:
            manager.discover()
        self.assertIn("缺少 name", str(ctx.exception))
        self.assertIn("Nameless", str(ctx.exception))

    def test_duplicate_names_fail_with_both_paths(self) -> None:
        write_plugin(self.directory, "one.py",
                     SIMPLE_PLUGIN.format(class_name="A", name="dup", deps=()))
        write_plugin(self.directory, "two.py",
                     SIMPLE_PLUGIN.format(class_name="B", name="dup", deps=()))
        manager = PluginManager([self.directory])
        with self.assertRaises(PluginError) as ctx:
            manager.discover()
        self.assertIn("重名", str(ctx.exception))
        self.assertIn("one.py", str(ctx.exception))
        self.assertIn("two.py", str(ctx.exception))

    def test_disabled_unknown_name_is_harmless(self) -> None:
        write_plugin(self.directory, "a.py",
                     SIMPLE_PLUGIN.format(class_name="A", name="a", deps=()))
        manager = PluginManager([self.directory], disabled=["ghost"])
        manager.discover()
        self.assertEqual(list(manager.plugins), ["a"])
        self.assertEqual(manager.disabled_names(), set())


class DependencyOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = contextlib.ExitStack()
        self.directory = Path(self._tmp.enter_context(
            __import__("tempfile").TemporaryDirectory()
        ))

    def tearDown(self) -> None:
        self._tmp.close()

    def _manager(self, disabled=()) -> PluginManager:
        source = SIMPLE_PLUGIN.format(class_name="A", name="a", deps=()) + \
            SIMPLE_PLUGIN.format(class_name="B", name="b", deps=("a",)) + \
            SIMPLE_PLUGIN.format(class_name="C", name="c", deps=("a", "b"))
        write_plugin(self.directory, "chain.py", source)
        manager = PluginManager([self.directory], disabled=disabled)
        manager.discover()
        return manager

    def test_topological_order_is_deterministic(self) -> None:
        for _ in range(3):
            manager = self._manager()
            self.assertEqual(
                [plugin.name for plugin in manager.load_order()], ["a", "b", "c"]
            )

    def test_cycle_is_reported_with_the_cycle_path(self) -> None:
        source = SIMPLE_PLUGIN.format(class_name="A", name="a", deps=("b",)) + \
            SIMPLE_PLUGIN.format(class_name="B", name="b", deps=("a",))
        write_plugin(self.directory, "cycle.py", source)
        manager = PluginManager([self.directory])
        with self.assertRaises(PluginError) as ctx:
            manager.discover()
        self.assertIn("依赖循环", str(ctx.exception))
        self.assertIn("a", str(ctx.exception))
        self.assertIn("b", str(ctx.exception))

    def test_self_dependency_is_reported_as_a_cycle(self) -> None:
        write_plugin(self.directory, "self_cycle.py",
                     SIMPLE_PLUGIN.format(class_name="A", name="a", deps=("a",)))
        manager = PluginManager([self.directory])
        with self.assertRaises(PluginError) as ctx:
            manager.discover()
        self.assertIn("依赖循环", str(ctx.exception))
        self.assertEqual(str(ctx.exception), "[插件] 依赖循环: a -> a")

    def test_missing_dependency_is_reported(self) -> None:
        write_plugin(self.directory, "orphan.py",
                     SIMPLE_PLUGIN.format(class_name="A", name="a", deps=("ghost",)))
        manager = PluginManager([self.directory])
        with self.assertRaises(PluginError) as ctx:
            manager.discover()
        self.assertIn("ghost", str(ctx.exception))
        self.assertIn("不存在", str(ctx.exception))

    def test_disabled_plugin_pulls_down_dependents(self) -> None:
        # c depends on (a, b), b depends on a: disabling a disables the chain.
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            manager = self._manager(disabled=["a"])
        self.assertEqual(manager.load_order(), [])
        self.assertEqual(manager.disabled_names(), {"a", "b", "c"})
        self.assertIn("一并禁用", stdout.getvalue())


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_enable_and_disable_run_in_dependency_order(self) -> None:
        calls: list[str] = []

        class Base(Plugin):
            name = ""

            async def on_enable(self) -> None:
                calls.append(f"enable:{self.name}")

            async def on_disable(self) -> None:
                calls.append(f"disable:{self.name}")

        class A(Base):
            name = "a"

        class B(Base):
            name = "b"
            dependencies = ("a",)

        class C(Base):
            name = "c"
            dependencies = ("b",)

        manager = PluginManager([])
        manager._plugins = {p.name: p for p in (C(), B(), A())}
        manager._order = list(manager._plugins.values())[::-1]
        await manager.enable_all()
        await manager.disable_all()
        self.assertEqual(calls, [
            "enable:a", "enable:b", "enable:c",
            "disable:c", "disable:b", "disable:a",
        ])

    async def test_failing_hooks_are_logged_not_fatal(self) -> None:
        calls: list[str] = []

        class Bad(Plugin):
            name = "bad"

            async def on_enable(self) -> None:
                raise RuntimeError("boom")

        class Good(Plugin):
            name = "good"
            dependencies = ("bad",)

            async def on_enable(self) -> None:
                calls.append("good")

        bad, good = Bad(), Good()
        manager = PluginManager([])
        manager._plugins = {"bad": bad, "good": good}
        manager._order = [bad, good]
        await manager.enable_all()
        self.assertEqual(calls, ["good"])  # the failure did not stop the chain

    async def test_on_bot_ready_runs_once_per_bot_and_is_isolated(self) -> None:
        ready_count = 0

        class P(Plugin):
            name = "p"

            async def on_bot_ready(self) -> None:
                nonlocal ready_count
                ready_count += 1

        class Raiser(Plugin):
            name = "raiser"

            async def on_bot_ready(self) -> None:
                raise RuntimeError("boom")

        p, raiser = P(), Raiser()
        manager = PluginManager([])
        manager._plugins = {"p": p, "raiser": raiser}
        manager._order = [p, raiser]

        bot1, bot2 = FakeBot(), FakeBot()
        await manager.bind_all(bot1)  # raiser's failure must not propagate
        await manager.bind_all(bot2)
        self.assertEqual(ready_count, 2)
        self.assertIs(p.bot, bot2)  # self.bot follows the newest bot


class IsolationTest(unittest.IsolatedAsyncioTestCase):
    def _bound_plugin(self, handler):
        class P(Plugin):
            name = "p"

        plugin = P()
        plugin.subscribe("boom", handler)
        bot = FakeBot()
        plugin.bot = bot
        plugin._bind(bot)
        return plugin, bot

    async def test_async_raising_handler_does_not_propagate(self) -> None:
        async def bad(arg):
            raise RuntimeError("kaboom")

        _, bot = self._bound_plugin(bad)
        await bot.events.emit("boom", 1)  # must not raise

    async def test_sync_raising_handler_does_not_propagate(self) -> None:
        def bad(arg):
            raise RuntimeError("kaboom")

        _, bot = self._bound_plugin(bad)
        await bot.events.emit("boom", 1)  # must not raise

    async def test_other_handlers_still_run_after_a_failure(self) -> None:
        calls: list[int] = []

        async def bad(arg):
            raise RuntimeError("kaboom")

        async def good(arg):
            calls.append(arg)

        class P(Plugin):
            name = "p"

        plugin = P()
        plugin.subscribe("boom", bad)
        plugin.subscribe("boom", good)
        bot = FakeBot()
        plugin.bot = bot
        plugin._bind(bot)
        await bot.events.emit("boom", 7)
        self.assertEqual(calls, [7])

    async def test_subscribe_works_as_a_decorator(self) -> None:
        class P(Plugin):
            name = "p"

            def __init__(self) -> None:
                super().__init__()

                @self.subscribe("decorated")
                async def handler(arg):
                    self.seen = arg

                self.seen = None

        plugin = P()
        bot = FakeBot()
        plugin.bot = bot
        plugin._bind(bot)
        await bot.events.emit("decorated", "hi")
        self.assertEqual(plugin.seen, "hi")

    async def test_unbind_removes_handlers_by_identity(self) -> None:
        calls: list[int] = []

        async def counter(arg):
            calls.append(arg)

        class P(Plugin):
            name = "p"

        plugin = P()
        plugin.subscribe("tick", counter)
        bot1, bot2 = FakeBot(), FakeBot()

        # Bind to bot1, then rebind to bot2 (reconnect): handlers follow.
        plugin.bot = bot1
        plugin._bind(bot1)
        plugin._unbind(bot1)
        plugin.bot = bot2
        plugin._bind(bot2)

        await bot1.events.emit("tick", 1)  # no-op: unbound
        await bot2.events.emit("tick", 2)
        self.assertEqual(calls, [2])

    async def test_session_subscriptions_bind_to_the_session_bus(self) -> None:
        calls: list[int] = []

        async def on_disconnect(reason, attempt):
            calls.append(attempt)

        class P(Plugin):
            name = "p"

        plugin = P()
        plugin.subscribe_session("session_disconnected", on_disconnect)

        class FakeSession:
            def __init__(self) -> None:
                self.events = EventBus()

        session = FakeSession()
        plugin.session = session
        plugin._bind_session(session)
        await session.events.emit("session_disconnected", "bye", 2)
        plugin._unbind_session(session)
        await session.events.emit("session_disconnected", "bye", 3)
        self.assertEqual(calls, [2])


class HotOperationTest(unittest.IsolatedAsyncioTestCase):
    """Hot load / reload / close against a manager with a live bot bound."""

    def setUp(self) -> None:
        self._tmp = contextlib.ExitStack()
        self.directory = Path(self._tmp.enter_context(
            __import__("tempfile").TemporaryDirectory()
        ))

    def tearDown(self) -> None:
        self._tmp.close()

    def _counting_plugin_source(self, name: str, dep: str | None = None) -> str:
        deps = f"({dep!r},)" if dep else "()"
        return f"""\
import json
from pathlib import Path
from protobot.plugin import Plugin

LOG = Path({str(self.directory)!r}) / "{name}.log"

class P(Plugin):
    name = {name!r}
    dependencies = {deps}

    def __init__(self):
        super().__init__()
        self.subscribe("tick", self._on_tick)

    async def on_enable(self):
        with LOG.open("a", encoding="utf-8") as f:
            f.write("enable\\n")

    async def on_disable(self):
        with LOG.open("a", encoding="utf-8") as f:
            f.write("disable\\n")

    async def _on_tick(self, value):
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"tick:{{value}}\\n")
"""

    async def test_hot_load_binds_to_the_current_bot(self) -> None:
        manager = PluginManager([self.directory])
        manager.discover()  # empty: the file lands after discovery

        bot = FakeBot()
        await manager.bind_all(bot)  # a live bot is bound
        file = write_plugin(self.directory, "new_plugin.py",
                            self._counting_plugin_source("new"))
        plugins = await manager.hot_load_file(file)
        self.assertEqual([p.name for p in plugins], ["new"])

        await bot.events.emit("tick", 1)  # must reach the hot-loaded plugin
        log = (self.directory / "new.log").read_text(encoding="utf-8")
        self.assertIn("enable\n", log)
        self.assertIn("tick:1\n", log)

    async def test_hot_load_name_clash_is_rejected(self) -> None:
        file = write_plugin(self.directory, "a.py",
                            SIMPLE_PLUGIN.format(class_name="A", name="a", deps=()))
        manager = PluginManager([self.directory])
        manager.discover()
        clash = write_plugin(self.directory, "clash.py",
                             SIMPLE_PLUGIN.format(class_name="B", name="a", deps=()))
        with self.assertRaises(PluginError) as ctx:
            await manager.hot_load_file(clash)
        self.assertIn("重名", str(ctx.exception))
        self.assertEqual(list(manager.plugins), ["a"])  # unchanged

    async def test_hot_reload_swaps_instances_and_keeps_handlers_bound(self) -> None:
        file = write_plugin(self.directory, "swap.py",
                            self._counting_plugin_source("swap"))
        manager = PluginManager([self.directory])
        manager.discover()
        await manager.enable_all()  # the container does this before sessions run
        old_plugin = manager.plugins["swap"]

        bot = FakeBot()
        await manager.bind_all(bot)
        await bot.events.emit("tick", 1)

        # Modify the file (a changed source) and reload: new instance takes over.
        file.write_text(self._counting_plugin_source("swap").replace(
            "name = 'swap'", "name = 'swap'\n    marker = 'reloaded'"
        ), encoding="utf-8")
        plugins = await manager.hot_reload_file(file)
        plugin = plugins[0]
        self.assertIsNot(plugin, old_plugin)
        self.assertIs(plugin.bot, bot)  # bound to the live bot immediately

        await bot.events.emit("tick", 2)
        log = (self.directory / "swap.log").read_text(encoding="utf-8")
        self.assertEqual(log, "enable\ntick:1\ndisable\nenable\ntick:2\n")

    async def test_failed_reload_leaves_the_old_plugin_running(self) -> None:
        file = write_plugin(self.directory, "keep.py",
                            self._counting_plugin_source("keep"))
        manager = PluginManager([self.directory])
        manager.discover()
        old_plugin = manager.plugins["keep"]

        bot = FakeBot()
        await manager.bind_all(bot)
        file.write_text("def broken(:\n", encoding="utf-8")
        with self.assertRaises(PluginError):
            await manager.hot_reload_file(file)

        self.assertIs(manager.plugins["keep"], old_plugin)  # untouched
        await bot.events.emit("tick", 1)  # old handler still responds
        self.assertIn("tick:1", (self.directory / "keep.log").read_text(encoding="utf-8"))

    async def test_hot_reload_closes_names_that_vanished(self) -> None:
        file = write_plugin(self.directory, "rename.py",
                            self._counting_plugin_source("old_name"))
        manager = PluginManager([self.directory])
        manager.discover()

        file.write_text(self._counting_plugin_source("new_name"), encoding="utf-8")
        plugins = await manager.hot_reload_file(file)
        self.assertEqual([p.name for p in plugins], ["new_name"])
        self.assertNotIn("old_name", manager.plugins)
        self.assertIn("disable", (self.directory / "old_name.log").read_text(encoding="utf-8"))

    async def test_hot_close_removes_plugin_and_dependents(self) -> None:
        write_plugin(self.directory, "base.py",
                     self._counting_plugin_source("base"))
        write_plugin(self.directory, "child.py",
                     self._counting_plugin_source("child", dep="base"))
        manager = PluginManager([self.directory])
        manager.discover()
        bot = FakeBot()
        await manager.bind_all(bot)

        closed = await manager.hot_close("base")
        self.assertIsNotNone(closed)
        self.assertEqual(set(manager.plugins), set())  # child pulled down too
        self.assertEqual(manager.load_order(), [])
        self.assertIn("disable", (self.directory / "child.log").read_text(encoding="utf-8"))

    async def test_hot_close_unknown_name_returns_none(self) -> None:
        manager = PluginManager([self.directory])
        manager.discover()
        self.assertIsNone(await manager.hot_close("ghost"))


class WatcherTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = contextlib.ExitStack()
        self.directory = Path(self._tmp.enter_context(
            __import__("tempfile").TemporaryDirectory()
        ))

    def tearDown(self) -> None:
        self._tmp.close()

    async def test_watcher_applies_load_reload_close(self) -> None:
        write_plugin(self.directory, "a.py",
                     SIMPLE_PLUGIN.format(class_name="A", name="a", deps=()))
        manager = PluginManager([self.directory])
        manager.discover()
        watcher = PluginWatcher(manager)  # snapshots the current state

        # New file -> hot load.
        write_plugin(self.directory, "b.py",
                     SIMPLE_PLUGIN.format(class_name="B", name="b", deps=("a",)))
        await watcher.check_once()
        self.assertEqual(sorted(manager.plugins), ["a", "b"])

        # Modified file -> hot reload.
        write_plugin(self.directory, "a.py",
                     SIMPLE_PLUGIN.format(class_name="A2", name="a", deps=()))
        await watcher.check_once()
        self.assertEqual(manager.plugins["a"].__class__.__name__, "A2")

        # Deleted file -> hot close.
        (self.directory / "b.py").unlink()
        await watcher.check_once()
        self.assertEqual(sorted(manager.plugins), ["a"])

    async def test_watcher_loop_observes_changes_until_stopped(self) -> None:
        manager = PluginManager([self.directory])
        manager.discover()
        watcher = PluginWatcher(manager, interval=0.01)
        task = asyncio.create_task(watcher.run())
        try:
            write_plugin(self.directory, "late.py",
                         SIMPLE_PLUGIN.format(class_name="L", name="late", deps=()))
            for _ in range(200):
                if "late" in manager.plugins:
                    break
                await asyncio.sleep(0.01)
            self.assertIn("late", manager.plugins)
        finally:
            watcher.request_stop()
            await task


class ExposedFunctionTest(unittest.IsolatedAsyncioTestCase):
    """expose()：插件间互调 + 供 LLM 使用的工具表。"""

    def _sources(self, extra: str = "") -> str:
        return (
            "from protobot import Plugin\n\n"
            "class Provider(Plugin):\n"
            '    name = "provider"\n\n'
            "    def __init__(self):\n"
            "        super().__init__()\n"
            "        self.calls = []\n"
            '        self.expose("add", self._add, description="Add numbers",\n'
            '                    parameters={"type": "object",\n'
            '                                "properties": {"a": {"type": "number"}}},\n'
            "                    llm=True)\n"
            '        self.expose("secret", self._secret, llm=True, admin=True)\n'
            '        self.expose("plain", self._plain)\n'
            '        self.expose("boom", self._boom)\n\n'
            "    async def _add(self, a=0, b=0):\n"
            "        self.calls.append((a, b))\n"
            "        return a + b\n\n"
            "    def _secret(self):\n"
            '        return "classified"\n\n'
            "    def _plain(self):\n"
            '        return "sync ok"\n\n'
            "    def _boom(self):\n"
            '        raise ValueError("nope")\n' + extra
        )

    async def _manager(self, tmp: str, extra: str = "") -> PluginManager:
        (Path(tmp) / "provider.py").write_text(
            self._sources(extra), encoding="utf-8"
        )
        manager = PluginManager([Path(tmp)])
        manager.discover()
        await manager.enable_all()
        return manager

    async def test_services_published_while_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp)
            try:
                self.assertEqual(
                    sorted(manager.services()),
                    ["provider.add", "provider.boom", "provider.plain",
                     "provider.secret"],
                )
            finally:
                await manager.disable_all()
            self.assertEqual(manager.services(), {})  # 关闭后撤回

    async def test_call_service_awaits_coroutines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp)
            try:
                self.assertEqual(
                    await manager.call_service("provider.add", a=2, b=3), 5
                )
                self.assertEqual(
                    manager.plugins["provider"].calls, [(2, 3)]
                )
            finally:
                await manager.disable_all()

    async def test_call_service_handles_sync_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp)
            try:
                self.assertEqual(
                    await manager.call_service("provider.plain"), "sync ok"
                )
            finally:
                await manager.disable_all()

    async def test_unknown_service_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp)
            try:
                with self.assertRaises(PluginError):
                    await manager.call_service("provider.nope")
                with self.assertRaises(PluginError):
                    await manager.call_service("ghost.thing")
            finally:
                await manager.disable_all()

    async def test_handler_exceptions_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp)
            try:
                with self.assertRaises(ValueError):
                    await manager.call_service("provider.boom")
            finally:
                await manager.disable_all()

    async def test_llm_services_filtered_and_schema_shaped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp)
            try:
                names = [s.qualified for s in manager.llm_services()]
                self.assertEqual(names, ["provider.add", "provider.secret"])
                schema = manager.get_service("provider.add").tool_schema()
                self.assertEqual(schema["function"]["name"], "provider_add")
                self.assertEqual(
                    schema["function"]["description"], "Add numbers"
                )
                self.assertIn("a", schema["function"]["parameters"]["properties"])
                # 未给 parameters 的暴露也要产出合法空 schema
                bare = manager.get_service("provider.secret").tool_schema()
                self.assertEqual(
                    bare["function"]["parameters"],
                    {"type": "object", "properties": {}},
                )
                self.assertIn("provider.secret", bare["function"]["description"])
                self.assertTrue(manager.get_service("provider.secret").admin)
            finally:
                await manager.disable_all()

    async def test_plugin_can_call_another_plugin(self) -> None:
        consumer = (
            "\n\nclass Consumer(Plugin):\n"
            '    name = "consumer"\n'
            '    dependencies = ("provider",)\n\n'
            "    async def on_enable(self):\n"
            '        self.result = await self.call("provider.add", a=4, b=6)\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp, extra=consumer)
            try:
                # 依赖先启用，因此 consumer 的 on_enable 里就能调用 provider
                self.assertEqual(manager.plugins["consumer"].result, 10)
            finally:
                await manager.disable_all()

    async def test_call_without_a_manager_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp)
            plugin = manager.plugins["provider"]
            await manager.disable_all()
            with self.assertRaises(PluginError):
                await plugin.call("provider.add", a=1)

    async def test_hot_close_withdraws_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp)
            try:
                await manager.hot_close("provider")
                self.assertEqual(manager.services(), {})
                with self.assertRaises(PluginError):
                    await manager.call_service("provider.add")
            finally:
                await manager.disable_all()

    async def test_hot_reload_republishes_the_new_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = await self._manager(tmp)
            try:
                file = Path(tmp) / "provider.py"
                file.write_text(
                    self._sources().replace("return a + b", "return a * b"),
                    encoding="utf-8",
                )
                await manager.hot_reload_file(file)
                # 服务指向新实例：乘法而不是加法
                self.assertEqual(
                    await manager.call_service("provider.add", a=3, b=4), 12
                )
                self.assertEqual(len(manager.services()), 4)
            finally:
                await manager.disable_all()

    async def test_duplicate_exposure_is_warned_and_ignored(self) -> None:
        source = (
            "from protobot import Plugin\n\n"
            "class Dup(Plugin):\n"
            '    name = "dup"\n\n'
            "    def __init__(self):\n"
            "        super().__init__()\n"
            '        self.expose("thing", self._first)\n'
            '        self.expose("thing", self._second)\n\n'
            "    def _first(self):\n"
            '        return "first"\n\n'
            "    def _second(self):\n"
            '        return "second"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dup.py").write_text(source, encoding="utf-8")
            manager = PluginManager([Path(tmp)])
            manager.discover()
            await manager.enable_all()
            try:
                self.assertEqual(
                    await manager.call_service("dup.thing"), "first"
                )
            finally:
                await manager.disable_all()

    async def test_expose_as_a_decorator(self) -> None:
        source = (
            "from protobot import Plugin\n\n"
            "class Deco(Plugin):\n"
            '    name = "deco"\n\n'
            "    def __init__(self):\n"
            "        super().__init__()\n\n"
            '        @self.expose("hello", description="Say hi")\n'
            "        async def hello():\n"
            '            return "hi"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "deco.py").write_text(source, encoding="utf-8")
            manager = PluginManager([Path(tmp)])
            manager.discover()
            await manager.enable_all()
            try:
                self.assertEqual(await manager.call_service("deco.hello"), "hi")
            finally:
                await manager.disable_all()


if __name__ == "__main__":
    unittest.main()
