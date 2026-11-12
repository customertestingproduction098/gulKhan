"""Ping the deployed backend so Render's free instance does not spin down.

Render stops free web services after ~15 minutes without inbound HTTP traffic.
A scheduler running *inside* the backend cannot prevent that: once the service
sleeps, the scheduler sleeps with it. This script is meant to run from an
external runner (the GitHub Actions workflow in this repo, an uptime monitor,
or any always-on host).
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("KEEP_ALIVE_URL", "https://gulkhan.onrender.com/api/health")
ATTEMPTS = max(1, int(os.environ.get("KEEP_ALIVE_ATTEMPTS", "3")))
TIMEOUT = float(os.environ.get("KEEP_ALIVE_TIMEOUT", "100"))
RETRY_DELAY = float(os.environ.get("KEEP_ALIVE_RETRY_DELAY", "10"))


def ping() -> bool:
    request = urllib.request.Request(
        URL,
        headers={"User-Agent": "sentinel-keep-alive/1.0", "Accept": "application/json"},
        method="GET",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            status = response.status
            response.read(1024)
    except urllib.error.HTTPError as error:
        status = error.code
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"attempt failed after {time.monotonic() - started:.1f}s: {error}")
        return False

    elapsed = time.monotonic() - started
    print(f"{URL} -> HTTP {status} in {elapsed:.1f}s")
    return 200 <= status < 300


def main() -> int:
    for attempt in range(1, ATTEMPTS + 1):
        if ping():
            return 0
        if attempt < ATTEMPTS:
            print(f"retrying in {RETRY_DELAY:.0f}s ({attempt}/{ATTEMPTS})")
            time.sleep(RETRY_DELAY)
    print("keep-alive failed: backend did not respond successfully")
    return 1


if __name__ == "__main__":
    sys.exit(main())
