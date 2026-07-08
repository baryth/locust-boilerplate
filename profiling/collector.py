import statistics
from collections import defaultdict, deque

# Cap memory use on very long runs; recent samples matter most for
# profiling anyway.
MAX_SAMPLES_PER_ENDPOINT = 5000


class Collector:
    """Keeps a bounded, time-ordered list of response times per endpoint
    name, plus success/failure counts. Populated by profiling.listeners
    via Locust's `request` event.
    """

    _samples = defaultdict(lambda: deque(maxlen=MAX_SAMPLES_PER_ENDPOINT))
    _failures = defaultdict(int)
    _total = defaultdict(int)

    @classmethod
    def record(cls, name: str, response_time_ms: float, success: bool) -> None:
        cls._samples[name].append(response_time_ms)
        cls._total[name] += 1
        if not success:
            cls._failures[name] += 1

    @classmethod
    def endpoint_names(cls):
        return list(cls._samples.keys())

    @classmethod
    def stats_for(cls, name: str):
        """Chronological + percentile stats for one endpoint, or None if
        no samples were recorded."""
        times = list(cls._samples[name])
        if not times:
            return None

        n = len(times)
        times_sorted = sorted(times)
        mean = statistics.mean(times)
        stdev = statistics.pstdev(times) if n > 1 else 0.0
        cv = (stdev / mean) if mean else 0.0

        # Naive percentiles - good enough for lightweight profiling
        # without pulling in numpy.
        p50 = times_sorted[int(0.50 * (n - 1))]
        p95 = times_sorted[int(0.95 * (n - 1))]

        # Drift: compare the average of the first and last slices of the
        # run (in original chronological order) to catch response times
        # that creep up over time - a common symptom of a bottleneck.
        window = max(1, n // 5)
        first_avg = statistics.mean(times[:window])
        last_avg = statistics.mean(times[-window:])
        drift_ratio = (last_avg / first_avg) if first_avg else 1.0

        total = cls._total[name]
        failures = cls._failures[name]

        return {
            "count": n,
            "failures": failures,
            "error_rate": (failures / total) if total else 0.0,
            "mean_ms": mean,
            "p50_ms": p50,
            "p95_ms": p95,
            "stdev_ms": stdev,
            "cv": cv,
            "drift_ratio": drift_ratio,
        }
