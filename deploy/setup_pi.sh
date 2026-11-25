#!/usr/bin/env bash
set -euo pipefail

# Setup script for Raspberry Pi OS (Debian/ARM64 or ARMhf)
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "==> Updating apt packages and installing Python & curl prerequisites..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip curl ca-certificates gnupg

echo "==> Creating Python virtual environment in ${APP_DIR}/.venv..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing cloudflared..."
if ! command -v cloudflared >/dev/null 2>&1; then
  ARCH="$(dpkg --print-architecture)"
  case "$ARCH" in
    arm64|amd64|armhf)
      DEB_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb"
      ;;
    *)
      echo "Unsupported architecture: ${ARCH}" >&2
      exit 1
      ;;
  esac
  TMP_DEB="/tmp/cloudflared.deb"
  curl -fsSL "$DEB_URL" -o "$TMP_DEB"
  sudo dpkg -i "$TMP_DEB"
  rm -f "$TMP_DEB"
else
  echo "cloudflared is already installed: $(cloudflared --version)"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from .env.example. Edit .env with your Ring credentials before starting."
fi

mkdir -p /home/pi/ring-clips

echo "==> Setup complete!"
echo "Next steps:"
echo "  1. Edit ${APP_DIR}/.env with RING_CLIENT_ID, RING_CLIENT_SECRET, RING_HMAC_KEY, and CLOUD_STORAGE_DIR."
echo "  2. Install the systemd service:"
echo "     sudo cp ${APP_DIR}/deploy/ring-analyzer.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now ring-analyzer"
echo "  3. Follow deploy/README_CLOUDFLARE.md to expose port 8080 over HTTPS."
