#!/usr/bin/env bash
# Weekly restore test for the launchkit Postgres nightly dump (ai4trade v0.2 WP4; review-v0.1 07 B5, WP4).
# Runs ON THE VPS as root from cron (install and schedule: ai4trade/ops/BACKUP.md). It only reads the live
# database, except for one upsert of ai4trade.state 'backup.last_ok' after every check has passed.
#
# Steps: take the newest kopia snapshot of the host root, take its newest nightly dump directory, check that
# it is not marked FAILED, check free space on the Docker disk and the scratch directory (refuses a short
# disk before anything is written), copy the dump out of the kopia container and verify SHA256SUMS, restore it into a
# throwaway postgres:17 container (no network, no published ports, removed on exit), then compare row counts
# for key tables with the live database as of the dump time. Every temporary object is removed on exit,
# whether the run passes or fails.
#
# Exit status: 0 = restore verified and recorded; non-zero = the first failed check (message on stderr).
# On a non-zero exit one line is posted to RESTORE_TEST_ALERT_URL when that variable is set (never printed).
# This file holds no secret. The throwaway superuser password is generated per run, kept in a shell
# variable and passed to docker by name, so it never appears in the command line.
set -euo pipefail

KOPIA_CONTAINER=kopia
LIVE_CONTAINER=postgres
PG_IMAGE=postgres:17                    # same major version as the live server (observed: postgres:17)
SNAP_SOURCE=/host                       # kopia source = host root; the dumps sit under root/backups/pg-nightly
SNAP_DUMP_DIR=root/backups/pg-nightly   # relative to the snapshot root
MAX_SNAPSHOT_AGE_S=$((36 * 3600))       # refuse to test a snapshot older than 36 h
TOL_ROWS=${RESTORE_TOL_ROWS:-5}         # allowed absolute difference per table, as of the dump time
SCRATCH_PARENT=${RESTORE_SCRATCH_PARENT:-/var/tmp}   # host directory for the copied dump (removed on exit)
LOCAL_DUMP_DIR=/root/backups/pg-nightly # host copy of the same directory (snapshot path root/backups/pg-nightly)
DOCKER_ROOT=/var/lib/docker             # filesystem of the kopia temporary copy and of the throwaway database
FREE_FACTOR=${RESTORE_FREE_FACTOR:-3}   # free space needed on each of the two filesystems:
FREE_EXTRA_GB=${RESTORE_FREE_EXTRA_GB:-5} #   FREE_FACTOR x dump size + FREE_EXTRA_GB (GiB)

# schema.table and the timestamp column used for the as-of-dump comparison (same predicate on both sides).
TABLES=(
  "ai4trade.runs scheduled_for"
  "ai4trade.run_steps started_at"
  "barak.decisions created_at"
  "barak.opportunities created_at"
  "barak.order_execution_events created_at"
  "barak.news created_at"
)

KTMP=""      # temporary directory inside the kopia container
SCRATCH=""   # temporary directory on the host
NAME=""      # throwaway postgres container

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
LAST_ERR=""  # the first failed check, used in the alert line
die() { LAST_ERR="$*"; log "FAIL: $*" >&2; exit 1; }
rand_hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

# Posts one line to RESTORE_TEST_ALERT_URL as form field "text". The URL is never printed or logged: curl's
# output goes to /dev/null and only a generic line is logged. The receiving endpoint must accept a form field "text".
alert_webhook() {
  if curl --fail --silent --max-time 10 --data-urlencode "text=$1" "$RESTORE_TEST_ALERT_URL" >/dev/null 2>&1; then
    log "alert posted"
  else
    log "warning: alert post failed"
  fi
}

cleanup() {
  local rc=$?
  set +e
  if [ -n "$NAME" ]; then docker rm -f -v "$NAME" >/dev/null 2>&1; fi
  if [ -n "$KTMP" ]; then docker exec "$KOPIA_CONTAINER" rm -rf -- "$KTMP" >/dev/null 2>&1; fi
  if [ -n "$SCRATCH" ]; then rm -rf -- "$SCRATCH"; fi
  if [ "$rc" -eq 0 ]; then log "restore test passed"; else log "restore test FAILED (exit $rc)"; fi
  if [ "$rc" -ne 0 ] && [ -n "${RESTORE_TEST_ALERT_URL:-}" ]; then
    alert_webhook "ai4trade restore test FAILED: ${LAST_ERR:-no FAIL line was logged} (exit $rc)"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

for c in "$KOPIA_CONTAINER" "$LIVE_CONTAINER"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || true)" = true ] || die "container $c is not running"
done
docker image inspect "$PG_IMAGE" >/dev/null 2>&1 || die "image $PG_IMAGE is not present on this host (this script does not pull)"

