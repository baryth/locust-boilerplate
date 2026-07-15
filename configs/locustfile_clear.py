"""
Locust load test for the Topics/Subscriptions API.

On VU startup (once, not part of the load loop):
  GET /topics -> fetch topic list (static reference data, no need to re-poll)

Repeated load tasks per virtual user (VU), weighted/randomized by Locust:
  - POST   /subscriptions/subscribe        -> create subscription from random topics (starts "Paused")
  - PUT    /subscriptions/{id}/resume      -> resume/activate a random PAUSED subscription owned by this VU
  - PUT    /subscriptions/{id}/pause       -> pause a random ACTIVE subscription owned by this VU
  - GET    /subscriptions                  -> fetch current subscriptions and verify each one's
                                               state matches what this VU expects locally
  - DELETE /subscriptions/{id}/unsubscribe -> unsubscribe a random subscription owned by this VU

Each VU keeps its own local pool of subscription IDs + expected state, so:
  - VUs never touch each other's data (no cross-VU write contention on one account)
  - resume is only ever called on subscriptions this VU believes are "Paused"
  - pause is only ever called on subscriptions this VU believes are "Active"
  - the GET /subscriptions step cross-checks the API's reported state against
    what this VU expects, and fails the request if they disagree (catches
    stale/incorrect state -- a real correctness signal, not just latency)

--- EDIT THESE BEFORE RUNNING ---
"""

import csv
import itertools
import logging
import random
from threading import Lock

import gevent
from locust import HttpUser, task, between, events

# ---- CONFIG: edit to match your real API ----
AUTH_URL = "REPLACE_WITH_AUTH_URI"  # paste the token endpoint URI here

CREDENTIALS_FILE = "credentials.csv"  # CSV with header: client_id,client_secret
AUTH_SCOPE = "123"                    # scope is shared across all credential pairs

# Client credentials grant -- sent as form-data (not JSON)
AUTH_GRANT_TYPE = "client_credentials"

# Names of the one-time-per-VU startup requests, excluded from the
# --smoke-iterations count below since they aren't part of the repeated
# scenario mix (create/activate/pause/get/delete).
STARTUP_REQUEST_NAMES = {"/auth [GET] (startup)", "/topics [GET] (startup)"}


@events.init_command_line_parser.add_listener
def add_smoke_test_args(parser):
    parser.add_argument(
        "--smoke-iterations",
        type=int,
        default=None,
        env_var="LOCUST_SMOKE_ITERATIONS",
        help=(
            "Stop the whole swarm once this many scenario requests "
            "(create/activate/pause/get/delete -- startup auth/topics calls "
            "don't count) have completed. Intended for Phase 1 smoke runs. "
            "Leave unset for load/stress phases, where --run-time controls "
            "duration instead."
        ),
    )


def load_credentials(path):
    """Load (client_id, client_secret) pairs from a CSV file with a header
    row: client_id,client_secret"""
    creds = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            creds.append((row["client_id"], row["client_secret"]))
    if not creds:
        raise ValueError(f"No credentials found in {path}")
    return creds


# Loaded once per Locust worker process at import time. Each VU that starts
# up pulls the next pair from this pool (round-robin), so concurrent VUs
# authenticate as different accounts instead of all sharing one identity.
CREDENTIAL_POOL = load_credentials(CREDENTIALS_FILE)
_credential_cycle = itertools.cycle(CREDENTIAL_POOL)
_credential_lock = Lock()


def get_next_credentials():
    with _credential_lock:
        return next(_credential_cycle)

TOPICS_ENDPOINT = "/topics"
SUBSCRIPTIONS_ENDPOINT = "/subscriptions"
CREATE_PATH = "/subscriptions/subscribe"           # POST
ACTIVATE_PATH = "/subscriptions/{id}/resume"       # PUT
PAUSE_PATH = "/subscriptions/{id}/pause"           # PUT
DELETE_PATH = "/subscriptions/{id}/unsubscribe"    # DELETE

# Status string values as returned by the API -- edit if yours differ
STATUS_PAUSED = "Paused"
STATUS_ACTIVE = "Active"

