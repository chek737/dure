"""ingest_coordinator: pull repos.txt entries and (re)index changed chunks.

TODO: port camp-60's ingest.py (10-minute cron pull+incremental index) here.
"""
from __future__ import annotations

import time
from typing import Optional

POLL_INTERVAL_SECONDS = 600


def _ingest_once() -> None:
    # TODO: git pull each repo in repos.txt, diff, re-embed changed chunks
    pass


def run(port: Optional[int] = None) -> None:
    while True:
        _ingest_once()
        time.sleep(POLL_INTERVAL_SECONDS)
