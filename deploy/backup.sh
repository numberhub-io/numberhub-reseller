#!/bin/sh
# Consistent copy of the live SQLite database (safe while the bots run), kept 14 days.
#   crontab:  17 */6 * * *  /opt/numberhub-reseller/deploy/backup.sh
# Keep a copy of SECRET_KEY somewhere else too: without it the stored bot tokens
# and API keys in a backup cannot be decrypted.
set -eu
APP=${APP:-/opt/numberhub-reseller}
DEST=${DEST:-$APP/backups}
mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sqlite3 "$APP/reseller.db" ".backup '$DEST/reseller-$STAMP.db'"
gzip "$DEST/reseller-$STAMP.db"
find "$DEST" -name 'reseller-*.db.gz' -mtime +14 -delete
