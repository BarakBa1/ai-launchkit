#!/usr/bin/env bash
# Nightly logical backup of the launchkit Postgres (database "postgres": schemas ai4trade, barak, ...).
# Installed on the VPS as /root/ops/pg_dump_nightly.sh, run by root's crontab under flock (06:15 server local time, Europe/Stockholm = 05:15 UTC in winter).
# Writes /root/backups/pg-nightly/<UTC stamp>/{postgres.dump,toc.txt,SHA256SUMS}.
# Each dump is checked by listing its table of contents (pg_restore --list); a failed dump or an empty TOC
# leaves a FAILED marker and exits non-zero (cron mails/logs it). Restore test: see the runbook.
# Retention (B-002, ruling W-08), applied after tonight's dump succeeded:
#   nightly dumps: the KEEP_NIGHTLY newest good ones are kept; a FAILED one (FAILED marker) does not count
#   toward KEEP_NIGHTLY and is deleted once older than FAILED_DAYS (operator ruling, B-203);
#   deploy dumps (predeploy-*, written by scripts/deploy.sh): the KEEP_PREDEPLOY newest by mtime are kept;
#   housekeeping-* and timing-* older than HOUSEKEEPING_DAYS are deleted;
#   a dump named in $BACKUPS/KEEP (one path or name per line; # starts a comment) is never deleted.
# --list prints the retention decisions and changes nothing: no dump, no deletion.
set -euo pipefail

ROOT="${PG_NIGHTLY_ROOT:-/root/backups/pg-nightly}"
BACKUPS="${PG_BACKUPS_DIR:-/root/backups}"
KEEP_LIST="$BACKUPS/KEEP"
KEEP_NIGHTLY=7
KEEP_PREDEPLOY=3
HOUSEKEEPING_DAYS=7
FAILED_DAYS=7
START=$(date +%s)
TS=$(date -u +%Y%m%dT%H%M%SZ)
D="$ROOT/$TS"
LIST_ONLY=0
case "${1:-}" in
  --list) LIST_ONLY=1 ;;
  "") ;;
  *) echo "usage: $0 [--list]" >&2; exit 2 ;;
esac

# is_kept PATH: 0 when PATH, or its name, is a line of the KEEP list.
is_kept() {
  [ -f "$KEEP_LIST" ] || return 1
  local base="${1##*/}" line
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(printf '%s' "$line" | sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -n "$line" ] || continue
    if [ "$line" = "$1" ] || [ "$line" = "$base" ]; then return 0; fi
  done < "$KEEP_LIST"
  return 1
}

# remove_dump PATH: deletes PATH unless the KEEP list names it. With --list it only says what it would do.
remove_dump() {
  if is_kept "$1"; then
    echo "keep         $1 (named in $KEEP_LIST)"
  elif [ "$LIST_ONLY" -eq 1 ]; then
    echo "would delete $1"
  else
    rm -r -- "$1"
    echo "deleted      $1"
  fi
}

# prune_nightly: the good nightly dumps beyond the KEEP_NIGHTLY newest (the UTC stamps sort by name). A FAILED dump
# (FAILED marker) does not count toward KEEP_NIGHTLY; it is deleted once its directory is older than FAILED_DAYS.
prune_nightly() {
  [ -d "$ROOT" ] || return 0
  find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -printf '%f\n' | sort -r \
    | while IFS= read -r name; do [ -e "$ROOT/$name/FAILED" ] || printf '%s\n' "$name"; done \
    | tail -n +"$((KEEP_NIGHTLY + 1))" | while IFS= read -r name; do remove_dump "$ROOT/$name"; done
  find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -mtime +"$FAILED_DAYS" -printf '%f\n' | sort \
    | while IFS= read -r name; do if [ -e "$ROOT/$name/FAILED" ]; then remove_dump "$ROOT/$name"; fi; done
}

# prune_predeploy: the deploy dumps beyond the KEEP_PREDEPLOY newest by mtime.
prune_predeploy() {
  [ -d "$BACKUPS" ] || return 0
  find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d -name 'predeploy-*' -printf '%T@ %p\n' \
    | sort -rn | tail -n +"$((KEEP_PREDEPLOY + 1))" | cut -d' ' -f2- \
    | while IFS= read -r old; do remove_dump "$old"; done
}

# prune_housekeeping: housekeeping-* and timing-* dumps older than HOUSEKEEPING_DAYS.
prune_housekeeping() {
  [ -d "$BACKUPS" ] || return 0
  find "$BACKUPS" -mindepth 1 -maxdepth 1 \( -name 'housekeeping-*' -o -name 'timing-*' \) \
    -mtime +"$HOUSEKEEPING_DAYS" | while IFS= read -r old; do remove_dump "$old"; done
}

if [ "$LIST_ONLY" -eq 1 ]; then
  echo "retention plan (nothing is dumped or deleted): nightly newest $KEEP_NIGHTLY good (FAILED after $FAILED_DAYS days), deploy newest $KEEP_PREDEPLOY, housekeeping and timing older than $HOUSEKEEPING_DAYS days; KEEP list $KEEP_LIST"
  prune_nightly
  prune_predeploy
  prune_housekeeping
  exit 0
fi

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

# retention: only after tonight's dump succeeded (set -e above), so a failed dump never prunes anything.
prune_nightly
prune_predeploy
prune_housekeeping
