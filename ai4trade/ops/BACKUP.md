# AI4TRADE backups: coverage, restore test, VPS steps

Status: `ai4trade/ops/restore_test.sh` is written and tested against a docker stub on the workstation (happy path and six failure paths). It has NOT run on the VPS. The VPS was read-only for this work. No kopia policy or compose mount change is needed for coverage (see verdict).

Commands below use `$VPS_HOST`, the VPS address. Export it in your shell first; the address is kept out of the repo.

## Observed coverage (read-only, 2026-11-18 08:09 to 08:13 UTC)

| Item | Location on the VPS | Covered by | Notes |
| --- | --- | --- | --- |
| Nightly logical dump of database `postgres` (schemas `ai4trade`, `barak`) | `/root/backups/pg-nightly/<UTC stamp>/` | kopia snapshot of `/host` (host root), nightly 23:45 UTC | Cron 06:15 local = 05:15 UTC, under flock. Newest dump `20261118T051501Z`: `postgres.dump` 4.15 GB, `toc.txt` 125 KB, finished 06:23 UTC. Not in any kopia snapshot until the 23:45 UTC run. |
| `/root/ai4trade` (compose, `.env`, `.env.prev`, `db.env`, `operator.env`, `secrets/`) | `/root/ai4trade` | same `/host` snapshot | Snapshot of 2026-11-17 contains these entries. Secrets are inside the encrypted kopia repository. |
| Docker volumes (Postgres data, Caddy data, kopia config) | `/var/lib/docker/volumes` | same `/host` snapshot | File-level only. Containers are paused during each snapshot (see G5). |
| Miniflux database (2.1 GB) and Langfuse database (48 MB) | databases `miniflux`, `langfuse` on the same Postgres server | file-level snapshot only | Not in the logical dump (G4). |

Kopia: container `kopia` healthy (running since 2026-11-17T06:02:45Z, image `kopia/kopia:latest`). Repository type azureBlob, container `n8n`, off-site. Encryption AES256-GCM. Only one policy, the global one: retention latest 3, daily 3, weekly 4, monthly 3; schedule 23:45. Snapshots of `/host`: 2026-09-30, 10-16, 11-08, 11-15, 11-16, and 2026-11-17 23:45:02 UTC (`k3ee6c2a7eb4bed1008d50d6c5a905cd2`, 124.8 GB, 769,556 files). Newest dump inside that snapshot: `20261117T062906Z`.

Other facts: `/root/ops/pg_dump_nightly.sh` on the VPS has the same sha256 prefix as the repo file (`3881877d751fce42`). Root crontab has `15 6 * * *` for the dump (local time), `7 5 * * 0` for `weekly_cleanup.sh`, and an hourly archive job. There is no restore cron. `ai4trade.state` has no `backup.last_ok` row. The only backup or kill-related key is `kill_switch`. `/root/backups/pg-nightly/restore_test_20261117.log` exists and is 0 bytes (an earlier attempt that left no output).

## Verdict

- The dumps and `/root/ai4trade` are covered by the nightly kopia snapshot of the host root. No kopia policy change and no compose mount change is proposed.
- Coverage is not the same as recoverability. No restore has ever been verified (G1).

## Gap list

- G1. No verified restore. The 0-byte `restore_test_20261117.log` is not evidence. `backup.last_ok` is absent. `restore_test.sh` fixes this.
- G2. The nightly job checks the dump only with `pg_restore --list` (at least 100 TOC lines). A dump can list correctly and still fail to restore.
- G3. Off-site copy happens only at the 23:45 UTC snapshot, so the newest dump is local-only for up to about 24 hours.
- G4. The logical dump covers only database `postgres`. The `miniflux` and `langfuse` databases are covered only by file-level snapshots.
- G5. The kopia global policy runs a before-hook that pauses every container except kopia (`docker ps -a -q --filter "name!=kopia" | xargs -r docker pause`, 5 min timeout) and an after-hook that unpauses them. This includes `ai4trade-main` and `ai4trade-guard`. The snapshot duration and the behaviour if kopia fails mid-snapshot were not measured.
- G6. Local dump retention is 14 days (`KEEP_DAYS=14`) at about 4.15 GB per dump, which is about 58 GB at steady state. At read time, `/` had 64 GB available (68% used). At the current dump size, 14 days of local dumps reach about 58 GB around 2026-12-01 (estimate). This is a disk-full risk, not a coverage gap. Lower `KEEP_DAYS` or move dumps off this disk before then.
- G7. Disaster recovery needs the kopia repository password and the Azure storage credential. Their storage outside the VPS was not verified (not read, per M6).
- G8. Unpinned images: `kopia/kopia:latest` and `miniflux/miniflux:latest`. A restore may behave differently after an upstream change.

