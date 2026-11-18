# Reconcile the AI4TRADE Caddy addon with the repo

Status: prepared, NOT applied. Every step below changes the VPS (`root@$VPS_HOST`) and is for the orchestrator to run after approval. Nothing here prints a credential value; every check compares lengths or hashes. Export `VPS_HOST` (the VPS address) in your shell first; the address is kept out of the repo.

## Observed state (read-only, 2026-11-18 08:07 to 08:13 UTC)

- Addon: `/root/ai-launchkit/caddy-addon/ai4trade.conf` (VPS) differs from `ai4trade/caddy-ai4trade.conf` (repo). The only functional difference is the `basic_auth` line: the VPS holds the literal username and bcrypt hash; the repo uses `{$AI4TRADE_BASIC_AUTH_USER} {$AI4TRADE_BASIC_AUTH_HASH}`. The header comments also differ. All directives match: `@internal` 404 for `/metrics`, `reverse_proxy ai4trade-main:8080` with `header_up X-Forwarded-For {remote_host}`, and the header block.
- Caddy container: `caddy` (`docker.io/library/caddy:2-alpine`), running, RestartCount 0, started 2026-09-14T23:07:41Z. It reads `./caddy-addon:/etc/caddy/addons:ro`; the Caddyfile line 615 is `import /etc/caddy/addons/*.conf`.
- Caddy environment: the `caddy` service in `/root/ai-launchkit/docker-compose.yml` has an inline `environment:` list and no `env_file:`. Its values are literals or `${NAME}` references, which Compose fills from `/root/ai-launchkit/.env`. The VPS compose has no `AI4TRADE_BASIC_AUTH_*` entries yet.
- Repo: root `docker-compose.yml`, `caddy` service, already has `AI4TRADE_BASIC_AUTH_USER=${AI4TRADE_BASIC_AUTH_USER}` and `AI4TRADE_BASIC_AUTH_HASH=${AI4TRADE_BASIC_AUTH_HASH}` (after the `BASE_DOMAIN` line in the caddy block).
- Public probe `https://ai4trade.8739.site/` without credentials: HTTP 401 at 08:09:07 UTC. A probe of the Caddy admin API returned no matches, so the running Caddy configuration was not verified.

## Why the order matters

Caddy substitutes `{$VAR}` when it loads the configuration. An unset or empty value makes the `basic_auth` block invalid, and Caddy then fails to start. Caddy serves every route on this VPS, so a bad recreate takes all of them down. The variables must exist in the caddy container before the addon that references them is loaded. A reload keeps the old process environment, so the caddy container must be recreated, not reloaded.

Steps 2 to 4 change files only and do nothing until caddy is recreated. They can be done ahead of the approval window.

## Procedure

Approval: step 6 interrupts every HTTP(S) route for a few seconds. Get explicit approval for steps 5 and 6 first.

### 0. Record the pre-state (read-only)

```bash
cd /root/ai-launchkit && docker compose config --quiet; echo "compose_rc=$?"
curl -s -o /dev/null -w "%{http_code}\n" https://ai4trade.8739.site/     # expect 401
# also record the status of one other route (any BASE_DOMAIN host) so the post-check can compare
```

