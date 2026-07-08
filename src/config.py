import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    API_HOST = os.getenv("API_HOST", "https://api.example.com")

    TENANT_ID = os.getenv("TENANT_ID", "")
    CLIENT_ID = os.getenv("CLIENT_ID", "")
    CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
    SCOPE = os.getenv("SCOPE", "")
    TOKEN_URL = os.getenv(
        "TOKEN_URL",
        "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
    )

    SUBSCRIBE_SAMPLE_SIZE = int(os.getenv("SUBSCRIBE_SAMPLE_SIZE", "3"))

    BASELINE_FILE = os.getenv(
        "BASELINE_FILE",
        os.path.join(os.path.dirname(__file__), "..", "profiling", "baselines.yml"),
    )


settings = Settings()
