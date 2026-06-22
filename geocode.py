from __future__ import annotations

import math
import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional


ORIGIN_COORDS = (43.5776, -79.7857)
POSTAL_CODES_PATH = Path(__file__).resolve().parent / "data" / "geonames_ca_postal_codes.tsv"

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


def normalize_postal_code(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", "", value).upper()


@lru_cache(maxsize=1)
def _postal_code_index() -> dict[str, tuple[str, str, float, float]]:
    index: dict[str, tuple[str, str, float, float]] = {}
    if not POSTAL_CODES_PATH.exists():
        return index
    with POSTAL_CODES_PATH.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row in reader:
            if len(row) < 12 or row[0] != "CA":
                continue
            postal_code = normalize_postal_code(row[1])
            if not postal_code:
                continue
            city = row[2].strip()
            province = row[4].strip()
            try:
                lat = float(row[9])
                lon = float(row[10])
            except ValueError:
                continue
            index[postal_code] = (city, province, lat, lon)
    return index


def postal_code_record(postal_code: str | None) -> Optional[tuple[str, str, float, float]]:
    normalized = normalize_postal_code(postal_code)
    if not normalized:
        return None
    index = _postal_code_index()
    record = index.get(normalized)
    if record is not None:
        return record
    if len(normalized) >= 3:
        return index.get(normalized[:3])
    return None


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


def distance_between_postal_codes_km(origin_postal_code: str | None, destination_postal_code: str | None) -> Optional[float]:
    origin = postal_code_record(origin_postal_code)
    destination = postal_code_record(destination_postal_code)
    if not origin or not destination:
        return None
    _, _, origin_lat, origin_lon = origin
    _, _, dest_lat, dest_lon = destination
    return haversine_miles((origin_lat, origin_lon), (dest_lat, dest_lon)) * 1.609344
