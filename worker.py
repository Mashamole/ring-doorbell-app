"""Background worker pipeline for Ring webhook ingestion, clip download, and analysis."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time
from typing import Any
import uuid

from config import Settings, get_settings
import db
import ring_client

logger = logging.getLogger(__name__)

# Prevents concurrent webhook workers from racing on the same refresh_token.
_token_refresh_lock = asyncio.Lock()

_VIDEO_EVENT_TYPES = {
    "motion",
    "motion_detected",
    "ding",
    "button_press",
    "doorbell",
    "ring",
    "video_available",
    "clip_available",
    "media_available",
}


def analyze_video(filepath: str) -> None:
    """Analyze a downloaded MP4 clip.

    TODO: Plug in custom video analysis logic here (e.g., object/person detection,
    VLM/Gemini frame analysis, alerting, or metadata extraction).
    """
    clip = Path(filepath)
    size_bytes = clip.stat().st_size if clip.exists() else 0
    logger.info("TODO analyze_video stub invoked for %s (%d bytes)", filepath, size_bytes)


def extract_event_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize Ring Partner API webhook payloads (JSON:API or flat JSON)."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}

    event_id = (
        payload.get("event_id")
        or payload.get("id")
        or data.get("id")
        or attrs.get("event_id")
        or str(uuid.uuid4())
    )

    event_type = (
        payload.get("event_type")
        or payload.get("type")
        or attrs.get("event_type")
        or attrs.get("kind")
        or data.get("type")
        or "unknown"
    )

    device_id = (
        payload.get("device_id")
        or attrs.get("device_id")
        or attrs.get("source_id")
    )
    if not device_id and isinstance(data.get("relationships"), dict):
        device_rel = data["relationships"].get("device", {})
        if isinstance(device_rel, dict) and isinstance(device_rel.get("data"), dict):
            device_id = device_rel["data"].get("id")

    timestamp = (
        payload.get("timestamp")
        or payload.get("created_at")
        or attrs.get("timestamp")
        or attrs.get("created_at")
        or attrs.get("occurred_at")
    )

    return {
        "event_id": str(event_id),
        "event_type": str(event_type).lower(),
        "device_id": str(device_id) if device_id else None,
        "timestamp": str(timestamp) if timestamp is not None else None,
        "start_time": payload.get("start_time") or attrs.get("start_time"),
        "end_time": payload.get("end_time") or attrs.get("end_time"),
        "clip_url": payload.get("clip_url") or attrs.get("clip_url"),
    }


def _is_media_event(event_type: str, metadata: dict[str, Any]) -> bool:
    if metadata.get("clip_url"):
        return True
    if not metadata.get("device_id"):
        return False
    if event_type in _VIDEO_EVENT_TYPES:
        return True
    return any(keyword in event_type for keyword in ("motion", "ding", "doorbell", "video", "clip"))


async def ensure_valid_access_token(
    settings: Settings,
    refresh_buffer_seconds: int = 60,
) -> str:
    """Return a valid Ring access token, refreshing it automatically if expired."""
    account = db.get_active_account(settings.database_path)
    if account is None:
        raise RuntimeError("No Ring account is currently linked.")

    if time.time() < float(account["expires_at"]) - refresh_buffer_seconds:
        return str(account["access_token"])

    async with _token_refresh_lock:
        # Re-read inside the lock in case another task already refreshed it.
        account = db.get_active_account(settings.database_path)
        if account is None:
            raise RuntimeError("No Ring account is currently linked.")
        if time.time() < float(account["expires_at"]) - refresh_buffer_seconds:
            return str(account["access_token"])

        logger.info("Ring access token expired or expiring soon; refreshing token.")
        token_data = await ring_client.refresh_access_token(
            refresh_token=str(account["refresh_token"]),
            settings=settings,
        )
        new_access_token = token_data["access_token"]
        new_refresh_token = token_data.get("refresh_token") or str(account["refresh_token"])
        expires_in = int(token_data.get("expires_in", 3600))

        db.update_account_tokens(
            db_path=settings.database_path,
            account_row_id=int(account["id"]),
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            expires_in=expires_in,
        )
        return str(new_access_token)


async def process_webhook_event(
    payload: dict[str, Any],
    settings: Settings | None = None,
) -> None:
    """Download the Ring MP4 clip for a webhook event and run video analysis."""
    cfg = settings or get_settings()
    metadata = extract_event_metadata(payload)
    event_id = metadata["event_id"]
    event_type = metadata["event_type"]
    device_id = metadata["device_id"]

    if not _is_media_event(event_type, metadata):
        logger.info("Skipping non-media Ring event %s (type=%s)", event_id, event_type)
        db.update_event_status(cfg.database_path, event_id, status="skipped")
        return

    try:
        access_token = await ensure_valid_access_token(cfg)
        clip_path = await ring_client.download_media_clip(
            access_token=access_token,
            device_id=device_id or "unknown-device",
            event_id=event_id,
            dest_dir=cfg.cloud_storage_dir,
            settings=cfg,
            event_timestamp=metadata["timestamp"],
            start_time=metadata["start_time"],
            end_time=metadata["end_time"],
            clip_url=metadata["clip_url"],
        )
        db.update_event_status(
            cfg.database_path,
            event_id,
            status="downloaded",
            clip_path=str(clip_path),
        )

        # Run stubbed analysis off the event loop so blocking ML/FFmpeg logic won't stall FastAPI.
        await asyncio.to_thread(analyze_video, str(clip_path))
        db.update_event_status(
            cfg.database_path,
            event_id,
            status="analyzed",
            clip_path=str(clip_path),
        )
        logger.info("Completed processing for event %s -> %s", event_id, clip_path)
    except Exception as exc:
        logger.exception("Failed processing Ring webhook event %s", event_id)
        db.update_event_status(
            cfg.database_path,
            event_id,
            status="failed",
            error_message=str(exc),
        )
