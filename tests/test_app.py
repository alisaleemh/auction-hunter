from datetime import datetime, timedelta, timezone

import app as auction_app
from models import make_lot_record
from store import AuctionStore


def _seed_store(store: AuctionStore):
    now = datetime.now(timezone.utc)
    started_at = now.isoformat()
    store.upsert_source_status("HiBid", "success", started_at, started_at, None)
    run_id = store.start_index_run("manual", started_at)
    store.upsert_snapshot(
        "HiBid",
        run_id,
        started_at,
        [
            {
                "provider_auction_id": "a1",
                "title": "Auction",
                "url": "https://example.com/auction",
                "address": "20 Automatic Rd, Brampton, ON",
                "city": "Brampton",
                "state": "ON",
                "postal_code": "L6S 5N6",
                "country": "Canada",
                "latitude": None,
                "longitude": None,
                "distance_miles": 3.2,
                "raw_payload": {"id": "a1"},
            }
        ],
        [
            make_lot_record(
                source="HiBid",
                provider_auction_id="a1",
                provider_lot_id="l1",
                title="Baby Gate",
                lot_number="12",
                condition="Open Box",
                description="A gate",
                current_bid=5,
                shipping_available=True,
                end_time=(now + timedelta(days=2, hours=2)).isoformat(),
                url="https://example.com/lot/12",
                raw_payload={"id": "l1", "imageUrl": "https://example.com/image.jpg"},
            )
        ],
    )
    store.prune_source_rows("HiBid", run_id, (now + timedelta(days=7)).isoformat())
    store.finish_index_run(
        run_id,
        (now + timedelta(minutes=5)).isoformat(),
        {"HiBid": {"status": "success", "auctions": 1, "lots": 1}},
        "1/1 sources indexed",
        None,
    )


def _seed_store_with_extra_lot(store: AuctionStore):
    _seed_store(store)
    now = datetime.now(timezone.utc)
    started_at = (now + timedelta(minutes=1)).isoformat()
    run_id = store.start_index_run("manual", started_at)
    store.upsert_snapshot(
        "HiBid",
        run_id,
        started_at,
        [
            {
                "provider_auction_id": "a1",
                "title": "Auction",
                "url": "https://example.com/auction",
                "address": "20 Automatic Rd, Brampton, ON",
                "city": "Brampton",
                "state": "ON",
                "postal_code": "L6S 5N6",
                "country": "Canada",
                "latitude": None,
                "longitude": None,
                "distance_miles": 3.2,
                "raw_payload": {"id": "a1"},
            }
        ],
        [
            make_lot_record(
                source="HiBid",
                provider_auction_id="a1",
                provider_lot_id="l2",
                title="Baby Gate Deluxe",
                lot_number="13",
                condition="Open Box",
                description="A better gate",
                current_bid=12,
                shipping_available=False,
                end_time=(now + timedelta(days=2, hours=1)).isoformat(),
                url="https://example.com/lot/13",
            )
        ],
    )
    store.prune_source_rows("HiBid", run_id, (now + timedelta(days=7)).isoformat())
    store.finish_index_run(
        run_id,
        (now + timedelta(minutes=6)).isoformat(),
        {"HiBid": {"status": "success", "auctions": 1, "lots": 1}},
        "1/1 sources indexed",
        None,
    )


def test_get_root_empty_query(tmp_path, monkeypatch):
    test_store = AuctionStore(tmp_path / "index.sqlite3")
    monkeypatch.setattr(auction_app, "store", test_store)
    client = auction_app.app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"All indexed lots" in response.data


def test_get_root_renders_indexed_results(tmp_path, monkeypatch):
    test_store = AuctionStore(tmp_path / "index.sqlite3")
    _seed_store(test_store)
    monkeypatch.setattr(auction_app, "store", test_store)
    client = auction_app.app.test_client()
    response = client.get("/?q=gate")
    assert response.status_code == 200
    assert b"Baby Gate" in response.data
    assert b"Last indexed:" in response.data


def test_api_search_returns_indexed_shape(tmp_path, monkeypatch):
    test_store = AuctionStore(tmp_path / "index.sqlite3")
    _seed_store(test_store)
    monkeypatch.setattr(auction_app, "store", test_store)
    client = auction_app.app.test_client()
    response = client.get("/api/search?q=gate")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["count"] == 1
    assert payload["total"] == 1
    assert payload["results"][0]["lot_title"] == "Baby Gate"
    assert payload["results"][0]["sourceAuction"] == "Auction"
    assert payload["results"][0]["productUrl"] == "https://example.com/lot/12"
    assert payload["results"][0]["currentPrice"] == 5
    assert payload["results"][0]["imageUrl"] == "https://example.com/image.jpg"
    assert payload["indexed_at"]
    assert payload["indexed_lot_count"] == 1
    assert payload["indexed_auction_count"] == 1
    assert "time_left" in payload["results"][0]


def test_api_search_supports_sort_and_limit(tmp_path, monkeypatch):
    test_store = AuctionStore(tmp_path / "index.sqlite3")
    _seed_store_with_extra_lot(test_store)
    monkeypatch.setattr(auction_app, "store", test_store)
    client = auction_app.app.test_client()
    response = client.get("/api/search?q=gate&sort=price_low_high&limit=1")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["count"] == 1
    assert payload["results"][0]["lot_title"] == "Baby Gate"


def test_api_search_browses_all_lots_when_query_empty(tmp_path, monkeypatch):
    test_store = AuctionStore(tmp_path / "index.sqlite3")
    _seed_store(test_store)
    monkeypatch.setattr(auction_app, "store", test_store)
    client = auction_app.app.test_client()
    response = client.get("/api/search")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["count"] == 1
    assert payload["total"] == 1
    assert payload["results"][0]["lot_title"] == "Baby Gate"
