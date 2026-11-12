"""Slack Web API client for the knowledge service (SPEC-007 part 2, RA-KNOW-012).

The contract is ``docs/architecture/knowledge-loop-slack.md``: decisions 2, 4 and 5, "Timers and
budgets" and "Errors". Every number here comes from those tables.

Throttle first, retry second. Every call takes a token from a per-method limiter held under
Slack's published tier, and a post also takes one from a per-channel limiter, before the
transport sees it. Slack's ``429`` is a fallback, not the plan: it blocks only its method for
``Retry-After`` plus jitter and halves that method's rate for a while. Each failure is
classified, and a call is retried only when Slack definitely didn't act on it or the method is
idempotent, within a budget and behind a per-workspace circuit breaker.

The client opens no connection and holds no token. The caller injects the transport, which owns
the network and the credentials, and the clock, sleep and random source, so tests run on a fake
clock with no network. It can reach nothing but the transport it is given: no supervisor, shell,
queue, board, GitHub or consumer sync.
"""

from __future__ import annotations

import json
import math
import random as _random
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Literal, Mapping, Optional, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

Tier = Literal[1, 2, 3, 4]

# Slack publishes floors ("50+ per minute"), not exact limits. The limiter runs at 80% of the
# floor, never above 1 call per second, which is the rate Slack asks every method to design for.
TIER_FLOOR_PER_MINUTE: Mapping[int, int] = {1: 1, 2: 20, 3: 50, 4: 100}
LIMITER_MARGIN = 0.8
MAX_CALLS_PER_SECOND = 1.0
LIMITER_BURST = 3

# chat.postMessage's per-channel limit, applied on top of the method limiter.
CHANNEL_POSTS_PER_SECOND = 1.0
CHANNEL_POST_BURST = 1

RETRY_AFTER_JITTER_SECONDS = 1.0
# A 429 without Retry-After shouldn't happen; when it does, assume the worst published wait.
RETRY_AFTER_MISSING_SECONDS = 60.0
SLOWDOWN_FACTOR = 0.5
SLOWDOWN_HOLD_SECONDS = 600.0
SLOWDOWN_RESTORE_STEP = 0.1
SLOWDOWN_RESTORE_INTERVAL_SECONDS = 60.0

BACKOFF_BASE_SECONDS = 1.0

BREAKER_FAILURE_THRESHOLD = 5
BREAKER_FIRST_PROBE_SECONDS = 300.0
BREAKER_MAX_PROBE_SECONDS = 3600.0

# Float sums of waits land a hair short of the instant they aimed for; this much is "now".
_EPSILON = 1e-9


class ErrorClass(StrEnum):
    THROTTLED = "throttled"
    TRANSIENT = "transient"
    ACCESS_GONE = "access_gone"
    CREDENTIALS_GONE = "credentials_gone"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


THROTTLED_ERRORS = frozenset({"ratelimited"})
TRANSIENT_ERRORS = frozenset({"internal_error", "fatal_error", "service_unavailable", "request_timeout"})
ACCESS_GONE_ERRORS = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "access_denied",
        "no_permission",
        "channel_is_limited_access",
        "team_access_not_granted",
        "ekm_access_denied",
        "user_not_in_channel",
    }
)
CREDENTIALS_GONE_ERRORS = frozenset({"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"})
# Plus every other ``invalid_*`` error. ``invalid_auth`` is a credentials error and is matched first.
CONFIGURATION_ERRORS = frozenset({"missing_scope", "not_allowed_token_type", "invalid_arguments", "method_deprecated"})


def classify(status: int, error: Optional[str]) -> ErrorClass:
    """Classify a Web API answer by HTTP status and Slack ``error`` code, as the spec's Errors table does."""
    if status == 429 or error in THROTTLED_ERRORS:
        return ErrorClass.THROTTLED
    if 500 <= status <= 599 or error in TRANSIENT_ERRORS:
        return ErrorClass.TRANSIENT
    if error in CREDENTIALS_GONE_ERRORS:
        return ErrorClass.CREDENTIALS_GONE
    if error in ACCESS_GONE_ERRORS:
        return ErrorClass.ACCESS_GONE
    if error is not None and (error in CONFIGURATION_ERRORS or error.startswith("invalid_")):
        return ErrorClass.CONFIGURATION
    return ErrorClass.UNKNOWN


