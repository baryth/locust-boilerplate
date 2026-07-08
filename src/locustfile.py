from locust import HttpUser, between, task

from src.auth import TokenManager
from src.config import settings
from src.tasks import subscriptions, topics

# Registers the profiling event listeners (request capture + end-of-run
# report). Imported for its side effect only.
import profiling.listeners  # noqa: F401,E402


class ApiUser(HttpUser):
    host = settings.API_HOST

    # Gentle pacing on purpose: this suite is for profiling steady-state
    # behaviour with a handful of users, not for maximizing throughput.
    wait_time = between(1, 3)

    def on_start(self):
        self.known_topics = []
        topics.get_topics(self)  # warm the topic cache before tasks run

    # -- thin request helpers: inject a fresh bearer token every call --

    def _auth_headers(self):
        return {"Authorization": f"Bearer {TokenManager.get_token()}"}

    def get(self, path, **kwargs):
        headers = kwargs.pop("headers", {}) or {}
        headers.update(self._auth_headers())
        return self.client.get(path, headers=headers, **kwargs)

    def post(self, path, **kwargs):
        headers = kwargs.pop("headers", {}) or {}
        headers.update(self._auth_headers())
        return self.client.post(path, headers=headers, **kwargs)

    # -- tasks: add new endpoints here, one line each --

    @task(3)
    def get_topics(self):
        topics.get_topics(self)

    @task(1)
    def subscribe(self):
        subscriptions.subscribe(self)