## Restore test: `ai4trade/ops/restore_test.sh`

What it does, in order: take the newest kopia snapshot of `/host` (refuses one older than 36 h); take the newest `YYYYMMDDTHHMMSSZ` directory under `root/backups/pg-nightly` in that snapshot (refuses one marked `FAILED`); restore it with `kopia restore` into the kopia container, copy it out with `docker cp`, verify `SHA256SUMS`; start `postgres:17` as `ai4trade-restore-<random>` with `--rm --network none --memory 2g --cpus 2` and no published ports; wait for the final server; run `pg_restore --no-owner --no-privileges`; compare `count(*)` of rows with the key timestamp below the dump stamp, for `ai4trade.runs`, `ai4trade.run_steps`, `barak.decisions`, `barak.opportunities`, `barak.order_execution_events`, `barak.news`, against the live database, with an absolute tolerance of `RESTORE_TOL_ROWS` (default 5) and at least one row per table; upsert `ai4trade.state` key `backup.last_ok` (`value` JSON with dump stamp, snapshot id, counts, times; `updated_by` `restore_test.sh`). The upsert happens only if every check passed. An EXIT trap removes the container (with its volume), the kopia temporary directory and the host scratch directory, on success and on failure.

Environment knobs (optional): `RESTORE_TOL_ROWS`, `RESTORE_SCRATCH_PARENT` (default `/var/tmp`), `RESTORE_FREE_FACTOR` (default 3), `RESTORE_FREE_EXTRA_GB` (default 5).

Free-space gate: before anything is written, the script reads the size of the local copy of the same dump (`/root/backups/pg-nightly/<stamp>/postgres.dump`) and requires that both `/var/lib/docker` and `RESTORE_SCRATCH_PARENT` have at least `RESTORE_FREE_FACTOR` x dump size + `RESTORE_FREE_EXTRA_GB` GiB free. Otherwise it stops with a `FAIL:` line and no restore runs. At the current dump size (about 4.15 GB) that is roughly 17 GiB on each filesystem.

Resource use on the VPS, for a 4.2 GB dump. Peak disk, summed (the budget figure, about 19.5 GB): the kopia container's `/tmp` copy of about 4.2 GB (Docker disk), the host scratch copy of about 4.2 GB (`RESTORE_SCRATCH_PARENT`, `/var/tmp` by default), and the throwaway database of about 11 GB (Docker disk, removed with the container). The script removes the `/tmp` copy right after `docker cp`, so the `/tmp` copy and the database normally never exist together (if that removal fails, the exit trap retries it and the overlap can happen). The simultaneous peak is therefore about 15 GB (host scratch copy plus the database). Up to 2 GB RAM for the throwaway container. The live Postgres and the Docker disk share this space, so the free-space gate above runs first. Separately, the 23:45 UTC kopia snapshot pauses every container except kopia, including `ai4trade-main` and `ai4trade-guard` (G5). That is still an open operator decision (D1) and this runbook does not change it. The nightly dump took about 68 minutes to write (05:15:01 to 06:23 UTC). The restore is expected to take at least as long. This was not measured.

### Install (orchestrator, after approval)

```bash
scp ai4trade/ops/restore_test.sh root@$VPS_HOST:/root/ops/restore_test.sh.new
ssh root@$VPS_HOST 'chmod 700 /root/ops/restore_test.sh.new && bash -n /root/ops/restore_test.sh.new && mv /root/ops/restore_test.sh.new /root/ops/restore_test.sh && ls -la /root/ops/restore_test.sh'
```

### First run (approval needed: it writes one row to `ai4trade.state`)

Run it detached, because it takes a long time:

