#!/usr/bin/env bash
# Host watchdog for the two AI4TRADE containers, ai4trade-main and ai4trade-guard (plan 07-WP2, decision H1 A).
# Runs ON THE VPS as root from cron, every minute, under flock:
#   * * * * * /usr/bin/flock -n /run/ai4trade-host-watch.lock /root/ops/host_watch.sh >> /var/log/ai4trade-host-watch.log 2>&1
#
# A running container is UNHEALTHY when any of these holds (unhealthy runs are counted; see the restart rule below):
#   - it is paused (docker pause: no heartbeat, no outside ping, no Docker health), or its state is restarting;
#   - Docker's health status is unhealthy (the image HEALTHCHECK reads /live, the event loop);
#   - its /ready answer names SCHEDULE_LOOP_STALE: the schedule loop is frozen while /live still answers (07-F2).
#     Other /ready reasons (a closed start gate, a schema mismatch) are NOT unhealthy: a restart does not fix them.
# A container that is exited, created, dead or missing is NOT restarted: a deliberate `docker stop` stays stopped. It is
# alerted instead (a log line, and the host check fails) on every run until it is started again.
# A probe that cannot reach a running container (docker exec fails or times out) is not counted: it is no evidence of a stall.
#
# A restart happens after HOST_WATCH_UNHEALTHY_LIMIT (3) consecutive unhealthy runs, and only when
#   - no container was restarted in the last HOST_WATCH_MIN_INTERVAL_S (3600) seconds (one an hour per container), and
#   - the deploy drain is OFF. The drain is read from the deploy_drain row of ai4trade.state, straight from Postgres
#     (`docker exec postgres psql`; the host has no ai4trade CLI). It is ON only while the row says ON and its expires_at
#     is ahead of now (the app's rule; the row expires 30 min after `drain on`). A drain that is ON refuses quietly (a
#     deploy is deliberate). A drain that cannot be read refuses and reports a failure (fail closed): Postgres does not
#     answer, or the row is ON but its expires_at cannot be read.
# A paused container is unpaused, then restarted (docker restart waits for the container's stop_grace_period, 60 s).
#
# Timing (read before changing a threshold): /ready reports SCHEDULE_LOOP_STALE after 60 s without a schedule-loop pass
# (app.py LOOP_FRESH_S), while main's outside ping fails only after 180 s (liveness.py). This script follows /ready, so
# after 3 unhealthy runs a restart can come up to about 4 min after the loop stops. A Postgres outage longer than about
# 3 min can leave the loop without a pass; then main and guard may each be restarted once, once the drain row is readable
# again (one an hour per container). While Postgres is down the drain read fails, so nothing is restarted then.
#
# Each run pings the host watchdog check (HEARTBEAT_PING_URL_HOST) with a success, or with its /fail endpoint when a
# restart failed or was refused (the hour limit, an unreadable drain) or a container is not running, while it stays so.
# The URL is never printed. A run that does not happen at all (dead host, or a hung run holding the lock) is the silence
# the check reports.
#
# Environment (all optional; the defaults are the VPS):
#   HEARTBEAT_PING_URL_HOST   the host watchdog Healthchecks URL (or the line in HOST_WATCH_ENV_FILE, mode 600; one pair of
#                             surrounding single or double quotes is removed from that line)
#   HOST_WATCH_ENV_FILE       default /root/ops/host_watch.env (only its HEARTBEAT_PING_URL_HOST line is read)
#   HOST_WATCH_CONTAINERS     default "ai4trade-main ai4trade-guard"
#   HOST_WATCH_UNHEALTHY_LIMIT, HOST_WATCH_MIN_INTERVAL_S, HOST_WATCH_PROBE_TIMEOUT_S, HOST_WATCH_STATE_DIR
#   HOST_WATCH_NOW            epoch seconds; the stub test sets it. Unset: the real clock.
#
# The stub test (test_host_watch.sh) sources this file: main runs only when the file is executed, so the test can call
# main once per cron run, in one shell, with docker, curl and timeout replaced by functions.
set -uo pipefail

READY_PY='import urllib.error as e, urllib.request as u
try:
    print(u.urlopen("http://127.0.0.1:8080/ready", timeout=4).read().decode())
except e.HTTPError as x:
    print(x.read().decode())'

