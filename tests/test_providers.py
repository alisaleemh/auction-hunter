from pathlib import Path

from geocode import distance_from_l9t8n6_miles
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