### 1. Back up (mode and owner kept; the backup directory is 0700)

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ); B=/root/backups/caddy-reconcile-$STAMP
install -d -m 700 "$B"
cp -p /root/ai-launchkit/docker-compose.yml "$B/"
cp -p /root/ai-launchkit/.env "$B/"
cp -p /root/ai-launchkit/caddy-addon/ai4trade.conf "$B/"
echo "$B"
```

`.env` is copied because rollback needs the exact previous file. It is never printed.

### 2. Move the two values into `.env` without printing them

```bash
A=/root/ai-launchkit/caddy-addon/ai4trade.conf
read -r U H < <(awk '/basic_auth[[:space:]]*\{/{getline; print; exit}' "$A")
printf "\nAI4TRADE_BASIC_AUTH_USER='%s'\nAI4TRADE_BASIC_AUTH_HASH='%s'\n" "$U" "$H" >> /root/ai-launchkit/.env
unset U H
```

Single quotes stop Compose from interpolating the `$` characters in the bcrypt hash. If a name is defined twice in `.env`, Compose uses the last definition. Check `.env` mode is still `600` (`ls -la`), without reading its contents.

### 3. Add the two entries to the VPS compose, copied from the repo

Check first that the anchor matches exactly one line (the command prints `1`):

```bash
grep -c '^      - BASE_DOMAIN=' /root/ai-launchkit/docker-compose.yml
```

If it prints `1`:

```bash
sed -i '/^      - BASE_DOMAIN=/a\      - AI4TRADE_BASIC_AUTH_USER=${AI4TRADE_BASIC_AUTH_USER}\n      - AI4TRADE_BASIC_AUTH_HASH=${AI4TRADE_BASIC_AUTH_HASH}' /root/ai-launchkit/docker-compose.yml
```

If it prints anything else, stop and add the two lines by hand under the caddy `environment:` list, with the same text as repo lines 470 and 471.

### 4. Validate, and check the values by length only

```bash
cd /root/ai-launchkit && docker compose config --quiet && echo "compose_valid"
docker compose config --format json | python3 -c 'import json,sys; e=json.load(sys.stdin)["services"]["caddy"]["environment"]; print({k: len(e.get(k) or "") for k in ("AI4TRADE_BASIC_AUTH_USER","AI4TRADE_BASIC_AUTH_HASH")})'
```

Expected: both lengths greater than 0. The hash length is 60 (bcrypt). Stop if either is 0.

### 5. Install the repo addon (after step 4 passes)

From the merged repo commit, on the workstation:

```bash
scp ai4trade/caddy-ai4trade.conf root@$VPS_HOST:/root/ai-launchkit/caddy-addon/.ai4trade.conf.tmp
ssh root@$VPS_HOST 'mv /root/ai-launchkit/caddy-addon/.ai4trade.conf.tmp /root/ai-launchkit/caddy-addon/ai4trade.conf'
```

The temporary name does not end in `.conf`, so the `*.conf` import never sees it.

### 6. Recreate the caddy service only (approval needed)

```bash
cd /root/ai-launchkit && docker compose up -d --no-deps caddy
```

### 7. Readback

```bash
docker inspect caddy --format '{{.State.Status}} restarts={{.RestartCount}}'      # running, restarts stays 0
docker exec caddy caddy validate --config /etc/caddy/Caddyfile                    # Valid configuration
docker exec caddy sh -c 'test -n "$AI4TRADE_BASIC_AUTH_USER" && test -n "$AI4TRADE_BASIC_AUTH_HASH" && echo env-set'
docker logs caddy --tail 30                                                        # no error lines
curl -s -o /dev/null -w "%{http_code}\n" https://ai4trade.8739.site/              # expect 401, as in step 0
```

Value match without printing (both lines must print the same 16 characters):

```bash
B=<the directory from step 1>
awk '/basic_auth[[:space:]]*\{/{getline; print; exit}' "$B/ai4trade.conf" | awk '{printf "%s", $2}' | sha256sum | cut -c1-16
docker exec caddy sh -c 'printf %s "$AI4TRADE_BASIC_AUTH_HASH"' | sha256sum | cut -c1-16
awk '/basic_auth[[:space:]]*\{/{getline; print; exit}' "$B/ai4trade.conf" | awk '{printf "%s", $1}' | sha256sum | cut -c1-16
docker exec caddy sh -c 'printf %s "$AI4TRADE_BASIC_AUTH_USER"' | sha256sum | cut -c1-16
```

Then the operator signs in once in a browser. Agents do not use the credentials.

### 8. Rollback (any failed check in step 7, or caddy will not start)

```bash
B=<the directory from step 1>
cp -p "$B/docker-compose.yml" /root/ai-launchkit/docker-compose.yml
cp -p "$B/.env" /root/ai-launchkit/.env
cp -p "$B/ai4trade.conf" /root/ai-launchkit/caddy-addon/ai4trade.conf
cd /root/ai-launchkit && docker compose up -d --no-deps caddy
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
curl -s -o /dev/null -w "%{http_code}\n" https://ai4trade.8739.site/             # expect 401
```

If caddy is down after a failed step 6, run this rollback first, then investigate.

### 9. Keep

Keep the backup directory for 14 days, then remove it. It holds a copy of `.env`, so it stays in `/root/backups` (inside the kopia snapshot, which is encrypted).

## If the reconcile is not approved

Steps 2 to 4 are harmless without a recreate. Leaving the literal addon in place is also safe; the drift then stays documented here until the reconcile is approved.
