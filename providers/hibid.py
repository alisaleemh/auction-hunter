from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urljoin
from zoneinfo import ZoneInfo

import requests
from requests import RequestException
from bs4 import BeautifulSoup

from geocode import distance_from_l9t8n6_miles
from models import ProviderEstimate, ProviderSnapshot, make_lot_record


logger = logging.getLogger(__name__)
BASE_URL = "https://hibid.com"
DEFAULT_ZIP = "L9T 8N6"
DEFAULT_MILES = 25
PAGE_LENGTH = 100
REQUEST_TIMEOUT = 15
REQUEST_RETRIES = 2
REQUEST_BACKOFF_SECONDS = 1.0
LOT_PARSE_RETRIES = 2
MAX_PAGE_WORKERS = 8
DEFAULT_SEARCH_PARTITIONS = [
    "baby",
    "car seat",
    "stroller",
    "gate",
    "seagate",
    "hard drive",
    "ssd",
    "laptop",
    "monitor",
    "dji",
    "drone",
]
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
    last_error: Exception | None = None
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.text
        except RequestException as exc:
            last_error = exc
            if attempt >= REQUEST_RETRIES:
                break
            time.sleep(REQUEST_BACKOFF_SECONDS * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError(f"failed to fetch {url}")


def _extract_og_image_url(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for selector in ('meta[property="og:image"]', 'meta[name="twitter:image"]'):
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            return tag["content"].replace("&amp;", "&")
    return None


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
    parts = _address_parts_from_fr8star_url(url)
    return ", ".join(part for part in [parts["address"], parts["city"], parts["state"], parts["postal_code"], parts["country"]] if part)


def _address_parts_from_fr8star_url(url: str | None) -> dict[str, str]:
    if not url or "?" not in url:
        return {"address": "", "city": "", "state": "", "postal_code": "", "country": ""}
    params = parse_qs(url.split("?", 1)[1])
    return {
        "address": params.get("origin_address_line_1", [""])[0].replace("+", " ").strip(),
        "city": params.get("origin_address_city", [""])[0].replace("+", " ").strip(),
        "state": params.get("origin_address_state", [""])[0].replace("+", " ").strip(),
        "postal_code": params.get("origin_address_postal_code", [""])[0].replace("+", " ").strip(),
        "country": params.get("origin_address_country", [""])[0].replace("+", " ").strip(),
    }


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
    address_parts = _address_parts_from_fr8star_url(lot.get("fr8StarUrl"))
    city = address_parts["city"] or None
    state = address_parts["state"] or None
    postal_code = address_parts["postal_code"] or None
    country = address_parts["country"] or None
    distance = lot.get("distanceMiles")
    if distance is None and address:
        distance = distance_from_l9t8n6_miles(address)
    return {
        "provider_auction_id": str((auction_ref or "unknown").split(":")[-1]),
        "title": auction.get("title") or auction.get("name") or auction.get("eventName") or "",
        "url": urljoin(BASE_URL, auction.get("urlPath") or f"/catalog/{(auction_ref or 'unknown').split(':')[-1]}"),
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
    lot_url = lot_links.get(str(lot.get("id")), urljoin(BASE_URL, f"/lot/{lot.get('id')}"))
    image_url = None
    featured_picture = lot.get("featuredPicture") or {}
    for key in ("hdThumbnailLocation", "thumbnailLocation", "fullSizeLocation"):
        if featured_picture.get(key):
            image_url = featured_picture[key]
            break
    lot_payload = dict(lot)
    if image_url:
        lot_payload["imageUrl"] = image_url
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
        url=lot_url,
        raw_payload=lot_payload,
    )
    return lot_record, auction


def _lots_url(zip_code: str, miles: int, search_text: str | None = None) -> str:
    params = {"zip": zip_code, "miles": miles}
    if search_text:
        params["q"] = search_text
    return f"{BASE_URL}/lots?{urlencode(params)}"


def _page_url(page_number: int, zip_code: str, miles: int, search_text: str | None = None) -> str:
    lots_url = _lots_url(zip_code, miles, search_text)
    return lots_url if page_number <= 1 else f"{lots_url}&apage={page_number}"


def _fetch_page(page_number: int, zip_code: str, miles: int, search_text: str | None = None) -> tuple[int, str]:
    client = _session()
    return page_number, _fetch_text(client, _page_url(page_number, zip_code, miles, search_text))


def _append_lot_record(lot: dict, apollo_state: dict, lot_links: dict[str, str], lots: list[dict], auctions: dict[str, dict], seen_lots: set[str]) -> bool:
    provider_lot_id = str(lot.get("id"))
    if not provider_lot_id or provider_lot_id in seen_lots:
        return False
    lot_record, auction = _lot_record(lot, apollo_state, lot_links)
    auctions[auction["provider_auction_id"]] = auction
    seen_lots.add(provider_lot_id)
    if lot_record:
        lots.append(lot_record)
        return True
    return False


def _detail_lot_record(provider_lot_id: str, lot_url: str) -> tuple[dict | None, dict]:
    html = _fetch_text(_session(), lot_url)
    apollo_state = _parse_state(html)
    lot = apollo_state.get(f"Lot:{provider_lot_id}")
    if not lot:
        raise ValueError(f"HiBid detail page missing Lot:{provider_lot_id}")
    return _lot_record(lot, apollo_state, {provider_lot_id: lot_url.split("?", 1)[0]})


def _retry_failed_lot(
    provider_lot_id: str,
    lot: dict,
    apollo_state: dict,
    lot_links: dict[str, str],
    lots: list[dict],
    auctions: dict[str, dict],
    seen_lots: set[str],
    first_error: Exception,
) -> bool:
    for attempt in range(LOT_PARSE_RETRIES):
        try:
            if _append_lot_record(lot, apollo_state, lot_links, lots, auctions, seen_lots):
                logger.info("hibid lot retry succeeded lot_id=%s attempt=%s", provider_lot_id, attempt + 1)
                return True
        except Exception as exc:
            first_error = exc

    lot_url = lot_links.get(provider_lot_id)
    if lot_url:
        try:
            lot_record, auction = _detail_lot_record(provider_lot_id, lot_url)
            auctions[auction["provider_auction_id"]] = auction
            seen_lots.add(provider_lot_id)
            if lot_record:
                lots.append(lot_record)
                logger.info("hibid lot detail retry succeeded lot_id=%s", provider_lot_id)
                return True
        except Exception as exc:
            first_error = exc

    logger.warning("hibid lot skipped after retries lot_id=%s error=%s", provider_lot_id, first_error)
    return False


def _collect_page_snapshot(html: str, lots: list[dict], auctions: dict[str, dict], seen_lots: set[str]) -> tuple[int, int]:
    apollo_state = _parse_state(html)
    lot_links = _extract_lot_links(html)
    lot_refs, current_page, page_length, filtered_count = _root_search_refs(apollo_state)
    failed_lots: list[tuple[str, dict, Exception]] = []
    for ref in lot_refs:
        lot_ref = ref["__ref"]
        lot = apollo_state.get(lot_ref, {})
        provider_lot_id = str(lot.get("id"))
        if not provider_lot_id or provider_lot_id in seen_lots:
            continue
        try:
            _append_lot_record(lot, apollo_state, lot_links, lots, auctions, seen_lots)
        except Exception as exc:
            failed_lots.append((provider_lot_id, lot, exc))
    for provider_lot_id, lot, exc in failed_lots:
        if provider_lot_id not in seen_lots:
            _retry_failed_lot(provider_lot_id, lot, apollo_state, lot_links, lots, auctions, seen_lots, exc)
    total_pages = max(1, math.ceil(filtered_count / max(page_length, 1))) if filtered_count else current_page
    return current_page, total_pages


def _collect_partition(zip_code: str, miles: int, search_text: str | None, lots: list[dict], auctions: dict[str, dict], seen_lots: set[str]) -> None:
    client = _session()
    first_page_html = _fetch_text(client, _lots_url(zip_code, miles, search_text))
    _, total_pages = _collect_page_snapshot(first_page_html, lots, auctions, seen_lots)
    if total_pages > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        page_numbers = list(range(2, total_pages + 1))
        with ThreadPoolExecutor(max_workers=min(MAX_PAGE_WORKERS, len(page_numbers))) as executor:
            futures = [executor.submit(_fetch_page, page_number, zip_code, miles, search_text) for page_number in page_numbers]
            for future in as_completed(futures):
                try:
                    _, html = future.result()
                    _collect_page_snapshot(html, lots, auctions, seen_lots)
                except Exception as exc:
                    logger.warning("hibid page fetch skipped search=%r error=%s", search_text, exc)


def _search_partitions(config: dict) -> list[str]:
    raw_terms = config.get("search_partitions")
    if isinstance(raw_terms, str):
        terms = [term.strip() for term in raw_terms.split(",")]
    elif isinstance(raw_terms, list):
        terms = [str(term).strip() for term in raw_terms]
    else:
        terms = DEFAULT_SEARCH_PARTITIONS
    return [term for term in terms if term]


def fetch_snapshot(config: dict | None = None) -> ProviderSnapshot:
    config = config or {}
    zip_code = str(config.get("zip_code") or DEFAULT_ZIP)
    miles = int(config.get("miles") or DEFAULT_MILES)
    lots: list[dict] = []
    auctions: dict[str, dict] = {}
    seen_lots: set[str] = set()
    _collect_partition(zip_code, miles, None, lots, auctions, seen_lots)
    for term in _search_partitions(config):
        try:
            _collect_partition(zip_code, miles, term, lots, auctions, seen_lots)
        except Exception as exc:
            logger.warning("hibid search partition skipped term=%r error=%s", term, exc)
    return ProviderSnapshot(source="HiBid", auctions=list(auctions.values()), lots=lots)


def estimate_snapshot(config: dict | None = None) -> ProviderEstimate:
    config = config or {}
    zip_code = str(config.get("zip_code") or DEFAULT_ZIP)
    miles = int(config.get("miles") or DEFAULT_MILES)
    html = _fetch_text(_session(), _lots_url(zip_code, miles))
    apollo_state = _parse_state(html)
    _, _, page_length, filtered_count = _root_search_refs(apollo_state)
    if filtered_count:
        return ProviderEstimate(source="HiBid", auctions=None, lots=int(filtered_count))
    return ProviderEstimate(source="HiBid", auctions=None, lots=page_length)
