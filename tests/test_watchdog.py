"""The event-loop watchdog exits only after a real stall, past its grace period."""

from __future__ import annotations

from umbra_py.watchdog import LoopWatchdog, _env_flag


def test_stall_after_grace_calls_exit():
    clock = {"now": 0.0}
    beat = {"at": 0.0}
    exited: list[int] = []
    dog = LoopWatchdog(
        stall_s=10,
        grace_s=30,
        now=lambda: clock["now"],
        beat=lambda: beat["at"],
        exit=exited.append,
    )
    beat["at"] = 1.0
    clock["now"] = 20  # still inside the grace window
    assert dog.check() is False
    assert exited == []
    clock["now"] = 50  # grace over; last beat is 49s old
    assert dog.check() is True
    assert exited == [1]


def test_a_fresh_beat_does_not_exit():
    clock = {"now": 0.0}
    dog = LoopWatchdog(
        stall_s=10,
        grace_s=0,
        now=lambda: clock["now"],
        beat=lambda: clock["now"],
        exit=lambda _code: (_ for _ in ()).throw(AssertionError("exit")),
    )
    clock["now"] = 100
    assert dog.check() is False


def test_no_beat_yet_does_not_exit_during_startup():
    clock = {"now": 1000.0}
    exited: list[int] = []
    dog = LoopWatchdog(
        stall_s=1,
        grace_s=0,
        now=lambda: clock["now"],
        beat=lambda: 0.0,
        exit=exited.append,
    )
    assert dog.check() is False
    assert exited == []


def test_disabled_watchdog_never_exits(monkeypatch):
    monkeypatch.setenv("UMBRA_WATCHDOG", "off")
    assert _env_flag("UMBRA_WATCHDOG", True) is False
    exited: list[int] = []
    dog = LoopWatchdog(
        stall_s=1,
        grace_s=0,
        now=lambda: 100.0,
        beat=lambda: 1.0,
        exit=exited.append,
        enabled=False,
    )
    assert dog.check() is False
    assert exited == []
