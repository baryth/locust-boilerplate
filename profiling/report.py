import json
import time
from pathlib import Path

from profiling.baseline import get_baseline
from profiling.collector import Collector

# A last-window-average / first-window-average ratio above this is
# treated as a potential bottleneck (response times degrading over the
# course of the run).
DEGRADATION_DRIFT_THRESHOLD = 1.5


def build_report() -> dict:
    report = {}

    for name in Collector.endpoint_names():
        stats = Collector.stats_for(name)
        if not stats:
            continue

        baseline = get_baseline(name)
        flags = []

        if baseline.get("p50_ms") and stats["p50_ms"] > baseline["p50_ms"]:
            flags.append(f"p50 {stats['p50_ms']:.0f}ms exceeds baseline {baseline['p50_ms']}ms")

        if baseline.get("p95_ms") and stats["p95_ms"] > baseline["p95_ms"]:
            flags.append(f"p95 {stats['p95_ms']:.0f}ms exceeds baseline {baseline['p95_ms']}ms")

        max_cv = baseline.get("max_cv")
        if max_cv and stats["cv"] > max_cv:
            flags.append(f"unstable: CV {stats['cv']:.2f} exceeds max {max_cv}")

        if stats["drift_ratio"] > DEGRADATION_DRIFT_THRESHOLD:
            flags.append(
                f"possible bottleneck: response time drifted x{stats['drift_ratio']:.2f} over the run"
            )

        if stats["error_rate"] > 0:
            flags.append(f"errors: {stats['error_rate'] * 100:.1f}% of requests failed")

        report[name] = {**stats, "baseline": baseline, "flags": flags}

    return report


def print_report(report: dict) -> None:
    print("\n" + "=" * 70)
    print("PROFILING REPORT")
    print("=" * 70)

    if not report:
        print("\n(no requests were recorded)")

    for name, data in report.items():
        status = "OK" if not data["flags"] else "ATTENTION"
        print(f"\n[{status}] {name}")
        print(
            f"  samples={data['count']}  mean={data['mean_ms']:.0f}ms  "
            f"p50={data['p50_ms']:.0f}ms  p95={data['p95_ms']:.0f}ms  "
            f"stdev={data['stdev_ms']:.0f}ms  cv={data['cv']:.2f}"
        )
        for flag in data["flags"]:
            print(f"  \u26a0 {flag}")

    print("\n" + "=" * 70)


def save_report(report: dict, out_dir: str = "reports") -> Path:
    Path(out_dir).mkdir(exist_ok=True, parents=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    path = Path(out_dir) / f"profile-{timestamp}.json"
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    return path