CONTENT_TYPE_HEADER = {"Content-Type": "application/json"}

logger = logging.getLogger(__name__)

_smoke_counter = {"count": 0}
_smoke_lock = Lock()


@events.init.add_listener
def setup_smoke_stop_condition(environment, **kwargs):
    target = environment.parsed_options.smoke_iterations
    if not target:
        return  # not a smoke run -- normal load/stress phases ignore this entirely

    logger.info("Smoke mode: will stop after %d scenario request(s)", target)

    @events.request.add_listener
    def on_request(name, **kwargs):
        if name in STARTUP_REQUEST_NAMES:
            return
        with _smoke_lock:
            _smoke_counter["count"] += 1
            count = _smoke_counter["count"]
        if count == target:
            logger.info("Smoke target of %d scenario request(s) reached -- stopping", target)
            # Spawn this in its own greenlet rather than calling it inline.
            # environment.runner.quit() calls self.greenlet.kill(block=True),
            # and this callback is itself running on one of the runner's own
            # greenlets (triggered synchronously from the request event) --
            # calling quit() directly from there can hang shutdown instead
            # of completing it, which also means the --html report never
            # gets written. Decoupling it into a fresh greenlet avoids that.
            gevent.spawn(environment.runner.quit)
        elif count > target:
            # A few in-flight requests can land after the quit signal is
            # sent; that's expected and harmless, just noise to be aware of.
            pass


