"""
Locust load test for the Topics/Subscriptions API.

At startup, each simulated user (VU) grabs the topic list once -- topics
don't change, so there's no reason to keep asking for them.

Then each VU loops over these tasks at random (numbers are the weights):
  - POST   /subscriptions/subscribe        -> create a subscription (starts "Paused")
  - PUT    /subscriptions/{id}/resume      -> resume one of its own Paused subs
  - PUT    /subscriptions/{id}/pause       -> pause one of its own Active subs
  - GET    /subscriptions                  -> list its subs and check the states match
  - DELETE /subscriptions/{id}/unsubscribe -> unsubscribe one of its own subs

Every VU only ever touches subscriptions it created, and remembers what
state each one should be in. Two things fall out of that:
  - VUs never step on each other's data, so no false failures from sharing.
  - The GET task compares what the API says against what the VU expects and
    fails the request if they disagree -- so this test catches wrong state,
    not just slow responses.

--- Fill in the CONFIG values below before running ---
"""

import csv
import itertools
import random
from threading import Lock

from locust import HttpUser, task, constant

# ---- CONFIG: fill these in to match your real API ----
AUTH_URL = "REPLACE_WITH_AUTH_URI"  # the login / token endpoint

CREDENTIALS_FILE = "credentials.csv"  # CSV with a header row: client_id,client_secret
AUTH_SCOPE = "123"                    # same scope for every account
AUTH_GRANT_TYPE = "client_credentials"  # login type, sent as form data (not JSON)


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


# Loaded once when the file is imported. VUs take turns pulling the next
# pair from this list, so they log in as different accounts instead of all
# hammering the same one.
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


class SubscriptionUser(HttpUser):
    wait_time = constant(11)  # fixed think-time between tasks, per VU

    # Shared by all VUs in this process. The first VU to need topics fetches
    # them; everyone else reuses that result instead of asking the API again.
    # (Running distributed? Each worker process fetches its own copy once --
    # still one call per worker, not one per VU.)
    _topics_cache = None
    _topics_lock = Lock()

    def on_start(self):
        self.headers = {}
        self.subscriptions = {}     # this VU's own subs: {subscription_id: status we expect}
        self.client_id, self.client_secret = get_next_credentials()
        self.authenticate()
        self.topics = self._get_shared_topics()

    def authenticate(self):
        """Log in once at startup and save the bearer token for later calls.

        Uses this VU's own client_id/client_secret (sent as form data, not
        JSON). The token comes back in the response's access_token field."""
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
            if resp.status_code != 200:
                resp.failure(f"Unexpected status {resp.status_code}")
                return
            try:
                data = resp.json()
            except ValueError:
                resp.failure("Invalid JSON response")
                return
            token = data.get("access_token")
            if not token:
                resp.failure("No access_token in auth response")
                return
            self.headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            resp.success()

    def _ids_with_status(self, status):
        return [sid for sid, st in self.subscriptions.items() if st == status]

    def _get_shared_topics(self):
        """Return the topic list, fetching it from the API only the first
        time. It's not a @task on purpose -- topics don't change, so asking
        for them over and over during the test would just add noise."""
        cls = type(self)
        if cls._topics_cache:
            return cls._topics_cache

        with cls._topics_lock:
            # Someone else may have finished the fetch while we waited for
            # the lock -- check again before doing it ourselves.
            if cls._topics_cache:
                return cls._topics_cache
            # Only cache a real, non-empty result. If the fetch fails we
            # return [] without caching, so the next VU tries again instead
            # of everyone being stuck with one bad first result.
            fetched = self._fetch_topics_from_api()
            if fetched:
                cls._topics_cache = fetched
            return fetched

    def _fetch_topics_from_api(self):
        with self.client.get(
            TOPICS_ENDPOINT, headers=self.headers,
            name="/topics [GET] (startup)", catch_response=True
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Unexpected status {resp.status_code}")
                return []
            try:
                data = resp.json()
            except ValueError:
                resp.failure("Invalid JSON response")
                return []
            topics = data.get("topics", []) if isinstance(data, dict) else data
            # Each topic looks like {"name": ..., "description": ...}. There's
            # no id -- "name" is what we use to create a subscription.
            fetched = [t["name"] for t in topics if "name" in t]
            resp.success()
            return fetched

    @task(4)
    def create_subscription(self):
        if not self.topics:
            return  # can't subscribe to anything without topics

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

                # new subscriptions always start out Paused
                self.subscriptions[sub_id] = STATUS_PAUSED
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}")

    @staticmethod
    def _extract_subscription_id(data):
        """Pull the subscription id out of a subscribe response.

        The body looks like {"subscriptionId": "...", "queue": "..."}.
        We only trust "subscriptionId". "queue" is a different value, not
        another name for the id, so we never fall back to it.
        """
        if not isinstance(data, dict):
            return None
        return data.get("subscriptionId")

    def _change_state(self, sub_id, path, new_status, name):
        """Resume or pause one subscription. On success, remember the new
        status locally. The resume and pause tasks both use this -- they only
        differ in which path, status, and report name they pass in."""
        with self.client.put(
            path.format(id=sub_id), headers=self.headers,
            name=name, catch_response=True
        ) as resp:
            if resp.status_code in (200, 204):
                self.subscriptions[sub_id] = new_status
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}")

    @task(3)
    def activate_subscription(self):
        candidates = self._ids_with_status(STATUS_PAUSED)
        if not candidates:
            return  # nothing paused to resume right now
        self._change_state(
            random.choice(candidates), ACTIVATE_PATH,
            STATUS_ACTIVE, "/subscriptions/:id/resume [PUT]")

    @task(3)
    def pause_subscription(self):
        candidates = self._ids_with_status(STATUS_ACTIVE)
        if not candidates:
            return  # nothing active to pause right now
        self._change_state(
            random.choice(candidates), PAUSE_PATH,
            STATUS_PAUSED, "/subscriptions/:id/pause [PUT]")

    @task(1)
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

            # Compare what the API reports against what this VU expects.
            # Each item looks like {"id": ..., "state": "Active"/"Paused", ...}.
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