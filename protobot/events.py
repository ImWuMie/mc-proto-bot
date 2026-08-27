"""Small asynchronous event dispatcher used by high- and low-level APIs."""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

EventHandler = Callable[..., Any | Awaitable[Any]]
HandlerT = TypeVar("HandlerT", bound=EventHandler)


class EventBus:
    """Register coroutine or regular function handlers by event name."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def on(self, event: str, handler: HandlerT | None = None):  # type: ignore[no-untyped-def]
        def register(candidate: HandlerT) -> HandlerT:
            self._handlers[event].append(candidate)
            return candidate

        return register(handler) if handler is not None else register

    def remove(self, event: str, handler: EventHandler) -> None:
        handlers = self._handlers.get(event)
        if handlers and handler in handlers:
            handlers.remove(handler)

    async def emit(self, event: str, *args: Any) -> None:
        for handler in tuple(self._handlers.get(event, ())):
            result = handler(*args)
            if inspect.isawaitable(result):
                await result
