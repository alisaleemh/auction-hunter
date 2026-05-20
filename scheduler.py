from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from indexer import run_index
from store import AuctionStore


@dataclass(frozen=True)
class NightlySchedule:
    hour: int = 2
    minute: int = 0
    poll_seconds: int = 300


def should_run_nightly(now: datetime, last_success: datetime | None, schedule: NightlySchedule) -> bool:
    current = now
    target = current.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
    if current < target:
        return False
    if last_success is None:
        return True
    return last_success < target


class NightlyIndexer:
    def __init__(self, store: AuctionStore, schedule: NightlySchedule | None = None):
        self.store = store
        self.schedule = schedule or NightlySchedule()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="auction-index-nightly", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            last_success = self.store.last_success_for_scope("nightly")
            if should_run_nightly(now, last_success, self.schedule):
                run_index(self.store, scope="nightly", now=now)
            self._stop.wait(self.schedule.poll_seconds)
