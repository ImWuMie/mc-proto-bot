"""Plugin framework: discovery, dependency ordering, isolated handlers, hot ops.

Plugins are :class:`Plugin` subclasses discovered from ``*.py`` files in one or
more directories.  Each plugin declares ``name`` (required) and
``dependencies`` (prerequisite plugin names); the manager loads them in
topological order and registers their event handlers on each bot the session
spawns.

Handler isolation is the load-bearing feature: every emit site in the client
lives inside the network read loop, where an uncaught handler exception would
tear down the connection.  ``subscribe`` / ``subscribe_session`` therefore wrap
every handler so that plugin errors are logged and the connection survives.

Hot operations: :meth:`PluginManager.hot_load_file`, :meth:`hot_reload_file`,
and :meth:`hot_close` mutate the running plugin set while the bot stays
connected -- new plugins bind to the current bot immediately, and a
:class:`PluginWatcher` can drive all three from filesystem changes.  A failed
reload (syntax error, missing dependency) leaves the old plugin running.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import traceback
import types
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .events import EventHandler
from .log import error as log_error
from .log import warn

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from .client import Bot
    from .session import BotSession

__all__ = ["Plugin", "PluginError", "PluginManager", "PluginWatcher"]


class PluginError(Exception):
    """Discovery or dependency-graph failure; ``str`` carries a Chinese message."""


class Plugin:
    """Base class for ProtoBot plugins.

    Subclasses declare ``name`` and optional ``dependencies``.  The manager sets
    ``self.bot`` to the current :class:`~protobot.Bot` before binding handlers
    and resets it to ``None`` after unbinding -- handlers and tasks must re-read
    ``self.bot`` each use, because reconnects replace the bot object.

    Lifecycle: ``on_enable``/``on_disable`` run once per process (hot-reloaded
    plugins get a fresh instance, so their hooks run again); ``on_bot_ready``
    runs once per spawned bot.  Tasks created in ``on_enable`` outlive
    individual bots -- cancel them in ``on_disable`` (the framework never
    cancels them).
    """

    name: str = ""  # required; validated at discovery
    dependencies: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.bot: Bot | None = None
        self.session: BotSession | None = None
        self._subscriptions: list[tuple[str, EventHandler]] = []
        self._session_subscriptions: list[tuple[str, EventHandler]] = []

    # ---- lifecycle hooks (override in subclasses) ----

    async def on_enable(self) -> None:
        """Called once, in dependency order, before any session starts."""

    async def on_disable(self) -> None:
        """Called once, in reverse dependency order, at teardown or hot-close."""

    async def on_bot_ready(self) -> None:
        """Called once per spawned bot, after handlers are bound."""

    # ---- event subscription ----

    def subscribe(self, event: str, handler: EventHandler | None = None):
        """Register an exception-isolated handler on the current bot's events.

        Usable directly (``subscribe("player_chat", handler)``) or as a
        decorator (``@plugin.subscribe("player_chat")``).  Returns the wrapped
        handler, which is what gets registered on each spawned bot.
        """

        def register(candidate: EventHandler) -> EventHandler:
            wrapped = self._isolate(candidate)
            self._subscriptions.append((event, wrapped))
            return wrapped

        return register(handler) if handler is not None else register

    def subscribe_session(self, event: str, handler: EventHandler | None = None):
        """Register an exception-isolated handler on the session's lifecycle bus.

        Same forms as :meth:`subscribe`; bound for the whole process, not per bot.
        """

        def register(candidate: EventHandler) -> EventHandler:
            wrapped = self._isolate(candidate)
            self._session_subscriptions.append((event, wrapped))
            return wrapped

        return register(handler) if handler is not None else register

    # ---- internals (called by PluginManager) ----

    def _bind(self, bot: Bot) -> None:
        for event, wrapped in self._subscriptions:
            bot.events.on(event, wrapped)

    def _unbind(self, bot: Bot) -> None:
        for event, wrapped in self._subscriptions:
            bot.events.remove(event, wrapped)

    def _bind_session(self, session: BotSession) -> None:
        for event, wrapped in self._session_subscriptions:
            session.events.on(event, wrapped)

    def _unbind_session(self, session: BotSession) -> None:
        for event, wrapped in self._session_subscriptions:
            session.events.remove(event, wrapped)

    def _isolate(self, handler: EventHandler) -> EventHandler:
        """Wrap a handler so exceptions are logged instead of propagated.

        The wrapper is always a coroutine function so the try/except covers both
        the call and the await; ``_bind``/``_unbind`` pass the same wrapper
        object (EventBus.remove matches by identity), and the closure captures
        no bot -- handlers read ``self.bot`` at call time.
        """
        plugin_name = self.name

        async def wrapped(*args: Any, **kwargs: Any) -> None:
            try:
                result = handler(*args, **kwargs)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise  # never swallow cancellation
            except Exception as error:  # Exception only, never BaseException
                log_error(f"[插件] {plugin_name} 处理事件时出错: {error!r}")
                traceback.print_exc()

        return wrapped


def resolve_load_order(plugins: dict[str, Plugin], disabled: set[str]) -> list[Plugin]:
    """Order enabled plugins by dependency (Kahn's algorithm), deterministic.

    Disabled plugins pull their dependents down with a warning.  Missing
    dependencies and dependency cycles raise :class:`PluginError`.  Ready nodes
    are popped in name order, so the result is independent of discovery order.
    """
    disabled = set(disabled)

    # Disabled propagation: a plugin whose dependency is disabled cannot work.
    changed = True
    while changed:
        changed = False
        for name, plugin in plugins.items():
            if name in disabled:
                continue
            if any(dep in disabled for dep in plugin.dependencies):
                disabled.add(name)
                warn(f"[插件] {name} 依赖的插件已被禁用，将一并禁用。")
                changed = True

    # Validation: enabled plugins may only depend on existing, enabled plugins.
    for name, plugin in plugins.items():
        if name in disabled:
            continue
        for dep in plugin.dependencies:
            if dep not in plugins or dep in disabled:
                raise PluginError(
                    f"[插件] {name} 依赖的插件 {dep} 不存在或已被禁用"
                )

    # Kahn topological sort over "depends on" edges (dependency -> dependent).
    indegree = {name: 0 for name in plugins if name not in disabled}
    dependents: dict[str, list[str]] = {name: [] for name in indegree}
    for name in indegree:
        for dep in plugins[name].dependencies:
            indegree[name] += 1
            dependents[dep].append(name)

    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()

    if len(order) < len(indegree):
        cycle = _extract_cycle(plugins, set(indegree) - set(order))
        raise PluginError(f"[插件] 依赖循环: {' -> '.join(cycle)}")

    return [plugins[name] for name in order]


def _extract_cycle(plugins: dict[str, Plugin], remaining: set[str]) -> list[str]:
    """Find one actual dependency cycle among the leftover (unorderable) nodes."""
    visited: set[str] = set()
    path: list[str] = []

    def walk(name: str) -> list[str] | None:
        if name in visited:
            return None
        visited.add(name)
        path.append(name)
        for dep in plugins[name].dependencies:
            if dep not in remaining:
                continue
            if dep in path:
                start = path.index(dep)
                return path[start:] + [dep]
            found = walk(dep)
            if found is not None:
                return found
        path.pop()
        return None

    for name in sorted(remaining):
        cycle = walk(name)
        if cycle is not None:
            return cycle
    # Unreachable in a finite graph: leftover nodes from Kahn always contain a
    # cycle. Fall back to a self-referencing name so the error stays readable.
    return sorted(remaining)


class PluginManager:
    """Discover, order, and run plugins from plugin directories.

    Supports hot operations: :meth:`hot_load_file` (new file), :meth:`hot_reload_file`
    (file changed), :meth:`hot_close` (stop one plugin and its dependents).  The
    dependency graph is re-resolved after every mutation and the difference with
    the running order drives disable/enable calls, so ordering invariants hold
    while the bot stays connected.
    """

    def __init__(
        self, directories: Iterable[Path], *, disabled: Iterable[str] = ()
    ) -> None:
        self._directories = [Path(directory) for directory in directories]
        self._disabled = set(disabled)
        self._plugins: dict[str, Plugin] = {}
        self._sources: dict[str, Path] = {}
        self._files: dict[Path, list[str]] = {}
        self._order: list[Plugin] = []
        self._counter = 0
        self._current_bot: Bot | None = None
        self._current_session: BotSession | None = None

    # ---- discovery ----

    def discover(self) -> None:
        """Scan the directories for ``*.py`` files and collect Plugin subclasses.

        Raises :class:`PluginError` on load failures, plugins without ``name``,
        duplicate names, missing dependencies, and dependency cycles.
        """
        self._plugins = {}
        self._sources = {}
        self._files = {}
        for directory in self._directories:
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.py")):
                for plugin in self._instantiate_file(file):
                    if plugin.name in self._plugins:
                        raise PluginError(
                            f"[插件] 插件重名: {plugin.name} "
                            f"({self._sources[plugin.name]}, {file})"
                        )
                    self._plugins[plugin.name] = plugin
                    self._sources[plugin.name] = file
                    self._files.setdefault(file, []).append(plugin.name)
        self._order = resolve_load_order(self._plugins, self._disabled)

    def _import_file(self, file: Path):
        self._counter += 1
        module_name = f"_protobot_plugin_{self._counter}"
        module = types.ModuleType(module_name)
        sys.modules[module_name] = module
        try:
            source = file.read_text(encoding="utf-8")
        except OSError as error:
            del sys.modules[module_name]
            raise PluginError(f"[插件] 加载失败: {file}: {error}") from error
        # Compile from the freshly read source.  The importlib machinery's
        # bytecode cache matches on (mtime-second, size), so an edit saved
        # within the same second with an unchanged file size would silently
        # execute stale code -- unacceptable for hot reload.
        try:
            code = compile(source, str(file), "exec")
            exec(code, module.__dict__)
        except Exception as error:
            del sys.modules[module_name]
            raise PluginError(f"[插件] 加载失败: {file}: {error}") from error
        return module

    def _instantiate_file(self, file: Path) -> list[Plugin]:
        """Import a file and instantiate its Plugin subclasses (no commit)."""
        module = self._import_file(file)
        plugins: list[Plugin] = []
        seen: set[str] = set()
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if not (isinstance(obj, type) and issubclass(obj, Plugin)):
                continue
            if obj is Plugin:
                continue
            plugin = obj()
            if not plugin.name:
                raise PluginError(
                    f"[插件] {file}: 插件类 {obj.__name__} 缺少 name"
                )
            if plugin.name in seen:
                raise PluginError(f"[插件] 插件重名: {plugin.name} ({file})")
            seen.add(plugin.name)
            plugins.append(plugin)
        return plugins

    # ---- accessors ----

    @property
    def plugins(self) -> dict[str, Plugin]:
        """All loaded plugins by name, including disabled ones."""
        return self._plugins

    def source_of(self, name: str) -> Path | None:
        """The file a plugin was discovered from, for the ``plugins`` listing."""
        return self._sources.get(name)

    def load_order(self) -> list[Plugin]:
        """Enabled plugins in topological (load) order."""
        return self._order

    def disabled_names(self) -> set[str]:
        """Config-disabled names plus dependents pulled down with them."""
        enabled = {plugin.name for plugin in self._order}
        return set(self._plugins) - enabled

    # ---- lifecycle ----

    async def enable_all(self) -> None:
        """Run ``on_enable`` in load order; hook failures are logged, not fatal."""
        self._current_bot = None
        self._current_session = None
        for plugin in self._order:
            await self._enable_one(plugin)

    async def disable_all(self) -> None:
        """Run ``on_disable`` in reverse load order; hook failures are logged."""
        for plugin in reversed(self._order):
            await self._disable_one(plugin)
        self._current_bot = None
        self._current_session = None

    async def bind_all(self, bot: Bot) -> None:
        """Bind handlers to a freshly spawned bot, then fire ``on_bot_ready``."""
        self._current_bot = bot
        for plugin in self._order:
            plugin.bot = bot
            plugin._bind(bot)
            await self._safe_hook(plugin, "on_bot_ready", plugin.on_bot_ready())

    def unbind_all(self, bot: Bot) -> None:
        """Remove handlers from a bot in reverse order (synchronous, no awaits)."""
        for plugin in reversed(self._order):
            plugin._unbind(bot)
            plugin.bot = None
        self._current_bot = None

    def bind_session_all(self, session: BotSession) -> None:
        self._current_session = session
        for plugin in self._order:
            plugin.session = session
            plugin._bind_session(session)

    def unbind_session_all(self, session: BotSession) -> None:
        for plugin in reversed(self._order):
            plugin._unbind_session(session)
            plugin.session = None
        self._current_session = None

    # ---- hot operations ----

    async def hot_load_file(self, file: Path) -> list[Plugin]:
        """Load plugins from a new file and enable them immediately.

        Raises :class:`PluginError` (leaving the manager unchanged) on load
        errors, name clashes with already-loaded plugins, or unresolvable
        dependencies.
        """
        file = Path(file)
        new_plugins = self._instantiate_file(file)
        for plugin in new_plugins:
            if plugin.name in self._plugins:
                raise PluginError(
                    f"[插件] 插件重名: {plugin.name} "
                    f"({self._sources[plugin.name]}, {file})"
                )
        candidates = dict(self._plugins)
        candidates.update({plugin.name: plugin for plugin in new_plugins})
        new_order = resolve_load_order(candidates, self._disabled)  # validates
        self._plugins = candidates
        for plugin in new_plugins:
            self._sources[plugin.name] = file
            self._files.setdefault(file, []).append(plugin.name)
        await self._apply_order(new_order)
        return new_plugins

    async def hot_reload_file(self, file: Path) -> list[Plugin]:
        """Re-import a file and swap its plugins for fresh instances.

        Names that disappeared from the file are hot-closed; names that remain
        get a new instance (old ``on_disable``, new ``on_enable``).  Any failure
        leaves the previously running plugins untouched.
        """
        file = Path(file)
        old_names = set(self._files.get(file, []))
        new_plugins = self._instantiate_file(file)
        for plugin in new_plugins:
            if plugin.name in self._plugins and plugin.name not in old_names:
                raise PluginError(
                    f"[插件] 插件重名: {plugin.name} "
                    f"({self._sources[plugin.name]}, {file})"
                )
        new_by_name = {plugin.name: plugin for plugin in new_plugins}
        vanished = old_names - set(new_by_name)
        candidates = {
            name: plugin
            for name, plugin in self._plugins.items()
            if name not in vanished
        }
        candidates.update(new_by_name)
        new_order = resolve_load_order(candidates, self._disabled)  # validates
        self._plugins = candidates
        for name in vanished:
            self._sources.pop(name, None)
        for plugin in new_plugins:
            self._sources[plugin.name] = file
        self._files[file] = [plugin.name for plugin in new_plugins]
        await self._apply_order(new_order)
        return new_plugins

    async def hot_close(self, name: str) -> Plugin | None:
        """Stop a plugin and, with a warning, any plugin that depends on it.

        Returns the closed plugin, or ``None`` if no plugin had that name.
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            return None
        to_close = {name}
        changed = True
        while changed:
            changed = False
            for other_name, other in self._plugins.items():
                if other_name in to_close:
                    continue
                if any(dep in to_close for dep in other.dependencies):
                    warn(f"[插件] {other_name} 依赖的插件已关闭，将一并关闭。")
                    to_close.add(other_name)
                    changed = True
        for closing in to_close:
            self._plugins.pop(closing, None)
            self._sources.pop(closing, None)
            for names in self._files.values():
                if closing in names:
                    names.remove(closing)
        new_order = resolve_load_order(self._plugins, self._disabled)
        await self._apply_order(new_order)
        return plugin

    async def hot_close_file(self, file: Path) -> list[str]:
        """Hot-close every plugin loaded from a file (and their dependents)."""
        names = list(self._files.get(Path(file), []))
        closed: list[str] = []
        for name in names:
            if await self.hot_close(name) is not None:
                closed.append(name)
        return closed

    # ---- internals ----

    async def _enable_one(self, plugin: Plugin) -> None:
        plugin.bot = self._current_bot
        plugin.session = self._current_session
        if self._current_bot is not None:
            plugin._bind(self._current_bot)
        if self._current_session is not None:
            plugin._bind_session(self._current_session)
        await self._safe_hook(plugin, "on_enable", plugin.on_enable())

    async def _disable_one(self, plugin: Plugin) -> None:
        await self._safe_hook(plugin, "on_disable", plugin.on_disable())
        if self._current_bot is not None:
            plugin._unbind(self._current_bot)
        if self._current_session is not None:
            plugin._unbind_session(self._current_session)
        plugin.bot = None
        plugin.session = None

    async def _apply_order(self, new_order: list[Plugin]) -> None:
        """Reconcile the running set with a recomputed order.

        Plugins that dropped out are disabled in reverse order, then new ones
        are enabled in load order -- so a hot-reloaded plugin's old instance
        tears down before its replacement starts.
        """
        old_set = {id(plugin) for plugin in self._order}
        new_set = {id(plugin) for plugin in new_order}
        for plugin in reversed(self._order):
            if id(plugin) not in new_set:
                await self._disable_one(plugin)
        for plugin in new_order:
            if id(plugin) not in old_set:
                await self._enable_one(plugin)
        self._order = new_order

    async def _safe_hook(
        self, plugin: Plugin, hook: str, awaitable: Awaitable[None]
    ) -> None:
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_error(f"[插件] {plugin.name} 生命周期钩子出错 ({hook}): {error!r}")
            traceback.print_exc()


class PluginWatcher:
    """Poll the plugin directories and apply hot operations on file changes.

    New files are hot-loaded, modified files hot-reloaded, deleted files
    hot-closed.  A broken edit (syntax error, bad dependencies) is logged and
    retried on the next change; the running plugins are never taken down by a
    failed reload.
    """

    def __init__(self, manager: PluginManager, *, interval: float = 1.0) -> None:
        self._manager = manager
        self._interval = interval
        self._stop = asyncio.Event()
        self._mtimes: dict[Path, float] = {}
        self._snapshot()

    def request_stop(self) -> None:
        self._stop.set()

    def _snapshot(self) -> None:
        self._mtimes = {}
        for directory in self._manager._directories:
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.py")):
                try:
                    self._mtimes[file] = file.stat().st_mtime
                except OSError:
                    continue

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_once()
            except Exception as error:
                log_error(f"[插件] 热更新失败: {error!r}")
                traceback.print_exc()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    async def check_once(self) -> None:
        """One diff of the directories against the last snapshot.  Testable."""
        snapshot: dict[Path, float] = {}
        for directory in self._manager._directories:
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.py")):
                try:
                    snapshot[file] = file.stat().st_mtime
                except OSError:
                    continue
        known = set(self._mtimes)
        current = set(snapshot)
        for file in sorted(current - known):  # new file -> hot load
            await self._manager.hot_load_file(file)
        for file in sorted(known - current):  # deleted file -> hot close
            await self._manager.hot_close_file(file)
        for file in sorted(known & current):
            if self._mtimes[file] != snapshot[file]:  # changed -> hot reload
                await self._manager.hot_reload_file(file)
        self._mtimes = snapshot
