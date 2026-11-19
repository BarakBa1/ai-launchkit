#!/usr/bin/env bash
# Stub test of host_watch.sh (plan 07-WP2 acceptance; decision H1 A). No Docker, no VPS, no network.
#
# Each cron run is one call to main() in this shell. docker, curl and timeout are bash functions here, so a run costs no
# process start (on a Windows Git Bash each process start takes about 2 s). One case is the real thing: the script
# executed as a file with fake docker and curl programs on PATH.
#
# The deploy drain is read with `docker exec postgres psql` (the stub answers it from $STUB/drain.row, or fails when
# $STUB/postgres.down exists). No row in drain.row is "no drain row".
#
# Run (Git Bash or Linux bash), from anywhere:   bash ai4trade/ops/test_host_watch.sh
# Exit 0 when every case passes. Each case prints PASS or FAIL with the observed value.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/host_watch.sh"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/host-watch-test.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
URL="https://hc-ping.example/test"   # the documented test host; never a real check URL
T0=1800000000                        # an arbitrary epoch (2027-01-15T08:00:00Z): every run gets an explicit HOST_WATCH_NOW
FAILS=0
CASES=0

# shellcheck source=host_watch.sh
source "$SCRIPT"                     # defines main(); main does not run when sourced

# ---- stubs (functions shadow the programs of the same name) -------------------------------------------------------
docker() {
  echo "docker $*" >>"$STUB/calls.log"
  local name line
  case "$1" in
    inspect)
      name=${!#}
      [ -r "$STUB/$name.inspect" ] || return 1
      IFS= read -r line <"$STUB/$name.inspect"
      printf '%s\n' "$line" ;;   # the probe's format: "status paused health"
    exec)
      if [ "$3" = psql ]; then   # the drain read: the deploy_drain row as psql -At prints it, nothing when there is none
        [ -e "$STUB/postgres.down" ] && return 1
        [ -r "$STUB/drain.row" ] && cat "$STUB/drain.row"
        return 0
      fi
      name=$2
      [ -e "$STUB/$name.exec_fail" ] && return 1
      IFS= read -r line <"$STUB/$name.ready"
      printf '%s\n' "$line" ;;
    restart | unpause)
      printf 'running false healthy\n' >"$STUB/$2.inspect" ;;
  esac
  return 0
}

curl() {
  local last=""
  for last; do :; done
  printf '%s\n' "$last" >>"$STUB/pings.log"
  return 0
}

timeout() { shift; "$@"; }  # `timeout N cmd ...` runs cmd (the real timeout needs an executable on Windows)

# ---- helpers (no process starts) ----------------------------------------------------------------------------------
new_case() { # NAME: fresh state, a healthy guard and main, the test ping URL, no env file, no drain row
  CASE="$WORK/$1"; STUB="$CASE/stub"; STATE="$CASE/state"
  mkdir -p "$STUB" "$STATE"
  printf 'running false healthy\n' >"$STUB/ai4trade-guard.inspect"
  printf 'running false healthy\n' >"$STUB/ai4trade-main.inspect"
  printf '{"ready": true, "failures": []}\n' >"$STUB/ai4trade-guard.ready"
  printf '{"ready": true, "failures": []}\n' >"$STUB/ai4trade-main.ready"
  export HOST_WATCH_STATE_DIR="$STATE" HOST_WATCH_CONTAINERS=ai4trade-guard HOST_WATCH_PROBE_TIMEOUT_S=5
  export HOST_WATCH_ENV_FILE="$CASE/no-such.env" HEARTBEAT_PING_URL_HOST="$URL"
  export HOST_WATCH_UNHEALTHY_LIMIT=3 HOST_WATCH_MIN_INTERVAL_S=3600
  export STUB
  : >"$CASE/out.log"
}

tick() { # NOW: one cron run, its log lines kept for the case
  HOST_WATCH_NOW=$1 main >>"$CASE/out.log" 2>&1
}

unhealthy() { printf 'running false %s\n' "$2" >"$STUB/$1.inspect"; }
ready_body() { printf '%s\n' "$2" >"$STUB/$1.ready"; }
drain_row() { # STATE EXPIRES_ISO: the deploy_drain value as psql -At prints it (jsonb text)
  printf '{"reason": "deploy test", "state": "%s", "expires_at": "%s"}\n' "$1" "$2" >"$STUB/drain.row"
}
iso_at() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%S+00:00; }

restarts() { # sets RESTARTS to the number of docker restart calls so far
  RESTARTS=0
  local l
  [ -r "$STUB/calls.log" ] || return 0
  while IFS= read -r l; do
    [[ "$l" == "docker restart "* ]] && RESTARTS=$((RESTARTS + 1))
  done <"$STUB/calls.log"
}

