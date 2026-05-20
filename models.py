from __future__ import annotations

from dataclasses import dataclass

from search import normalize_text


@dataclass(frozen=True)
class ProviderSnapshot:
    source: str
    auctions: list[dict]
    lots: list[dict]


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
    raw_payload: dict | None = None,
) -> dict:
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
