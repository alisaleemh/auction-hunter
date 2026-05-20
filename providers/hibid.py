from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from geocode import distance_from_l9t8n6_miles
from models import ProviderSnapshot, make_lot_record


BASE_URL = "https://hibid.com"
LOTS_URL = f"{BASE_URL}/lots?zip=L9T%208N6&miles=25"
PAGE_LENGTH = 100
REQUEST_TIMEOUT = 15
MAX_PAGE_WORKERS = 8
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"
    )
}


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _fetch_text(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def _parse_state(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    state_script = soup.select_one("script#hibid-state")
    if state_script is None or not state_script.string:
        raise ValueError("HiBid page is missing script#hibid-state")
    state = json.loads(state_script.string)
    return state["apollo.state"]


def _extract_lot_links(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    links: dict[str, str] = {}
    for anchor in soup.select("a[href*='/lot/']"):
        href = anchor.get("href", "")
        match = re.search(r"/lot/(\d+)/", href)
        if match:
            links[match.group(1)] = urljoin(BASE_URL, href.split("?")[0])
    return links


def _root_search_refs(apollo_state: dict) -> tuple[list[dict], int, int, int]:
    root_query = apollo_state.get("ROOT_QUERY", {})
    for key, value in root_query.items():
        if not key.startswith("lotSearch("):
            continue
        paged = value["pagedResults"]
        return (
            paged["results"],
            paged.get("pageNumber", 1),
            paged.get("pageLength", PAGE_LENGTH),
            paged.get("filteredCount", len(paged.get("results", []))),
        )
    return [], 1, PAGE_LENGTH, 0


def _condition_from_description(description: str) -> str | None:
    match = re.search(r"Condition:\s*(.+)", description or "")
    if match:
        return match.group(1).splitlines()[0].strip()
    return None


def _address_from_fr8star_url(url: str | None) -> str:
    if not url or "?" not in url:
        return ""
    params = parse_qs(url.split("?", 1)[1])
    parts = [
        params.get("origin_address_line_1", [""])[0],
        params.get("origin_address_city", [""])[0],
        params.get("origin_address_state", [""])[0],
        params.get("origin_address_postal_code", [""])[0],
        params.get("origin_address_country", [""])[0],
    ]
    return ", ".join(part.replace("+", " ").strip() for part in parts if part.strip())


def _parse_hibid_end_time(lot_state: dict) -> str | None:
    title = lot_state.get("timeLeftTitle")
    if not title or ":" not in title:
        return None
    raw = title.split(":", 1)[1].strip()
    for suffix in (" EST", " EDT", " CST", " CDT"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    try:
        return datetime_from_us(raw)
    except ValueError:
        return None


def datetime_from_us(value: str) -> str:
    parsed = datetime.strptime(value, "%m/%d/%Y %I:%M:%S %p")
    localized = parsed.replace(tzinfo=ZoneInfo("America/Toronto"))
    return localized.astimezone(timezone.utc).isoformat()


def _auction_record(auction_ref: str | None, apollo_state: dict, lot: dict) -> dict:
    auction = apollo_state.get(auction_ref, {}) if auction_ref else {}
    address = _address_from_fr8star_url(lot.get("fr8StarUrl"))
    parts = [part.strip() for part in address.split(",")] if address else []
    city = parts[1] if len(parts) > 1 else None
    state = parts[2] if len(parts) > 2 else None
    postal_code = parts[3] if len(parts) > 3 else None
    country = parts[4] if len(parts) > 4 else None
    distance = lot.get("distanceMiles")
    if distance is None and address:
        distance = distance_from_l9t8n6_miles(address)
    return {
        "provider_auction_id": str((auction_ref or "unknown").split(":")[-1]),
        "title": auction.get("title") or auction.get("name") or "",
        "url": urljoin(BASE_URL, auction.get("urlPath") or "/lots"),
        "address": address,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "country": country,
        "latitude": None,
        "longitude": None,
        "distance_miles": round(distance, 1) if isinstance(distance, (int, float)) else None,
        "raw_payload": auction or {},
    }


def _lot_record(lot: dict, apollo_state: dict, lot_links: dict[str, str]) -> tuple[dict | None, dict]:
    auction_ref = (lot.get("auction") or {}).get("__ref")
    auction = _auction_record(auction_ref, apollo_state, lot)
    lot_state = lot.get("lotState", {})
    end_time = _parse_hibid_end_time(lot_state)
    if end_time is None:
        return None, auction
    lot_record = make_lot_record(
        source="HiBid",
        provider_auction_id=auction["provider_auction_id"],
        provider_lot_id=str(lot.get("id")),
        title=lot.get("lead") or "",
        lot_number=lot.get("lotNumber") or "",
        condition=_condition_from_description(lot.get("description", "")) or "",
        description=lot.get("description") or "",
        details="",
        current_bid=lot_state.get("highBid"),
        shipping_available=bool(lot.get("shippingOffered")),
        status="open" if lot_state.get("status") == "OPEN" else "closed",
        end_time=end_time,
        url=lot_links.get(str(lot.get("id")), urljoin(BASE_URL, f"/lot/{lot.get('id')}")),
        raw_payload=lot,
    )
    return lot_record, auction


def _page_url(page_number: int) -> str:
    return LOTS_URL if page_number <= 1 else f"{LOTS_URL}&apage={page_number}"


def _fetch_page(page_number: int) -> tuple[int, str]:
    client = _session()
    return page_number, _fetch_text(client, _page_url(page_number))


def _collect_page_snapshot(html: str, lots: list[dict], auctions: dict[str, dict], seen_lots: set[str]) -> tuple[int, int]:
    apollo_state = _parse_state(html)
    lot_links = _extract_lot_links(html)
    lot_refs, current_page, page_length, filtered_count = _root_search_refs(apollo_state)
    for ref in lot_refs:
        lot_ref = ref["__ref"]
        lot = apollo_state.get(lot_ref, {})
        provider_lot_id = str(lot.get("id"))
        if not provider_lot_id or provider_lot_id in seen_lots:
            continue
        seen_lots.add(provider_lot_id)
        lot_record, auction = _lot_record(lot, apollo_state, lot_links)
        auctions[auction["provider_auction_id"]] = auction
        if lot_record:
            lots.append(lot_record)
    total_pages = max(1, math.ceil(filtered_count / max(page_length, 1))) if filtered_count else current_page
    return current_page, total_pages


def fetch_snapshot() -> ProviderSnapshot:
    client = _session()
    first_page_html = _fetch_text(client, LOTS_URL)
    lots: list[dict] = []
    auctions: dict[str, dict] = {}
    seen_lots: set[str] = set()
    _, total_pages = _collect_page_snapshot(first_page_html, lots, auctions, seen_lots)
    if total_pages > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        page_numbers = list(range(2, total_pages + 1))
        with ThreadPoolExecutor(max_workers=min(MAX_PAGE_WORKERS, len(page_numbers))) as executor:
            futures = [executor.submit(_fetch_page, page_number) for page_number in page_numbers]
            for future in as_completed(futures):
                _, html = future.result()
                _collect_page_snapshot(html, lots, auctions, seen_lots)
    return ProviderSnapshot(source="HiBid", auctions=list(auctions.values()), lots=lots)
