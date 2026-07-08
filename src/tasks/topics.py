REQUEST_NAME = "GET /api/v1/topics"


def get_topics(user):
    """Fetch the topic list and cache it on the user for other tasks
    (e.g. subscriptions) to draw from."""
    response = user.get("/api/v1/topics", name=REQUEST_NAME)

    if response.status_code != 200:
        return

    try:
        data = response.json()
    except ValueError:
        return

    topics = data if isinstance(data, list) else data.get("topics", [])
    if topics:
        user.known_topics = topics
