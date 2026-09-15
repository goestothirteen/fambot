#!/usr/bin/env bash
# Nightly SQLite backup for Fambot. Mirrors the fpl-backup.sh pattern:
# run from a crontab that has docker group perms.
#   30 3 * * *  /home/mark/fambot/scripts/backup.sh >> /home/mark/fambot/backups/backup.log 2>&1
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${REPO_DIR}/backups"
RETENTION_DAYS=14
STAMP="$(date +%F)"
DEST="${BACKUP_DIR}/fambot-${STAMP}.db"

mkdir -p "${BACKUP_DIR}"

# .backup is safe on a live WAL database; a plain cp is not.
docker exec fambot python -c "
import sqlite3
src = sqlite3.connect('/data/fambot.db')
dst = sqlite3.connect('/data/backup-tmp.db')
with dst:
    src.backup(dst)
dst.close(); src.close()
"
docker cp fambot:/data/backup-tmp.db "${DEST}"
docker exec fambot rm -f /data/backup-tmp.db
gzip -f "${DEST}"

find "${BACKUP_DIR}" -name 'fambot-*.db.gz' -mtime "+${RETENTION_DAYS}" -delete

echo "$(date -Is) backed up to ${DEST}.gz ($(du -h "${DEST}.gz" | cut -f1))"
