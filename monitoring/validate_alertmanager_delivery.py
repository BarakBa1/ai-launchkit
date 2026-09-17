#!/usr/bin/env python3
"""Fail closed when the monitoring profile lacks its Slack receiver secret."""

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


MAX_RECEIVER_FILE_BYTES = 4096


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
            path.is_file()
            and os.access(path, os.R_OK)
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
