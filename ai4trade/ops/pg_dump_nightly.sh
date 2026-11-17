#!/usr/bin/env bash
# Nightly logical backup of the launchkit Postgres (database "postgres": schemas ai4trade, barak, ...).
# Installed on the VPS as /root/ops/pg_dump_nightly.sh, run by root's crontab under flock (06:15 server local time, Europe/Stockholm = 05:15 UTC in winter).
# Writes /root/backups/pg-nightly/<UTC stamp>/{postgres.dump,toc.txt,SHA256SUMS}; keeps 14 days.
# Each dump is checked by listing its table of contents (pg_restore --list); a failed dump or an empty TOC
# leaves a FAILED marker and exits non-zero (cron mails/logs it). Restore test: see the runbook.
set -euo pipefail

ROOT=/root/backups/pg-nightly
KEEP_DAYS=14
TS=$(date -u +%Y%m%dT%H%M%SZ)
D="$ROOT/$TS"

install -d -m 700 "$ROOT" "$D"
if ! docker exec postgres pg_dump -U postgres -d postgres -Fc > "$D/postgres.dump"; then
  touch "$D/FAILED"; echo "pg_dump failed" >&2; exit 1
fi
docker exec -i postgres pg_restore --list < "$D/postgres.dump" > "$D/toc.txt"
if [ "$(wc -l < "$D/toc.txt")" -lt 100 ]; then
  touch "$D/FAILED"; echo "dump TOC too short" >&2; exit 1
fi
( cd "$D" && sha256sum postgres.dump toc.txt > SHA256SUMS )
echo "ok $D $(du -sh "$D" | cut -f1)"

# retention: only dated directories directly under $ROOT, older than KEEP_DAYS
find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -mtime +"$KEEP_DAYS" -exec rm -r -- {} +