```bash
ssh root@$VPS_HOST 'nohup /usr/bin/flock -n /run/ai4trade-restore-test.lock /root/ops/restore_test.sh >> /var/log/ai4trade-restore-test.log 2>&1 < /dev/null &'
ssh root@$VPS_HOST 'tail -n 30 /var/log/ai4trade-restore-test.log'
```

The last line must be `restore test passed`, preceded by one `ok` line per table and `recorded backup.last_ok`. A `FAIL:` line names the first failed check.

Readback:

```bash
ssh root@$VPS_HOST 'docker exec postgres psql -U postgres -d postgres -At -c "select updated_at, value from ai4trade.state where key = '"'"'backup.last_ok'"'"';"'
```

### Weekly cron (after a passing first run)

Server time zone is Europe/Stockholm. Sunday 10:30 local is 09:30 UTC in winter. It does not overlap the 05:15 UTC dump, the 23:45 UTC kopia snapshot, or the Sunday 05:07 local cleanup.

```bash
ssh root@$VPS_HOST 'crontab -l > /root/crontab.before-restore-test && (crontab -l; echo "30 10 * * 0 /usr/bin/flock -n /run/ai4trade-restore-test.lock /root/ops/restore_test.sh >> /var/log/ai4trade-restore-test.log 2>&1") | crontab - && crontab -l | tail -n 2'
```

Expected: 4 lines in the crontab, the last one the new job (5 with the alert line below).

### Failure alert (recommended)

On a failed run the script posts one line to `RESTORE_TEST_ALERT_URL`, sent as the form field `text`. The receiving endpoint must accept that field. The URL is a secret: keep it out of chat, logs and this repo. Add it as the first line of root's crontab with `crontab -e` on the VPS (the crontab file is root-only), so no URL goes on a command line:

```
RESTORE_TEST_ALERT_URL=<webhook URL from the operator>
30 10 * * 0 /usr/bin/flock -n /run/ai4trade-restore-test.lock /root/ops/restore_test.sh >> /var/log/ai4trade-restore-test.log 2>&1
```

A manual run posts an alert only if the variable is exported in that shell. Without the variable a failed run leaves only the log line, so the staleness check below is the backstop.

### Log rotation

The job appends to `/var/log/ai4trade-restore-test.log` and never rotates it. Install this as `/etc/logrotate.d/ai4trade-restore-test`:

```
/var/log/ai4trade-restore-test.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

`copytruncate` is needed because the job appends with `>>` and does not reopen the file. Check the snippet with `logrotate -d /etc/logrotate.d/ai4trade-restore-test` (dry run) before relying on it.

### Staleness check (the backstop)

`backup.last_ok` is written only by a passing run, and the test runs weekly. If its `updated_at` is more than 8 days old, the restore test is failing (8 days allows one missed Sunday). Readback:

```bash
ssh root@$VPS_HOST 'docker exec postgres psql -U postgres -d postgres -At -c "select updated_at < now() - interval '"'"'8 days'"'"' as stale, updated_at from ai4trade.state where key = '"'"'backup.last_ok'"'"';"'
```

`stale = t` means failing. No row at all means no run has ever passed.

### Rollback

```bash
ssh root@$VPS_HOST 'crontab /root/crontab.before-restore-test && rm -f /root/ops/restore_test.sh && crontab -l | wc -l'
```

To also remove the state row (a production write, needs approval):

```bash
ssh root@$VPS_HOST 'docker exec postgres psql -U postgres -d postgres -At -c "delete from ai4trade.state where key = '"'"'backup.last_ok'"'"';"'
```

## Decisions for the operator

- D1. The 23:45 UTC snapshot pauses the AI4TRADE containers (G5). Decide whether to keep it, measure its duration, and check the after-hook behaviour on failure.
- D2. Confirm that the kopia repository password and the Azure storage credential are stored outside the VPS (G7).
- D3. Decide the local dump retention (G6). Options: `KEEP_DAYS=3` to 7, or keep 14 days and add free-space alerting.
- D4. Decide whether `miniflux` and `langfuse` need logical dumps as well (G4). Proposed addition to `pg_dump_nightly.sh`, same dated directory and TOC check: `docker exec postgres pg_dump -U postgres -d miniflux -Fc > "$D/miniflux.dump"`, and the same for `langfuse`. Not implemented here.
- D5. Pin the kopia and Miniflux image tags (G8).