last_ping() { # sets LAST to the last ping sent ("" when none)
  LAST=""
  local l
  [ -r "$STUB/pings.log" ] || return 0
  while IFS= read -r l; do LAST=$l; done <"$STUB/pings.log"
}

logged() { # PATTERN: true when a log line of this case contains it
  local l
  while IFS= read -r l; do
    [[ "$l" == *"$1"* ]] && return 0
  done <"$CASE/out.log"
  return 1
}

check() { # LABEL ACTUAL EXPECTED
  CASES=$((CASES + 1))
  if [ "$2" = "$3" ]; then
    echo "PASS  $1"
  else
    echo "FAIL  $1 (got '$2', expected '$3')"
    FAILS=$((FAILS + 1))
  fi
}

check_true() { # LABEL then a command whose exit status is the verdict
  CASES=$((CASES + 1))
  local label=$1; shift
  if "$@"; then
    echo "PASS  $label"
  else
    echo "FAIL  $label"
    FAILS=$((FAILS + 1))
  fi
}

utc_stamp_ok() { # the log stamp ends in Z and its hour is the UTC hour, even with TZ set to a zone 5 h behind UTC
  local out="" utc="" i re='^[0-9]{4}-[0-9]{2}-[0-9]{2}T([0-9]{2}):[0-9]{2}:[0-9]{2}Z probe$'
  for i in 1 2; do   # a second read only when the clock crosses an hour between the two reads
    out=$(TZ=EST5EDT log probe)
    utc=$(date -u +%H)
    if [[ "$out" =~ $re ]] && [ "${BASH_REMATCH[1]}" = "$utc" ]; then return 0; fi
  done
  return 1
}

# ---- cases ---------------------------------------------------------------------------------------------------------

# 1. three unhealthy runs restart once; the first two do not
new_case three-then-restart
unhealthy ai4trade-guard unhealthy
tick $T0; tick $((T0 + 60)); restarts; check "two unhealthy runs: no restart yet" "$RESTARTS" 0
tick $((T0 + 120)); restarts; check "the third unhealthy run restarts the container once" "$RESTARTS" 1
last_ping; check "the run that restarted pings the host check with success" "$LAST" "$URL"

# 2. a second unhealthy episode inside the hour is refused (failure ping); after the hour it is allowed again
unhealthy ai4trade-guard unhealthy
tick $((T0 + 300)); tick $((T0 + 360)); tick $((T0 + 420))
restarts; check "a second restart inside the hour is refused" "$RESTARTS" 1
last_ping; check "a refused restart pings the /fail endpoint" "$LAST" "$URL/fail"
check_true "the refusal is logged as one an hour per container" logged "one an hour per container"
unhealthy ai4trade-guard unhealthy
tick $((T0 + 3700)); tick $((T0 + 3760)); tick $((T0 + 3820))
restarts; check "a restart is allowed again once the hour has passed" "$RESTARTS" 2

# 3. a deploy drain that is on refuses the restart quietly; the first run after it ends restarts at once
new_case drain-on
drain_row ON "$(iso_at $((T0 + 3600)))"
unhealthy ai4trade-guard unhealthy
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "a drain that is on refuses the restart" "$RESTARTS" 0
last_ping; check "a drain that is on is not a failure (no /fail ping)" "$LAST" "$URL"
check_true "the drain is read from Postgres (docker exec postgres psql)" grep -q "^docker exec postgres psql" "$STUB/calls.log"
drain_row OFF "$(iso_at $T0)"
tick $((T0 + 180))
restarts; check "the first run after the drain ends restarts" "$RESTARTS" 1

# 4. the drain cannot be read because Postgres does not answer: refuse and report a failure (fail closed)
new_case postgres-unreachable
touch "$STUB/postgres.down"
unhealthy ai4trade-guard unhealthy
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "an unreadable drain (Postgres down) refuses the restart" "$RESTARTS" 0
last_ping; check "an unreadable drain (Postgres down) pings /fail" "$LAST" "$URL/fail"
check_true "the refusal says the drain state is unreadable" logged "deploy drain state unreadable"

# 4b. an ON row whose expires_at cannot be read is ambiguous: refuse and report (fail closed)
new_case drain-row-unreadable
printf '{"state": "ON", "expires_at": "soon"}\n' >"$STUB/drain.row"
unhealthy ai4trade-guard unhealthy
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "an ON drain row with an unreadable expiry refuses the restart" "$RESTARTS" 0
last_ping; check "an ON drain row with an unreadable expiry pings /fail" "$LAST" "$URL/fail"

