"""Exit the process when the ASGI event loop stops beating.

Sync catalog reads run in the server's thread pool, so a stuck loop is a
different failure from a slow query: the process stays up, CPU sits idle, and
Railway's health check does not run again after deploy. ``restartPolicyType =
ON_FAILURE`` only restarts a container that has exited. A daemon thread
compares a loop heartbeat with a deadline and calls ``os._exit`` so a wedged
loop cannot sit there until someone restarts it by hand.

The thread waits out ``UMBRA_WATCHDOG_GRACE_S`` (default 120s) before it will
fire, and it ignores a loop that has not published a beat yet (the entrypoint
fetches the index before the socket listens; that wait is the deploy health
check's job). Set ``UMBRA_WATCHDOG=0`` to disable. A slow catalog read does
not trip this: the heartbeat is the event loop, not a database call.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable

_beat = 0.0
_beat_lock = threading.Lock()


def note_heartbeat(when: float | None = None) -> None:
    """Record that the event loop ran. ``when`` overrides the clock in tests."""
    global _beat
    with _beat_lock:
        _beat = time.monotonic() if when is None else when


def last_heartbeat() -> float:
    """Monotonic time of the last :func:`note_heartbeat`, or ``0`` if none."""
    with _beat_lock:
        return _beat


def reset_heartbeat() -> None:
    """Forget any beat. Tests use this so a previous case cannot arm the next."""
    global _beat
    with _beat_lock:
        _beat = 0.0


async def heartbeat_forever(interval: float = 1.0) -> None:
    """Publish a beat until cancelled. Run this on the server's event loop."""
    while True:
        note_heartbeat()
        await asyncio.sleep(interval)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class LoopWatchdog:
    """One check of "has the loop gone quiet for too long?".

    ``now`` and ``beat`` are injectable so tests do not sleep. ``exit`` is
    called with status ``1`` when a stall is declared; production passes
    ``os._exit`` because ``sys.exit`` in a daemon thread only kills the thread.
    """

    def __init__(
        self,
        *,
        stall_s: float,
        grace_s: float,
        now: Callable[[], float],
        beat: Callable[[], float],
        exit: Callable[[int], None],
        enabled: bool = True,
    ) -> None:
        self.stall_s = stall_s
        self.grace_s = grace_s
        self._now = now
        self._beat = beat
        self._exit = exit
        self._enabled = enabled
        self._t0 = now()

    def check(self) -> bool:
        """Return True when this check asked the process to exit."""
        if not self._enabled:
            return False
        now = self._now()
        if now - self._t0 < self.grace_s:
            return False
        beat = self._beat()
        # No beat yet: the loop has not entered its lifespan. A deploy that
        # never listens fails the platform health check instead.
        if beat <= 0 or now - beat <= self.stall_s:
            return False
        self._exit(1)
        return True


def start_watchdog() -> Callable[[], None]:
    """Start the daemon checker. Return a stop callback.

    Disabled when ``UMBRA_WATCHDOG`` is ``0``, ``false``, ``off``, or ``no``.
    Stall and grace are ``UMBRA_WATCHDOG_STALL_S`` (default 90) and
    ``UMBRA_WATCHDOG_GRACE_S`` (default 120).
    """
    if not _env_flag("UMBRA_WATCHDOG", True):
        return lambda: None
    watchdog = LoopWatchdog(
        stall_s=_env_float("UMBRA_WATCHDOG_STALL_S", 90.0),
        grace_s=_env_float("UMBRA_WATCHDOG_GRACE_S", 120.0),
        now=time.monotonic,
        beat=last_heartbeat,
        exit=os._exit,
    )
    stop = threading.Event()

    def _run() -> None:
        while not stop.wait(1.0):
            watchdog.check()

    threading.Thread(target=_run, name="umbra-watchdog", daemon=True).start()
    return stop.set
