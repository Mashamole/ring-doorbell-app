"""Application configuration loaded from environment variables and .env."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the Ring Doorbell Analyzer service."""

    ring_client_id: str
    ring_client_secret: str
    ring_hmac_key: str
    cloud_storage_dir: Path
    port: int
    database_path: Path
    ring_oauth_url: str
    ring_api_base_url: str
    unclaimed_token_ttl_seconds: int
    user_agent: str


def get_settings() -> Settings:
    """Load current settings from environment variables."""
    return Settings(
        ring_client_id=os.getenv("RING_CLIENT_ID", ""),
        ring_client_secret=os.getenv("RING_CLIENT_SECRET", ""),
        ring_hmac_key=os.getenv("RING_HMAC_KEY", ""),
        cloud_storage_dir=Path(os.getenv("CLOUD_STORAGE_DIR", "./clips")),
        port=int(os.getenv("PORT", "8080")),
        database_path=Path(os.getenv("DATABASE_PATH", "./ring_analyzer.db")),
        ring_oauth_url=os.getenv("RING_OAUTH_URL", "https://oauth.ring.com/oauth/token"),
        ring_api_base_url=os.getenv("RING_API_BASE_URL", "https://api.amazonvision.com").rstrip("/"),
        unclaimed_token_ttl_seconds=int(os.getenv("UNCLAIMED_TOKEN_TTL_SECONDS", "900")),
        user_agent=os.getenv("USER_AGENT", "RingDoorbellAnalyzer/1.0"),
    )
