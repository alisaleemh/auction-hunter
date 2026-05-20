from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from geocode import distance_from_l9t8n6_miles
from models import ProviderSnapshot, make_lot_record


BASE_URL = "https://www.403auction.com"
AUCTIONS_URL = f"{BASE_URL}/auctions"
REQUEST_TIMEOUT = 15
MAX_AUCTION_WORKERS = 6
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


def _slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return value.strip("-")


def _current_auction_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for anchor in soup.select("a[href^='/auctions/']"):
        href = anchor.get("href", "")
        if not re.match(r"^/auctions/\d", href):
            continue
        full_url = urljoin(BASE_URL, href)
        if full_url not in urls:
            urls.append(full_url)
    return urls


def _parse_apollo_state(html: str) -> dict:
    match = re.search(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;", html, re.DOTALL)
    if not match:
        raise ValueError("403 page is missing window.__APOLLO_STATE__")
    return json.loads(match.group(1))


def _extract_lot_urls(html: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for match in re.finditer(r'href="(/auctions/\d+/lot/(\d+)-[^"]+)"', html):
        links[match.group(2)] = urljoin(BASE_URL, unescape(match.group(1)))
    return links


def _condition_from_dynamic_fields(lot: dict) -> str | None:
    for field in lot.get("dynamic_fields", []):
        label = (field.get("label") or "").strip().lower()
        if label.startswith("condition"):
            data = field.get("data") or {}
            value = data.get("value")
            if value:
                return str(value).strip()
    return None


def _description_text(html_fragment: str) -> str:
    soup = BeautifulSoup(html_fragment or "", "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def _auction_title(apollo_state: dict, auction_id: str) -> str:
    for key in (f"Auction:{auction_id}", f"PublicAuction:{auction_id}"):
        auction = apollo_state.get(key)
        if auction:
            return auction.get("title") or auction.get("name") or ""
    return ""


def _shipping_available(html: str) -> bool | None:
    text = html.lower()
    if "shipping is not available" in text:
        return False
    if "shipping available" in text:
        return True
    return None


def _auction_address_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    location = soup.select_one(".auctionLocation")
    if not location:
        return ""
    for heading in location.select(".location-heading"):
        heading.extract()
    return " ".join(location.get_text(" ", strip=True).split())


def _auction_record(auction_id: str, apollo_state: dict, auction_url: str, auction_address: str) -> dict:
    title = _auction_title(apollo_state, auction_id)
    parts = [part.strip() for part in auction_address.split(",")] if auction_address else []
    city = parts[1] if len(parts) > 1 else None
    state = parts[2] if len(parts) > 2 else None
    distance = distance_from_l9t8n6_miles(auction_address) if auction_address else None
    return {
        "provider_auction_id": auction_id,
        "title": title,
        "url": auction_url,
        "address": auction_address,
        "city": city,
        "state": state,
        "postal_code": None,
        "country": "Canada" if auction_address else None,
        "latitude": None,
        "longitude": None,
        "distance_miles": round(distance, 1) if isinstance(distance, (int, float)) else None,
        "raw_payload": apollo_state.get(f"Auction:{auction_id}") or apollo_state.get(f"PublicAuction:{auction_id}") or {},
    }


def _fetch_auction_snapshot(auction_url: str) -> tuple[dict, list[dict]]:
    client = _session()
    page_url = f"{auction_url}?page=1&pageSize=500"
    html = _fetch_text(client, page_url)
    apollo_state = _parse_apollo_state(html)
    lot_links = _extract_lot_urls(html)
    shipping_available = _shipping_available(html)
    auction_address = _auction_address_from_html(html)
    first_lot = next((value for key, value in apollo_state.items() if key.startswith("AuctionLot.")), None)
    if first_lot is None:
        raise ValueError(f"No lots found for auction {auction_url}")
    auction_id = str(first_lot.get("auction_id"))
    auction = _auction_record(auction_id, apollo_state, auction_url, auction_address)
    lots: list[dict] = []
    for key, value in apollo_state.items():
        if not key.startswith("AuctionLot."):
            continue
        title = value.get("title") or ""
        lots.append(
            make_lot_record(
                source="403 Auction",
                provider_auction_id=auction_id,
                provider_lot_id=str(value.get("auction_lot_id")),
                title=title,
                lot_number=value.get("lot_number") or "",
                condition=_condition_from_dynamic_fields(value) or "",
                description=_description_text(value.get("description") or ""),
                details="",
                current_bid=value.get("winning_bid_amount"),
                shipping_available=shipping_available,
                status="closed" if value.get("is_past_end_time") else "open",
                end_time=value.get("end_time"),
                url=lot_links.get(str(value.get("auction_lot_id")))
                or urljoin(BASE_URL, f"/auctions/{auction_id}/lot/{value.get('auction_lot_id')}-{_slugify(title)}"),
                raw_payload=value,
            )
        )
    return auction, lots


def fetch_snapshot() -> ProviderSnapshot:
    client = _session()
    listing_html = _fetch_text(client, AUCTIONS_URL)
    auction_urls = _current_auction_urls(listing_html)
    auctions: list[dict] = []
    lots: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=min(MAX_AUCTION_WORKERS, len(auction_urls) or 1)) as executor:
        futures = [executor.submit(_fetch_auction_snapshot, auction_url) for auction_url in auction_urls]
        for future in as_completed(futures):
            auction, auction_lots = future.result()
            auctions.append(auction)
            lots.extend(auction_lots)
    return ProviderSnapshot(source="403 Auction", auctions=auctions, lots=lots)