@dataclass(frozen=True)
class MethodSpec:
    """How the client treats one Web API method.

    ``idempotent`` decides whether a call whose result is unknown may be repeated. ``posts``
    puts the call under the per-channel posting limit and requires a ``channel`` parameter.
    """

    tier: Tier
    idempotent: bool
    posts: bool = False

    def __post_init__(self) -> None:
        if self.tier not in TIER_FLOOR_PER_MINUTE:
            raise ValueError(f"tier must be 1, 2, 3 or 4, not {self.tier!r}")
        if self.posts and self.idempotent:
            raise ValueError("a posting method has no idempotency key, so it can't be idempotent")


# Only the methods the spec names. A later slice registers the others it needs, with the tier
# Slack documents for each, through ``SlackClient(methods=...)``.
METHODS: Mapping[str, MethodSpec] = {
    # The spec treats it as Tier 1 until RA-KNOW-013 verifies it. Opening a second connection
    # URL has no lasting effect, so a repeat is harmless.
    "apps.connections.open": MethodSpec(tier=1, idempotent=True),
    "conversations.history": MethodSpec(tier=3, idempotent=True),
    # Same limits as conversations.history for an internal app (Slack's 2025-05-29 change).
    "conversations.replies": MethodSpec(tier=3, idempotent=True),
    "chat.postEphemeral": MethodSpec(tier=4, idempotent=False, posts=True),
    # Slack lists it as a special tier. The 1-per-second ceiling and the channel limiter are the
    # spec's whole budget for it, which Tier 4 at the ceiling expresses.
    "chat.postMessage": MethodSpec(tier=4, idempotent=False, posts=True),
}


def rate_per_second(tier: Tier) -> float:
    """The limiter's steady rate for a tier: 80% of the floor, capped at 1 per second."""
    return min(TIER_FLOOR_PER_MINUTE[tier] * LIMITER_MARGIN / 60.0, MAX_CALLS_PER_SECOND)


@dataclass(frozen=True)
class RetryBudget:
    """How long a caller may keep retrying one call.

    ``deadline_seconds`` bounds every wait of the call, limiter waits included, from the call's
    start. A caller whose deadline spans several calls passes ``deadline_at`` to each instead.
    """

    cap_seconds: float
    max_attempts: int
    deadline_seconds: Optional[float] = None


# A person is waiting for the answer.
QUESTION_BUDGET = RetryBudget(cap_seconds=8.0, max_attempts=3, deadline_seconds=20.0)
# Capture can wait; a page that exhausts this marks its channel failed for the run.
CAPTURE_BUDGET = RetryBudget(cap_seconds=300.0, max_attempts=8)


def backoff_ceiling(retry_index: int, cap_seconds: float) -> float:
    """The upper bound of the full-jitter wait before retry ``retry_index`` (0 for the first retry)."""
    return min(cap_seconds, BACKOFF_BASE_SECONDS * 2.0**retry_index)


def retry_after_seconds(headers: Mapping[str, str]) -> float:
    """Slack's ``Retry-After`` in seconds, or 60 when it is missing or isn't a non-negative number."""
    for name, value in headers.items():
        if name.lower() != "retry-after":
            continue
        try:
            seconds = float(value.strip())
        except ValueError:
            break
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
        break
    return RETRY_AFTER_MISSING_SECONDS


@dataclass(frozen=True)
class SlackRequest:
    method: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class SlackHttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """Sends one Web API request and returns Slack's answer.

    The transport adds the token. It raises ``ConnectFailed`` when the request can't have
    reached Slack, and ``ResponseLost`` when it may have but no answer arrived.
    """

    def __call__(self, request: SlackRequest) -> SlackHttpResponse: ...


class TransportError(Exception):
    """The transport couldn't complete the exchange."""


class ConnectFailed(TransportError):
    """The request never reached Slack: connection refused, or a DNS or TLS handshake failure."""


class ResponseLost(TransportError):
    """The request may have reached Slack but no answer came back: a timeout or a dropped connection."""


