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
    rows = store.query_results("gate", now=now)
    assert len(rows) == 1
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
    rows = store.query_results("gate", now=now)
    assert len(rows) == 1
