"""
Phase 2 -- Load test (staged step ramp).

Reuses the same SubscriptionUser scenario from locustfile.py -- this file
only adds a LoadTestShape that controls how many VUs are running over
time. Locust aggregates User classes and LoadTestShape classes across all
files passed via -f/locustfile=, so locustfile.py doesn't need to know
anything about this file, and Phase 1's smoke config is unaffected.

Timeline (with the defaults below, PEAK_USERS=30):
    0s   ->  90s : 25% ( 8 users)
   90s   -> 180s : 50% (15 users)
  180s   -> 270s : 75% (23 users)
  270s   -> 870s : 100% (30 users)   <- the requested 10 min sustained load
  870s+         : test stops automatically

Run with:
    locust --config configs/phase2_load.conf

--- EDIT PEAK_USERS / STAGE_HOLD_SECONDS / SUSTAINED_SECONDS BELOW IF NEEDED ---
"""

import math

from locust import LoadTestShape

# ---- CONFIG: edit to adjust the shape of Phase 2 ----
PEAK_USERS = 30                  # target concurrent VUs at 100%
STAGE_SPAWN_RATE = 10            # users/sec -- fast jump between plateaus
STAGE_HOLD_SECONDS = 90          # how long to hold at 25/50/75% before stepping up
SUSTAINED_SECONDS = 10 * 60      # how long to hold at 100% (the actual load phase)
STAGE_PERCENTAGES = [0.25, 0.50, 0.75, 1.00]


class Phase2StagedLoadShape(LoadTestShape):
    """Step up through STAGE_PERCENTAGES of PEAK_USERS, holding
    STAGE_HOLD_SECONDS at each intermediate step, then holding
    SUSTAINED_SECONDS at 100% before stopping the run automatically."""

    def __init__(self):
        super().__init__()
        self.stages = []
        cumulative = 0.0
        for pct in STAGE_PERCENTAGES:
            duration = SUSTAINED_SECONDS if pct == 1.0 else STAGE_HOLD_SECONDS
            cumulative += duration
            users = max(1, math.ceil(PEAK_USERS * pct))
            self.stages.append({
                "end_time": cumulative,
                "users": users,
                "spawn_rate": STAGE_SPAWN_RATE,
            })

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["end_time"]:
                return stage["users"], stage["spawn_rate"]
        return None  # past the last stage -- stop the test
