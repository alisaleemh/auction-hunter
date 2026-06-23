from pathlib import Path

from geocode import distance_from_l9t8n6_miles
from geocode import distance_between_postal_codes_km, postal_code_record, normalize_postal_code
from providers.auction403 import (
    _auction_address_from_html,
    _current_auction_urls,
    _extract_lot_urls,
    _fetch_auction_snapshot,
    _parse_apollo_state,
)
from providers.hibid import (
    _address_from_fr8star_url,
    _extract_lot_links,
    _lot_record,
    _parse_state,
    _root_search_refs,
)
from providers.kotn import (
    _auction_location_from_html,
    _auction_title_from_html,
    _current_auction_urls as _kotn_current_auction_urls,
    _parse_listing_data,
    fetch_snapshot,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_hibid_fixture_parses_state_and_links():
    html = (FIXTURES / "hibid_page.html").read_text(encoding="utf-8")
    state = _parse_state(html)
    links = _extract_lot_links(html)
    refs, page_number, page_length, filtered_count = _root_search_refs(state)
    assert refs
    assert page_number == 1
    assert page_length == 100
    assert filtered_count == 1
    assert links["296257805"].endswith("/lot/296257805/-76-huggies-little-movers-baby-disposable---")


def test_403_fixture_parses_auctions_apollo_state_and_lot_links():
    listing_html = (FIXTURES / "403_auctions.html").read_text(encoding="utf-8")
    detail_html = (FIXTURES / "403_auction_page.html").read_text(encoding="utf-8")
    urls = _current_auction_urls(listing_html)
    state = _parse_apollo_state(detail_html)
    links = _extract_lot_urls(detail_html)
    assert urls == ["https://www.403auction.com/auctions/5247-reseller-and-liquidator-bulk-lots-auction"]
    assert "AuctionLot.58173" in state
    assert links["58173"].endswith("/auctions/5247/lot/58173-partials-lost-and-unclaimed-freight-pallet-lot")


def test_hibid_lot_record_tolerates_missing_auction_ref():
    lot = {
        "id": 1,
        "lead": "Gate",
        "lotNumber": "7",
        "description": "Condition: Used",
        "fr8StarUrl": (
            "https://example.com/?origin_address_line_1=20+Automatic+Rd"
            "&origin_address_city=Brampton&origin_address_state=ON"
            "&origin_address_postal_code=L6S+5N6&origin_address_country=Canada"
        ),
        "distanceMiles": 3.5,
        "shippingOffered": True,
        "lotState": {"highBid": 5, "timeLeftTitle": "Internet Bidding closes at: 4/20/2026 7:00:00 PM EST", "status": "OPEN"},
    }
    result, auction = _lot_record(lot, {}, {})
    assert auction["address"] == "20 Automatic Rd, Brampton, ON, L6S 5N6, Canada"
    assert auction["distance_miles"] == 3.5
    assert result["provider_lot_id"] == "1"


def test_hibid_address_from_fr8star_url():
    url = (
        "https://example.com/?origin_address_line_1=20+Automatic+Rd"
        "&origin_address_city=Brampton&origin_address_state=ON"
        "&origin_address_postal_code=L6S+5N6&origin_address_country=Canada"
    )
    assert _address_from_fr8star_url(url) == "20 Automatic Rd, Brampton, ON, L6S 5N6, Canada"


def test_403_auction_address_from_html():
    detail_html = (FIXTURES / "403_auction_page.html").read_text(encoding="utf-8")
    assert _auction_address_from_html(detail_html) == "80 Westcreek Blvd, Unit 2, Brampton, Ontario L6T0B8"


def test_distance_helper_uses_local_overrides():
    assert distance_from_l9t8n6_miles("80 Westcreek Blvd, Unit 2, Brampton, Ontario L6T0B8") is not None
    assert distance_from_l9t8n6_miles("Lake Shore Blvd E & Don Roadway Area, Toronto, Ontario M4M ***") is not None


def test_postal_code_lookup_and_distance():
    record = postal_code_record("L9T 8N6")
    assert record is not None
    assert normalize_postal_code("l9t 8n6") == "L9T8N6"
    assert distance_between_postal_codes_km("L9T 8N6", "L6S 5N6") is not None


def test_kotn_fixture_parses_auction_urls_title_and_listing_data():
    listing_page = (FIXTURES / "kotn_auction_page.html").read_text(encoding="utf-8")
    all_page = (FIXTURES / "kotn_auctions_all.html").read_text(encoding="utf-8")

    urls = _kotn_current_auction_urls(all_page)
    listing_data = _parse_listing_data(listing_page)

    assert urls == ["https://kotnauction.com/auctions/1051"]
    assert _auction_title_from_html(listing_page) == "Huronia High-Value Auction"
    assert _auction_location_from_html(listing_page) == "Huronia"
    assert listing_data["3941850"]["bid"] == 10


def test_kotn_fetch_snapshot_filters_out_future_lots(monkeypatch):
    listing_page = (FIXTURES / "kotn_auction_page.html").read_text(encoding="utf-8")
    all_page = (FIXTURES / "kotn_auctions_all.html").read_text(encoding="utf-8")

    def fake_fetch_text(session, url):
        if url.endswith("/auctions/all"):
            return all_page
        if url.startswith("https://kotnauction.com/auctions/1051"):
            return listing_page
        raise AssertionError(url)

    monkeypatch.setattr("providers.kotn._fetch_text", fake_fetch_text)

    snapshot = fetch_snapshot({"now": "2026-06-18T00:00:00+00:00"})
    assert snapshot.source == "King of the North Auction"
    assert [auction["provider_auction_id"] for auction in snapshot.auctions] == ["1051"]
    assert [lot["provider_lot_id"] for lot in snapshot.lots] == ["3941850", "3941851"]
    assert snapshot.lots[0]["current_bid"] == 10
    assert snapshot.lots[0]["raw_payload"]["imageUrl"] == "https://example.com/image.jpg"


def test_kotn_fetch_snapshot_skips_auction_pages_without_listing_data(monkeypatch, caplog):
    listing_page = (FIXTURES / "kotn_auction_page.html").read_text(encoding="utf-8")
    all_page = """
    <!doctype html>
    <html>
      <body>
        <nav>
          <a href="/auctions/1051">June 21 - Huronia High-Value Auction</a>
          <a href="/auctions/1052">June 21 - Huronia Pallet Auction</a>
        </nav>
      </body>
    </html>
    """
    empty_page = """
    <!doctype html>
    <html>
      <head><title>Pallet Auction | King of the North Auction</title></head>
      <body>
        <div class="listings-header-name">
          <span class="text">Huronia Pallet Auction</span>
        </div>
        <div class="listings-grid">
          <div class="listing-tile" data-id="9999999"></div>
        </div>
      </body>
    </html>
    """

    def fake_fetch_text(session, url):
        if url.endswith("/auctions/all"):
            return all_page
        if url.startswith("https://kotnauction.com/auctions/1051"):
            return listing_page
        if url.startswith("https://kotnauction.com/auctions/1052"):
            return empty_page
        raise AssertionError(url)

    monkeypatch.setattr("providers.kotn._fetch_text", fake_fetch_text)

    with caplog.at_level("WARNING"):
        snapshot = fetch_snapshot({"now": "2026-06-18T00:00:00+00:00"})

    assert snapshot.source == "King of the North Auction"
    assert [auction["provider_auction_id"] for auction in snapshot.auctions] == ["1051"]
    assert [lot["provider_lot_id"] for lot in snapshot.lots] == ["3941850", "3941851"]
    assert "kotn missing listingData url=https://kotnauction.com/auctions/1052" in caplog.text
    assert "kotn auction skipped url=https://kotnauction.com/auctions/1052 auction_id=1052 reason=no listingData" in caplog.text
