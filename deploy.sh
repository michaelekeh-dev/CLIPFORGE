#!/usr/bin/env bash
# One-command deploy / update on a Linux server:  bash deploy.sh
# Installs Docker if needed, builds the image, starts (or restarts) everything with auto-restart on crash.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - open it and fill in APP_PASSWORD, ANTHROPIC_API_KEY and SITE_ADDRESS, then run this again."
  exit 1
fi
if ! grep -q '^APP_PASSWORD=.\+' .env; then
  echo "Set APP_PASSWORD in .env first (that is your login)."; exit 1
fi
if ! grep -q '^SITE_ADDRESS=' .env; then
  IP=$(curl -4 -s https://ifconfig.me || hostname -I | awk '{print $1}')
  echo "SITE_ADDRESS=${IP//./-}.sslip.io" >> .env
  echo "No SITE_ADDRESS in .env, using https://${IP//./-}.sslip.io (free DNS that points at this server)."
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || { echo "Docker Compose plugin missing. Install docker-compose-plugin."; exit 1; }

docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null 2>&1 || true
SITE=$(grep '^SITE_ADDRESS=' .env | cut -d= -f2)
echo
echo "CLIPFORGE is starting. Open: https://${SITE}   (first start downloads the speech model, give it a few minutes)"
echo "Logs: docker compose logs -f clipforge"
