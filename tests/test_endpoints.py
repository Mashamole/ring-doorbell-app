"""End-to-end tests for Ring Doorbell Video Analyzer endpoints and background worker."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

import db
from main import app
import ring_client
import worker


TEST_CLIENT_ID = "test-ring-client-id"
TEST_CLIENT_SECRET = "test-ring-client-secret"
TEST_HMAC_KEY = "super-secret-ring-hmac-key"


@pytest.fixture()
def test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Configure isolated temporary SQLite DB and clip storage directory per test."""
    db_path = tmp_path / "test_ring.db"
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("RING_CLIENT_ID", TEST_CLIENT_ID)
    monkeypatch.setenv("RING_CLIENT_SECRET", TEST_CLIENT_SECRET)
    monkeypatch.setenv("RING_HMAC_KEY", TEST_HMAC_KEY)
    monkeypatch.setenv("CLOUD_STORAGE_DIR", str(clips_dir))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PORT", "8080")

    db.init_db(db_path)
    return {"db_path": db_path, "clips_dir": clips_dir}


def _sign_webhook(body_bytes: bytes, hmac_key: str = TEST_HMAC_KEY) -> str:
    digest = hmac.new(hmac_key.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_home_status_dashboard(test_env: dict[str, Path]) -> None:
    """GET /home reports health, link status, and processed clip counts."""
    with TestClient(app) as client:
        # Initially unlinked
        resp_html = client.get("/home")
        assert resp_html.status_code == 200
        assert "Healthy" in resp_html.text
        assert "Not Linked" in resp_html.text

        resp_json = client.get("/home?format=json")
        assert resp_json.status_code == 200
        assert resp_json.json() == {
            "status": "healthy",
            "ring_linked": False,
            "recent_clips_count": 0,
            "total_clips_count": 0,
            "pending_unclaimed_tokens": 0,
        }

        # Seed a linked account and one analyzed event
        unclaimed_id = db.save_unclaimed_token(
            db_path=test_env["db_path"],
            access_token="acc-123",
            refresh_token="ref-123",
            expires_in=3600,
            ring_account_id="acct-9999",
        )
        db.claim_token(test_env["db_path"], unclaimed_id, "acct-9999")
        db.record_event_queued(test_env["db_path"], "evt-1", "dev-1", "motion")
        db.update_event_status(test_env["db_path"], "evt-1", "analyzed", clip_path="/tmp/a.mp4")

        resp_after = client.get("/home", headers={"Accept": "application/json"})
        assert resp_after.status_code == 200
        data = resp_after.json()
        assert data["status"] == "healthy"
        assert data["ring_linked"] is True
        assert data["recent_clips_count"] == 1
        assert data["total_clips_count"] == 1


def test_token_exchange_stores_unclaimed_and_cleans_stale(
    test_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /ring/token exchanges code with Ring OAuth and purges >15m unclaimed tokens."""
    # Insert a stale unclaimed token created 20 minutes ago
    stale_time = time.time() - 1200
    db.save_unclaimed_token(
        db_path=test_env["db_path"],
        access_token="stale-access",
        refresh_token="stale-refresh",
        expires_in=3600,
        ring_account_id="stale-acct",
        now=stale_time,
    )

    captured_calls: list[dict[str, Any]] = []

    async def fake_exchange(code: str, settings: Any, client: Any = None) -> dict[str, Any]:
        captured_calls.append({"code": code, "client_id": settings.ring_client_id})
        return {
            "access_token": "fresh-access-token",
            "refresh_token": "fresh-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    async def fake_fetch_account(access_token: str, settings: Any, client: Any = None) -> str:
        assert access_token == "fresh-access-token"
        return "ring-acct-42"

    monkeypatch.setattr(ring_client, "exchange_authorization_code", fake_exchange)
    monkeypatch.setattr(ring_client, "fetch_ring_account_id", fake_fetch_account)

    with TestClient(app) as client:
        # Wrong client_id should be rejected
        bad_resp = client.post(
            "/ring/token",
            data={"code": "auth-code-1", "client_id": "wrong-client"},
        )
        assert bad_resp.status_code == 400

        resp = client.post(
            "/ring/token",
            data={"code": "auth-code-1", "client_id": TEST_CLIENT_ID},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    assert captured_calls == [{"code": "auth-code-1", "client_id": TEST_CLIENT_ID}]
    unclaimed = db.list_unclaimed_tokens(test_env["db_path"])
    assert len(unclaimed) == 1
    assert unclaimed[0]["access_token"] == "fresh-access-token"
    assert unclaimed[0]["refresh_token"] == "fresh-refresh-token"
    assert unclaimed[0]["ring_account_id"] == "ring-acct-42"


def test_account_link_verifies_nonce_and_finalizes_integration(
    test_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /ring/link verifies URL-safe Base64 nonce and calls POST + PATCH app-integrations."""
    db.save_unclaimed_token(
        db_path=test_env["db_path"],
        access_token="link-access-token",
        refresh_token="link-refresh-token",
        expires_in=3600,
        ring_account_id="ring-acct-77",
    )

    api_requests: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        api_requests.append((request.method, str(request.url), body))
        return httpx.Response(200, json={"status": body.get("status", "awaiting")})

    transport = httpx.MockTransport(handler)
    orig_async_client = httpx.AsyncClient

    def mocked_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(ring_client.httpx, "AsyncClient", mocked_async_client)

    ts = str(int(time.time()))
    valid_nonce = ring_client.compute_link_nonce(ts, "ring-acct-77", TEST_HMAC_KEY)
    assert "=" not in valid_nonce

    with TestClient(app) as client:
        bad_resp = client.get(f"/ring/link?time={ts}&nonce=tampered-nonce")
        assert bad_resp.status_code == 401

        ok_resp = client.get(f"/ring/link?time={ts}&nonce={valid_nonce}")
        assert ok_resp.status_code == 200
        assert "Ring Account Linked" in ok_resp.text

    assert len(api_requests) == 2
    assert api_requests[0][0] == "POST"
    assert api_requests[0][1].endswith("/v1/accounts/me/app-integrations")
    assert api_requests[0][2]["nonce"] == valid_nonce
    assert api_requests[1][0] == "PATCH"
    assert api_requests[1][1].endswith("/v1/accounts/me/app-integrations")
    assert api_requests[1][2] == {"status": "completed"}

    assert db.list_unclaimed_tokens(test_env["db_path"]) == []
    active = db.get_active_account(test_env["db_path"])
    assert active is not None
    assert active["ring_account_id"] == "ring-acct-77"
    assert active["integration_status"] == "completed"


def test_webhook_hmac_verification_and_background_dispatch(
    test_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /ring/webhook enforces X-Signature HMAC-SHA256 and enqueues valid events."""
    processed_payloads: list[dict[str, Any]] = []

    async def fake_process(payload: dict[str, Any], settings: Any = None) -> None:
        processed_payloads.append(payload)

    monkeypatch.setattr(worker, "process_webhook_event", fake_process)

    payload = {
        "event_id": "evt-motion-001",
        "event_type": "motion",
        "device_id": "doorbell-front",
        "timestamp": "2026-11-25T12:00:00Z",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    valid_sig = _sign_webhook(raw_body)

    with TestClient(app) as client:
        # Missing signature -> 401
        missing_resp = client.post(
            "/ring/webhook",
            content=raw_body,
            headers={"Content-Type": "application/json"},
        )
        assert missing_resp.status_code == 401

        # Invalid signature -> 401
        invalid_resp = client.post(
            "/ring/webhook",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": "sha256=0000deadbeef",
            },
        )
        assert invalid_resp.status_code == 401

        # Valid signature -> 200 and background task executes
        valid_resp = client.post(
            "/ring/webhook",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": valid_sig,
            },
        )
        assert valid_resp.status_code == 200
        assert valid_resp.json() == {"status": "accepted", "event_id": "evt-motion-001"}

        # Duplicate event_id -> still 200, not processed twice
        dup_resp = client.post(
            "/ring/webhook",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": valid_sig,
            },
        )
        assert dup_resp.status_code == 200

    assert len(processed_payloads) == 1
    assert processed_payloads[0]["event_id"] == "evt-motion-001"


def test_worker_refreshes_expired_token_downloads_clip_and_analyzes(
    test_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker.process_webhook_event refreshes expired tokens, saves MP4, and calls analyze_video."""
    # Seed an already-expired linked account
    unclaimed_id = db.save_unclaimed_token(
        db_path=test_env["db_path"],
        access_token="expired-token",
        refresh_token="valid-refresh-token",
        expires_in=-10,
        ring_account_id="acct-worker-1",
    )
    db.claim_token(test_env["db_path"], unclaimed_id, "acct-worker-1")

    analyzed_files: list[str] = []

    def fake_analyze(filepath: str) -> None:
        analyzed_files.append(filepath)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth/token" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "access_token": "refreshed-token",
                    "refresh_token": "rotated-refresh-token",
                    "expires_in": 3600,
                },
            )
        if str(request.url).endswith("/media/video/download"):
            assert request.headers["Authorization"] == "Bearer refreshed-token"
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42fake-mp4-data")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_async_client = httpx.AsyncClient

    def mocked_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(ring_client.httpx, "AsyncClient", mocked_async_client)
    monkeypatch.setattr(worker, "analyze_video", fake_analyze)

    payload = {
        "event_id": "evt-ding-77",
        "event_type": "ding",
        "device_id": "doorbell-1",
        "timestamp": "2026-11-25T12:05:00Z",
    }
    db.record_event_queued(test_env["db_path"], "evt-ding-77", "doorbell-1", "ding")

    asyncio.run(worker.process_webhook_event(payload))

    # Verify refreshed token persisted in SQLite
    account = db.get_active_account(test_env["db_path"])
    assert account is not None
    assert account["access_token"] == "refreshed-token"
    assert account["refresh_token"] == "rotated-refresh-token"

    # Verify MP4 saved in CLOUD_STORAGE_DIR and analyze_video called
    saved_files = list(test_env["clips_dir"].glob("*.mp4"))
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes().startswith(b"\x00\x00\x00\x18ftypmp42")
    assert analyzed_files == [str(saved_files[0])]
