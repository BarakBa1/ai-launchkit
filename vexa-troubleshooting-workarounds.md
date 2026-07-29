# Vexa setup and troubleshooting

Vexa is cloned into `vexa/` and runs as a separate Compose project. The main
repository manages it through `scripts/04a_setup_vexa.sh` and
`scripts/05a_init_vexa.sh`.

Never print `.env`, admin tokens, or API tokens while troubleshooting.

## Normal lifecycle

```bash
sudo bash scripts/04a_setup_vexa.sh
sudo bash scripts/05_run_services.sh
sudo bash scripts/05a_init_vexa.sh
```

The setup script runs only when the selected profiles contain `vexa`; it clones
the upstream Vexa repository, initializes submodules, applies the fork's CPU
Whisper/Playwright adjustments, and writes Vexa's managed local environment.
The init script waits for the Admin API, creates the configured user, and
creates or preserves the API token.

## Triage

### Setup reports a missing Vexa directory

1. Confirm `vexa` is selected without displaying the rest of the environment.
2. Confirm the `vexa/` directory and its Git submodules exist.
3. Re-run `04a_setup_vexa.sh`.
4. Start services, then run `05a_init_vexa.sh`.

### API returns 401/403

1. Confirm the Admin API and API gateway containers are healthy.
2. Confirm a user and token record exist using a database query that returns
   counts only—never token values.
3. Confirm n8n uses the current credential through its credential store.
4. If regeneration is necessary, back up configuration, rotate the token, and
   update the n8n credential as one maintenance action.

### Connection refused

1. Check the external Vexa Compose project:

   ```bash
   cd vexa
   docker compose ps
   docker compose logs --tail=200 api-gateway admin-api
   ```

2. Verify host ports from `vexa/.env` without printing values; expected defaults
   are defined by `04a_setup_vexa.sh`.
3. Test the Admin API locally from the host before testing cross-container or
   public access.
4. Verify firewall and routing only after local health passes.

## Recovery order

1. Capture container status and redacted logs.
2. Back up Vexa data/configuration.
3. Retry setup/init idempotently.
4. Rebuild only affected containers.
5. Reset volumes or delete `vexa/` only with explicit approval; those actions
   can destroy users, tokens, and transcription data.

## Verification receipt

Record:

- selected profile;
- Vexa commit/submodule state;
- container health;
- Admin API and gateway HTTP status;
- user/token record counts, never values;
- n8n credential test result;
- backup and rollback location.