# 1. Newest kopia snapshot of the host root. kopia lists snapshots oldest first, grouped under a
#    "<user>@<host>:<path>" header line; keep the last row under the /host header.
SNAP_LIST=$(docker exec "$KOPIA_CONTAINER" kopia snapshot list --all) || die "kopia snapshot list failed"
read -r SNAP_ID SNAP_WHEN < <(printf '%s\n' "$SNAP_LIST" | awk '
  /:\/host[[:space:]]*$/ { inh = 1; next }
  /^[^ ]/                { inh = 0 }
  inh && $4 ~ /^k[0-9a-f]+$/ { id = $4; when = $1 " " $2 }
  END { print id, when }')
[[ "$SNAP_ID" =~ ^k[0-9a-f]{16,}$ ]] || die "no kopia snapshot found for $SNAP_SOURCE"
SNAP_EPOCH=$(date -u -d "$SNAP_WHEN" +%s) || die "cannot parse snapshot time '$SNAP_WHEN'"
if (( $(date -u +%s) - SNAP_EPOCH > MAX_SNAPSHOT_AGE_S )); then
  die "newest snapshot $SNAP_ID is older than 36 h ($SNAP_WHEN UTC)"
fi
log "snapshot $SNAP_ID taken $SNAP_WHEN UTC"

# 2. Newest nightly dump directory inside that snapshot (names are UTC stamps, so they sort in time order).
DUMPS=$(docker exec "$KOPIA_CONTAINER" kopia ls "$SNAP_ID/$SNAP_DUMP_DIR") || die "no $SNAP_DUMP_DIR in snapshot $SNAP_ID"
STAMP=$(printf '%s\n' "$DUMPS" | awk '{ print $NF }' \
  | sed -n 's#^\(20[0-9]\{6\}T[0-9]\{6\}Z\)/\{0,1\}$#\1#p' | sort | tail -n 1)
[[ "$STAMP" =~ ^20[0-9]{6}T[0-9]{6}Z$ ]] || die "no nightly dump directory in snapshot $SNAP_ID"
ENTRIES=$(docker exec "$KOPIA_CONTAINER" kopia ls "$SNAP_ID/$SNAP_DUMP_DIR/$STAMP") || die "cannot list dump $STAMP"
if grep -qx 'FAILED' <<<"$ENTRIES"; then die "dump $STAMP carries a FAILED marker"; fi
log "testing dump $STAMP from snapshot $SNAP_ID"

# 2b. Free-space check before anything is written. Peak use is the kopia temporary copy (inside the kopia
#     container, on the Docker disk), the host copy in $SCRATCH_PARENT, and the throwaway database (Docker disk).
#     The dump size is read from the host copy of the same dump directory (same file that kopia captured).
#     Read-only up to this point; the script refuses to start the restore when either filesystem is short.
DUMP_BYTES=$(stat -c %s "$LOCAL_DUMP_DIR/$STAMP/postgres.dump") \
  || die "cannot read the size of $LOCAL_DUMP_DIR/$STAMP/postgres.dump (nothing written)"
[[ "$DUMP_BYTES" =~ ^[0-9]+$ ]] || die "unexpected size for the local copy of dump $STAMP (nothing written)"
GIB=$((1024 * 1024 * 1024))
NEED_BYTES=$(( DUMP_BYTES * FREE_FACTOR + FREE_EXTRA_GB * GIB ))
for p in "$DOCKER_ROOT" "$SCRATCH_PARENT"; do
  avail_kb=$(df -Pk "$p" | awk 'NR == 2 { print $4 }') || avail_kb=""
  [[ "$avail_kb" =~ ^[0-9]+$ ]] || die "cannot read the free space on $p (nothing written)"
  avail=$(( avail_kb * 1024 ))
  if (( avail < NEED_BYTES )); then
    die "$p has $(( avail / GIB )) GiB free, needs $(( NEED_BYTES / GIB )) GiB = ${FREE_FACTOR} x dump of $(( DUMP_BYTES / GIB )) GiB + ${FREE_EXTRA_GB} GiB (nothing written)"
  fi
  log "free space on $p: $(( avail / GIB )) GiB (needs $(( NEED_BYTES / GIB )) GiB)"
done

# 3. Copy the dump out of the kopia container. kopia restores into a path inside that container (it cannot
#    write to the host), then docker cp moves the three files to a private host directory.
KTMP="/tmp/ai4trade-restore-$(rand_hex 6)"
docker exec "$KOPIA_CONTAINER" kopia restore "$SNAP_ID/$SNAP_DUMP_DIR/$STAMP" "$KTMP" >/dev/null \
  || die "kopia restore of dump $STAMP failed"
SCRATCH=$(mktemp -d "$SCRATCH_PARENT/ai4trade-restore.XXXXXX")
chmod 700 "$SCRATCH"
for f in postgres.dump toc.txt SHA256SUMS; do
  docker cp "$KOPIA_CONTAINER:$KTMP/$f" "$SCRATCH/$f" || die "copy of $f from the kopia container failed"
done
# The kopia temporary copy is not needed once the files are on the host. If removal fails, the exit trap retries.
if docker exec "$KOPIA_CONTAINER" rm -rf -- "$KTMP"; then
  KTMP=""
else
  log "warning: could not remove $KTMP in the kopia container; the exit trap retries"
fi
(cd "$SCRATCH" && sha256sum -c SHA256SUMS >/dev/null) || die "SHA256SUMS does not match the copied dump"
TOC_LINES=$(wc -l < "$SCRATCH/toc.txt")
(( TOC_LINES >= 100 )) || die "toc.txt has only $TOC_LINES lines"
log "dump copied and checksums verified ($TOC_LINES TOC lines)"

# 4. Throwaway postgres:17: no network, memory and CPU capped, password only in the environment of this shell.
NAME="ai4trade-restore-$(rand_hex 4)"
POSTGRES_PASSWORD=$(rand_hex 24)
export POSTGRES_PASSWORD
docker run -d --rm --name "$NAME" --network none --memory 2g --cpus 2 \
  -e POSTGRES_PASSWORD "$PG_IMAGE" >/dev/null || die "could not start $PG_IMAGE"
ready=""
for _ in $(seq 1 120); do
  # The image starts a temporary server during initdb; wait for the second "ready" line (final server).
  if [ "$(docker logs "$NAME" 2>&1 | grep -c 'ready to accept connections' || true)" -ge 2 ] \
     && docker exec "$NAME" pg_isready -q -U postgres >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
[ -n "$ready" ] || die "throwaway postgres did not become ready"

# 5. Restore. Owners and privileges are skipped: the throwaway server has no matching roles.
docker exec -i "$NAME" pg_restore -U postgres -d postgres --no-owner --no-privileges \
  < "$SCRATCH/postgres.dump" || die "pg_restore failed"
log "pg_restore finished"

# 6. Row counts as of the dump time. The cut-off is the dump stamp (the dump starts at that instant), so rows
#    created later are excluded on both sides and the difference should be near zero.
CUTOFF="${STAMP:0:4}-${STAMP:4:2}-${STAMP:6:2} ${STAMP:9:2}:${STAMP:11:2}:${STAMP:13:2}+00"
COUNTS_JSON=""
for entry in "${TABLES[@]}"; do
  tbl=${entry% *}
  col=${entry#* }
  q="select count(*) from $tbl where $col < timestamptz '$CUTOFF'"
  restored=$(docker exec "$NAME" psql -X -q -At -U postgres -d postgres -c "$q") || die "count on restored $tbl failed"
  live=$(docker exec "$LIVE_CONTAINER" psql -X -q -At -U postgres -d postgres -c "$q") || die "count on live $tbl failed"
  [[ "$restored" =~ ^[0-9]+$ && "$live" =~ ^[0-9]+$ ]] || die "non-numeric count for $tbl"
  (( restored >= 1 )) || die "$tbl is empty after restore"
  diff=$(( restored > live ? restored - live : live - restored ))
  (( diff <= TOL_ROWS )) || die "$tbl: restored $restored, live $live as of $STAMP (tolerance $TOL_ROWS)"
  log "ok $tbl restored=$restored live_as_of_dump=$live"
  COUNTS_JSON+="${COUNTS_JSON:+,}\"$tbl\":{\"restored\":$restored,\"live\":$live}"
done

# 7. Record the result. Only reached when every check above passed.
RESTORED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PAYLOAD="{\"dump\":\"$STAMP\",\"snapshot\":\"$SNAP_ID\",\"snapshot_at\":\"$SNAP_WHEN\",\"restored_at\":\"$RESTORED_AT\",\"tolerance_rows\":$TOL_ROWS,\"counts\":{$COUNTS_JSON}}"
docker exec "$LIVE_CONTAINER" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d postgres -c \
  "insert into ai4trade.state (key, value, updated_at, updated_by)
   values ('backup.last_ok', '$PAYLOAD'::jsonb, now(), 'restore_test.sh')
   on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at, updated_by = excluded.updated_by;" \
  >/dev/null || die "could not record backup.last_ok"
log "recorded backup.last_ok (dump $STAMP, snapshot $SNAP_ID)"
