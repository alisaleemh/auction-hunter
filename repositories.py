from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from models import AuctionRecord, LotRecord


class AuctionIndexRepository(Protocol):
    """Persistence boundary used by the indexing layer.

    Indexing should depend on this interface, not on the SQLite implementation.
    AuctionStore is the current implementation.
    """

    def get_sources(self) -> list[dict]:
        ...

    def start_index_run(self, scope: str, started_at: str) -> int:
        ...

    def refresh_index_run_heartbeat(self, run_id: int) -> None:
        ...

    def update_index_run_progress(
        self,
        run_id: int,
        *,
        progress_total: int | None,
        progress_done: int | None,
        progress_percent: float | None,
        progress_message: str | None,
        source_progress: dict[str, dict] | None = None,
    ) -> None:
        ...

    def finish_index_run(
        self,
        run_id: int,
        finished_at: str,
        source_stats: dict,
        success_summary: str,
        error_text: str | None,
    ) -> None:
        ...

    def upsert_source_status(
        self,
        source_name: str,
        status: str,
        started_at: str,
        finished_at: str | None,
        error_text: str | None,
    ) -> int:
        ...

    def upsert_snapshot(
        self,
        source_name: str,
        run_id: int,
        indexed_at: str,
        auctions: Iterable[AuctionRecord],
        lots: Iterable[LotRecord],
    ) -> dict:
        ...

    def prune_source_rows(self, source_name: str, run_id: int, window_end: str) -> None:
        ...
