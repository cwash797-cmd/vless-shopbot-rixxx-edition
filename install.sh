#!/usr/bin/env bash
# Safe local setup: does not modify the panel, firewall, reverse proxy or packages.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v docker >/dev/null || { echo 'Install Docker and Compose first.' >&2; exit 1; }
docker compose version >/dev/null
command -v python3 >/dev/null
if [[ ! -f .env ]]; then
    umask 077
    python3 - <<'SETUP'
import os
import secrets
with open('.env', 'x') as config:
    config.write('ADMIN_PASSWORD=' + secrets.token_urlsafe(24) + '\n')
    config.write('FLASK_SECRET_KEY=' + secrets.token_hex(32) + '\n')
    config.write('COOKIE_SECURE=true\nWEBHOOK_PORT=1488\nLOG_LEVEL=INFO\n')
os.chmod('.env', 0o600)
SETUP
    echo 'Created .env (0600). Login: admin. Read ADMIN_PASSWORD locally from .env.'
fi
docker compose config --quiet
if [[ "${1:-}" == '--check' ]]; then
    echo 'Compose configuration valid; no containers started.'
    exit 0
fi
docker compose up -d --build
echo 'Bot bound to 127.0.0.1:1488. Configure an HTTPS reverse proxy separately.'
echo 'No panel, Caddy, Nginx or firewall files were changed.'
