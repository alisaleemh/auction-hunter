import app as auction_app
from models import make_lot_record
from store import AuctionStore


def _seed_store(store: AuctionStore):
    store.upsert_source_status("HiBid", "success", "2026-04-18T00:00:00+00:00", "2026-04-18T00:00:00+00:00", None)
    run_id = store.start_index_run("manual", "2026-04-18T00:00:00+00:00")
    store.upsert_snapshot(
        "HiBid",
        run_id,
        "2026-04-18T00:00:00+00:00",
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
                end_time="2026-04-20T23:00:00+00:00",
                url="https://example.com/lot/12",
            )
        ],
    )
    store.prune_source_rows("HiBid", run_id, "2026-04-25T00:00:00+00:00")
    store.finish_index_run(run_id, "2026-04-18T00:05:00+00:00", {"HiBid": {"status": "success"}}, "1/1 sources indexed", None)


def test_get_root_empty_query(tmp_path, monkeypatch):
    test_store = AuctionStore(tmp_path / "index.sqlite3")
    monkeypatch.setattr(auction_app, "store", test_store)
    client = auction_app.app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"Auction Item Search" in response.data


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
    assert payload["results"][0]["lot_title"] == "Baby Gate"
    assert payload["indexed_at"] == "2026-04-18T00:05:00+00:00"
    assert "time_left" in payload["results"][0]
