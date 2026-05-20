from __future__ import annotations

import math
import re
from typing import Optional


ORIGIN_COORDS = (43.5776, -79.7857)

# Local approximate coordinate overrides to avoid live geocoding/rate limits.
ADDRESS_COORDS = {
    "80 westcreek blvd": (43.7252, -79.6832),
    "lake shore blvd e": (43.6418, -79.3406),
    "20 automatic rd": (43.7427, -79.7132),
}

CITY_COORDS = {
    "brampton": (43.7315, -79.7624),
    "mississauga": (43.5890, -79.6441),
    "milton": (43.5183, -79.8774),
    "toronto": (43.6532, -79.3832),
    "oakville": (43.4675, -79.6877),
    "vaughan": (43.8361, -79.4983),
}


def haversine_miles(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return 3958.7613 * c


def _normalize(address: str) -> str:
    return re.sub(r"\s+", " ", address.strip().lower())


def _coords_for_address(address: str) -> Optional[tuple[float, float]]:
    normalized = _normalize(address)
    for needle, coords in ADDRESS_COORDS.items():
        if needle in normalized:
            return coords

    for city, coords in CITY_COORDS.items():
        if re.search(rf"\b{re.escape(city)}\b", normalized):
            return coords

    return None


def distance_from_l9t8n6_miles(address: str) -> Optional[float]:
    coords = _coords_for_address(address)
    if not coords:
        return None
    return haversine_miles(ORIGIN_COORDS, coords)
