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
    results, total = store.query_results("baby stair gate", now=now)
    assert len(results) == 1
    assert total == 1
    assert results[0]["lot_title"] == "Baby Stair Gate"


def test_query_results_extracts_relative_image_urls(tmp_path):
    store = AuctionStore(tmp_path / "index.sqlite3")
    store.upsert_source_status("HiBid", "success", "2026-04-18T00:00:00+00:00", "2026-04-18T00:00:00+00:00", None)
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    run_id = store.start_index_run("manual", to_iso(now))
    store.upsert_snapshot(
        "HiBid",
        run_id,
        to_iso(now),
        [_auction("a1")],
        [
            make_lot_record(
                source="HiBid",
                provider_auction_id="a1",
                provider_lot_id="l1",
                title="Gate",
                end_time=to_iso(now + timedelta(days=1)),
                url="https://example.com/lot/1",
                raw_payload={"images": [{"url": "/images/gate.jpg"}]},
            )
        ],
    )
    store.prune_source_rows("HiBid", run_id, to_iso(now + timedelta(days=7)))
    results, total = store.query_results("gate", now=now)
    assert total == 1
    assert results[0]["imageUrl"] == "https://example.com/images/gate.jpg"


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

    results, total = store.query_results("gate", now=now)
    assert results == []
    assert total == 0


def test_query_results_returns_all_for_empty_query(tmp_path):
    store = AuctionStore(tmp_path / "index.sqlite3")
    store.upsert_source_status("HiBid", "success", "2026-04-18T00:00:00+00:00", "2026-04-18T00:00:00+00:00", None)
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    run_id = store.start_index_run("manual", to_iso(now))
    store.upsert_snapshot(
        "HiBid",
        run_id,
        to_iso(now),
        [_auction("a1")],
        [
            make_lot_record(
                source="HiBid",
                provider_auction_id="a1",
                provider_lot_id="l1",
                title="Gate",
                end_time=to_iso(now + timedelta(days=1)),
                url="https://example.com/lot/1",
            )
        ],
    )
    store.prune_source_rows("HiBid", run_id, to_iso(now + timedelta(days=7)))
    results, total = store.query_results("", now=now)
    assert total == 1
    assert len(results) == 1


def test_query_results_filters_by_source_and_ending_window(tmp_path):
    store = AuctionStore(tmp_path / "index.sqlite3")
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    started_at = to_iso(now)
    store.upsert_source_status("HiBid", "success", started_at, started_at, None)
    store.upsert_source_status("403 Auction", "success", started_at, started_at, None)
    hibid_run = store.start_index_run("manual", started_at)
    store.upsert_snapshot(
        "HiBid",
        hibid_run,
        started_at,
        [_auction("h1", title="HiBid Auction")],
        [
            make_lot_record(
                source="HiBid",
                provider_auction_id="h1",
                provider_lot_id="l1",
                title="Near Gate",
                end_time=to_iso(now + timedelta(hours=3)),
                url="https://example.com/lot/hibid",
            )
        ],
    )
    store.prune_source_rows("HiBid", hibid_run, to_iso(now + timedelta(days=7)))
    auction403_run = store.start_index_run("manual", started_at)
    store.upsert_snapshot(
        "403 Auction",
        auction403_run,
        started_at,
        [_auction("a1", title="403 Auction")],
        [
            make_lot_record(
                source="403 Auction",
                provider_auction_id="a1",
                provider_lot_id="l2",
                title="Far Gate",
                end_time=to_iso(now + timedelta(days=3)),
                url="https://example.com/lot/403",
            )
        ],
    )
    store.prune_source_rows("403 Auction", auction403_run, to_iso(now + timedelta(days=7)))

    results, total = store.query_results("", now=now, sources=["HiBid"], ending_within_hours=6)
    assert total == 1
    assert results[0]["source"] == "HiBid"


def test_metadata_reflects_last_run(tmp_path):
    store = AuctionStore(tmp_path / "index.sqlite3")
    run_id = store.start_index_run("manual", "2026-04-18T00:00:00+00:00")
    store.finish_index_run(
        run_id,
        "2026-04-18T00:10:00+00:00",
        {"HiBid": {"status": "success", "auctions": 2, "lots": 5}},
        "1/1 sources indexed",
        None,
    )
    metadata = store.get_metadata()
    assert metadata.indexed_at == "2026-04-18T00:10:00+00:00"
    assert metadata.last_run_status == "success"
    assert metadata.indexed_source_count == 1
    assert metadata.indexed_auction_count == 2
    assert metadata.indexed_lot_count == 5
    assert metadata.last_run_duration_seconds == 600.0


def test_format_time_left():
    now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    assert format_time_left("2026-04-18T14:30:00+00:00", now) == "2h 30m"
    assert format_time_left("2026-04-19T15:00:00+00:00", now) == "1d 3h"
    assert format_time_left("2026-04-18T11:00:00+00:00", now) == "Ended"
