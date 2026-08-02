"""Wait for the BWiki WAF block to cool down, then run the real-data refresh.

Operational helper (not part of the pipeline): BWiki's WAF flagged this
machine's IP after the rehearsal bursts (HTTP 567 hard block). This script
probes the wiki index periodically, and once a real page comes back it runs
``refresh_roco_data.py --force --no-cards --delay-seconds 2`` with clean env
(default gate thresholds), then regenerates cards for the change set.

Exit codes:
  0  refresh published (cards redrawn for the change set)
  2  refresh ran but failed gates/errors (report written; inspect it)
  3  wiki still blocked after MAX_PROBES probes (re-run later)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
INDEX_URL = "https://wiki.biligame.com/rocom/%E7%B2%BE%E7%81%B5%E7%AD%9B%E9%80%89"
PROBE_INTERVAL_SECONDS = 5 * 60
MAX_PROBES = 48  # ~4 hours of waiting
PROBE_TIMEOUT = 30

# Keep the WAF-detection logic identical to the pipeline's definition.
_CHALLENGE_MARKERS = ("<title></title>", "box-sizing:border-box")


def _is_unblocked(body: str) -> bool:
    """A real index page is large and has a real title; a WAF response is not."""
    return len(body) > 100_000 and not all(marker in body for marker in _CHALLENGE_MARKERS)


def probe_wiki() -> bool:
    try:
        request = Request(
            INDEX_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urlopen(request, timeout=PROBE_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
            return _is_unblocked(body)
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _clean_env() -> dict[str, str]:
    """Default gate thresholds: drop any rehearsal-permissive DATA_* exports."""
    env = dict(os.environ)
    for key in [
        "DATA_MIN_RECORDS",
        "DATA_MAX_RECORD_DROP",
        "DATA_MAX_NEW_NUMBER_GAPS",
        "DATA_MIN_STATS_COMPLETE_RATE",
        "DATA_MIN_TOTAL_RACE_RATE",
        "DATA_MAX_DANGLING_EDGES",
        "DATA_MAX_SKILL_KEY_MISSING_RATE",
    ]:
        env.pop(key, None)
    return env


def run_refresh() -> int:
    refresh = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "refresh_roco_data.py"),
            "--force",
            "--no-cards",
            "--delay-seconds",
            "2",
        ],
        cwd=ROOT,
        env=_clean_env(),
        timeout=3600,
    )
    if refresh.returncode != 0:
        return 2
    cards = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_roco_pet_cards.py"),
            "--change-set",
            str(ROOT / "data" / "manifests" / "change_set.json"),
        ],
        cwd=ROOT,
        env=_clean_env(),
        timeout=3600,
    )
    return 0 if cards.returncode == 0 else 2


def main() -> int:
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"[retry-refresh] started {started}; probing every {PROBE_INTERVAL_SECONDS}s, up to {MAX_PROBES} probes",
        flush=True,
    )
    for attempt in range(1, MAX_PROBES + 1):
        print(
            f"[retry-refresh] probe {attempt}/{MAX_PROBES} at {time.strftime('%H:%M:%S')}",
            flush=True,
        )
        if probe_wiki():
            print("[retry-refresh] wiki is unblocked; starting refresh", flush=True)
            code = run_refresh()
            print(f"[retry-refresh] refresh finished with exit {code}", flush=True)
            return code
        time.sleep(PROBE_INTERVAL_SECONDS)
    print(
        f"[retry-refresh] wiki still blocked after {MAX_PROBES} probes; re-run later",
        flush=True,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
