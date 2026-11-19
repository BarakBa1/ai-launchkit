#!/usr/bin/env bash
# Nightly logical backup of the launchkit Postgres (database "postgres": schemas ai4trade, barak, ...).
# Installed on the VPS as /root/ops/pg_dump_nightly.sh, run by root's crontab under flock (06:15 server local time, Europe/Stockholm = 05:15 UTC in winter).
# Writes /root/backups/pg-nightly/<UTC stamp>/{postgres.dump,toc.txt,SHA256SUMS}; keeps 5 days of nightly dumps and the 2 newest predeploy dumps (ruling 0.3, 2026-11-19).
# Each dump is checked by listing its table of contents (pg_restore --list); a failed dump or an empty TOC
# leaves a FAILED marker and exits non-zero (cron mails/logs it). Restore test: see the runbook.
set -euo pipefail

ROOT=/root/backups/pg-nightly
KEEP_DAYS=5
KEEP_PREDEPLOY=2
BACKUPS=/root/backups
START=$(date +%s)
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

# 07-WP1: record the good dump for ops_watch (rule backup_age reads backup.dump_ok, limit 26 h). Written only after the
# TOC check and the checksums; a failed write exits non-zero, so the cron mail and the missing row both show it.
PAYLOAD="{\"at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"bytes\":$(stat -c %s "$D/postgres.dump"),\"seconds\":$(( $(date +%s) - START ))}"
docker exec postgres psql -X -q -v ON_ERROR_STOP=1 -U postgres -d postgres -c \
  "insert into ai4trade.state (key, value, updated_at, updated_by)
   values ('backup.dump_ok', '$PAYLOAD'::jsonb, now(), 'pg_dump_nightly.sh')
   on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at,
   updated_by = excluded.updated_by;" \
  >/dev/null || { echo "could not record backup.dump_ok" >&2; exit 1; }
echo "ok $D $(du -sh "$D" | cut -f1)"

# retention: only dated directories directly under $ROOT, older than KEEP_DAYS
find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -mtime +"$KEEP_DAYS" -exec rm -r -- {} +

# predeploy dumps (scripts/deploy.sh writes $BACKUPS/predeploy-<sha>-<UTC>/): keep the KEEP_PREDEPLOY newest by mtime.
# Runs only after tonight's dump succeeded (set -e above), so a rollback dump is never the last one removed.
find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d -name 'predeploy-*' -printf '%T@ %p\n' \
  | sort -rn | tail -n +"$((KEEP_PREDEPLOY + 1))" | cut -d' ' -f2- \
  | while IFS= read -r old; do rm -r -- "$old"; done