# 5. a healthy run in between resets the count: two, healthy, two is not three in a row
new_case reset-count
unhealthy ai4trade-guard unhealthy
tick $T0; tick $((T0 + 60))
unhealthy ai4trade-guard healthy
tick $((T0 + 120))
unhealthy ai4trade-guard unhealthy
tick $((T0 + 180)); tick $((T0 + 240))
restarts; check "two, healthy, two does not restart" "$RESTARTS" 0

# 6. a frozen schedule loop (/ready says SCHEDULE_LOOP_STALE) counts as unhealthy though Docker health is fine
new_case frozen-loop
ready_body ai4trade-guard '{"ready": false, "role": "guard", "failures": ["SCHEDULE_LOOP_STALE"]}'
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "a stalled schedule loop restarts the container after three runs" "$RESTARTS" 1
check_true "the stall is named in the log" logged "schedule loop stalled"

# 7. a closed start gate is not unhealthy: a restart does not open a gate
new_case gate-closed
ready_body ai4trade-guard '{"ready": false, "role": "guard", "failures": ["START_GATE_PENDING"]}'
tick $T0; tick $((T0 + 60)); tick $((T0 + 120)); tick $((T0 + 180)); tick $((T0 + 240))
restarts; check "a closed start gate never restarts the container" "$RESTARTS" 0

# 8. a paused container is unpaused, then restarted, in that order
new_case paused
printf 'running true healthy\n' >"$STUB/ai4trade-guard.inspect"
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "a paused container is restarted" "$RESTARTS" 1
check_true "a paused container is unpaused before it is restarted" \
  bash -c 'a=$(grep -n "^docker unpause" "$1" | head -n1 | cut -d: -f1); r=$(grep -n "^docker restart" "$1" | head -n1 | cut -d: -f1); [ -n "$a" ] && [ -n "$r" ] && [ "$a" -lt "$r" ]' _ "$STUB/calls.log"

# 9. an unreachable container (docker exec fails) is not counted: no evidence of a stall, so no restart
new_case exec-fails
touch "$STUB/ai4trade-guard.exec_fail"
tick $T0; tick $((T0 + 60)); tick $((T0 + 120)); tick $((T0 + 180))
restarts; check "a probe that is not answered is not counted" "$RESTARTS" 0

# 10. no host ping URL: the restart still runs, and nothing is pinged
new_case no-url
unset HEARTBEAT_PING_URL_HOST
unhealthy ai4trade-guard unhealthy
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "without a host ping URL the restart still happens" "$RESTARTS" 1
check "without a host ping URL nothing is pinged" "$(if [ -s "$STUB/pings.log" ]; then echo sent; else echo none; fi)" none

# 11. the script as a file, with the real PATH lookup: one run, exit 0, one success ping, no URL in the log
new_case as-a-file
BIN="$CASE/bin"; mkdir -p "$BIN"
cat >"$BIN/docker" <<'EOS'
#!/usr/bin/env bash
case "$1" in
  inspect) echo "running false healthy" ;;
esac
exit 0
EOS
cat >"$BIN/curl" <<'EOS'
#!/usr/bin/env bash
echo "${@: -1}" >>"$STUB/pings.log"
exit 0
EOS
chmod +x "$BIN/docker" "$BIN/curl"
env -i PATH="$BIN:$PATH" STUB="$STUB" HOST_WATCH_NOW="$T0" HOST_WATCH_STATE_DIR="$STATE" \
  HOST_WATCH_CONTAINERS=ai4trade-guard HEARTBEAT_PING_URL_HOST="$URL" HOST_WATCH_ENV_FILE="$CASE/no-such.env" \
  bash "$SCRIPT" >"$CASE/file-run.log" 2>&1
rc=$?
check "the script runs as a file and exits 0" "$rc" 0
last_ping; check "the file run pings the host check with success" "$LAST" "$URL"
check_true "the ping URL never reaches the log" bash -c '! grep -q "hc-ping.example" "$1"' _ "$CASE/file-run.log"

# 12. (P1 a) ai4trade-main is down and no drain is set: main is restarted after three unhealthy runs
new_case main-down
export HOST_WATCH_CONTAINERS=ai4trade-main
unhealthy ai4trade-main unhealthy
tick $T0; tick $((T0 + 60)); restarts; check "main: two unhealthy runs, no restart yet" "$RESTARTS" 0
tick $((T0 + 120)); restarts; check "main: the third unhealthy run restarts ai4trade-main" "$RESTARTS" 1
check_true "the restart is docker restart of ai4trade-main" grep -q "^docker restart ai4trade-main$" "$STUB/calls.log"
last_ping; check "main restart pings the host check with success" "$LAST" "$URL"

# 13. (P1 b) an expired drain row is off: the guard is restarted after three unhealthy runs
new_case drain-expired
drain_row ON "$(iso_at $((T0 - 1)))"
unhealthy ai4trade-guard unhealthy
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "an expired drain row is off: the guard is restarted" "$RESTARTS" 1

