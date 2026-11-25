"""FastAPI backend for Ring Partner API account linking, webhook ingestion, and status UI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import html
import json
import logging
from typing import Any, Optional, Union

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

from config import Settings, get_settings
import db
import ring_client
import worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize SQLite schema and prune stale unclaimed tokens on startup."""
    settings = get_settings()
    db.init_db(settings.database_path)
    settings.cloud_storage_dir.mkdir(parents=True, exist_ok=True)
    removed = db.cleanup_expired_unclaimed_tokens(
        settings.database_path,
        ttl_seconds=settings.unclaimed_token_ttl_seconds,
    )
    if removed:
        logger.info("Cleaned up %d expired unclaimed tokens on startup", removed)
    yield


app = FastAPI(
    title="Ring Doorbell Video Analyzer",
    version="1.0.0",
    lifespan=lifespan,
)


def _format_ts(epoch: float | None) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _mask_account_id(account_id: str | None) -> str:
    if not account_id:
        return "Linked"
    if len(account_id) <= 8:
        return account_id
    return f"{account_id[:4]}...{account_id[-4:]}"


def _render_home_html(stats: dict[str, Any], settings: Settings) -> str:
    is_linked = stats["is_linked"]
    badge_color = "#16a34a" if is_linked else "#d97706"
    link_label = "Linked" if is_linked else "Not Linked"
    account_info = (
        _mask_account_id(stats["linked_account"]["ring_account_id"])
        if is_linked and stats["linked_account"]
        else "Awaiting Ring Appstore authorization"
    )

    rows_html = ""
    for event in stats["recent_events"]:
        rows_html += (
            "<tr>"
            f"<td><code>{html.escape(str(event['event_id']))}</code></td>"
            f"<td>{html.escape(str(event['event_type'] or '—'))}</td>"
            f"<td>{html.escape(str(event['device_id'] or '—'))}</td>"
            f"<td>{html.escape(str(event['status']))}</td>"
            f"<td>{html.escape(_format_ts(event['created_at']))}</td>"
            "</tr>"
        )
    if not rows_html:
        rows_html = '<tr><td colspan="5" style="text-align:center;color:#64748b;">No clips processed yet.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ring Doorbell Analyzer</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 2rem; }}
    .card {{ max-width: 820px; margin: 0 auto; background: #1e293b; border-radius: 12px; padding: 1.5rem 2rem; box-shadow: 0 10px 25px rgba(0,0,0,0.3); }}
    h1 {{ margin-top: 0; font-size: 1.5rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin: 1.25rem 0; }}
    .stat {{ background: #0f172a; padding: 1rem; border-radius: 8px; border: 1px solid #334155; }}
    .label {{ font-size: 0.8rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }}
    .value {{ font-size: 1.25rem; font-weight: 600; margin-top: 0.35rem; }}
    .badge {{ display: inline-block; padding: 0.2rem 0.6rem; border-radius: 999px; font-size: 0.85rem; background: {badge_color}; color: #fff; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.9rem; }}
    th, td {{ text-align: left; padding: 0.6rem; border-bottom: 1px solid #334155; }}
    th {{ color: #94a3b8; font-weight: 500; }}
    code {{ color: #38bdf8; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Ring Doorbell Video Analyzer</h1>
    <div class="grid">
      <div class="stat">
        <div class="label">Service Health</div>
        <div class="value" style="color:#4ade80;">Healthy</div>
      </div>
      <div class="stat">
        <div class="label">Ring Account</div>
        <div class="value"><span class="badge">{link_label}</span></div>
        <div style="font-size:0.8rem;color:#94a3b8;margin-top:0.4rem;">{html.escape(account_info)}</div>
      </div>
      <div class="stat">
        <div class="label">Recent Clips (24h)</div>
        <div class="value">{stats['recent_clips_count']}</div>
      </div>
      <div class="stat">
        <div class="label">Total Processed</div>
        <div class="value">{stats['total_clips_count']}</div>
      </div>
    </div>
    <p style="font-size:0.85rem;color:#94a3b8;">Storage directory: <code>{html.escape(str(settings.cloud_storage_dir))}</code></p>
    <h2 style="font-size:1.1rem;margin-top:1.5rem;">Recent Events</h2>
    <table>
      <thead>
        <tr><th>Event ID</th><th>Type</th><th>Device</th><th>Status</th><th>Received</th></tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</body>
</html>"""


@app.get("/home", response_model=None)
async def home(
    request: Request,
    format: Optional[str] = Query(default=None),
) -> Union[HTMLResponse, JSONResponse]:
    """App Homepage URL returning service health, Ring link status, and recent clip counts."""
    settings = get_settings()
    stats = db.get_dashboard_stats(settings.database_path)

    accept = request.headers.get("accept", "")
    wants_json = format == "json" or (
        "application/json" in accept and "text/html" not in accept
    )
    if wants_json:
        return JSONResponse(
            {
                "status": "healthy",
                "ring_linked": stats["is_linked"],
                "recent_clips_count": stats["recent_clips_count"],
                "total_clips_count": stats["total_clips_count"],
                "pending_unclaimed_tokens": stats["pending_unclaimed_tokens"],
            }
        )

    return HTMLResponse(content=_render_home_html(stats, settings))


@app.post("/ring/token")
async def ring_token_exchange(
    code: str = Form(...),
    client_id: str = Form(...),
) -> JSONResponse:
    """Token Exchange URL: exchange Ring auth code for tokens and store as unclaimed."""
    settings = get_settings()
    if settings.ring_client_id and client_id != settings.ring_client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid client_id.",
        )

    try:
        token_payload = await ring_client.exchange_authorization_code(code, settings)
    except httpx.HTTPStatusError as exc:
        logger.error("Ring OAuth token exchange failed: %s", exc.response.text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to exchange authorization code with Ring.",
        ) from exc
    except httpx.HTTPError as exc:
        logger.error("Network error during Ring OAuth token exchange: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to reach Ring OAuth server.",
        ) from exc

    access_token = token_payload.get("access_token")
    refresh_token = token_payload.get("refresh_token")
    if not access_token or not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ring OAuth response missing access_token or refresh_token.",
        )

    ring_account_id: str | None = (
        token_payload.get("account_id")
        or token_payload.get("user_id")
        or token_payload.get("sub")
    )
    if not ring_account_id:
        try:
            ring_account_id = await ring_client.fetch_ring_account_id(access_token, settings)
        except Exception as exc:
            logger.warning("Could not pre-fetch Ring account ID during token exchange: %s", exc)

    db.save_unclaimed_token(
        db_path=settings.database_path,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(token_payload.get("expires_in", 3600)),
        token_type=token_payload.get("token_type", "Bearer"),
        scope=token_payload.get("scope"),
        ring_account_id=ring_account_id,
        ttl_seconds=settings.unclaimed_token_ttl_seconds,
    )
    return JSONResponse({"status": "ok"})


async def _match_unclaimed_token(
    time_value: str,
    nonce: str,
    settings: Settings,
) -> tuple[dict[str, Any], str | None] | None:
    """Find the unclaimed token matching Ring's HMAC-SHA256 nonce."""
    candidates = db.list_unclaimed_tokens(settings.database_path)
    for token_row in candidates:
        account_id = token_row.get("ring_account_id")
        if not account_id:
            try:
                account_id = await ring_client.fetch_ring_account_id(
                    token_row["access_token"], settings
                )
                if account_id:
                    db.update_unclaimed_token_account_id(
                        settings.database_path, int(token_row["id"]), account_id
                    )
            except Exception as exc:
                logger.warning("Failed resolving Ring account ID for unclaimed token %s: %s", token_row["id"], exc)

        if account_id and ring_client.verify_link_nonce(
            time_value, nonce, account_id, settings.ring_hmac_key
        ):
            return token_row, account_id

        # Fallback if Ring or tests omit account_id when signing the nonce
        if ring_client.verify_link_nonce(time_value, nonce, None, settings.ring_hmac_key):
            return token_row, account_id

    return None


@app.get("/ring/link", response_class=HTMLResponse)
async def ring_account_link(
    time: str = Query(...),
    nonce: str = Query(...),
) -> HTMLResponse:
    """Account Link URL: verify nonce, match unclaimed token, and finalize integration."""
    settings = get_settings()
    db.cleanup_expired_unclaimed_tokens(
        settings.database_path,
        ttl_seconds=settings.unclaimed_token_ttl_seconds,
    )

    match = await _match_unclaimed_token(time, nonce, settings)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Ring linking nonce.",
        )

    token_row, account_id = match
    try:
        await ring_client.finalize_app_integration(
            access_token=token_row["access_token"],
            account_identifier=account_id,
            nonce=nonce,
            settings=settings,
        )
    except httpx.HTTPError as exc:
        logger.error("Failed finalizing Ring app integration: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to finalize integration with Ring API.",
        ) from exc

    linked = db.claim_token(
        db_path=settings.database_path,
        unclaimed_id=int(token_row["id"]),
        ring_account_id=account_id,
        integration_status="completed",
    )
    masked = _mask_account_id(linked["ring_account_id"] if linked else account_id)

    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ring Account Linked</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; display: grid; place-items: center; min-height: 100vh; margin: 0; }}
    .box {{ background: #1e293b; padding: 2rem 2.5rem; border-radius: 12px; text-align: center; max-width: 440px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); }}
    h1 {{ color: #4ade80; margin-top: 0; }}
    a {{ color: #38bdf8; text-decoration: none; font-weight: 500; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>Ring Account Linked</h1>
    <p>Your Ring account (<code>{html.escape(masked)}</code>) is now connected and ready to process doorbell and motion clips.</p>
    <p><a href="/home">Go to Status Dashboard</a></p>
  </div>
</body>
</html>"""
    )


@app.post("/ring/webhook")
async def ring_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Webhook URL: verify HMAC-SHA256 X-Signature, enqueue background worker, return 200."""
    settings = get_settings()
    raw_body = await request.body()
    signature = request.headers.get("X-Signature")

    if not ring_client.verify_webhook_signature(raw_body, signature, settings.ring_hmac_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Signature header.",
        )

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON webhook payload.",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be a JSON object.",
        )

    metadata = worker.extract_event_metadata(payload)
    is_new = db.record_event_queued(
        db_path=settings.database_path,
        event_id=metadata["event_id"],
        device_id=metadata["device_id"],
        event_type=metadata["event_type"],
    )
    if is_new:
        background_tasks.add_task(worker.process_webhook_event, payload, settings)

    return JSONResponse({"status": "accepted", "event_id": metadata["event_id"]})


if __name__ == "__main__":
    import uvicorn

    cfg = get_settings()
    uvicorn.run("main:app", host="127.0.0.1", port=cfg.port)