# The log time is UTC whatever the host time zone is, so the trailing Z is true.
log() { TZ=UTC printf '%(%Y-%m-%dT%H:%M:%SZ)T %s\n' -1 "$*"; }

# ping_host "" sends a success; ping_host /fail sends the failure endpoint. The URL is never logged.
ping_host() {
  [ -n "$PING_URL" ] || return 0
  if ! curl -fsS -m 10 --retry 2 -o /dev/null "${PING_URL}$1" >/dev/null 2>&1; then
    log "host watchdog ping did not reach the monitor"
  fi
}

# drain_state sets DRAIN to on, off or unknown. The row is read straight from Postgres. No row is off. Anything that is
# not a readable OFF, or an ON whose expires_at is ahead of NOW, is handled as the header says: unknown (fail closed)
# when Postgres does not answer or the ON row cannot be read, off when its expiry has passed.
drain_state() {
  DRAIN=unknown
  timeout 60 docker exec postgres psql -U postgres -d postgres -At \
    -c "select value from ai4trade.state where key='deploy_drain'" >"$SCRATCH/drain" 2>/dev/null || return 0
  local row="" state="" exp_iso="" exp_s=""
  IFS= read -r row <"$SCRATCH/drain" || true
  if [ -z "$row" ]; then DRAIN=off; return 0; fi
  state=$(printf '%s\n' "$row" | sed -n 's/.*"state": *"\([A-Za-z]*\)".*/\1/p')
  if [ "$state" = OFF ]; then DRAIN=off; return 0; fi
  [ "$state" = ON ] || return 0
  exp_iso=$(printf '%s\n' "$row" | sed -n 's/.*"expires_at": *"\([^"]*\)".*/\1/p')
  # the app writes UTC with +00:00 (datetime.isoformat of an aware UTC time); anything else is unreadable here
  if ! [[ "$exp_iso" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?(\+00:00|Z)$ ]]; then
    return 0
  fi
  exp_s=$(date -u -d "${BASH_REMATCH[1]} ${BASH_REMATCH[2]}" +%s 2>/dev/null) || return 0
  [[ "$exp_s" =~ ^[0-9]+$ ]] || return 0
  if (( exp_s > NOW )); then DRAIN=on; else DRAIN=off; fi
}

# probe NAME sets PROBE_WHY and PROBE_PAUSED. Returns 0 when healthy, 1 when unhealthy (counted; restartable), and 2 when
# the container is not running (exited, created, dead or missing: alerted, never restarted).
probe() {
  local name=$1 status="" paused="" health="" body=""
  PROBE_PAUSED=false
  if ! docker inspect -f '{{.State.Status}} {{.State.Paused}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$name" >"$SCRATCH/inspect" 2>/dev/null; then
    PROBE_WHY="no such container, or docker inspect failed"
    return 2
  fi
  read -r status paused health <"$SCRATCH/inspect" || true
  if [ -z "$status" ]; then PROBE_WHY="docker inspect gave no state"; return 2; fi
  if [ "$paused" = true ] || [ "$status" = paused ]; then
    PROBE_PAUSED=true
    PROBE_WHY="paused (docker pause)"
    return 1
  fi
  case "$status" in
    running) ;;
    restarting) PROBE_WHY="state restarting"; return 1 ;;
    *) PROBE_WHY="state $status"; return 2 ;;
  esac
  if [ "$health" = unhealthy ]; then PROBE_WHY="docker health unhealthy (/live)"; return 1; fi
  if ! timeout "$PROBE_TIMEOUT_S" docker exec "$name" python -c "$READY_PY" >"$SCRATCH/ready" 2>/dev/null; then
    PROBE_WHY="probe not answered (not counted)"
    return 0
  fi
  body=$(<"$SCRATCH/ready")
  if [[ "$body" == *SCHEDULE_LOOP_STALE* ]]; then
    PROBE_WHY="schedule loop stalled (/ready SCHEDULE_LOOP_STALE)"
    return 1
  fi
  PROBE_WHY="ok"
  return 0
}