# 13b. the boundary: a drain whose expires_at is exactly the run time has expired (the app's rule is expires_at > now)
new_case drain-expires-now
drain_row ON "$(iso_at $((T0 + 120)))"
unhealthy ai4trade-guard unhealthy
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "a drain that expires at the run time is off: the guard is restarted" "$RESTARTS" 1

# 14. (P2) an exited container is not restarted (a deliberate docker stop stays stopped); it is alerted on every run
new_case exited-alert
printf 'exited false none\n' >"$STUB/ai4trade-guard.inspect"
tick $T0; tick $((T0 + 60)); tick $((T0 + 120)); tick $((T0 + 180)); tick $((T0 + 240))
restarts; check "an exited container is never restarted" "$RESTARTS" 0
check_true "an exited container is never unpaused either" bash -c '! grep -q "^docker unpause" "$1"' _ "$STUB/calls.log"
last_ping; check "an exited container fails the host check on every run" "$LAST" "$URL/fail"
check_true "the exited container is logged as alert only" logged "not restarted, alert only (state exited)"

# 14b. (P2) a created container is not restarted either
new_case created-alert
printf 'created false none\n' >"$STUB/ai4trade-guard.inspect"
tick $T0; tick $((T0 + 60)); tick $((T0 + 120)); tick $((T0 + 180))
restarts; check "a created container is never restarted" "$RESTARTS" 0
last_ping; check "a created container fails the host check" "$LAST" "$URL/fail"

# 14c. (P2) a missing container (docker inspect fails) is alerted, not restarted
new_case missing-alert
rm -f "$STUB/ai4trade-guard.inspect"
tick $T0; tick $((T0 + 60)); tick $((T0 + 120)); tick $((T0 + 180))
restarts; check "a missing container is never restarted" "$RESTARTS" 0
last_ping; check "a missing container fails the host check" "$LAST" "$URL/fail"

# 14d. (P2) a restarting container is counted and restarted after three runs, like an unhealthy one
new_case restarting
printf 'restarting false none\n' >"$STUB/ai4trade-guard.inspect"
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "a container in the restarting state is restarted after three runs" "$RESTARTS" 1

# 15. (P3) a non-numeric state file is treated as empty: a bad unhealthy count starts at one, a bad restart stamp blocks nothing
new_case garbage-state-files
printf 'abc\n' >"$STATE/ai4trade-guard.restarted"
printf 'abc\n' >"$STATE/ai4trade-guard.unhealthy"
unhealthy ai4trade-guard unhealthy
tick $T0
check_true "a non-numeric unhealthy count starts again at one" logged "unhealthy (1 of 3)"
tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "a non-numeric restart stamp does not block the restart" "$RESTARTS" 1
last_ping; check "that restart pings the host check with success (not /fail)" "$LAST" "$URL"

# 15b. (P3) values that would change the outcome if they were read as numbers: a count of -5 would never reach three,
#      and a restart stamp of +1800000000 would look like one second ago (refused) instead of being empty
new_case signed-state-files
printf -- '-5\n' >"$STATE/ai4trade-guard.unhealthy"
printf -- '+1800000000\n' >"$STATE/ai4trade-guard.restarted"
unhealthy ai4trade-guard unhealthy
tick $T0; tick $((T0 + 60)); tick $((T0 + 120))
restarts; check "a signed count and a signed restart stamp are read as empty: the restart happens at three runs" "$RESTARTS" 1

# 16. (P3) the log stamp is UTC (the trailing Z is true) whatever TZ the host has
check_true "the log stamp is UTC even when TZ is not UTC" utc_stamp_ok

# 17. (P3) one pair of surrounding quotes is removed from the env-file URL: double quotes, then single quotes
new_case quoted-env-double
unset HEARTBEAT_PING_URL_HOST
printf 'HEARTBEAT_PING_URL_HOST="%s"\n' "$URL" >"$CASE/host_watch.env"
export HOST_WATCH_ENV_FILE="$CASE/host_watch.env"
tick $T0
last_ping; check "a double-quoted env-file URL is pinged without its quotes" "$LAST" "$URL"

new_case quoted-env-single
unset HEARTBEAT_PING_URL_HOST
printf "HEARTBEAT_PING_URL_HOST='%s'\n" "$URL" >"$CASE/host_watch.env"
export HOST_WATCH_ENV_FILE="$CASE/host_watch.env"
tick $T0
last_ping; check "a single-quoted env-file URL is pinged without its quotes" "$LAST" "$URL"

echo
if [ "$FAILS" = 0 ]; then
  echo "all $CASES host_watch cases passed"
  exit 0
fi
echo "$FAILS of $CASES host_watch case(s) FAILED"
exit 1
