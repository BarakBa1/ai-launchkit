"""Host status for the System page (v0.50 H-3, B-085). Run by the host cron on the VPS, never inside a container.

The dashboard reads one JSON file and never talks to Docker (the Docker socket is not mounted into any container). The
host cron runs this script; it writes the file atomically (a temporary file in the same directory, then os.replace), with
mode 0644, and the app mounts its directory read-only. Usage (the host, as root):

    python3 scripts/host_status.py [--output PATH]

Default: --output /var/lib/ai4trade/host-status/status.json (mounted into main at /run/host-status/status.json, the
default of the setting system.host_status_path).

The file (schema version 1; the page reads it in ai4trade/ops/web/pages/_host_status.py):

    {
      "schema_version": 1,
      "generated_at": "2026-11-21T10:00:00Z",                       UTC, written at each run
      "host": {"mem_total_bytes": 16777216000,                      /proc/meminfo MemTotal, bytes (null when unreadable)
               "mem_available_bytes": 9000000000,                   /proc/meminfo MemAvailable, bytes
               "load1": 0.42},                                      /proc/loadavg, 1-minute load
      "containers": [{"name": "main",                               every container from docker ps -a
                      "state": "running",                          docker ps State: running, exited, restarting, ...
                      "health": "healthy",                         docker inspect State.Health.Status, or "none"
                      "started_at": "2026-11-20T10:00:00Z",        docker inspect State.StartedAt; null if never started
                      "restarts": 0}],                             docker inspect RestartCount; null if unknown
      "errors": []                                                 what this run could not read, in words
    }

The host's maintenance results (the nightly dump, the restore test) are not in this file: they are state rows in the
database (backup.dump_ok, backup.restore_ok), so this script reads no job directory.

The page treats the file as unavailable when it is missing, not readable or not valid (its generated_at is not a time),
and as stale when generated_at is more than host_status_stale_s old, or more than host_status_future_s ahead.

Docker is called with two commands only: `docker ps -a --format '{{json .}}'` and `docker inspect <ids>`. A failure is
written into "errors" and the file is still written (containers then come out empty), so the page shows why.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT = Path("/var/lib/ai4trade/host-status/status.json")
MEMINFO = Path("/proc/meminfo")
LOADAVG = Path("/proc/loadavg")
DOCKER_TIMEOUT_S = 60
SCHEMA_VERSION = 1
_DOCKER_NEVER_STARTED = "0001-"  # docker reports a container that never started as 0001-01-01T00:00:00Z
_MAX_TEXT = 500

Runner = Callable[[Sequence[str]], str]


class DockerError(RuntimeError):
    """A docker command failed or could not run; the message is its stderr (or the reason it did not run)."""


def run_docker(args: Sequence[str]) -> str:
    """Run ``docker <args>`` and return its stdout. Raises DockerError on a non-zero exit or when docker cannot run."""
    try:
        done = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerError(str(exc)) from exc
    if done.returncode != 0:
        raise DockerError((done.stderr or "").strip()[:_MAX_TEXT] or f"exit status {done.returncode}")
    return done.stdout


def parse_meminfo(text: str) -> dict[str, int | None]:
    """MemTotal and MemAvailable of /proc/meminfo, in bytes (the file reports kB). A missing line gives None."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) == 2 and parts[1] == "kB" and parts[0].isdigit():
            values[name.strip()] = int(parts[0]) * 1024
    return {"mem_total_bytes": values.get("MemTotal"), "mem_available_bytes": values.get("MemAvailable")}


def parse_loadavg(text: str) -> float | None:
    """The 1-minute load: the first field of /proc/loadavg. None when it is not a number."""
    parts = text.split()
    try:
        return float(parts[0]) if parts else None
    except ValueError:
        return None


def parse_ps(text: str) -> list[dict[str, str]]:
    """One row per line of ``docker ps -a --format '{{json .}}'``: the short id, the first name and the state.
    Raises ValueError when a line is not a JSON object."""
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("docker ps printed a line that is not a JSON object")
        rows.append(
            {
                "id": str(item.get("ID", "")),
                "name": str(item.get("Names", "")).split(",")[0].strip(),
                "state": str(item.get("State", "")).strip().lower(),
            }
        )
    return [row for row in rows if row["id"]]


