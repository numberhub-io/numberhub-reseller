#!/bin/sh
# Consistent copy of the live SQLite database (safe while the bots run), kept 14 days.
#   crontab:  17 */6 * * *  /opt/numberhub-reseller/deploy/backup.sh
# The database path comes from DB_URL in .env (sqlite+aiosqlite:///./reseller.db
# -> $APP/reseller.db). Copy the backups AND SECRET_KEY to another machine: a
# backup on the same disk does not survive the disk, and without SECRET_KEY the
# stored bot tokens and API keys in a backup cannot be decrypted.
set -eu
umask 077          # backups hold customer data and encrypted secrets: owner-only
APP=${APP:-/opt/numberhub-reseller}
URL=$(grep -E '^DB_URL=' "$APP/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)
DB=${URL#sqlite+aiosqlite:///}
[ -n "$URL" ] || DB=./reseller.db
case "$DB" in
  /*) ;;
  *) DB="$APP/${DB#./}" ;;
esac
DEST=${DEST:-$(dirname "$DB")/backups}
mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sqlite3 "$DB" ".backup '$DEST/reseller-$STAMP.db'"
gzip "$DEST/reseller-$STAMP.db"
find "$DEST" -name 'reseller-*.db.gz' -mtime +14 -delete
