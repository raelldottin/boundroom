"""Acceptance tests for the Slack Web API client (SPEC-007 part 2, RA-KNOW-012).

Everything runs on a fake clock against a scripted fake transport. Nothing here waits, opens a
socket or calls Slack.
"""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pytest

from automation.knowledge import slack_client as sc
from automation.knowledge.slack_client import (
    CAPTURE_BUDGET,
    QUESTION_BUDGET,
    CircuitOpen,
    ConnectFailed,
    DeadlineExceeded,
    ErrorClass,
    MethodSpec,
    ResponseLost,
    RetryBudget,
    SlackCallError,
    SlackClient,
    SlackHttpResponse,
    SlackRequest,
)

MODULE = Path(sc.__file__)
HISTORY = "conversations.history"
EPHEMERAL = "chat.postEphemeral"
POST = "chat.postMessage"
CONNECT = "apps.connections.open"
ONE_TRY = RetryBudget(cap_seconds=8.0, max_attempts=1)


class FakeClock:
    """Monotonic time that moves only when the client sleeps or a test advances it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


Scripted = Union[SlackHttpResponse, BaseException]


class FakeSlack:
    """Answers each request with the next scripted response, or ``ok`` once the script runs out."""

    def __init__(self, clock: FakeClock, script: Iterable[Scripted] = ()) -> None:
        self.clock = clock
        self.script = list(script)
        self.calls: list[tuple[float, SlackRequest]] = []

    def __call__(self, request: SlackRequest) -> SlackHttpResponse:
        self.calls.append((self.clock.now, request))
        item = self.script.pop(0) if self.script else ok()
        if isinstance(item, BaseException):
            raise item
        return item

    def times(self, method: Optional[str] = None) -> list[float]:
        return [at for at, request in self.calls if method is None or request.method == method]


def ok(**fields: Any) -> SlackHttpResponse:
    return SlackHttpResponse(200, json.dumps({"ok": True, **fields}).encode())


def slack_error(code: str, status: int = 200, headers: Optional[dict[str, str]] = None) -> SlackHttpResponse:
    return SlackHttpResponse(status, json.dumps({"ok": False, "error": code}).encode(), headers or {})


def throttled(retry_after: Optional[str] = None) -> SlackHttpResponse:
    return slack_error("ratelimited", 429, {} if retry_after is None else {"Retry-After": retry_after})


def make(
    script: Iterable[Scripted] = (), *, jitter: float = 0.5, methods: Optional[dict[str, MethodSpec]] = None
) -> tuple[SlackClient, FakeSlack, FakeClock]:
    clock = FakeClock()
    slack = FakeSlack(clock, script)
    client = SlackClient("T0001", slack, clock=clock, sleep=clock.sleep, random=lambda: jitter, methods=methods)
    return client, slack, clock


def assert_within_rate(times: list[float], rate: float, burst: int) -> None:
    """No window [t_i, t_j] holds more than ``burst + rate * (t_j - t_i)`` calls: the token-bucket bound."""
    for i in range(len(times)):
        for j in range(i, len(times)):
            assert j - i + 1 <= burst + rate * (times[j] - times[i]) + 1e-6, (i, j, times[j] - times[i])


# The limiter.


def test_limiter_rates_are_eighty_percent_of_the_tier_floor_capped_at_one_per_second() -> None:
    per_minute = {tier: sc.rate_per_second(tier) * 60 for tier in (1, 2, 3, 4)}
    assert per_minute == pytest.approx({1: 0.8, 2: 16.0, 3: 40.0, 4: 60.0})
    assert sc.LIMITER_BURST == 3


@pytest.mark.parametrize(("method", "calls"), [(CONNECT, 8), (HISTORY, 60), (EPHEMERAL, 60)])
def test_limiter_never_exceeds_its_rate(method: str, calls: int) -> None:
    client, slack, _ = make()
    for index in range(calls):
        # Distinct channels keep the per-channel limiter out of the way of the method limiter.
        client.call(method, {"channel": f"C{index}"}, budget=CAPTURE_BUDGET)
    times = slack.times(method)
    rate = sc.rate_per_second(sc.METHODS[method].tier)
    assert_within_rate(times, rate, sc.LIMITER_BURST)
    # The bound is tight: the burst goes at once, and the steady state runs at the rate.
    assert times[: sc.LIMITER_BURST] == [times[0]] * sc.LIMITER_BURST
    assert times[-1] - times[sc.LIMITER_BURST - 1] == pytest.approx((calls - sc.LIMITER_BURST) / rate)


def test_limiter_is_per_method() -> None:
    client, slack, _ = make()
    for _ in range(10):
        client.call(HISTORY, budget=CAPTURE_BUDGET)
    before = slack.clock.now
    client.call(EPHEMERAL, {"channel": "C1"}, budget=CAPTURE_BUDGET)
    assert slack.times(EPHEMERAL) == [before]


def test_posts_to_one_channel_go_one_per_second_on_top_of_the_method_limiter() -> None:
    client, slack, _ = make()
    for _ in range(5):
        client.call(POST, {"channel": "C1", "text": "hi"}, budget=CAPTURE_BUDGET)
    times = slack.times(POST)
    assert_within_rate(times, sc.CHANNEL_POSTS_PER_SECOND, sc.CHANNEL_POST_BURST)
    assert [b - a for a, b in zip(times, times[1:])] == pytest.approx([1.0] * 4)


def test_posts_to_different_channels_share_only_the_method_limiter() -> None:
    client, slack, _ = make()
    for channel in ("C1", "C2", "C3"):
        client.call(POST, {"channel": channel}, budget=CAPTURE_BUDGET)
    assert len(set(slack.times(POST))) == 1


def test_a_post_without_a_channel_is_refused_before_the_transport() -> None:
    client, slack, _ = make()
    with pytest.raises(SlackCallError) as caught:
        client.call(EPHEMERAL, {"text": "hi"}, budget=QUESTION_BUDGET)
    assert caught.value.error_class is ErrorClass.CONFIGURATION
    assert slack.calls == []


def test_a_method_with_no_tier_is_refused_and_can_be_registered() -> None:
    client, slack, _ = make()
    with pytest.raises(SlackCallError, match="users.info: configuration") as caught:
        client.call("users.info", budget=QUESTION_BUDGET)
    assert caught.value.attempts == 0 and slack.calls == []

    client, slack, _ = make(methods={"users.info": MethodSpec(tier=4, idempotent=True)})
    assert client.call("users.info", {"user": "U1"}, budget=QUESTION_BUDGET) == {"ok": True}
    assert slack.calls[0][1] == SlackRequest("users.info", {"user": "U1"})


def test_the_spec_methods_cannot_be_loosened() -> None:
    with pytest.raises(ValueError, match="conversations.history"):
        make(methods={HISTORY: MethodSpec(tier=4, idempotent=True)})
    with pytest.raises(ValueError, match="tier"):
        MethodSpec(tier=5, idempotent=True)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="idempotency"):
        MethodSpec(tier=4, idempotent=True, posts=True)
    with pytest.raises(ValueError, match="workspace"):
        SlackClient("", FakeSlack(FakeClock()))


# Retry-After and the adaptive slowdown.


@pytest.mark.parametrize("jitter", [0.0, 0.25, 0.999999])
def test_a_429_waits_exactly_retry_after_plus_at_most_one_second(jitter: float) -> None:
    client, slack, _ = make([throttled("7")], jitter=jitter)
    assert client.call(HISTORY, budget=CAPTURE_BUDGET) == {"ok": True}
    first, retry = slack.times()
    assert retry - first == pytest.approx(7 + jitter)
    assert 7 <= retry - first <= 8


def test_a_429_without_retry_after_waits_sixty_seconds() -> None:
    client, slack, _ = make([throttled()], jitter=0.0)
    client.call(HISTORY, budget=CAPTURE_BUDGET)
    first, retry = slack.times()
    assert retry - first == pytest.approx(60.0)


def test_ratelimited_with_http_200_is_throttled_too() -> None:
    client, slack, _ = make([slack_error("ratelimited", headers={"retry-after": "3"})], jitter=0.0)
    client.call(HISTORY, budget=CAPTURE_BUDGET)
    first, retry = slack.times()
    assert retry - first == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({"Retry-After": "7"}, 7.0),
        ({"Content-Type": "application/json", "retry-after": " 1.5 "}, 1.5),
        ({"RETRY-AFTER": "0"}, 0.0),
        ({}, 60.0),
        ({"Retry-After": ""}, 60.0),
        ({"Retry-After": "soon"}, 60.0),
        ({"Retry-After": "-3"}, 60.0),
        ({"Retry-After": "nan"}, 60.0),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 60.0),
    ],
)
def test_retry_after_parsing(headers: dict[str, str], seconds: float) -> None:
    assert sc.retry_after_seconds(headers) == seconds


def test_a_429_blocks_only_its_method() -> None:
    client, slack, clock = make([throttled("30")], jitter=0.0)
    with pytest.raises(SlackCallError) as caught:
        client.call(HISTORY, budget=ONE_TRY)
    assert caught.value.error_class is ErrorClass.THROTTLED
    blocked_at = clock.now
    client.call(EPHEMERAL, {"channel": "C1"}, budget=ONE_TRY)
    client.call(HISTORY, budget=ONE_TRY)
    assert slack.times(EPHEMERAL) == [blocked_at]
    assert slack.times(HISTORY) == [blocked_at, pytest.approx(blocked_at + 30)]


def test_a_429_halves_the_rate_for_ten_minutes_then_restores_it_in_ten_percent_steps() -> None:
    client, slack, clock = make([throttled("1")], jitter=0.0)
    base = sc.rate_per_second(3)
    client.call(HISTORY, budget=CAPTURE_BUDGET)
    throttled_at = slack.times()[0]

    def rate_at(seconds_after: float) -> float:
        clock.now = throttled_at + seconds_after
        return client.current_rate(HISTORY) / base

    assert rate_at(1) == pytest.approx(0.5)
    assert rate_at(599) == pytest.approx(0.5)
    steps = [rate_at(600 + 60 * minute) for minute in range(6)]
    assert steps == pytest.approx([0.6, 0.7, 0.8, 0.9, 1.0, 1.0])


def test_after_a_429_calls_are_paced_at_half_rate_with_no_burst() -> None:
    client, slack, _ = make([throttled("2")], jitter=0.0)
    for _ in range(6):
        client.call(HISTORY, budget=CAPTURE_BUDGET)
    # The first call's retry and the five calls after it.
    after = slack.times()[1:]
    half = sc.rate_per_second(3) * sc.SLOWDOWN_FACTOR
    assert [b - a for a, b in zip(after, after[1:])] == pytest.approx([1 / half] * 5)


def test_a_second_429_halves_the_slowed_rate_again() -> None:
    client, _, _ = make([throttled("1"), throttled("1")], jitter=0.0)
    client.call(HISTORY, budget=CAPTURE_BUDGET)
    assert client.current_rate(HISTORY) == pytest.approx(sc.rate_per_second(3) / 4)


def test_a_question_stops_rather_than_wait_past_its_deadline() -> None:
    client, slack, clock = make([throttled("30")], jitter=0.0)
    start = clock.now
    with pytest.raises(DeadlineExceeded) as caught:
        client.call(EPHEMERAL, {"channel": "C1"}, budget=QUESTION_BUDGET)
    assert caught.value.error_class is ErrorClass.THROTTLED
    assert len(slack.calls) == 1 and clock.now == start


def test_a_shared_deadline_covers_the_limiter_wait() -> None:
    client, slack, clock = make()
    deadline = clock.now + 5
    for _ in range(sc.LIMITER_BURST):
        client.call(HISTORY, budget=QUESTION_BUDGET, deadline_at=deadline)
    # The fourth call needs 1.5 seconds of limiter wait: fine within the deadline, then not.
    client.call(HISTORY, budget=QUESTION_BUDGET, deadline_at=deadline)
    clock.now = deadline - 0.1
    for _ in range(sc.LIMITER_BURST - 1):
        client.call(HISTORY, budget=QUESTION_BUDGET, deadline_at=deadline)
    with pytest.raises(DeadlineExceeded, match="rate limit"):
        client.call(HISTORY, budget=QUESTION_BUDGET, deadline_at=deadline)
    assert clock.now == deadline - 0.1


# Error classes and their responses.


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (429, None, ErrorClass.THROTTLED),
        (200, "ratelimited", ErrorClass.THROTTLED),
        (500, None, ErrorClass.TRANSIENT),
        (503, "invalid_auth", ErrorClass.TRANSIENT),
        *[(200, code, ErrorClass.TRANSIENT) for code in sorted(sc.TRANSIENT_ERRORS)],
        *[(200, code, ErrorClass.ACCESS_GONE) for code in sorted(sc.ACCESS_GONE_ERRORS)],
        *[(200, code, ErrorClass.CREDENTIALS_GONE) for code in sorted(sc.CREDENTIALS_GONE_ERRORS)],
        *[(200, code, ErrorClass.CONFIGURATION) for code in sorted(sc.CONFIGURATION_ERRORS)],
        (200, "invalid_cursor", ErrorClass.CONFIGURATION),
        (200, "invalid_ts_latest", ErrorClass.CONFIGURATION),
        (200, "something_new", ErrorClass.UNKNOWN),
        (404, None, ErrorClass.UNKNOWN),
        (200, None, ErrorClass.UNKNOWN),
    ],
)
def test_classify(status: int, error: Optional[str], expected: ErrorClass) -> None:
    assert sc.classify(status, error) is expected


@pytest.mark.parametrize(
    "code",
    sorted(sc.ACCESS_GONE_ERRORS | sc.CREDENTIALS_GONE_ERRORS | sc.CONFIGURATION_ERRORS) + ["invalid_cursor", "something_new"],
)
def test_errors_that_are_not_retried_reach_the_caller_after_one_attempt(code: str) -> None:
    client, slack, clock = make([slack_error(code)])
    with pytest.raises(SlackCallError) as caught:
        client.call(HISTORY, {"channel": "C1"}, budget=CAPTURE_BUDGET)
    assert caught.value.error == code
    assert caught.value.error_class is sc.classify(200, code)
    assert caught.value.attempts == 1 and len(slack.calls) == 1
    assert clock.sleeps == []
    assert HISTORY in str(caught.value)


@pytest.mark.parametrize("body", [b"<html>maintenance</html>", b"[true]", b'{"ok": "true"}', b'{"error": "x"}'])
def test_an_answer_that_is_not_a_slack_envelope_is_unknown_and_not_retried(body: bytes) -> None:
    client, slack, _ = make([SlackHttpResponse(200, body)])
    with pytest.raises(SlackCallError, match="not a Slack JSON envelope") as caught:
        client.call(HISTORY, budget=CAPTURE_BUDGET)
    assert caught.value.error_class is ErrorClass.UNKNOWN and len(slack.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [SlackHttpResponse(502, b""), slack_error("internal_error"), ConnectFailed("refused"), ResponseLost("timeout")],
    ids=["http-502", "internal_error", "connect-failed", "response-lost"],
)
def test_transient_failures_on_a_read_back_off_with_full_jitter(failure: Scripted) -> None:
    client, slack, clock = make([failure, failure, failure], jitter=0.5)
    assert client.call(HISTORY, budget=CAPTURE_BUDGET) == {"ok": True}
    assert len(slack.calls) == 4
    # Full jitter: half of min(cap, 1 * 2^retry) for retries 0, 1 and 2. No limiter wait is due.
    assert clock.sleeps == [0.5, 1.0, 2.0]


def test_backoff_ceilings_double_from_one_second_up_to_the_cap() -> None:
    assert [sc.backoff_ceiling(n, QUESTION_BUDGET.cap_seconds) for n in range(5)] == [1, 2, 4, 8, 8]
    assert [sc.backoff_ceiling(n, CAPTURE_BUDGET.cap_seconds) for n in (7, 8, 9)] == [128, 256, 300]


def test_budgets_match_the_spec() -> None:
    assert QUESTION_BUDGET == RetryBudget(cap_seconds=8.0, max_attempts=3, deadline_seconds=20.0)
    assert CAPTURE_BUDGET == RetryBudget(cap_seconds=300.0, max_attempts=8, deadline_seconds=None)


def test_a_question_gives_up_after_three_attempts() -> None:
    client, slack, _ = make([slack_error("internal_error")] * 3)
    with pytest.raises(SlackCallError, match="gave up after 3 attempts") as caught:
        client.call(HISTORY, budget=QUESTION_BUDGET)
    assert caught.value.error_class is ErrorClass.TRANSIENT and len(slack.calls) == 3


def test_a_capture_page_gives_up_after_eight_attempts() -> None:
    # Throttles count as attempts but not as breaker failures, so all eight attempts are used.
    client, slack, _ = make([throttled("1")] * 4 + [slack_error("internal_error"), throttled("1")] * 2)
    with pytest.raises(SlackCallError, match="gave up after 8 attempts"):
        client.call(HISTORY, budget=CAPTURE_BUDGET)
    assert len(slack.calls) == 8
    assert client.breaker.state == "closed"


def test_backoff_that_would_pass_the_deadline_stops_the_question() -> None:
    client, slack, clock = make([slack_error("internal_error")] * 2, jitter=0.99)
    deadline = clock.now + 1.5
    with pytest.raises(DeadlineExceeded, match="backoff") as caught:
        client.call(HISTORY, budget=QUESTION_BUDGET, deadline_at=deadline)
    assert caught.value.error_class is ErrorClass.TRANSIENT
    assert len(slack.calls) == 2 and clock.now <= deadline


@pytest.mark.parametrize("method", [EPHEMERAL, POST])
@pytest.mark.parametrize(
    "failure",
    [ResponseLost("read timed out"), SlackHttpResponse(500, b""), slack_error("internal_error"), slack_error("fatal_error")],
    ids=["response-lost", "http-500", "internal_error", "fatal_error"],
)
def test_a_post_whose_result_is_unknown_is_not_retried(method: str, failure: Scripted) -> None:
    client, slack, clock = make([failure])
    with pytest.raises(SlackCallError, match="result unknown") as caught:
        client.call(method, {"channel": "C1", "text": "answer"}, budget=QUESTION_BUDGET)
    assert caught.value.outcome_unknown and caught.value.error_class is ErrorClass.TRANSIENT
    assert len(slack.calls) == 1 and clock.sleeps == []


def test_a_post_that_never_reached_slack_is_retried() -> None:
    client, slack, _ = make([ConnectFailed("connection refused"), throttled("1")])
    assert client.call(EPHEMERAL, {"channel": "C1"}, budget=QUESTION_BUDGET) == {"ok": True}
    assert len(slack.calls) == 3


def test_success_returns_slacks_whole_answer() -> None:
    client, _, _ = make([ok(messages=[{"ts": "1.0"}], has_more=False)])
    assert client.call(HISTORY, {"channel": "C1", "limit": 200}, budget=CAPTURE_BUDGET) == {
        "ok": True,
        "messages": [{"ts": "1.0"}],
        "has_more": False,
    }


# The circuit breaker.


@pytest.mark.parametrize("code", sorted(sc.CREDENTIALS_GONE_ERRORS))
def test_a_credentials_error_opens_the_breaker(code: str) -> None:
    client, slack, _ = make([slack_error(code)])
    with pytest.raises(SlackCallError) as caught:
        client.call(HISTORY, budget=CAPTURE_BUDGET)
    assert caught.value.error_class is ErrorClass.CREDENTIALS_GONE and not isinstance(caught.value, CircuitOpen)
    assert client.breaker.state == "open" and client.breaker.opened_for is ErrorClass.CREDENTIALS_GONE
    with pytest.raises(CircuitOpen) as refused:
        client.call(EPHEMERAL, {"channel": "C1"}, budget=QUESTION_BUDGET)
    assert refused.value.error_class is ErrorClass.CREDENTIALS_GONE
    assert len(slack.calls) == 1


def test_five_consecutive_transient_failures_open_the_breaker_mid_call() -> None:
    client, slack, _ = make([slack_error("internal_error")] * 8)
    with pytest.raises(CircuitOpen, match="breaker opened") as caught:
        client.call(HISTORY, budget=CAPTURE_BUDGET)
    assert caught.value.error_class is ErrorClass.TRANSIENT and caught.value.attempts == 5
    assert len(slack.calls) == 5 and client.breaker.state == "open"


def test_an_answer_resets_the_failure_streak() -> None:
    script = [slack_error("internal_error")] * 2 + [ok()] + [slack_error("internal_error")] * 2 + [slack_error("not_in_channel")]
    client, _, _ = make(script)
    client.call(HISTORY, budget=QUESTION_BUDGET)
    with pytest.raises(SlackCallError):
        client.call(HISTORY, budget=QUESTION_BUDGET)
    assert client.breaker.state == "closed" and client.breaker.consecutive_failures == 0


def test_the_breaker_probes_after_five_minutes_doubling_to_an_hour() -> None:
    client, slack, clock = make([slack_error("token_revoked")] * 7)
    with pytest.raises(SlackCallError):
        client.call(HISTORY, budget=ONE_TRY)
    waits = []
    for _ in range(6):
        opened_at = clock.now
        clock.now = client.breaker.open_until - 0.001
        with pytest.raises(CircuitOpen):
            client.call(HISTORY, budget=ONE_TRY)
        clock.now = client.breaker.open_until
        calls = len(slack.calls)
        with pytest.raises(SlackCallError):
            client.call(HISTORY, budget=CAPTURE_BUDGET)
        assert len(slack.calls) == calls + 1, "a probe is one attempt"
        waits.append(clock.now - opened_at)
    assert waits == pytest.approx([300, 600, 1200, 2400, 3600, 3600])


def test_a_successful_probe_closes_the_breaker_and_resets_its_schedule() -> None:
    client, slack, clock = make([slack_error("internal_error")] * 5 + [ok()] + [slack_error("invalid_auth")])
    with pytest.raises(CircuitOpen):
        client.call(HISTORY, budget=CAPTURE_BUDGET)
    clock.now = client.breaker.open_until
    client.call(HISTORY, budget=CAPTURE_BUDGET)
    assert client.breaker.state == "closed" and client.breaker.probe_delay == sc.BREAKER_FIRST_PROBE_SECONDS
    with pytest.raises(SlackCallError):
        client.call(HISTORY, budget=CAPTURE_BUDGET)
    assert client.breaker.open_until == pytest.approx(clock.now + 300)


def test_a_probe_answered_with_a_non_transient_error_closes_the_breaker() -> None:
    client, _, clock = make([slack_error("account_inactive"), slack_error("channel_not_found")])
    with pytest.raises(SlackCallError):
        client.call(HISTORY, budget=ONE_TRY)
    clock.now = client.breaker.open_until
    with pytest.raises(SlackCallError) as caught:
        client.call(HISTORY, budget=ONE_TRY)
    assert caught.value.error_class is ErrorClass.ACCESS_GONE
    assert client.breaker.state == "closed"


# No network, and no path out of the knowledge service.


def test_the_client_never_touches_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the Slack client tried to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    script = [throttled("2"), slack_error("internal_error"), ConnectFailed("refused"), ok(), slack_error("invalid_auth")]
    client, slack, clock = make(script)
    assert client.call(HISTORY, budget=CAPTURE_BUDGET) == {"ok": True}
    with pytest.raises(SlackCallError):
        client.call(EPHEMERAL, {"channel": "C1"}, budget=QUESTION_BUDGET)
    clock.now = client.breaker.open_until
    client.call(POST, {"channel": "C1"}, budget=QUESTION_BUDGET)
    assert len(slack.calls) == 6


def test_the_client_imports_nothing_that_reaches_the_network_or_the_supervisor() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = ("socket", "ssl", "http", "urllib", "asyncio", "subprocess", "automation.supervisor", "automation.benchmark")
    assert not {name for name in imported if name.split(".")[0] in forbidden or name.startswith(forbidden)}
    assert {name.split(".")[0] for name in imported} <= {
        "__future__",
        "json",
        "math",
        "random",
        "time",
        "dataclasses",
        "enum",
        "typing",
        "pydantic",
    }
