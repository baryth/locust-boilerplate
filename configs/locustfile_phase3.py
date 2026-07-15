"""
Phase 3 -- Stress test (continuous ramp, auto-stop on response-time degradation).

Reuses SubscriptionUser from locustfile.py -- same file-combining pattern
as Phase 2 (Locust aggregates User/LoadTestShape classes across all files
passed via locustfile=, so locustfile.py itself needs no changes).

What this does:
  - Ramps CONTINUOUSLY (no holds) from RAMP_START_USERS up to PEAK_USERS
    over RAMP_DURATION_SECONDS, then holds at PEAK_USERS.
  - Monitors a ROLLING window of the most recent WINDOW_SIZE request
    response times (across all endpoints combined) and computes the
    MONITOR_PERCENTILE percentile every CHECK_INTERVAL_SECONDS.
  - Auto-stops the whole run the first time that rolling percentile
    exceeds RESPONSE_TIME_THRESHOLD_MS -- that's the "it broke" signal
    for a stress test, distinct from Phase 1/2's fixed-duration approach.
  - MAX_DURATION_SECONDS is a safety net in case the threshold is never
    crossed (peak would otherwise hold indefinitely).

Threshold reasoning: Phase 2 (30 VUs, light load) already showed p95
latencies of roughly 230-310ms and occasional p99.9 tail spikes up to
~1.1-1.3s on GET /subscriptions specifically -- that's already-known,
non-broken behavior. RESPONSE_TIME_THRESHOLD_MS=1000 on a rolling p95
(not a single-request outlier, not the existing known tail) means this
only trips when at least 5% of very recent requests are meaningfully
slower than anything seen at light load -- a real broad degradation
signal, not noise from one already-slow-but-fine endpoint.

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
PEAK_USERS = 50
RAMP_DURATION_SECONDS = 10 * 60   # time to climb from START to PEAK, continuous (no holds)
SPAWN_RATE = 1                    # users/sec -- keeps the climb smooth, not step-y
MAX_DURATION_SECONDS = 20 * 60    # safety net if the threshold is never crossed

MONITOR_PERCENTILE = 0.95
RESPONSE_TIME_THRESHOLD_MS = 1000
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
_window_lock = Lock()


@events.request.add_listener
def _record_response_time(response_time, **kwargs):
    with _window_lock:
        _response_times.append(response_time)


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return sorted_values[idx]


@events.init.add_listener
def _start_response_time_monitor(environment, **kwargs):
    def monitor_loop():
        while True:
            gevent.sleep(CHECK_INTERVAL_SECONDS)
            with _window_lock:
                snapshot = sorted(_response_times)
            if len(snapshot) < MIN_REQUESTS_BEFORE_CHECK:
                continue
            current = _percentile(snapshot, MONITOR_PERCENTILE)
            if current is not None and current > RESPONSE_TIME_THRESHOLD_MS:
                print(
                    f"[stress] rolling p{int(MONITOR_PERCENTILE * 100)} response time "
                    f"{current:.0f}ms exceeded threshold {RESPONSE_TIME_THRESHOLD_MS}ms "
                    f"(window of {len(snapshot)} requests) -- stopping"
                )
                # Spawn in its own greenlet rather than calling quit() inline
                # -- same reasoning as Phase 1's smoke-stop: quit() calls
                # self.greenlet.kill(block=True), and calling that from
                # inside a greenlet the runner itself manages can hang
                # shutdown instead of completing it.
                gevent.spawn(environment.runner.quit)
                return

    gevent.spawn(monitor_loop)