class SubscriptionUser(HttpUser):
    wait_time = between(1, 3)  # think-time between tasks, per VU

    # Shared across every VU in this worker process. Topics are static
    # reference data -- fetched once by whichever VU gets there first,
    # then reused by every other VU instead of re-hitting the API.
    # (In distributed mode each worker process still fetches its own copy
    # once, since Python memory isn't shared across processes -- that's
    # still one call per worker instead of one call per VU.)
    _topics_cache = None
    _topics_lock = Lock()

    def on_start(self):
        self.headers = {}
        self.subscriptions = {}     # {subscription_id: expected_status}, owned by THIS VU only
        self.client_id, self.client_secret = get_next_credentials()
        self.authenticate()
        self.topics = self._get_shared_topics()

    def authenticate(self):
        """Get a bearer token via the client-credentials grant, once per VU
        at startup, using this VU's own client_id/client_secret pulled from
        the credentials pool. Sent as form-data, and the token is read from
        the JSON response's access_token field."""
        payload = {
            "grant_type": AUTH_GRANT_TYPE,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": AUTH_SCOPE,
        }
        with self.client.get(
            AUTH_URL, data=payload,
            name="/auth [GET] (startup)", catch_response=True
        ) as resp:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    token = data.get("access_token")
                    if token:
                        self.headers = {
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        }
                        resp.success()
                    else:
                        resp.failure("No access_token in auth response")
                except ValueError:
                    resp.failure("Invalid JSON response")
            else:
                resp.failure(f"Unexpected status {resp.status_code}")

    def _ids_with_status(self, status):
        return [sid for sid, st in self.subscriptions.items() if st == status]

    def _get_shared_topics(self):
        """Return the cached topic list, fetching it over HTTP only if no
        VU in this worker process has fetched it yet. Not a repeated
        @task -- topics are static reference data and re-fetching them
        during the steady-state load adds no useful signal, just noise."""
        cls = type(self)
        if cls._topics_cache is not None:
            return cls._topics_cache

        with cls._topics_lock:
            # Re-check after acquiring the lock -- another VU may have
            # finished the fetch while we were waiting on it.
            if cls._topics_cache is None:
                cls._topics_cache = self._fetch_topics_from_api()
        return cls._topics_cache

    def _fetch_topics_from_api(self):
        with self.client.get(
            TOPICS_ENDPOINT, headers=self.headers,
            name="/topics [GET] (startup)", catch_response=True
        ) as resp:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    topics = data.get("topics", []) if isinstance(data, dict) else data
                    # topics come back as {"name": ..., "description": ...} -- no id field,
                    # "name" is the identifier used to create a subscription
                    fetched = [t["name"] for t in topics if "name" in t]
                    resp.success()
                    return fetched
                except ValueError:
                    resp.failure("Invalid JSON response")
                    return []
            else:
                resp.failure(f"Unexpected status {resp.status_code}")
                return []

    @task(2)
    def create_subscription(self):
        if not self.topics:
            return  # need topics first

        sample_size = random.randint(1, min(3, len(self.topics)))
        chosen = random.sample(self.topics, sample_size)
        payload = {"topics": chosen}

        with self.client.post(
            CREATE_PATH, json=payload, headers=self.headers,
            name="/subscriptions/subscribe [POST]", catch_response=True
        ) as resp:
            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                except ValueError:
                    resp.failure("Invalid JSON response")
                    return

                sub_id = self._extract_subscription_id(data)
                if sub_id is None:
                    resp.failure("Response body had no subscriptionId")
                    return

                # newly created subscriptions start out "paused"
                self.subscriptions[sub_id] = STATUS_PAUSED
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}")

    @staticmethod
    def _extract_subscription_id(data):
        """Extract the id from a POST /subscriptions/subscribe response.

        Confirmed real shape (flat body):
            {"subscriptionId": "...", "queue": "..."}

        "subscriptionId" is the only source of truth here -- "queue" is a
        separate, distinct value on the real API (not a reliable stand-in
        for the id) and is intentionally not used as a fallback.
        """
        if not isinstance(data, dict):
            return None
        return data.get("subscriptionId")

    @task(2)
    def activate_subscription(self):
        candidates = self._ids_with_status(STATUS_PAUSED)
        if not candidates:
            return  # nothing eligible to activate right now
        sub_id = random.choice(candidates)
        url = ACTIVATE_PATH.format(id=sub_id)
        with self.client.put(
            url, headers=self.headers,
            name="/subscriptions/:id/resume [PUT]", catch_response=True
        ) as resp:
            if resp.status_code in (200, 204):
                self.subscriptions[sub_id] = STATUS_ACTIVE
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}")

    @task(2)
    def pause_subscription(self):
        candidates = self._ids_with_status(STATUS_ACTIVE)
        if not candidates:
            return  # nothing eligible to pause right now
        sub_id = random.choice(candidates)
        url = PAUSE_PATH.format(id=sub_id)
        with self.client.put(
            url, headers=self.headers,
            name="/subscriptions/:id/pause [PUT]", catch_response=True
        ) as resp:
            if resp.status_code in (200, 204):
                self.subscriptions[sub_id] = STATUS_PAUSED
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}")

    @task(2)
    def get_subscriptions(self):
        with self.client.get(
            SUBSCRIPTIONS_ENDPOINT, headers=self.headers,
            name="/subscriptions [GET]", catch_response=True
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Unexpected status {resp.status_code}")
                return
            try:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("subscriptions", [])
            except ValueError:
                resp.failure("Invalid JSON response")
                return

            # Cross-check API-reported state against what this VU expects
            # Real shape: {"id": ..., "queue": ..., "state": "Active"/"Paused",
            #              "topics": [...], "updatedAtUtc": ...}
            mismatches = []
            for item in items:
                sid = item.get("id")
                api_state = item.get("state")
                if sid is None:
                    continue
                expected = self.subscriptions.get(sid)
                if expected is not None and api_state != expected:
                    mismatches.append(f"{sid}: expected={expected} got={api_state}")

            if mismatches:
                resp.failure(f"Status mismatch: {'; '.join(mismatches)}")
            else:
                resp.success()

    @task(1)
    def delete_subscription(self):
        if not self.subscriptions:
            return
        sub_id = random.choice(list(self.subscriptions.keys()))
        url = DELETE_PATH.format(id=sub_id)
        with self.client.delete(
            url, headers=self.headers,
            name="/subscriptions/:id/unsubscribe [DELETE]", catch_response=True
        ) as resp:
            if resp.status_code in (200, 204):
                del self.subscriptions[sub_id]
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}")
