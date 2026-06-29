from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from models import make_lot_record
from shadow_models import ShadowDiscovery, ShadowLotResult, ShadowLotWorkUnit
from shadow_runner import IndexRunner
from shadow_store import ShadowIndexStore
from store import to_iso


def _auction(provider_auction_id: str = "a1") -> dict:
    return {
        "provider_auction_id": provider_auction_id,
        "title": "Auction",
        "url": f"https://example.com/auction/{provider_auction_id}",
        "address": "",
        "city": None,
        "state": None,
        "postal_code": None,
        "country": None,
        "latitude": None,
        "longitude": None,
        "distance_miles": None,
        "raw_payload": {"id": provider_auction_id},
    }


def _lot(provider_lot_id: str, provider_auction_id: str = "a1") -> dict:
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    return make_lot_record(
        source="Fake",
        provider_auction_id=provider_auction_id,
        provider_lot_id=provider_lot_id,
        title=f"Lot {provider_lot_id}",
        end_time=to_iso(now + timedelta(days=1)),
        url=f"https://example.com/lot/{provider_lot_id}",
    )


class FakeProvider:
    source = "Fake"

    def __init__(self, ids: list[str], *, total: int | None = None, failures_before_success: dict[str, int] | None = None):
        self.ids = ids
        self.total = total if total is not None else len(ids)
        self.failures_before_success = failures_before_success or {}
        self.calls: dict[str, int] = {}

    def discover(self) -> ShadowDiscovery:
        units = [
            ShadowLotWorkUnit(
                source=self.source,
                provider_lot_id=lot_id,
                provider_auction_id="a1",
                url=f"https://example.com/lot/{lot_id}",
                payload={"provider_lot_id": lot_id},
            )
            for lot_id in self.ids
        ]
        return ShadowDiscovery(source=self.source, auctions=[_auction()], lot_total=self.total, work_units=units)

    def fetch_lot(self, work_unit: ShadowLotWorkUnit) -> ShadowLotResult:
        lot_id = work_unit.provider_lot_id
        self.calls[lot_id] = self.calls.get(lot_id, 0) + 1
        if self.calls[lot_id] <= self.failures_before_success.get(lot_id, 0):
            raise RuntimeError(f"bad lot {lot_id}")
        return ShadowLotResult(auction=_auction(), lot=_lot(lot_id))


def _count(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def test_shadow_run_records_one_bad_lot_without_failing_run(tmp_path):
    db_path = tmp_path / "shadow.sqlite3"
    store = ShadowIndexStore(db_path)
    provider = FakeProvider(["good", "bad"], failures_before_success={"bad": 10})

    result = IndexRunner(store, [provider], global_workers=2, per_provider_workers=2, max_attempts=3, backoff_seconds=0).run()

    summary = store.run_summary(result["run_id"])
    assert result["status"] == "partial"
    assert summary["indexed_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["retry_count"] == 2
    assert summary["validation_success"] == 1
    assert _count(db_path, "shadow_lots") == 1
    assert _count(db_path, "shadow_index_failures") == 1
    assert _count(db_path, "shadow_lot_attempts") == 4


def test_shadow_failed_lot_retries_up_to_max_attempts(tmp_path):
    store = ShadowIndexStore(tmp_path / "shadow.sqlite3")
    provider = FakeProvider(["bad"], failures_before_success={"bad": 10})

    IndexRunner(store, [provider], max_attempts=3, backoff_seconds=0).run()

    failures = store.list_failures()
    assert provider.calls["bad"] == 3
    assert failures[0]["attempts"] == 3
    assert failures[0]["error_text"] == "bad lot bad"


def test_shadow_successful_retry_clears_final_failure(tmp_path):
    store = ShadowIndexStore(tmp_path / "shadow.sqlite3")
    first_provider = FakeProvider(["flaky"], failures_before_success={"flaky": 10})
    IndexRunner(store, [first_provider], max_attempts=1, backoff_seconds=0).run()
    assert len(store.list_failures()) == 1

    second_provider = FakeProvider([], failures_before_success={})
    result = IndexRunner(store, [second_provider], max_attempts=3, backoff_seconds=0).retry_failures()

    assert result["indexed"] == 1
    assert store.list_failures() == []


def test_shadow_progress_rolls_up_and_caps_at_100(tmp_path):
    store = ShadowIndexStore(tmp_path / "shadow.sqlite3")
    provider = FakeProvider(["1", "2", "3"], total=2)

    result = IndexRunner(store, [provider], global_workers=3, per_provider_workers=3, max_attempts=1, backoff_seconds=0).run()

    summary = store.run_summary(result["run_id"])
    assert summary["discovered_total"] == 2
    assert summary["indexed_count"] == 3
    assert summary["progress_percent"] == 100.0


def test_shadow_validation_fails_when_indexed_plus_failed_misses_discovered_total(tmp_path):
    store = ShadowIndexStore(tmp_path / "shadow.sqlite3")
    provider = FakeProvider(["1"], total=2)

    result = IndexRunner(store, [provider], max_attempts=1, backoff_seconds=0).run()

    summary = store.run_summary(result["run_id"])
    provider_summary = summary["providers"][0]
    assert summary["validation_success"] == 0
    assert provider_summary["validated"] == 0
    assert result["status"] == "partial"


def test_shadow_repository_stores_run_summaries_and_metrics(tmp_path):
    db_path = tmp_path / "shadow.sqlite3"
    store = ShadowIndexStore(db_path)
    provider = FakeProvider(["1"])

    result = IndexRunner(store, [provider], max_attempts=1, backoff_seconds=0).run()
    metrics = store.latest_metrics()

    assert _count(db_path, "shadow_index_runs") == 1
    assert _count(db_path, "shadow_provider_runs") == 1
    assert _count(db_path, "shadow_auctions") == 1
    assert _count(db_path, "shadow_lots") == 1
    assert metrics[("auction_shadow_lots_discovered_total", "Fake")] == 1
    assert metrics[("auction_shadow_lots_indexed_total", "Fake")] == 1
    assert metrics[("auction_shadow_index_validation_success", "Fake")] == 1
    assert metrics[("auction_shadow_index_run_active", "")] == 0
    assert result["status"] == "success"
