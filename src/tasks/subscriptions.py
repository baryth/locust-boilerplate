import random

from src.config import settings

REQUEST_NAME = "POST /api/v1/subscriptions/subscribe"


def subscribe(user):
    """Subscribe to a random sample of the topics known to this user.

    Relies on `user.known_topics` having been populated by the topics
    task (this happens once in on_start, then again on every scheduled
    GET /topics call).
    """
    topics_pool = getattr(user, "known_topics", None)
    if not topics_pool:
        return  # nothing to subscribe to yet, skip this iteration

    sample_size = min(settings.SUBSCRIBE_SAMPLE_SIZE, len(topics_pool))
    payload = {"topics": random.sample(topics_pool, sample_size)}

    user.post("/api/v1/subscriptions/subscribe", json=payload, name=REQUEST_NAME)
