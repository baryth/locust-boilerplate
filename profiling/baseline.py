from pathlib import Path

import yaml

from src.config import settings


def _load_baselines():
    path = Path(settings.BASELINE_FILE)
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


BASELINES = _load_baselines()


def get_baseline(name: str) -> dict:
    return BASELINES.get(name, {})