def parse_inspect(text: str) -> list[dict[str, Any]]:
    """The JSON array of ``docker inspect <ids>``. Raises ValueError when it is not an array."""
    if not text.strip():
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("docker inspect did not print a JSON array")
    return [item for item in data if isinstance(item, dict)]


def normalise_time(value: object) -> str | None:
    """An ISO 8601 instant with a zone (docker's RFC 3339 times) as UTC, cut to whole seconds and written ``...Z``. None
    when it is not a time, has no zone, or is docker's never-started time."""
    if not isinstance(value, str) or value.startswith(_DOCKER_NEVER_STARTED):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OverflowError:
        return None


def merge_containers(ps_rows: list[dict[str, str]], inspected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join each ``docker ps`` row to its ``docker inspect`` object (the inspect id starts with the short id). A row
    without an inspect object keeps its state and gets health none and no start or restart figures."""
    containers: list[dict[str, Any]] = []
    for row in ps_rows:
        match = next((d for d in inspected if str(d.get("Id", "")).startswith(row["id"])), None)
        state = match.get("State") if match else None
        state = state if isinstance(state, dict) else {}
        health = state.get("Health")
        health_status = health.get("Status") if isinstance(health, dict) else None
        restarts = match.get("RestartCount") if match else None
        if not row["name"] and match:
            row["name"] = str(match.get("Name", "")).lstrip("/")
        containers.append(
            {
                "name": row["name"] or row["id"],
                "state": row["state"] or str(state.get("Status", "")).lower() or "unknown",
                "health": str(health_status or "none"),
                "started_at": normalise_time(state.get("StartedAt")),
                "restarts": restarts
                if isinstance(restarts, int) and not isinstance(restarts, bool)
                else None,
            }
        )
    return containers


def collect_containers(run: Runner = run_docker) -> tuple[list[dict[str, Any]], list[str]]:
    """Every container with its health, and the problems met on the way. A failed ``docker ps`` gives no containers."""
    try:
        ps_rows = parse_ps(run(["ps", "-a", "--format", "{{json .}}"]))
    except (DockerError, ValueError) as exc:
        return [], [f"docker ps failed: {exc}"[:_MAX_TEXT]]
    if not ps_rows:
        return [], []
    try:
        inspected = parse_inspect(run(["inspect", *(row["id"] for row in ps_rows)]))
    except (DockerError, ValueError) as exc:
        return merge_containers(ps_rows, []), [f"docker inspect failed: {exc}"[:_MAX_TEXT]]
    return merge_containers(ps_rows, inspected), []


def build_status(
    *,
    now: datetime,
    meminfo: str | None,
    loadavg: str | None,
    containers: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    """The whole status document (schema version 1, see the module docstring)."""
    host: dict[str, Any] = {"mem_total_bytes": None, "mem_available_bytes": None, "load1": None}
    if meminfo is not None:
        host.update(parse_meminfo(meminfo))
    if loadavg is not None:
        host["load1"] = parse_loadavg(loadavg)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": host,
        "containers": containers,
        "errors": errors,
    }


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write the JSON to a temporary file in the same directory, flush it to disk, then rename it over ``path``. A
    reader sees the old file or the new one, never a part. The mode is 0644: the app's user reads it through the mount."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_or_error(path: Path, errors: list[str], label: str) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{label} unreadable: {exc.strerror or exc}"[:_MAX_TEXT])
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the host status file for the AI4TRADE System page.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="the status file to write")
    parser.add_argument("--meminfo", type=Path, default=MEMINFO, help=argparse.SUPPRESS)
    parser.add_argument("--loadavg", type=Path, default=LOADAVG, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    errors: list[str] = []
    meminfo = _read_or_error(args.meminfo, errors, "meminfo")
    loadavg = _read_or_error(args.loadavg, errors, "loadavg")
    containers, container_errors = collect_containers()
    errors.extend(container_errors)
    payload = build_status(
        now=datetime.now(UTC),
        meminfo=meminfo,
        loadavg=loadavg,
        containers=containers,
        errors=errors,
    )
    write_atomic(args.output, payload)
    print(f"host status written: {len(containers)} containers, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
