"""Unit tests for the in-process ring-buffer log handler.

Covers count eviction, age eviction (via an injected clock) including the
opportunistic left-prune in ``emit``, most-recent-error scanning, thread
safety, and the never-raise guarantee.
"""

import logging
import threading

from src.observability.log_buffer import RingBufferLogHandler


def _record(name: str = "test", level: int = logging.INFO, msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None
    )


class FakeClock:
    """A controllable monotonic-ish clock for deterministic age tests."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_count_eviction_keeps_newest():
    clock = FakeClock()
    handler = RingBufferLogHandler(max_records=3, max_age_seconds=0, time_fn=clock)
    for i in range(5):
        handler.emit(_record(msg=f"m{i}"))
    records = handler.get_records(since_seconds=10_000)
    assert [r.message for r in records] == ["m2", "m3", "m4"]


def test_get_records_filters_by_age():
    clock = FakeClock(1000.0)
    handler = RingBufferLogHandler(max_records=100, max_age_seconds=0, time_fn=clock)
    handler.emit(_record(msg="old"))  # ts=1000
    clock.t = 1500
    handler.emit(_record(msg="recent"))  # ts=1500
    # window of 300s from now (1500) → cutoff 1200 → only "recent"
    assert [r.message for r in handler.get_records(since_seconds=300)] == ["recent"]


def test_emit_left_prunes_stale_records():
    clock = FakeClock(1000.0)
    handler = RingBufferLogHandler(max_records=100, max_age_seconds=300, time_fn=clock)
    handler.emit(_record(msg="old"))  # ts=1000
    clock.t = 1200
    handler.emit(_record(msg="recent"))  # ts=1200
    clock.t = 1400
    # emit at 1400 prunes anything older than 1400-300=1100 → drops "old"
    handler.emit(_record(msg="newest"))  # ts=1400
    everything = handler.get_records(since_seconds=1_000_000)
    assert [r.message for r in everything] == ["recent", "newest"]


def test_most_recent_error_returns_newest_error():
    handler = RingBufferLogHandler(max_records=100, max_age_seconds=0)
    handler.emit(_record(level=logging.INFO, msg="info1"))
    handler.emit(_record(level=logging.ERROR, msg="err1"))
    handler.emit(_record(level=logging.WARNING, msg="warn1"))
    handler.emit(_record(level=logging.ERROR, msg="err2"))
    handler.emit(_record(level=logging.INFO, msg="info2"))
    err = handler.most_recent_error()
    assert err is not None and err.message == "err2"


def test_most_recent_error_none_when_only_non_errors():
    handler = RingBufferLogHandler(max_records=10, max_age_seconds=0)
    handler.emit(_record(level=logging.WARNING, msg="w"))
    handler.emit(_record(level=logging.INFO, msg="i"))
    assert handler.most_recent_error() is None


def test_most_recent_error_respects_window():
    clock = FakeClock(1000.0)
    handler = RingBufferLogHandler(max_records=100, max_age_seconds=0, time_fn=clock)
    handler.emit(_record(level=logging.ERROR, msg="old_err"))  # ts=1000
    clock.t = 2000
    handler.emit(_record(level=logging.INFO, msg="recent_info"))  # ts=2000
    # window 300s from now (2000) → cutoff 1700 → old_err excluded
    assert handler.most_recent_error(since_seconds=300) is None
    # wide window includes it
    wide = handler.most_recent_error(since_seconds=1_000_000)
    assert wide is not None and wide.message == "old_err"


def test_emit_never_raises_on_bad_record():
    handler = RingBufferLogHandler(max_records=5, max_age_seconds=0)
    handler.handleError = lambda record: None  # silence stderr
    # msg="%d" with a non-int arg makes getMessage() raise; emit must swallow.
    bad = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%d",
        args=("not-an-int",),
        exc_info=None,
    )
    handler.emit(bad)  # must not raise
    assert handler.get_records(since_seconds=10_000) == []


def test_thread_safety_smoke():
    handler = RingBufferLogHandler(max_records=1000, max_age_seconds=0)

    def worker() -> None:
        for i in range(200):
            handler.emit(_record(msg=f"t{i}"))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for _ in range(50):
        handler.get_records(since_seconds=10_000)  # concurrent reads
    for t in threads:
        t.join()
    # 5 * 200 = 1000 emits into a maxlen=1000 deque → exactly full, no error.
    assert len(handler.get_records(since_seconds=10_000)) == 1000


def test_clear_empties_buffer():
    handler = RingBufferLogHandler(max_records=5, max_age_seconds=0)
    handler.emit(_record(msg="x"))
    handler.clear()
    assert handler.get_records(since_seconds=10_000) == []
