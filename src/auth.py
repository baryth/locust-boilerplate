import threading
import time

import requests

from src.config import settings

# Refresh this many seconds before actual expiry, to avoid a request
# racing an about-to-expire token.
_EXPIRY_SAFETY_MARGIN_SECONDS = 30


class TokenManager:
    """Fetches and caches an OAuth2 client-credentials bearer token.

    Shared by every simulated user (class-level state), so a whole test
    run performs auth against the token endpoint only as often as the
    token actually needs refreshing — not once per user.
    """

    _lock = threading.Lock()
    _token = None
    _expires_at = 0.0

    @classmethod
    def get_token(cls) -> str:
        with cls._lock:
            if cls._token is None or time.time() >= cls._expires_at - _EXPIRY_SAFETY_MARGIN_SECONDS:
                cls._fetch()
            return cls._token

    @classmethod
    def _fetch(cls) -> None:
        url = settings.TOKEN_URL.format(tenant_id=settings.TENANT_ID)

        payload = {
            "grant_type": "client_credentials",
            "client_id": settings.CLIENT_ID,
            "client_secret": settings.CLIENT_SECRET,
            "scope": settings.SCOPE,
            # Some internal/enterprise auth servers expect tenant_id as a
            # body field rather than (or in addition to) a URL segment.
            "tenant_id": settings.TENANT_ID,
        }

        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        body = response.json()

        cls._token = body["access_token"]
        cls._expires_at = time.time() + int(body.get("expires_in", 3600))
