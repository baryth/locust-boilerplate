"""
Phase 3 -- Stress test (continuous ramp, auto-stop on breakage).

Reuses SubscriptionUser from locustfile.py -- same file-combining pattern
as Phase 2 (Locust aggregates User/LoadTestShape classes across all files
passed via locustfile=, so locustfile.py itself needs no changes).

What this does:
  - Ramps CONTINUOUSLY (no holds) from RAMP_START_USERS up to PEAK_USERS
    over RAMP_DURATION_SECONDS, then holds at PEAK_USERS.
  - Every CHECK_INTERVAL_SECONDS, checks a ROLLING window of the most
    recent WINDOW_SIZE requests (all endpoints combined) against TWO
    independent triggers -- either one stops the run:
      1. Response-time trigger: rolling MONITOR_PERCENTILE percentile of
         response times exceeds RESPONSE_TIME_THRESHOLD_MS.
      2. Failure-rate trigger: rolling fraction of requests with a
         non-None `exception` (covers both real request exceptions and
         manual resp.failure(...) calls) exceeds FAILURE_RATE_THRESHOLD.
  - MAX_DURATION_SECONDS is a safety net in case neither trigger ever
    fires (peak would otherwise hold indefinitely).

Why two triggers, not one: a 100-VU run surfaced a real gap in a
latency-only monitor -- create_subscription started returning 500s at a
24% failure rate, but the failing requests came back FAST (clustered
around ~1000ms, not multi-second hangs), so rolling response-time
percentiles never crossed the threshold and the run only ended via the
MAX_DURATION_SECONDS safety cap, despite a clear, sustained failure
being underway the whole time. Latency and error-rate are genuinely
different failure modes -- a system can break by erroring out quickly
just as easily as by slowing down, and a monitor watching only one of
those is structurally blind to the other.

Response-time threshold reasoning: Phase 2 (30 VUs, light load) already
showed p95 latencies of roughly 230-310ms and occasional p99.9 tail
spikes up to ~1.1-1.3s on GET /subscriptions specifically -- that's
already-known, non-broken behavior. RESPONSE_TIME_THRESHOLD_MS=2000 with
MONITOR_PERCENTILE=0.90 requires a clear, broad slowdown (not just one
endpoint's known tail) before stopping on latency alone.

Failure-rate threshold reasoning: 0% failures were observed at both 30
and 50 VUs baseline; FAILURE_RATE_THRESHOLD=0.10 (10%) is comfortably
above any noise level seen so far (the recurring low-single-digit count
of GET /subscriptions 500s), so it only fires on a genuine, sustained
break like the 24% create_subscription failure rate seen at 100 VUs.

Run with:
    locust --config configs/phase3_stress.conf

--- EDIT CONSTANTS BELOW TO TUNE THE STRESS TEST ---
"""

from collections import deque
from threading import Lock

import gevent
from locust import LoadTestShape, events

# ---- CONFIG ----
RAMP_START_USERS = 1
PEAK_USERS = 100
RAMP_DURATION_SECONDS = 10 * 60   # time to climb from START to PEAK, continuous (no holds)
SPAWN_RATE = 1                    # users/sec -- keeps the climb smooth, not step-y
MAX_DURATION_SECONDS = 20 * 60    # safety net if the threshold is never crossed

MONITOR_PERCENTILE = 0.90
RESPONSE_TIME_THRESHOLD_MS = 2000
FAILURE_RATE_THRESHOLD = 0.10     # rolling fraction (0.10 = 10%) of recent requests that failed
WINDOW_SIZE = 200                 # rolling window: most recent N requests, all endpoints combined
CHECK_INTERVAL_SECONDS = 5
MIN_REQUESTS_BEFORE_CHECK = 50    # don't judge on too little data early in the ramp


class Phase3StressShape(LoadTestShape):
    """Continuous ramp, no holds, from RAMP_START_USERS to PEAK_USERS over
    RAMP_DURATION_SECONDS, then holds at PEAK_USERS until MAX_DURATION_SECONDS
    (safety net) or until the response-time monitor below stops the run."""

    def tick(self):
        run_time = self.get_run_time()
        if run_time > MAX_DURATION_SECONDS:
            return None
        progress = min(1.0, run_time / RAMP_DURATION_SECONDS)
        users = RAMP_START_USERS + round((PEAK_USERS - RAMP_START_USERS) * progress)
        return users, SPAWN_RATE


_response_times = deque(maxlen=WINDOW_SIZE)
_failures = deque(maxlen=WINDOW_SIZE)  # True/False per request, aligned with _response_times
_window_lock = Lock()


@events.request.add_listener
def _record_request_outcome(response_time, exception, **kwargs):
    # `exception` is non-None for both real request exceptions AND manual
    # resp.failure(...) calls made via catch_response -- confirmed against
    # Locust's own clients.py (HttpSession._report_request /
    # ResponseContextManager.failure), so this correctly counts both.
    with _window_lock:
        _response_times.append(response_time)
        _failures.append(exception is not None)


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return sorted_values[idx]


@events.init.add_listener
def _start_breakage_monitor(environment, **kwargs):
    def stop_run(reason):
        print(f"[stress] {reason} -- stopping")
        # Spawn in its own greenlet rather than calling quit() inline --
        # same reasoning as Phase 1's smoke-stop: quit() calls
        # self.greenlet.kill(block=True), and calling that from inside a
        # greenlet the runner itself manages can hang shutdown instead of
        # completing it.
        gevent.spawn(environment.runner.quit)

    def monitor_loop():
        while True:
            gevent.sleep(CHECK_INTERVAL_SECONDS)
            with _window_lock:
                response_time_snapshot = sorted(_response_times)
                failure_snapshot = list(_failures)

            if len(response_time_snapshot) < MIN_REQUESTS_BEFORE_CHECK:
                continue

            # Trigger 1: response-time degradation
            current_pct = _percentile(response_time_snapshot, MONITOR_PERCENTILE)
            if current_pct is not None and current_pct > RESPONSE_TIME_THRESHOLD_MS:
                stop_run(
                    f"rolling p{int(MONITOR_PERCENTILE * 100)} response time "
                    f"{current_pct:.0f}ms exceeded threshold {RESPONSE_TIME_THRESHOLD_MS}ms "
                    f"(window of {len(response_time_snapshot)} requests)"
                )
                return

            # Trigger 2: failure rate -- catches fast-failing errors (like
            # a 500 returned quickly) that a latency-only monitor can't see.
            failure_rate = sum(failure_snapshot) / len(failure_snapshot)
            if failure_rate > FAILURE_RATE_THRESHOLD:
                stop_run(
                    f"rolling failure rate {failure_rate:.1%} exceeded threshold "
                    f"{FAILURE_RATE_THRESHOLD:.1%} (window of {len(failure_snapshot)} requests)"
                )
                return

    gevent.spawn(monitor_loop)
