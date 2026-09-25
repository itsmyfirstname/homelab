from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    ha_url: str
    ha_token: str
    sync_interval: float
    db_path: Path


def load_settings() -> Settings | None:
    ha_url = os.environ.get("HA_URL", "").strip()
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    if not ha_url or not ha_token:
        return None
    try:
        sync_interval = float(os.environ.get("SYNC_INTERVAL", "5"))
    except ValueError:
        sync_interval = 5.0
    db_path = Path(os.environ.get("DB_PATH", str(PROJECT_ROOT / ".data" / "home_assistant.db")))
    return Settings(
        ha_url=ha_url.rstrip("/"),
        ha_token=ha_token,
        sync_interval=sync_interval,
        db_path=db_path,
    )
