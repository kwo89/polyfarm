#!/usr/bin/env bash
# One-time setup for the GitHub auto-deploy webhook.
# Run this on the Hetzner server as root.
#
# Usage:
#   bash /opt/polyfarm/scripts/webhook_setup.sh

set -euo pipefail

REPO_DIR="/opt/polyfarm"
ENV_FILE="${REPO_DIR}/.env"
SERVICE_FILE="/etc/systemd/system/polyfarm-webhook.service"
PYTHON=$(which python3)

echo "=== PolyFarm webhook setup ==="

# ── Generate a webhook secret if not already set ──────────────────────────────
if ! grep -q "^WEBHOOK_SECRET=" "$ENV_FILE" 2>/dev/null || grep -q "^WEBHOOK_SECRET=$" "$ENV_FILE"; then
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    if grep -q "^WEBHOOK_SECRET=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s/^WEBHOOK_SECRET=.*/WEBHOOK_SECRET=${SECRET}/" "$ENV_FILE"
    else
        echo "WEBHOOK_SECRET=${SECRET}" >> "$ENV_FILE"
    fi
    echo "Generated WEBHOOK_SECRET and saved to .env"
else
    SECRET=$(grep "^WEBHOOK_SECRET=" "$ENV_FILE" | cut -d= -f2)
    echo "Using existing WEBHOOK_SECRET from .env"
fi

# ── Create log file ───────────────────────────────────────────────────────────
touch /var/log/polyfarm_deploy.log
chmod 644 /var/log/polyfarm_deploy.log

# ── Write systemd service ─────────────────────────────────────────────────────
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=PolyFarm GitHub Auto-Deploy Webhook
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${PYTHON} ${REPO_DIR}/scripts/webhook_server.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "Systemd service written to ${SERVICE_FILE}"

# ── Enable and start ──────────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable polyfarm-webhook
systemctl restart polyfarm-webhook
sleep 2
systemctl status polyfarm-webhook --no-pager

# ── Open firewall port 9000 ───────────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    ufw allow 9000/tcp comment "PolyFarm webhook"
    echo "UFW: port 9000 opened"
fi

# ── Print GitHub setup instructions ──────────────────────────────────────────
SERVER_IP=$(curl -s https://api.ipify.org 2>/dev/null || echo "YOUR_SERVER_IP")
echo ""
echo "========================================================"
echo "  Webhook server running on port 9000"
echo ""
echo "  Now go to GitHub and add the webhook:"
echo "  https://github.com/kwo89/polyfarm/settings/hooks/new"
echo ""
echo "  Payload URL:   http://${SERVER_IP}:9000/deploy"
echo "  Content type:  application/json"
echo "  Secret:        ${SECRET}"
echo "  Events:        Just the push event"
echo "  Active:        ✓"
echo ""
echo "  Deploy log:    tail -f /var/log/polyfarm_deploy.log"
echo "========================================================"
