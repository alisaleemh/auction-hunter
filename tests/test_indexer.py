from datetime import datetime, timedelta, timezone

from indexer import run_index
from models import ProviderSnapshot, make_lot_record
from store import AuctionStore, to_iso


def _auction(provider_auction_id: str, *, title: str = "Auction") -> dict:
    return {
        "provider_auction_id": provider_auction_id,
        "title": title,
        "url": "https://example.com/auction",
        "address": "80 Westcreek Blvd, Unit 2, Brampton, Ontario L6T0B8",
        "city": "Brampton",
        "state": "Ontario",
        "postal_code": "L6T0B8",
        "country": "Canada",
        "latitude": None,
        "longitude": None,
        "distance_miles": 12.0,
        "raw_payload": {"id": provider_auction_id},
    }


def test_run_index_filters_to_seven_days(tmp_path):
    store = AuctionStore(tmp_path / "index.sqlite3")
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)

    def loader():
        return ProviderSnapshot(
            source="HiBid",
            auctions=[_auction("a1"), _auction("a2")],
            lots=[
                make_lot_record(source="HiBid", provider_auction_id="a1", provider_lot_id="l1", title="Gate", end_time=to_iso(now + timedelta(days=2)), url="https://example.com/lot/1"),
                make_lot_record(source="HiBid", provider_auction_id="a2", provider_lot_id="l2", title="Far Future Gate", end_time=to_iso(now + timedelta(days=10)), url="https://example.com/lot/2"),
            ],
        )

    result = run_index(store, now=now, provider_loaders={"HiBid": loader})
    assert result["errors"] == []
    rows, total = store.query_results("gate", now=now)
    assert len(rows) == 1
    assert total == 1
    assert rows[0]["lot_title"] == "Gate"


def test_failed_source_does_not_corrupt_prior_rows(tmp_path):
    store = AuctionStore(tmp_path / "index.sqlite3")
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)

    def success():
        return ProviderSnapshot(
            source="HiBid",
            auctions=[_auction("a1")],
            lots=[make_lot_record(source="HiBid", provider_auction_id="a1", provider_lot_id="l1", title="Gate", end_time=to_iso(now + timedelta(days=2)), url="https://example.com/lot/1")],
        )

    run_index(store, now=now, provider_loaders={"HiBid": success})

    def failure():
        raise RuntimeError("boom")

    result = run_index(store, now=now + timedelta(hours=1), provider_loaders={"HiBid": failure})
    assert result["errors"] == ["HiBid: boom"]
    rows, total = store.query_results("gate", now=now)
    assert len(rows) == 1
    assert total == 1


class FakeIndexRepository:
    def __init__(self):
        self.snapshots = []
        self.pruned = []
        self.source_statuses = []
        self.progress_updates = []
        self.finished_runs = []

    def get_sources(self):
        return [{"name": "HiBid", "config": {}}]

    def start_index_run(self, scope: str, started_at: str) -> int:
        self.scope = scope
        self.started_at = started_at
        return 42

    def refresh_index_run_heartbeat(self, run_id: int) -> None:
        self.heartbeat_run_id = run_id

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
        self.progress_updates.append(
            {
                "run_id": run_id,
                "progress_total": progress_total,
                "progress_done": progress_done,
                "progress_percent": progress_percent,
                "progress_message": progress_message,
                "source_progress": source_progress,
            }
        )

    def finish_index_run(
        self,
        run_id: int,
        finished_at: str,
        source_stats: dict,
        success_summary: str,
        error_text: str | None,
    ) -> None:
        self.finished_runs.append(
            {
                "run_id": run_id,
                "finished_at": finished_at,
                "source_stats": source_stats,
                "success_summary": success_summary,
                "error_text": error_text,
            }
        )

    def upsert_source_status(
        self,
        source_name: str,
        status: str,
        started_at: str,
        finished_at: str | None,
        error_text: str | None,
    ) -> int:
        self.source_statuses.append((source_name, status, started_at, finished_at, error_text))
        return 7

    def upsert_snapshot(self, source_name: str, run_id: int, indexed_at: str, auctions, lots) -> dict:
        auction_rows = list(auctions)
        lot_rows = list(lots)
        self.snapshots.append((source_name, run_id, indexed_at, auction_rows, lot_rows))
        return {"auctions": len(auction_rows), "lots": len(lot_rows)}

    def prune_source_rows(self, source_name: str, run_id: int, window_end: str) -> None:
        self.pruned.append((source_name, run_id, window_end))


def test_run_index_uses_repository_interface():
    repo = FakeIndexRepository()
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)

    def loader():
        return ProviderSnapshot(
            source="HiBid",
            auctions=[_auction("a1")],
            lots=[
                make_lot_record(
                    source="HiBid",
                    provider_auction_id="a1",
                    provider_lot_id="l1",
                    title="Gate",
                    end_time=to_iso(now + timedelta(days=2)),
                    url="https://example.com/lot/1",
                )
            ],
        )

    result = run_index(repo, scope="manual", now=now, provider_loaders={"HiBid": loader})

    assert result["errors"] == []
    assert repo.snapshots[0][0] == "HiBid"
    assert repo.snapshots[0][3][0]["provider_auction_id"] == "a1"
    assert repo.snapshots[0][4][0]["provider_lot_id"] == "l1"
    assert repo.pruned[0][0] == "HiBid"
    assert repo.finished_runs[0]["success_summary"] == "1/1 sources indexed"
