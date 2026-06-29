from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from models import AuctionRecord, LotRecord


@dataclass(frozen=True)
class ShadowLotWorkUnit:
    source: str
    provider_lot_id: str
    provider_auction_id: str | None = None
    url: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ShadowLotResult:
    auction: AuctionRecord
    lot: LotRecord


@dataclass(frozen=True)
class ShadowDiscovery:
    source: str
    auctions: list[AuctionRecord]
    lot_total: int
    work_units: list[ShadowLotWorkUnit]


class AuctionProvider(Protocol):
    source: str

    def discover(self) -> ShadowDiscovery:
        ...

    def fetch_lot(self, work_unit: ShadowLotWorkUnit) -> ShadowLotResult:
        ...


class IndexRepository(Protocol):
    def start_run(self, scope: str) -> int:
        ...

    def finish_run(self, run_id: int, status: str) -> None:
        ...

    def refresh_run_heartbeat(self, run_id: int) -> None:
        ...

    def start_provider_run(self, run_id: int, source: str) -> None:
        ...

    def update_provider_progress(self, run_id: int, source: str, **progress: Any) -> None:
        ...

    def upsert_auction(self, run_id: int, source: str, auction: AuctionRecord) -> None:
        ...

    def upsert_lot(self, run_id: int, source: str, auction: AuctionRecord, lot: LotRecord) -> None:
        ...

    def record_lot_attempt(
        self,
        run_id: int,
        source: str,
        work_unit: ShadowLotWorkUnit,
        attempt_number: int,
        status: str,
        duration_seconds: float,
        error_text: str | None = None,
    ) -> None:
        ...

    def record_failure(
        self,
        run_id: int,
        source: str,
        work_unit: ShadowLotWorkUnit,
        error_text: str,
        attempts: int,
    ) -> None:
        ...

    def clear_failure(self, source: str, provider_lot_id: str) -> None:
        ...

    def list_failures(self, source: str | None = None) -> list[dict[str, Any]]:
        ...

    def run_summary(self, run_id: int) -> dict[str, Any]:
        ...

    def latest_metrics(self) -> dict[str, Any]:
        ...
