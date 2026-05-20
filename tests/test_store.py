from datetime import datetime, timedelta, timezone

from models import make_lot_record
from store import AuctionStore, format_time_left, to_iso


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


def test_upsert_and_query_results(tmp_path):
    store = AuctionStore(tmp_path / "index.sqlite3")
    store.upsert_source_status("HiBid", "success", "2026-04-18T00:00:00+00:00", "2026-04-18T00:00:00+00:00", None)
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    run_id = store.start_index_run("manual", to_iso(now))
    store.upsert_snapshot(
        "HiBid",
        run_id,
        to_iso(now),
        [_auction("a1", title="Baby Goods Auction")],
        [
            make_lot_record(
                source="HiBid",
                provider_auction_id="a1",
                provider_lot_id="l1",
                title="Baby Stair Gate",
                lot_number="4",
                condition="Used",
                description="Pressure mount gate",
                end_time=to_iso(now + timedelta(days=1)),
                url="https://example.com/lot/1",
                raw_payload={"id": "l1"},
            )
        ],
    )
    store.prune_source_rows("HiBid", run_id, to_iso(now + timedelta(days=7)))
    results = store.query_results("baby stair gate", now=now)
    assert len(results) == 1
    assert results[0]["lot_title"] == "Baby Stair Gate"


def test_prune_stale_rows(tmp_path):
    store = AuctionStore(tmp_path / "index.sqlite3")
    store.upsert_source_status("HiBid", "success", "2026-04-18T00:00:00+00:00", "2026-04-18T00:00:00+00:00", None)
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    first_run = store.start_index_run("manual", to_iso(now))
    store.upsert_snapshot(
        "HiBid",
        first_run,
        to_iso(now),
        [_auction("a1")],
        [make_lot_record(source="HiBid", provider_auction_id="a1", provider_lot_id="l1", title="Gate", end_time=to_iso(now + timedelta(days=1)), url="https://example.com/lot/1")],
    )
    store.prune_source_rows("HiBid", first_run, to_iso(now + timedelta(days=7)))

    second_run = store.start_index_run("manual", to_iso(now + timedelta(hours=1)))
    store.upsert_snapshot("HiBid", second_run, to_iso(now + timedelta(hours=1)), [], [])
    store.prune_source_rows("HiBid", second_run, to_iso(now + timedelta(days=7)))

    assert store.query_results("gate", now=now) == []


def test_metadata_reflects_last_run(tmp_path):
    store = AuctionStore(tmp_path / "index.sqlite3")
    run_id = store.start_index_run("manual", "2026-04-18T00:00:00+00:00")
    store.finish_index_run(run_id, "2026-04-18T00:10:00+00:00", {"HiBid": {"status": "success"}}, "1/1 sources indexed", None)
    metadata = store.get_metadata()
    assert metadata.indexed_at == "2026-04-18T00:10:00+00:00"
    assert metadata.last_run_status == "success"


def test_format_time_left():
    now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    assert format_time_left("2026-04-18T14:30:00+00:00", now) == "2h 30m"
    assert format_time_left("2026-04-19T15:00:00+00:00", now) == "1d 3h"
    assert format_time_left("2026-04-18T11:00:00+00:00", now) == "Ended"
