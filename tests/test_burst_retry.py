"""The retry rule of the burst script and of the portal demo's burst: by TIME on the monotonic clock (not by count),
waits doubling from 0.1 s to 1 s, the desk's Retry-After honoured up to that maximum, `max_retries=0` meaning no retry,
and the same rule as the load driver. A fake clock makes every check exact (sleeping moves the clock; nothing waits)."""

from __future__ import annotations

import http.client
import threading
import time
from email.utils import formatdate
from typing import Any

import pytest
from support import FakeClock

from demo_only import burst
from lead_desk.constants import (
    BURST_BACKOFF_MAX_SECONDS,
    BURST_BACKOFF_SECONDS,
    BURST_RETRY_DEADLINE_SECONDS,
    HTTP_BUSY,
    HTTP_CREATED,
)
from load_tools import load_driver

HTTP_NOT_FOUND = 404


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    return FakeClock(monkeypatch)


MAX_CALLS = 5000  # the longest honest loop here makes about 300 tries


def always(res: int | str, retry_after: float | None = None) -> Any:
    calls: list[float] = []

    def post() -> tuple[int | str, float | None, Any]:
        assert len(calls) < MAX_CALLS, "the loop under test never ends"
        calls.append(time.monotonic())
        return res, retry_after, {"n": len(calls)}

    post.calls = calls  # type: ignore[attr-defined]
    return post


def test_a_busy_desk_is_retried_for_the_whole_deadline_not_for_a_count(clock: FakeClock) -> None:
    """Fails on a count loop (6 tries, about 0.5 s): by time the loop keeps going until 30 s have passed."""
    post = always(HTTP_BUSY)
    res, reply = burst.post_with_retries(post)
    assert res == HTTP_BUSY and reply == {"n": len(post.calls)}
    assert len(post.calls) > 25  # 0.1 + 0.2 + 0.4 + 0.8, then 1 s waits: about 33 tries in 30 s
    assert post.calls[-1] - post.calls[0] <= BURST_RETRY_DEADLINE_SECONDS  # no try starts after the deadline
    assert clock.now - post.calls[0] == pytest.approx(BURST_RETRY_DEADLINE_SECONDS)  # the last wait is cut to fit


def test_waits_double_and_stop_growing_at_the_maximum(clock: FakeClock) -> None:
    burst.post_with_retries(always("refused"), deadline=5.0)
    assert clock.sleeps[:5] == pytest.approx([0.1, 0.2, 0.4, 0.8, BURST_BACKOFF_MAX_SECONDS])
    assert max(clock.sleeps) <= BURST_BACKOFF_MAX_SECONDS


def test_the_desks_retry_after_is_honoured_up_to_the_maximum(clock: FakeClock) -> None:
    burst.post_with_retries(always(HTTP_BUSY, retry_after=1.0), deadline=3.5)
    assert clock.sleeps[:3] == pytest.approx([1.0, 1.0, 1.0])  # not 0.1, 0.2, 0.4: the desk asked for a second
    clock.sleeps.clear()
    burst.post_with_retries(always(HTTP_BUSY, retry_after=60.0), deadline=10.0)
    assert max(clock.sleeps) == pytest.approx(BURST_BACKOFF_MAX_SECONDS)  # capped at the back-off maximum


def test_an_answer_that_is_not_busy_ends_the_retries(clock: FakeClock) -> None:
    seen = iter([(HTTP_BUSY, None, 1), (HTTP_BUSY, None, 2), (HTTP_CREATED, None, 3)])
    res, reply = burst.post_with_retries(lambda: next(seen))
    assert (res, reply) == (HTTP_CREATED, 3)
    assert len(clock.sleeps) == 2
    assert burst.post_with_retries(always(HTTP_NOT_FOUND))[0] == HTTP_NOT_FOUND and len(clock.sleeps) == 2


def test_max_retries_zero_means_no_retry_at_all(clock: FakeClock) -> None:
    for limit, tries in ((0, 1), (1, 2), (3, 4)):
        post = always(HTTP_BUSY)
        burst.post_with_retries(post, max_retries=limit)
        assert len(post.calls) == tries


def test_stop_ends_the_loop_and_every_busy_answer_is_counted(clock: FakeClock) -> None:
    stop = threading.Event()
    counted: list[int | str] = []

    def count(res: int | str) -> None:
        counted.append(res)
        if len(counted) == 3:
            stop.set()

    burst.post_with_retries(always(HTTP_BUSY), stop=stop, on_retryable=count)
    assert counted == [HTTP_BUSY] * 3  # the last answer, the one that was not tried again, is counted too


def test_a_forward_step_of_the_wall_clock_changes_nothing(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    real = time.time  # wall-clock: the step is added to the real calendar clock
    monkeypatch.setattr(time, "time", lambda: real() + 3600.0)
    post = always(HTTP_BUSY)
    burst.post_with_retries(post, deadline=10.0)
    assert len(post.calls) > 8  # a deadline read from the wall clock would have ended after one try


def test_the_retry_after_header_is_read_as_seconds() -> None:
    assert burst.retry_after_seconds("1") == 1.0
    assert burst.retry_after_seconds(None) is None
    assert burst.retry_after_seconds("-3") is None
    date_text = formatdate(0, usegmt=True)  # wall-clock: only builds a date text
    assert burst.retry_after_seconds(date_text) is None  # a date is ignored, the back-off decides


@pytest.mark.parametrize(
    ("wait", "after", "left"),
    [(0.1, None, 30.0), (0.1, 1.0, 30.0), (0.4, 0.2, 30.0), (0.8, 99.0, 30.0), (1.0, None, 0.3), (0.1, 5.0, 0.0)],
)
def test_the_burst_script_and_the_load_driver_wait_the_same(wait: float, after: float | None, left: float) -> None:
    assert burst.next_wait(wait, after, left) == load_driver.next_wait(wait, after, left)


def test_the_load_driver_fallback_values_are_the_constants() -> None:
    """Without src on the path the driver uses its own copy of the retry values: it must be the same numbers."""
    assert load_driver.FALLBACK_RETRY_VALUES == (
        BURST_BACKOFF_SECONDS,
        BURST_BACKOFF_MAX_SECONDS,
        BURST_RETRY_DEADLINE_SECONDS,
    )


@pytest.mark.parametrize("error", [http.client.IncompleteRead(b"x"), http.client.BadStatusLine("x")])
def test_a_cut_off_reply_is_a_network_error_not_a_dead_thread(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    class Opener:
        def open(self, *_a: object, **_k: object) -> None:
            raise error

    monkeypatch.setattr(burst.urllib.request, "build_opener", lambda *_a: Opener())
    res, after, _ = burst._post_once("http://127.0.0.1:1/leads", b"{}")
    assert res == type(error).__name__ and after is None


def test_a_tiny_wait_still_moves_the_fake_clock(clock: FakeClock) -> None:
    """A wait of 1e-13 s added to 1030.0 changes nothing in a float: without a minimum step the retry loop never
    reached its deadline on the fake clock."""
    clock.now = 1030.0
    before = clock.now
    time.sleep(1e-13)
    assert clock.now > before