# restart_one NAME COUNT: one restart, if the drain is off and the hour since the last restart has passed.
restart_one() {
  local name=$1 count=$2 last="" last_file="$STATE_DIR/$name.restarted"
  drain_state
  if [ "$DRAIN" = on ]; then
    log "$name restart refused: deploy drain is on"
    return 1
  fi
  if [ "$DRAIN" != off ]; then
    log "$name restart refused: deploy drain state unreadable (fail closed)"
    FAILED=1
    return 1
  fi
  if [ -r "$last_file" ]; then IFS= read -r last <"$last_file" || true; fi
  [[ "$last" =~ ^[0-9]+$ ]] || last=""
  if [ -n "$last" ] && (( NOW - last < MIN_INTERVAL_S )); then
    log "$name restart refused: already restarted at epoch $last (one an hour per container)"
    FAILED=1
    return 1
  fi
  # recorded before the attempt: a failed attempt also uses up the hour
  printf '%s\n' "$NOW" >"$last_file"
  if [ "$PROBE_PAUSED" = true ]; then
    docker unpause "$name" >/dev/null 2>&1 || log "$name unpause failed"
  fi
  if docker restart "$name" >/dev/null 2>&1; then
    printf '0\n' >"$STATE_DIR/$name.unhealthy"
    log "$name restarted after $count unhealthy checks ($PROBE_WHY)"
    return 0
  fi
  log "$name restart FAILED"
  FAILED=1
  return 1
}

check_one() { # NAME
  local name=$1 count_file="$STATE_DIR/$name.unhealthy" count=0 rc=0
  probe "$name" || rc=$?
  case $rc in
    0)
      printf '0\n' >"$count_file"
      return 0 ;;
    2)
      # not running: alert on every run, never restart; the count starts again if it runs later
      printf '0\n' >"$count_file"
      log "$name not restarted, alert only ($PROBE_WHY)"
      FAILED=1
      return 1 ;;
  esac
  if [ -r "$count_file" ]; then IFS= read -r count <"$count_file" || true; fi
  [[ "$count" =~ ^[0-9]+$ ]] || count=""
  count=$(( ${count:-0} + 1 ))
  printf '%s\n' "$count" >"$count_file"
  log "$name unhealthy ($count of $UNHEALTHY_LIMIT): $PROBE_WHY"
  if (( count < UNHEALTHY_LIMIT )); then
    return 1
  fi
  restart_one "$name" "$count"
}

# main is one cron run. It returns; it never exits, so a sourcing test can run it many times.
main() {
  NOW=${HOST_WATCH_NOW:-$(date +%s)}
  CONTAINERS=${HOST_WATCH_CONTAINERS:-"ai4trade-main ai4trade-guard"}
  UNHEALTHY_LIMIT=${HOST_WATCH_UNHEALTHY_LIMIT:-3}
  MIN_INTERVAL_S=${HOST_WATCH_MIN_INTERVAL_S:-3600}
  PROBE_TIMEOUT_S=${HOST_WATCH_PROBE_TIMEOUT_S:-15}
  STATE_DIR=${HOST_WATCH_STATE_DIR:-/root/ops/state/host-watch}
  ENV_FILE=${HOST_WATCH_ENV_FILE:-/root/ops/host_watch.env}
  SCRATCH=$STATE_DIR
  FAILED=0
  PROBE_WHY=""
  PROBE_PAUSED=false
  DRAIN=unknown

  PING_URL=${HEARTBEAT_PING_URL_HOST:-}
  if [ -z "$PING_URL" ] && [ -r "$ENV_FILE" ]; then
    PING_URL=$(sed -n 's/^HEARTBEAT_PING_URL_HOST=//p' "$ENV_FILE" | tail -n 1)
    # one pair of surrounding quotes (KEY="value" or KEY='value') is not part of the URL
    case "$PING_URL" in
      \"*\") PING_URL=${PING_URL#\"}; PING_URL=${PING_URL%\"} ;;
      \'*\') PING_URL=${PING_URL#\'}; PING_URL=${PING_URL%\'} ;;
    esac
  fi
  if [ -z "$PING_URL" ]; then
    log "HEARTBEAT_PING_URL_HOST is not set: the host watchdog check is not pinged (restarts still run)"
  fi

  if [ ! -d "$STATE_DIR" ] && ! mkdir -p "$STATE_DIR"; then
    log "cannot create $STATE_DIR"
    return 1
  fi
  local name
  for name in $CONTAINERS; do
    check_one "$name" || true
  done

  if [ "$FAILED" = 1 ]; then
    ping_host /fail
  else
    ping_host ""
  fi
  return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
  exit 0
fi
