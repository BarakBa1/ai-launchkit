# Host status file for the System page (B-085, W-36)

The dashboard System page shows host RAM, load and every container's state from one JSON file. The app never talks to
Docker (no socket is mounted). A root cron on the host writes the file; `ai4trade-main` reads it through a read-only bind
mount.

| Item | Value |
| --- | --- |
| Script | `ai4trade/ops/host_status.py`, a copy of `ai-n8n-trading/scripts/host_status.py` (schema version 1; the docstring documents the file). Installed as `/root/ops/host_status.py`. Python 3.11 or later, standard library only. |
| Output | `/var/lib/ai4trade/host-status/status.json`, mode 0644, written atomically |
| Mount | `/var/lib/ai4trade/host-status:/run/host-status:ro` on `ai4trade-main` (`ai4trade/compose.yml`) |
| App setting | `system.host_status_path` = `/run/host-status/status.json`; stale after `system.host_status_stale_s` (600 s) |
| Schedule | every minute, under flock |

Keep the copy in step with the app repo: when `scripts/host_status.py` changes there, copy it here and reinstall.

## Install (VPS, as root)

```bash
scp ai4trade/ops/host_status.py root@$VPS_HOST:/root/ops/host_status.py.new
ssh root@$VPS_HOST 'install -d -m 755 /var/lib/ai4trade/host-status && chmod 700 /root/ops/host_status.py.new && mv /root/ops/host_status.py.new /root/ops/host_status.py && /usr/bin/python3 -I /root/ops/host_status.py'
ssh root@$VPS_HOST 'crontab -l > /root/crontab.before-host-status && (crontab -l; echo "* * * * * /usr/bin/flock -n /run/ai4trade-host-status.lock /usr/bin/python3 -I /root/ops/host_status.py >/dev/null 2>>/var/log/ai4trade-host-status.log") | crontab - && crontab -l | tail -n 1'
```

Only errors reach the log (stdout goes to `/dev/null`), so the log stays small. The mount takes effect when
`ai4trade-main` is next recreated (a deploy recreates it); until then the page shows the file as unavailable.

## Readback

```bash
ssh root@$VPS_HOST 'python3 -c "import json; d=json.load(open(\"/var/lib/ai4trade/host-status/status.json\")); print(d[\"generated_at\"], len(d[\"containers\"]), d[\"errors\"])"'
ssh root@$VPS_HOST 'docker exec ai4trade-main ls -la /run/host-status/'
```

## Rollback

```bash
ssh root@$VPS_HOST 'crontab /root/crontab.before-host-status && rm -f /root/ops/host_status.py && rm -rf /var/lib/ai4trade/host-status'
```

Then remove the mount from `/root/ai4trade/compose.yml` (or restore its dated backup) before the next recreate. With
the mount in the compose file and the directory removed, Docker creates an empty root-owned directory at recreate; the
page then shows the file as unavailable.