class SlackCallError(Exception):
    """A call failed and won't be retried. ``error_class`` says what the caller must do next.

    ``outcome_unknown`` is set when Slack may have acted, on a method that can't safely repeat.
    """

    def __init__(
        self,
        method: str,
        error_class: ErrorClass,
        detail: str,
        *,
        attempts: int,
        error: Optional[str] = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(f"{method}: {error_class.value}: {detail}")
        self.method = method
        self.error_class = error_class
        self.error = error
        self.attempts = attempts
        self.outcome_unknown = outcome_unknown


class CircuitOpen(SlackCallError):
    """The workspace's breaker is open. ``error_class`` is what opened it."""


class DeadlineExceeded(SlackCallError):
    """The next wait would end after the call's deadline, so the call stopped without waiting."""


class _Envelope(BaseModel):
    # Slack's responses carry many more fields; the client reads only these two.
    model_config = ConfigDict(extra="ignore", strict=True)

    ok: bool
    error: Optional[str] = None


def _decode(body: bytes) -> tuple[Optional[_Envelope], Optional[dict[str, Any]]]:
    try:
        data = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    try:
        return _Envelope.model_validate(data), data
    except ValidationError:
        return None, None


class _Bucket:
    """A token bucket whose rate a ``429`` can halve, then restore in steps."""

    def __init__(self, rate: float, burst: int, now: float) -> None:
        self.base_rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.updated = now
        self.blocked_until = now
        self.slowed_at: Optional[float] = None
        self.slowed_to = 1.0

    def multiplier(self, now: float) -> float:
        if self.slowed_at is None:
            return 1.0
        elapsed = now - self.slowed_at
        if elapsed < SLOWDOWN_HOLD_SECONDS:
            return self.slowed_to
        steps = math.floor((elapsed - SLOWDOWN_HOLD_SECONDS) / SLOWDOWN_RESTORE_INTERVAL_SECONDS) + 1
        return min(1.0, self.slowed_to + steps * SLOWDOWN_RESTORE_STEP)

    def rate(self, now: float) -> float:
        return self.base_rate * self.multiplier(now)

    def _refill(self, now: float) -> None:
        # Each interval is credited at the rate in force when it began. Between 429s the rate
        # only rises, so this can under-credit but never lets the limiter run over its rate.
        if now > self.updated:
            self.tokens = min(float(self.burst), self.tokens + (now - self.updated) * self.rate(self.updated))
            self.updated = now

    def wait(self, now: float) -> float:
        self._refill(now)
        blocked = max(0.0, self.blocked_until - now)
        if self.tokens >= 1.0 - _EPSILON:
            return blocked
        return max(blocked, (1.0 - self.tokens) / self.rate(now))

    def take(self) -> None:
        self.tokens = max(0.0, self.tokens - 1.0)

    def slow_down(self, now: float, until: float) -> None:
        """Block until ``until``, then allow exactly one call and pace the rest at half the current rate."""
        self.slowed_to = self.multiplier(now) * SLOWDOWN_FACTOR
        self.slowed_at = now
        self.blocked_until = until
        # Nothing accrues while blocked, so the retry Slack allowed at ``until`` doesn't bring a
        # burst with it.
        self.tokens = 1.0
        self.updated = until


BreakerState = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    """Per-workspace breaker: open after 5 consecutive transient failures or any credentials error.

    An open breaker refuses calls until its probe time, then admits one probe. A probe that gets
    any answer except a transient or credentials failure closes it; otherwise the wait doubles,
    up to an hour.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.state: BreakerState = "closed"
        self.opened_for: Optional[ErrorClass] = None
        self.open_until = 0.0
        self.probe_delay = BREAKER_FIRST_PROBE_SECONDS
        self.consecutive_failures = 0

    def admit(self) -> bool:
        if self.state == "open":
            if self._clock() < self.open_until:
                return False
            self.state = "half_open"
        return True

    def answered(self) -> None:
        """Slack answered, with the workspace reachable and the token accepted."""
        self.state = "closed"
        self.opened_for = None
        self.probe_delay = BREAKER_FIRST_PROBE_SECONDS
        self.consecutive_failures = 0

    def failed(self, error_class: ErrorClass) -> None:
        """Record a transient or credentials failure."""
        if self.state == "half_open":
            self.probe_delay = min(self.probe_delay * 2.0, BREAKER_MAX_PROBE_SECONDS)
            self._open(error_class)
            return
        if error_class is ErrorClass.CREDENTIALS_GONE:
            self._open(error_class)
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= BREAKER_FAILURE_THRESHOLD:
            self._open(error_class)

    def _open(self, error_class: ErrorClass) -> None:
        self.state = "open"
        self.opened_for = error_class
        self.open_until = self._clock() + self.probe_delay


@dataclass(frozen=True)
class _Failure:
    error_class: ErrorClass
    detail: str
    error: Optional[str] = None
    # Slack may have acted on the request.
    maybe_acted: bool = False
    retry_after: float = 0.0


def _stop(
    cls: type[SlackCallError],
    method: str,
    failure: _Failure,
    attempts: int,
    why: str = "",
    *,
    outcome_unknown: bool = False,
) -> SlackCallError:
    detail = f"{failure.detail}; {why}" if why else failure.detail
    return cls(method, failure.error_class, detail, attempts=attempts, error=failure.error, outcome_unknown=outcome_unknown)


class SlackClient:
    """Web API client for one workspace, with its limiters and breaker.

    One client serves one workspace, as one adapter does, so limiters are keyed by method and
    channel and the breaker covers the whole client.
    """

    # ponytail: not thread-safe. One caller at a time; add a lock if a later slice calls it
    # from more than one thread.

    def __init__(
        self,
        workspace: str,
        transport: Transport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = _random.random,
        methods: Optional[Mapping[str, MethodSpec]] = None,
    ) -> None:
        if not workspace:
            raise ValueError("workspace must be a Slack workspace id")
        extra = dict(methods or {})
        # A caller may add methods but not loosen the spec's own entries.
        overlap = sorted(set(extra) & set(METHODS))
        if overlap:
            raise ValueError(f"methods already defined by the spec can't be overridden: {', '.join(overlap)}")
        self.workspace = workspace
        self.breaker = CircuitBreaker(clock)
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._random = random
        self._methods: dict[str, MethodSpec] = {**METHODS, **extra}
        self._method_buckets: dict[str, _Bucket] = {}
        self._channel_buckets: dict[str, _Bucket] = {}

    def current_rate(self, method: str) -> float:
        """The method limiter's rate now, in calls per second, after any 429 slowdown."""
        return self._method_bucket(method, self._spec(method)).rate(self._clock())

    def call(
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        budget: RetryBudget,
        deadline_at: Optional[float] = None,
    ) -> dict[str, Any]:
        """Call ``method`` and return Slack's decoded answer, or raise ``SlackCallError``."""
        spec = self._spec(method)
        request = SlackRequest(method, dict(params or {}))
        buckets = [self._method_bucket(method, spec)]
        if spec.posts:
            channel = request.params.get("channel")
            if not isinstance(channel, str) or not channel:
                raise SlackCallError(method, ErrorClass.CONFIGURATION, "a post needs a channel", attempts=0)
            buckets.append(self._channel_bucket(channel))
        if deadline_at is None and budget.deadline_seconds is not None:
            deadline_at = self._clock() + budget.deadline_seconds

        attempts = 0
        last: Optional[_Failure] = None
        while True:
            if not self.breaker.admit():
                raise CircuitOpen(
                    method,
                    self.breaker.opened_for or ErrorClass.TRANSIENT,
                    f"workspace {self.workspace} breaker is open",
                    attempts=attempts,
                )
            self._acquire(method, buckets, deadline_at, attempts, last)
            attempts += 1
            result = self._attempt(request)
            if isinstance(result, dict):
                self.breaker.answered()
                return result
            last = result
            self._handle(method, spec, buckets[0], result, attempts, budget, deadline_at)

    def _handle(
        self,
        method: str,
        spec: MethodSpec,
        bucket: _Bucket,
        failure: _Failure,
        attempts: int,
        budget: RetryBudget,
        deadline_at: Optional[float],
    ) -> None:
        """Raise unless ``failure`` may be retried; otherwise do its wait, or arm the limiter to."""
        error_class = failure.error_class
        if error_class in {ErrorClass.TRANSIENT, ErrorClass.CREDENTIALS_GONE}:
            self.breaker.failed(error_class)
        else:
            self.breaker.answered()

        if error_class is ErrorClass.TRANSIENT:
            if failure.maybe_acted and not spec.idempotent:
                # No idempotency key: a duplicate post is worse than a missing one.
                raise _stop(SlackCallError, method, failure, attempts, "result unknown, not retried", outcome_unknown=True)
            if self.breaker.state == "open":
                raise _stop(CircuitOpen, method, failure, attempts, f"workspace {self.workspace} breaker opened")
            if attempts >= budget.max_attempts:
                raise _stop(SlackCallError, method, failure, attempts, f"gave up after {attempts} attempts")
            wait = self._random() * backoff_ceiling(attempts - 1, budget.cap_seconds)
            if deadline_at is not None and self._clock() + wait > deadline_at:
                raise _stop(DeadlineExceeded, method, failure, attempts, "backoff would pass the deadline")
            self._sleep(wait)
            return
        if error_class is ErrorClass.THROTTLED:
            now = self._clock()
            # Armed even when giving up, so the method stays blocked for whoever calls it next.
            until = now + failure.retry_after + self._random() * RETRY_AFTER_JITTER_SECONDS
            bucket.slow_down(now, until)
            if attempts >= budget.max_attempts:
                raise _stop(SlackCallError, method, failure, attempts, f"gave up after {attempts} attempts")
            if deadline_at is not None and until > deadline_at:
                raise _stop(DeadlineExceeded, method, failure, attempts, "Retry-After would pass the deadline")
            return
        raise _stop(SlackCallError, method, failure, attempts)

    def _attempt(self, request: SlackRequest) -> dict[str, Any] | _Failure:
        try:
            response = self._transport(request)
        except ConnectFailed as error:
            return _Failure(ErrorClass.TRANSIENT, f"connection failed: {error}")
        except ResponseLost as error:
            return _Failure(ErrorClass.TRANSIENT, f"no response: {error}", maybe_acted=True)
        envelope, data = _decode(response.body)
        if response.status == 200 and envelope is not None and envelope.ok and data is not None:
            return data
        code = envelope.error if envelope is not None else None
        error_class = classify(response.status, code)
        detail = f"HTTP {response.status}" + (f" {code}" if code else "")
        if error_class is ErrorClass.UNKNOWN and envelope is None:
            detail += ", response is not a Slack JSON envelope"
        return _Failure(
            error_class,
            detail,
            error=code,
            maybe_acted=error_class is ErrorClass.TRANSIENT,
            retry_after=retry_after_seconds(response.headers) if error_class is ErrorClass.THROTTLED else 0.0,
        )

    def _acquire(
        self,
        method: str,
        buckets: list[_Bucket],
        deadline_at: Optional[float],
        attempts: int,
        last: Optional[_Failure],
    ) -> None:
        while True:
            now = self._clock()
            wait = max(bucket.wait(now) for bucket in buckets)
            if wait <= _EPSILON:
                for bucket in buckets:
                    bucket.take()
                return
            if deadline_at is not None and now + wait > deadline_at:
                error_class = last.error_class if last else ErrorClass.THROTTLED
                raise DeadlineExceeded(
                    method,
                    error_class,
                    "the rate limit's wait would pass the deadline",
                    attempts=attempts,
                    error=last.error if last else None,
                )
            self._sleep(wait)

    def _spec(self, method: str) -> MethodSpec:
        spec = self._methods.get(method)
        if spec is None:
            # Every call must pass a limiter, and a limiter needs the method's tier.
            raise SlackCallError(method, ErrorClass.CONFIGURATION, "method has no registered tier", attempts=0)
        return spec

    def _method_bucket(self, method: str, spec: MethodSpec) -> _Bucket:
        bucket = self._method_buckets.get(method)
        if bucket is None:
            bucket = _Bucket(rate_per_second(spec.tier), LIMITER_BURST, self._clock())
            self._method_buckets[method] = bucket
        return bucket

    def _channel_bucket(self, channel: str) -> _Bucket:
        bucket = self._channel_buckets.get(channel)
        if bucket is None:
            bucket = _Bucket(CHANNEL_POSTS_PER_SECOND, CHANNEL_POST_BURST, self._clock())
            self._channel_buckets[channel] = bucket
        return bucket
