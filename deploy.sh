#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "[1/5] installing Docker..."
if ! command -v docker >/dev/null; then
  sudo apt-get update -qq && sudo apt-get install -y -qq docker.io docker-compose-v2 > /dev/null
  sudo systemctl enable --now docker >/dev/null
fi

echo "[2/5] creating config..."
mkdir -p vol
[ -f vol/settings.json ] || \
  echo '{"download_limit_kb":0,"upload_limit_kb":0,"force_encryption":false,"listen_port":6881}' > vol/settings.json

echo "[3/5] opening firewall ports 8000 (web) + 6881 (torrent)..."
sudo ufw allow 8000/tcp >/dev/null 2>&1 || true
sudo ufw allow 6881/tcp >/dev/null 2>&1 || true
sudo ufw allow 6881/udp >/dev/null 2>&1 || true

echo "[4/5] building..."
docker compose up -d --build

echo "[5/5] done. API live at http://$(curl -s ifconfig.me):8000"