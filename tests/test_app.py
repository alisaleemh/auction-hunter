from datetime import datetime, timedelta, timezone
import threading
import time

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
    assert b"Reindex now" in response.data


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


def test_api_status_reports_indexing_state(tmp_path, monkeypatch):
    test_store = AuctionStore(tmp_path / "index.sqlite3")
    monkeypatch.setattr(auction_app, "store", test_store)
    client = auction_app.app.test_client()
    response = client.get("/api/status")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["indexing"] is False


def test_api_reindex_starts_and_reports_running(tmp_path, monkeypatch):
    test_store = AuctionStore(tmp_path / "index.sqlite3")
    monkeypatch.setattr(auction_app, "store", test_store)

    started = threading.Event()
    release = threading.Event()

    def fake_run_index(store, scope="manual", now=None, provider_loaders=None):
        run_id = store.start_index_run(scope, "2026-04-18T00:00:00+00:00")
        started.set()
        release.wait(timeout=5)
        store.finish_index_run(
            run_id,
            "2026-04-18T00:05:00+00:00",
            {"HiBid": {"status": "success", "auctions": 1, "lots": 1}},
            "1/1 sources indexed",
            None,
        )
        return {"run_id": run_id, "summary": "1/1 sources indexed", "errors": [], "source_stats": {}}

    monkeypatch.setattr(auction_app, "run_index", fake_run_index)
    client = auction_app.app.test_client()

    first = client.post("/api/reindex")
    assert first.status_code == 202
    assert started.wait(timeout=1)

    in_progress = client.get("/api/status")
    assert in_progress.get_json()["indexing"] is True

    conflict = client.post("/api/reindex")
    assert conflict.status_code == 409
    assert conflict.get_json()["status"] == "running"

    release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get("/api/status").get_json()
        if not status["indexing"]:
            break
        time.sleep(0.05)
    assert client.get("/api/status").get_json()["indexing"] is False


def test_api_search_returns_normalized_relative_image_urls(tmp_path, monkeypatch):
    test_store = AuctionStore(tmp_path / "index.sqlite3")
    now = datetime.now(timezone.utc)
    started_at = now.isoformat()
    test_store.upsert_source_status("HiBid", "success", started_at, started_at, None)
    run_id = test_store.start_index_run("manual", started_at)
    test_store.upsert_snapshot(
        "HiBid",
        run_id,
        started_at,
        [
            {
                "provider_auction_id": "a1",
                "title": "Auction",
                "url": "https://example.com/auction",
                "address": "",
                "city": "",
                "state": "",
                "postal_code": "",
                "country": "",
                "latitude": None,
                "longitude": None,
                "distance_miles": None,
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
                raw_payload={"id": "l1", "images": [{"url": "/images/image.jpg"}]},
            )
        ],
    )
    test_store.prune_source_rows("HiBid", run_id, (now + timedelta(days=7)).isoformat())
    test_store.finish_index_run(
        run_id,
        (now + timedelta(minutes=5)).isoformat(),
        {"HiBid": {"status": "success", "auctions": 1, "lots": 1}},
        "1/1 sources indexed",
        None,
    )
    monkeypatch.setattr(auction_app, "store", test_store)
    client = auction_app.app.test_client()
    response = client.get("/api/search?q=gate")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["results"][0]["imageUrl"] == "https://example.com/images/image.jpg"


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
