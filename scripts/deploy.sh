#!/usr/bin/env bash
# PolyFarm — Hetzner Cloud deployment script
# Run this ONCE on a fresh Hetzner CX21 (Ubuntu 22.04) to set everything up.
#
# Usage:
#   1. SSH into your Hetzner server
#   2. Clone the repo: git clone https://github.com/YOUR_USER/polyfarm /opt/polyfarm
#   3. cd /opt/polyfarm
#   4. cp .env.example .env && nano .env   (fill in ANTHROPIC_API_KEY etc.)
#   5. chmod +x scripts/deploy.sh && ./scripts/deploy.sh

set -e
echo "=== PolyFarm Deploy ==="

# ── 1. Install Docker (if not present) ───────────────────────────────────────
if ! command -v docker &> /dev/null; then
    echo "[1/4] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    echo "[1/4] Docker already installed: $(docker --version)"
fi

# ── 2. Install Docker Compose plugin ─────────────────────────────────────────
if ! docker compose version &> /dev/null; then
    echo "[2/4] Installing Docker Compose plugin..."
    apt-get install -y docker-compose-plugin
else
    echo "[2/4] Docker Compose already available: $(docker compose version)"
fi

# ── 3. Build and start ────────────────────────────────────────────────────────
echo "[3/4] Building image and starting services..."
docker compose build --no-cache
docker compose run --rm db_init
docker compose up -d bots dashboard

# ── 4. Status check ───────────────────────────────────────────────────────────
echo "[4/4] Status:"
docker compose ps
echo ""
echo "=== Done. PolyFarm is running. ==="
echo ""
echo "Useful commands:"
echo "  View logs:           docker compose logs -f bots"
echo "  Stop:                docker compose down"
echo "  Restart:             docker compose restart bots"
echo "  Run paper report:    docker compose exec bots python scripts/paper_report.py"
echo "  View trade logs:     docker compose exec bots python scripts/logs.py"
echo "  Add a bot:           docker compose exec bots python scripts/add_bot.py <wallet> --name 'X' --capital 2600"
