# Cloudflare Tunnel Setup for Raspberry Pi

This guide covers exposing your local FastAPI backend (`http://127.0.0.1:8080`) over HTTPS using `cloudflared`, without opening router ports or managing TLS certificates.

---

## Option A: Quick Tunnel (Immediate Testing)

Use a temporary `*.trycloudflare.com` HTTPS URL to verify webhooks and account linking right away.

1. Ensure the FastAPI app is running locally on port `8080`:
   ```bash
   source .venv/bin/activate
   uvicorn main:app --host 127.0.0.1 --port 8080
   ```
2. In another terminal, start a Quick Tunnel:
   ```bash
   cloudflared tunnel --url http://localhost:8080
   ```
3. Copy the printed `https://<random-subdomain>.trycloudflare.com` URL.

> **Note:** Quick Tunnel URLs change every time `cloudflared` restarts. Use Option B for 24/7 hosting.

---

## Option B: Managed Tunnel (Permanent 24/7 Hosting)

### 1. Create a Tunnel in Cloudflare Zero Trust
1. Sign in to the **Cloudflare Zero Trust Dashboard** -> **Networks** -> **Tunnels** -> **Create a tunnel**.
2. Choose **Cloudflared**, name your tunnel (e.g., `ring-pi-analyzer`), and save.
3. Under **Public Hostname**, add a route:
   - **Subdomain:** e.g., `ring` (yielding `https://ring.yourdomain.com`)
   - **Domain:** select your Cloudflare-managed domain
   - **Service Type:** `HTTP`
   - **URL:** `localhost:8080`

### 2. Install `cloudflared` as a `systemd` Service on the Pi
Copy the connector token command shown in the Cloudflare dashboard and run it on your Raspberry Pi:

```bash
sudo cloudflared service install <YOUR_CLOUDFLARE_TUNNEL_TOKEN>
sudo systemctl enable --now cloudflared
```

Verify both services are active:
```bash
sudo systemctl status ring-analyzer
sudo systemctl status cloudflared
```

---

## Amazon / Ring Developer Portal URLs

Replace `YOUR_TUNNEL_DOMAIN` with your Cloudflare domain (e.g., `ring.yourdomain.com` or your Quick Tunnel host) and paste these exact 4 URLs into the Ring / Amazon Developer Portal:

1. **App Homepage URL:**
   `https://YOUR_TUNNEL_DOMAIN/home`
2. **Token Exchange URL:**
   `https://YOUR_TUNNEL_DOMAIN/ring/token`
3. **Account Link URL:**
   `https://YOUR_TUNNEL_DOMAIN/ring/link`
4. **Webhook URL:**
   `https://YOUR_TUNNEL_DOMAIN/ring/webhook`
