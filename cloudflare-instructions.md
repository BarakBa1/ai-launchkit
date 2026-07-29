# Cloudflare Tunnel

Optional public-access pattern for hosts that should not expose ports 80/443
directly.

## Architecture warning

A tunnel route can bypass Caddy. When it points directly at a service, Caddy
authentication, headers, rate limits, and other middleware do not apply. Add
equivalent Cloudflare Access/service authentication before publishing admin or
monitoring surfaces.

## Setup

1. Create a Cloudflare Tunnel in the Zero Trust dashboard.
2. Store the tunnel token in the runtime secret environment; never commit or
   print it.
3. Select the `cloudflare-tunnel` profile through the supported installer or
   environment workflow.
4. Add only required public hostnames. Derive current internal service names and
   ports from `docker-compose.yml`; do not copy a stale service table.
5. Protect sensitive routes with Cloudflare Access and service-native
   authentication.
6. Start the profile and verify DNS, TLS, authentication, and service health.

## Verification

```bash
docker compose --profile cloudflare-tunnel ps
docker compose logs --tail=200 cloudflared
```

For every published hostname verify:

- unauthenticated access is denied where required;
- the expected service—not another container—answers;
- health checks pass internally;
- origin ports are closed when the tunnel is the sole ingress path;
- Caddy-bypassed security controls are replaced explicitly.

## Rollback

Disable public hostnames, stop the tunnel profile, confirm DNS no longer routes
to the tunnel, and restore the prior ingress/firewall state from the captured
baseline. Do not remove Caddy or firewall configuration until rollback has been
tested.
