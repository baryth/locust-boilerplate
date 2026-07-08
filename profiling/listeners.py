from locust import events

from profiling.collector import Collector
from profiling.report import build_report, print_report, save_report


@events.request.add_listener
def on_request(name, response_time, exception, **kwargs):
    Collector.record(name, response_time, success=exception is None)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    report = build_report()
    print_report(report)
    path = save_report(report)
    print(f"\nFull report saved to {path}\n")
