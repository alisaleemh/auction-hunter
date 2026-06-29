from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from search import normalize_text


class AuctionRecord(TypedDict, total=False):
    provider_auction_id: str
    title: str
    url: str
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    latitude: float | None
    longitude: float | None
    distance_miles: float | None
    raw_payload: dict[str, Any]


class LotRecord(TypedDict, total=False):
    source: str
    provider_auction_id: str
    provider_lot_id: str
    lot_number: str
    title: str
    condition: str
    description: str
    details: str
    searchable_text: str
    current_bid: float | None
    shipping_available: bool | None
    url: str
    status: str
    end_time: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class ProviderSnapshot:
    source: str
    auctions: list[AuctionRecord]
    lots: list[LotRecord]


@dataclass(frozen=True)
class ProviderEstimate:
    source: str
    auctions: int | None
    lots: int | None


def make_lot_record(
    source: str,
    provider_auction_id: str,
    provider_lot_id: str,
    title: str,
    end_time: str,
    url: str,
    *,
    lot_number: str = "",
    condition: str = "",
    description: str = "",
    details: str = "",
    current_bid: float | None = None,
    shipping_available: bool | None = None,
    status: str = "open",
    raw_payload: dict[str, Any] | None = None,
) -> LotRecord:
    return {
        "source": source,
        "provider_auction_id": provider_auction_id,
        "provider_lot_id": provider_lot_id,
        "lot_number": lot_number,
        "title": title,
        "condition": condition,
        "description": description,
        "details": details,
        "searchable_text": normalize_text(" ".join(part for part in [title, condition, description, details] if part)),
        "current_bid": current_bid,
        "shipping_available": shipping_available,
        "url": url,
        "status": status,
        "end_time": end_time,
        "raw_payload": raw_payload or {},
    }
