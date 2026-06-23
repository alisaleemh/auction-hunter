from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from models import ProviderEstimate, ProviderSnapshot, make_lot_record


BASE_URL = "https://kotnauction.com"
AUCTIONS_URL = f"{BASE_URL}/auctions/all"
REQUEST_TIMEOUT = 15
PAGE_LENGTH = 100
MAX_AUCTION_WORKERS = 3
MAX_PAGE_WORKERS = 8
DEFAULT_MAX_PAGES_PER_AUCTION = 1
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"
    )
}
LOCAL_TIMEZONE = ZoneInfo("America/Toronto")
SOURCE_NAME = "King of the North Auction"
logger = logging.getLogger(__name__)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _fetch_text(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    logger.info("kotn fetch ok url=%s status=%s bytes=%s", url, response.status_code, len(response.text))
    return response.text


def _current_auction_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for anchor in soup.select("a[href*='/auctions/']"):
        href = anchor.get("href", "")
        if not re.search(r"/auctions/\d+$", href):
            continue
        full_url = urljoin(BASE_URL, href)
        if full_url not in urls:
            urls.append(full_url)
    return urls


def _parse_listing_data(html: str, *, url: str | None = None) -> dict[str, dict] | None:
    match = re.search(r"var\s+listingData\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not match:
        soup = BeautifulSoup(html, "html.parser")
        title = " ".join((soup.title.get_text(" ", strip=True) if soup.title else "").split())
        snippet = " ".join(html[:300].split())
        logger.warning("kotn missing listingData url=%s title=%s snippet=%s", url or "", title, snippet)
        return None
    parsed = json.loads(match.group(1))
    return {str(key): value for key, value in parsed.items()}


def _max_page_number(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pages = [1]
    for anchor in soup.select("a[href*='page=']"):
        href = anchor.get("href", "")
        match = re.search(r"[?&]page=(\d+)", href)
        if match:
            pages.append(int(match.group(1)))
    return max(pages) if pages else 1


def _auction_title_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one(".listings-header-name .text")
    if not header:
        return ""
    date_label = header.select_one(".auction-date")
    if date_label:
        date_label.extract()
    return " ".join(header.get_text(" ", strip=True).split())


def _auction_date_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    date_label = soup.select_one(".listings-header-name .auction-date")
    if not date_label:
        return ""
    return " ".join(date_label.get_text(" ", strip=True).split()).replace("–", "").strip()


def _auction_location_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    location = soup.select_one(".auction-location strong")
    return " ".join(location.get_text(" ", strip=True).split()) if location else ""


def _parse_end_time(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    localized = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return localized.astimezone(timezone.utc).isoformat()


def _reference_now(config: dict | None) -> datetime:
    if not config:
        return datetime.now(timezone.utc)
    raw_now = config.get("now")
    if raw_now:
        if isinstance(raw_now, datetime):
            now = raw_now
        else:
            now = datetime.fromisoformat(str(raw_now))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _auction_record(auction_id: str, auction_url: str, html: str) -> dict:
    title = _auction_title_from_html(html)
    location = _auction_location_from_html(html)
    date_label = _auction_date_from_html(html)
    return {
        "provider_auction_id": auction_id,
        "title": title,
        "url": auction_url,
        "address": location,
        "city": location or None,
        "state": None,
        "postal_code": None,
        "country": "Canada" if location else None,
        "latitude": None,
        "longitude": None,
        "distance_miles": None,
        "raw_payload": {
            "auction_id": auction_id,
            "auction_url": auction_url,
            "auction_title": title,
            "auction_date": date_label,
            "auction_location": location,
        },
    }


def _parse_lots_page(
    auction_id: str,
    auction_url: str,
    html: str,
    *,
    window_end: datetime,
) -> tuple[dict | None, list[dict], datetime | None, bool]:
    listing_data = _parse_listing_data(html, url=auction_url)
    if listing_data is None:
        logger.warning("kotn skip page without listingData url=%s auction_id=%s", auction_url, auction_id)
        return None, [], None, False
    soup = BeautifulSoup(html, "html.parser")
    auction = _auction_record(auction_id, auction_url, html)
    lots: list[dict] = []
    page_min_end: datetime | None = None
    has_next = bool(soup.select_one("a[rel='next']"))

    for tile in soup.select(".listing-tile[data-id]"):
        listing_id = str(tile.get("data-id") or "").strip()
        if not listing_id:
            continue
        listing = listing_data.get(listing_id)
        if not listing or not listing.get("end"):
            continue

        end_time = datetime.fromisoformat(_parse_end_time(str(listing["end"])))
        if page_min_end is None or end_time < page_min_end:
            page_min_end = end_time

        if listing.get("is_closed"):
            continue
        if end_time > window_end:
            continue

        title_link = tile.select_one(".listing-tile-title-link")
        image = tile.select_one("img.listing-tile-image")
        location = tile.select_one(".listing-tile-location strong")
        item_condition = tile.select_one(".listing-item-condition")
        package_condition = tile.select_one(".listing-package-condition")
        title = " ".join(title_link.get_text(" ", strip=True).split()) if title_link else ""
        lot_url = urljoin(BASE_URL, title_link.get("href", f"/listings/{listing_id}")) if title_link else urljoin(BASE_URL, f"/listings/{listing_id}")
        location_text = " ".join(location.get_text(" ", strip=True).split()) if location else ""
        item_condition_text = " ".join(item_condition.get_text(" ", strip=True).split()) if item_condition else ""
        package_condition_text = " ".join(package_condition.get_text(" ", strip=True).split()) if package_condition else ""
        details = "; ".join(part for part in [f"Location: {location_text}" if location_text else "", package_condition_text] if part)
        raw_payload = {
            "listing": listing,
            "listing_id": listing_id,
            "auction_id": auction_id,
            "auction_url": auction_url,
            "auction_title": auction["title"],
            "location": location_text,
            "item_condition": item_condition_text,
            "package_condition": package_condition_text,
        }
        if image and image.get("src"):
            raw_payload["imageUrl"] = image["src"]

        lots.append(
            make_lot_record(
                source=SOURCE_NAME,
                provider_auction_id=auction_id,
                provider_lot_id=listing_id,
                title=title,
                lot_number="",
                condition=item_condition_text,
                description="",
                details=details,
                current_bid=listing.get("bid"),
                shipping_available=False,
                status="closed" if listing.get("is_closed") else "open",
                end_time=end_time.astimezone(timezone.utc).isoformat(),
                url=lot_url,
                raw_payload=raw_payload,
            )
        )

    return auction, lots, page_min_end, has_next


def _auction_page_url(auction_url: str, page_number: int) -> str:
    base = f"{auction_url}?per_page={PAGE_LENGTH}&order_by=ending_asc"
    return base if page_number <= 1 else f"{base}&page={page_number}"


def _fetch_auction_page(auction_url: str, page_number: int) -> tuple[int, str]:
    client = _session()
    return page_number, _fetch_text(client, _auction_page_url(auction_url, page_number))


def _fetch_auction_snapshot(auction_url: str, *, window_end: datetime, max_pages: int) -> tuple[dict | None, list[dict]]:
    logger.info("kotn auction start url=%s max_pages=%s", auction_url, max_pages)
    first_page_html = _fetch_text(_session(), _auction_page_url(auction_url, 1))
    match = re.search(r"/auctions/(\d+)$", auction_url)
    if not match:
        raise ValueError(f"Invalid auction URL: {auction_url}")
    auction_id = match.group(1)
    auction, first_page_lots, _, _ = _parse_lots_page(
        auction_id,
        auction_url,
        first_page_html,
        window_end=window_end,
    )
    if auction is None:
        logger.warning("kotn auction skipped url=%s auction_id=%s reason=no listingData", auction_url, auction_id)
        return None, []
    lots_by_id = {lot["provider_lot_id"]: lot for lot in first_page_lots}
    total_pages = _max_page_number(first_page_html)
    logger.info("kotn auction page1 url=%s auction_id=%s lots=%s total_pages=%s", auction_url, auction_id, len(first_page_lots), total_pages)

    if total_pages > 1 and max_pages > 1:
        page_numbers = list(range(2, min(total_pages, max_pages) + 1))
        logger.info("kotn auction additional pages url=%s pages=%s", auction_url, page_numbers)
        with ThreadPoolExecutor(max_workers=min(MAX_PAGE_WORKERS, len(page_numbers))) as executor:
            futures = [executor.submit(_fetch_auction_page, auction_url, page_number) for page_number in page_numbers]
            for future in as_completed(futures):
                _, page_html = future.result()
                _, page_lots, _, _ = _parse_lots_page(
                    auction_id,
                    auction_url,
                    page_html,
                    window_end=window_end,
                )
                for lot in page_lots:
                    lots_by_id[lot["provider_lot_id"]] = lot

    logger.info("kotn auction done url=%s lots=%s", auction_url, len(lots_by_id))
    return auction, list(lots_by_id.values())


def fetch_snapshot(config: dict | None = None) -> ProviderSnapshot:
    current = _reference_now(config)
    window_end = current + timedelta(days=7)
    max_pages = max(1, int((config or {}).get("max_pages") or DEFAULT_MAX_PAGES_PER_AUCTION))
    logger.info("kotn snapshot start window_end=%s max_pages=%s", window_end.isoformat(), max_pages)
    listing_html = _fetch_text(_session(), AUCTIONS_URL)
    auction_urls = _current_auction_urls(listing_html)
    logger.info("kotn snapshot auctions=%s urls=%s", len(auction_urls), auction_urls)
    auctions: dict[str, dict] = {}
    lots: list[dict] = []

    with ThreadPoolExecutor(max_workers=min(MAX_AUCTION_WORKERS, len(auction_urls) or 1)) as executor:
        futures = [
            executor.submit(_fetch_auction_snapshot, auction_url, window_end=window_end, max_pages=max_pages)
            for auction_url in auction_urls
        ]
        for future in as_completed(futures):
            auction, auction_lots = future.result()
            if auction is None:
                continue
            auctions[auction["provider_auction_id"]] = auction
            lots.extend(auction_lots)

    logger.info("kotn snapshot done auctions=%s lots=%s", len(auctions), len(lots))
    return ProviderSnapshot(source=SOURCE_NAME, auctions=list(auctions.values()), lots=lots)


def estimate_snapshot(config: dict | None = None) -> ProviderEstimate:
    current = _reference_now(config)
    max_pages = max(1, int((config or {}).get("max_pages") or DEFAULT_MAX_PAGES_PER_AUCTION))
    listing_html = _fetch_text(_session(), AUCTIONS_URL)
    auction_urls = _current_auction_urls(listing_html)
    estimated_lots = 0
    for auction_url in auction_urls:
        html = _fetch_text(_session(), _auction_page_url(auction_url, 1))
        soup = BeautifulSoup(html, "html.parser")
        page_count = len(soup.select(".listing-tile[data-id]"))
        total_pages = _max_page_number(html)
        if total_pages > 1 and max_pages > 1:
            estimated_lots += page_count * min(total_pages, max_pages)
        else:
            estimated_lots += page_count
    return ProviderEstimate(source=SOURCE_NAME, auctions=len(auction_urls), lots=estimated_lots)
