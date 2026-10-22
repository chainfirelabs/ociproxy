"""Edge-triggered wakeups for asyncio waiters, fired from any thread."""

from __future__ import annotations

import asyncio


class ChangeSignal:
    """Wake every waiter when something changes.

    Waiters grab a ``token()`` BEFORE checking state, then ``wait(token)``:
    a change that lands between the check and the wait still wakes them,
    because ``fire`` sets the token they already hold.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Allow ``notify`` from other threads (e.g. asyncio.to_thread)."""
        self._loop = loop

    def token(self) -> asyncio.Event:
        return self._event

    def fire(self) -> None:
        """Wake all waiters. Event-loop thread only."""
        event, self._event = self._event, asyncio.Event()
        event.set()

    def notify(self) -> None:
        """Thread-safe ``fire``; a no-op until bound to a running loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.fire()
        else:
            loop.call_soon_threadsafe(self.fire)

    @staticmethod
    async def wait(token: asyncio.Event, timeout: float) -> bool:
        """True if woken, False on timeout."""
        try:
            await asyncio.wait_for(token.wait(), timeout=max(timeout, 0))
            return True
        except asyncio.TimeoutError:
            return False
