"""Ring Partner API & OAuth client helpers (signature verification, linking, media download)."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
from typing import Any

import httpx

from config import Settings

logger = logging.getLogger(__name__)


def verify_webhook_signature(
    raw_body: bytes,
    signature_header: str | None,
    hmac_key: str,
) -> bool:
    """Verify Ring's X-Signature header (`sha256=<hex>`) in constant time."""
    if not signature_header or not hmac_key:
        return False

    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False

    provided_hex = signature_header[len(prefix):].strip().lower()
    if not provided_hex:
        return False

    expected_hex = hmac.new(
        hmac_key.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest().lower()

    return hmac.compare_digest(expected_hex, provided_hex)


def compute_link_nonce(
    time_value: str,
    account_id: str | None,
    hmac_key: str,
) -> str:
    """Compute URL-safe Base64 (no '=' padding) HMAC-SHA256 nonce for Ring account linking."""
    message = f"{time_value}:{account_id}" if account_id else time_value
    digest = hmac.new(
        hmac_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_link_nonce(
    time_value: str,
    nonce: str,
    account_id: str | None,
    hmac_key: str,
) -> bool:
    """Verify incoming Ring link nonce using constant-time comparison."""
    if not time_value or not nonce or not hmac_key:
        return False
    expected_nonce = compute_link_nonce(time_value, account_id, hmac_key)
    return hmac.compare_digest(expected_nonce, nonce)


def _api_headers(access_token: str, settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": settings.user_agent,
        "Accept": "application/json",
    }


async def exchange_authorization_code(
    code: str,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exchange a Ring authorization code for access and refresh tokens."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.ring_client_id,
        "client_secret": settings.ring_client_secret,
    }

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=20.0)
    try:
        response = await http_client.post(
            settings.ring_oauth_url,
            data=payload,
            headers={"User-Agent": settings.user_agent},
        )
        response.raise_for_status()
        return response.json()
    finally:
        if owns_client:
            await http_client.aclose()


async def refresh_access_token(
    refresh_token: str,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Refresh an expired Ring OAuth access token using the stored refresh token."""
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.ring_client_id,
        "client_secret": settings.ring_client_secret,
    }

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=20.0)
    try:
        response = await http_client.post(
            settings.ring_oauth_url,
            data=payload,
            headers={"User-Agent": settings.user_agent},
        )
        response.raise_for_status()
        return response.json()
    finally:
        if owns_client:
            await http_client.aclose()


def _extract_account_id_from_payload(data: dict[str, Any]) -> str | None:
    if not isinstance(data, dict):
        return None
    if "account_id" in data and data["account_id"]:
        return str(data["account_id"])
    if "id" in data and data["id"]:
        return str(data["id"])
    nested = data.get("data")
    if isinstance(nested, dict):
        attrs = nested.get("attributes") or {}
        if isinstance(attrs, dict) and attrs.get("account_id"):
            return str(attrs["account_id"])
        if nested.get("id"):
            return str(nested["id"])
    return None


async def fetch_ring_account_id(
    access_token: str,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Fetch the Ring user's account ID from /v1/users/me."""
    url = f"{settings.ring_api_base_url}/v1/users/me"
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=15.0)
    try:
        response = await http_client.get(url, headers=_api_headers(access_token, settings))
        response.raise_for_status()
        return _extract_account_id_from_payload(response.json())
    finally:
        if owns_client:
            await http_client.aclose()


async def finalize_app_integration(
    access_token: str,
    account_identifier: str | None,
    nonce: str,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Finalize one-way Ring account linking via POST then PATCH /v1/accounts/me/app-integrations."""
    url = f"{settings.ring_api_base_url}/v1/accounts/me/app-integrations"
    headers = {
        **_api_headers(access_token, settings),
        "Content-Type": "application/json",
    }

    post_body: dict[str, Any] = {"nonce": nonce}
    if account_identifier:
        post_body["account_identifier"] = account_identifier

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=20.0)
    try:
        post_resp = await http_client.post(url, json=post_body, headers=headers)
        post_resp.raise_for_status()

        patch_resp = await http_client.patch(
            url,
            json={"status": "completed"},
            headers=headers,
        )
        patch_resp.raise_for_status()
    finally:
        if owns_client:
            await http_client.aclose()


def _sanitize_filename_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "unknown"


def _resolve_clip_window(
    event_timestamp: str | None,
    start_time: str | None,
    end_time: str | None,
) -> tuple[str, str]:
    """Build an ISO-8601 UTC start/end window around a Ring event timestamp."""
    if start_time and end_time:
        return start_time, end_time

    anchor: datetime
    if event_timestamp:
        try:
            normalized = event_timestamp.replace("Z", "+00:00")
            anchor = datetime.fromisoformat(normalized)
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                epoch = float(event_timestamp)
                if epoch > 1e12:
                    epoch /= 1000.0
                anchor = datetime.fromtimestamp(epoch, tz=timezone.utc)
            except ValueError:
                anchor = datetime.now(timezone.utc)
    else:
        anchor = datetime.now(timezone.utc)

    start_dt = anchor - timedelta(seconds=5)
    end_dt = anchor + timedelta(seconds=30)
    return (
        start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


async def download_media_clip(
    access_token: str,
    device_id: str,
    event_id: str,
    dest_dir: Path,
    settings: Settings,
    event_timestamp: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    clip_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> Path:
    """Download an MP4 clip from Ring Media Clips API and atomically write it to dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts_label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = (
        f"{ts_label}_{_sanitize_filename_part(device_id)}_"
        f"{_sanitize_filename_part(event_id)}.mp4"
    )
    final_path = dest_dir / filename
    temp_path = dest_dir / f".{filename}.part"

    headers = _api_headers(access_token, settings)
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=60.0)
    try:
        if clip_url:
            request_ctx = http_client.stream("GET", clip_url, headers=headers)
        else:
            win_start, win_end = _resolve_clip_window(event_timestamp, start_time, end_time)
            url = f"{settings.ring_api_base_url}/v1/devices/{device_id}/media/video/download"
            request_ctx = http_client.stream(
                "POST",
                url,
                json={"start_time": win_start, "end_time": win_end},
                headers={**headers, "Content-Type": "application/json", "Accept": "video/mp4"},
            )

        async with request_ctx as response:
            response.raise_for_status()
            with open(temp_path, "wb") as fh:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)

        os.replace(temp_path, final_path)
        return final_path
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            await http_client.aclose()
