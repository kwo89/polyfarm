#!/usr/bin/env bash
# PolyFarm backup — runs daily via cron
# Backs up: polyfarm.db, ceo_memory.md → Google Drive (PolyFarm-Backup/)
#
# Cron setup (run once on server):
#   crontab -e
#   0 3 * * * /opt/polyfarm/scripts/backup.sh >> /var/log/polyfarm_backup.log 2>&1

set -euo pipefail

TIMESTAMP=$(date -u +"%Y-%m-%d_%H-%M-UTC")
DATA_DIR="/var/lib/docker/volumes/polyfarm_polyfarm_data/_data"
BACKUP_DIR="/tmp/polyfarm_backup_${TIMESTAMP}"
REMOTE="gdrive:PolyFarm-Backup"
KEEP_DAYS=30

echo "=== PolyFarm backup ${TIMESTAMP} ==="

# ── Verify source exists ──────────────────────────────────────────────────────
if [ ! -d "$DATA_DIR" ]; then
  echo "ERROR: Data dir not found: $DATA_DIR"
  echo "Try: docker volume inspect polyfarm_polyfarm_data"
  exit 1
fi

# ── Stage files ───────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

# SQLite: safe copy using sqlite3 backup command (avoids WAL corruption)
if [ -f "${DATA_DIR}/polyfarm.db" ]; then
  sqlite3 "${DATA_DIR}/polyfarm.db" ".backup '${BACKUP_DIR}/polyfarm_${TIMESTAMP}.db'"
  echo "DB backed up: $(du -sh "${BACKUP_DIR}/polyfarm_${TIMESTAMP}.db" | cut -f1)"
else
  echo "WARN: polyfarm.db not found, skipping"
fi

# CEO memory markdown
if [ -f "${DATA_DIR}/ceo_memory.md" ]; then
  cp "${DATA_DIR}/ceo_memory.md" "${BACKUP_DIR}/ceo_memory_${TIMESTAMP}.md"
  echo "Memory backed up"
else
  echo "INFO: ceo_memory.md not found yet (no CEO conversations yet)"
fi

# ── Upload to Google Drive ────────────────────────────────────────────────────
rclone copy "$BACKUP_DIR" "${REMOTE}/${TIMESTAMP}/" --log-level INFO
echo "Uploaded to ${REMOTE}/${TIMESTAMP}/"

# ── Clean up local temp ───────────────────────────────────────────────────────
rm -rf "$BACKUP_DIR"

# ── Prune old backups on Drive (keep last N days) ─────────────────────────────
echo "Pruning backups older than ${KEEP_DAYS} days from Drive..."
rclone delete "${REMOTE}/" --min-age "${KEEP_DAYS}d" --log-level INFO || true

echo "=== Done ==="
