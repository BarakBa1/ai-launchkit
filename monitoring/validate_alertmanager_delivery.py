#!/usr/bin/env python3
"""Fail closed when the monitoring profile lacks its Slack receiver secret."""

import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit


MAX_RECEIVER_FILE_BYTES = 4096
RECEIVER_GID = 65534
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def is_secure_receiver_file(path: Path) -> bool:
    """Require a bounded, external, regular file with container-readable Unix metadata."""

    if not path.is_absolute():
        return False
    try:
        path.resolve(strict=False).relative_to(REPOSITORY_ROOT.resolve())
        return False
    except ValueError:
        pass
    except (OSError, RuntimeError):
        return False

    try:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            return False
        if os.name == "posix":
            mode = stat.S_IMODE(file_stat.st_mode)
            if file_stat.st_uid != 0 or file_stat.st_gid != RECEIVER_GID:
                return False
            if mode & ~0o640 or mode & 0o440 != 0o440:
                return False
        return os.access(path, os.R_OK)
    except OSError:
        return False


def main() -> int:
    profiles = {item.strip() for item in os.environ.get("COMPOSE_PROFILES", "").split(",")}
    if "monitoring" not in profiles:
        return 0

    secret_file = os.environ.get("ALERTMANAGER_SLACK_WEBHOOK_FILE", "").strip()
    if not secret_file:
        print(
            "Monitoring alert delivery is enabled but "
            "ALERTMANAGER_SLACK_WEBHOOK_FILE is not set.",
            file=sys.stderr,
        )
        return 1

    path = Path(secret_file)
    try:
        valid = (
            is_secure_receiver_file(path)
            and 0 < path.stat().st_size <= MAX_RECEIVER_FILE_BYTES
        )
        if valid:
            receiver = path.read_text(encoding="utf-8").strip()
            parsed = urlsplit(receiver)
            valid = parsed.scheme.lower() == "https" and bool(parsed.netloc)
    except (OSError, UnicodeError):
        valid = False
    if not valid:
        print(
            "Monitoring alert delivery is enabled but "
            "ALERTMANAGER_SLACK_WEBHOOK_FILE is missing, empty, oversized, "
            "or not an HTTPS URL.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
