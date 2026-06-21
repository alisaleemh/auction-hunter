from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

from geocode import normalize_postal_code, postal_code_record, distance_between_postal_codes_km
from search import TERM_EXPANSIONS, expanded_query_tokens, filter_and_sort_results, normalize_text, query_tokens


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "auction_index.sqlite3"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT,
    last_index_status TEXT,
    last_index_started_at TEXT,
    last_index_finished_at TEXT,
    last_error_text TEXT
);

CREATE TABLE IF NOT EXISTS auctions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    provider_auction_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    address TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    country TEXT,
    latitude REAL,
    longitude REAL,
    distance_miles REAL,
    raw_payload_json TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_run_id INTEGER,
    UNIQUE(source_id, provider_auction_id)
);

CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    auction_id INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
    provider_lot_id TEXT NOT NULL,
    lot_number TEXT,
    title TEXT NOT NULL,
    condition TEXT,
    description TEXT,
    details TEXT,
    searchable_text TEXT NOT NULL,
    current_bid REAL,
    shipping_available INTEGER,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    end_time TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_run_id INTEGER,
    raw_payload_json TEXT NOT NULL,
    UNIQUE(source_id, provider_lot_id)
);

CREATE TABLE IF NOT EXISTS index_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    scope TEXT NOT NULL,
    heartbeat_at TEXT,
    progress_total INTEGER,
    progress_done INTEGER,
    progress_percent REAL,
    progress_message TEXT,
    source_stats_json TEXT,
    success_summary TEXT,
    error_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_lots_end_time ON lots(end_time);
CREATE INDEX IF NOT EXISTS idx_lots_source_status ON lots(source_id, status);
CREATE INDEX IF NOT EXISTS idx_auctions_source ON auctions(source_id);

CREATE VIRTUAL TABLE IF NOT EXISTS lots_fts USING fts5(
    title,
    condition,
    description,
    details,
    source,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS postal_codes (
    postal_code TEXT PRIMARY KEY,
    city TEXT,
    province TEXT,
    latitude REAL,
    longitude REAL
);
"""


@dataclass(frozen=True)
class SearchMetadata:
    deploy_commit: str | None
    indexed_at: str | None
    last_run_status: str | None
    last_run_finished_at: str | None
    last_run_summary: str | None
    last_run_duration_seconds: float | None
    last_success_duration_seconds: float | None
    index_heartbeat_at: str | None
    progress_total: int | None
    progress_done: int | None
    progress_percent: float | None
    progress_message: str | None
    indexed_source_count: int | None
    indexed_auction_count: int | None
    indexed_lot_count: int | None
    indexing: bool = False
    current_run_started_at: str | None = None
    current_run_scope: str | None = None
    index_stale: bool = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_time_left(end_time: str | None, now: datetime | None = None) -> str:
    parsed = parse_iso(end_time)
    if parsed is None:
        return ""
    current = (now or utc_now()).astimezone(timezone.utc)
    delta = parsed - current
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "Ended"

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _normalize_image_url(value: str, base_url: str | None = None) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    if cleaned.startswith(("http://", "https://")):
        return cleaned
    if base_url and cleaned.startswith("/"):
        return urljoin(base_url, cleaned)
    return None


def _extract_image_url(raw_payload_json: str | None, base_url: str | None = None) -> str | None:
    if not raw_payload_json:
        return None
    try:
        payload = json.loads(raw_payload_json)
    except json.JSONDecodeError:
        return None

    candidate_keys = (
        "image_url",
        "imageUrl",
        "image",
        "img",
        "thumbnail",
        "thumbnail_url",
        "thumbnailUrl",
        "photo",
        "photo_url",
        "photoUrl",
        "primary_image",
        "primaryImage",
        "main_image",
        "mainImage",
        "cover_image",
        "coverImage",
    )

    def walk(value: object) -> str | None:
        if isinstance(value, dict):
            for key in candidate_keys:
                candidate = value.get(key)
                if isinstance(candidate, str):
                    normalized = _normalize_image_url(candidate, base_url)
                    if normalized:
                        return normalized
                elif isinstance(candidate, list):
                    for item in candidate:
                        found = walk(item)
                        if found:
                            return found
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, str):
            normalized = _normalize_image_url(value, base_url)
            if normalized and any(normalized.lower().split("?", 1)[0].endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                return normalized
        return None

    return walk(payload)


class AuctionStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.executescript(SCHEMA)
            self._ensure_schema_migrations(conn)

    def _ensure_schema_migrations(self, conn: sqlite3.Connection) -> None:
        sources_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "config_json" not in sources_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN config_json TEXT")

        index_run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(index_runs)").fetchall()}
        for column, ddl_type in (
            ("heartbeat_at", "TEXT"),
            ("progress_total", "INTEGER"),
            ("progress_done", "INTEGER"),
            ("progress_percent", "REAL"),
            ("progress_message", "TEXT"),
        ):
            if column not in index_run_columns:
                conn.execute(f"ALTER TABLE index_runs ADD COLUMN {column} {ddl_type}")
        self._ensure_fts_schema(conn)
        self._ensure_postal_codes(conn)

    def _ensure_fts_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS lots_fts USING fts5(
                title,
                condition,
                description,
                details,
                source,
                tokenize='unicode61'
            );
            """
        )

    def _ensure_postal_codes(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) AS count FROM postal_codes").fetchone()
        if row and int(row["count"]) > 0:
            return
        from geocode import POSTAL_CODES_PATH

        if not POSTAL_CODES_PATH.exists():
            return
        with POSTAL_CODES_PATH.open(newline="", encoding="utf-8") as handle:
            import csv

            reader = csv.reader(handle, delimiter="\t")
            rows = []
            for row in reader:
                if len(row) < 12 or row[0] != "CA":
                    continue
                postal_code = normalize_postal_code(row[1])
                if not postal_code:
                    continue
                try:
                    rows.append((postal_code, row[2].strip(), row[4].strip(), float(row[9]), float(row[10])))
                except ValueError:
                    continue
        conn.executemany(
            "INSERT OR REPLACE INTO postal_codes (postal_code, city, province, latitude, longitude) VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    def start_index_run(self, scope: str, started_at: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO index_runs (
                    started_at, scope, heartbeat_at, progress_total, progress_done, progress_percent, progress_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (started_at, scope, started_at, None, None, None, None),
            )
            return int(cursor.lastrowid)

    def refresh_index_run_heartbeat(self, run_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE index_runs SET heartbeat_at = ? WHERE id = ?",
                (to_iso(utc_now()), run_id),
            )

    def update_index_run_progress(
        self,
        run_id: int,
        *,
        progress_total: int | None,
        progress_done: int | None,
        progress_percent: float | None,
        progress_message: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE index_runs
                SET heartbeat_at = ?, progress_total = ?, progress_done = ?, progress_percent = ?, progress_message = ?
                WHERE id = ?
                """,
                (to_iso(utc_now()), progress_total, progress_done, progress_percent, progress_message, run_id),
            )

    def finish_index_run(
        self,
        run_id: int,
        finished_at: str,
        source_stats: dict,
        success_summary: str,
        error_text: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE index_runs
                SET finished_at = ?, source_stats_json = ?, success_summary = ?, error_text = ?
                WHERE id = ?
                """,
                (finished_at, json.dumps(source_stats, sort_keys=True), success_summary, error_text, run_id),
            )

    def upsert_source_status(
        self,
        source_name: str,
        status: str,
        started_at: str,
        finished_at: str | None,
        error_text: str | None,
    ) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sources (name, enabled, config_json, last_index_status, last_index_started_at, last_index_finished_at, last_error_text)
                VALUES (?, 1, NULL, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    enabled = 1,
                    last_index_status = excluded.last_index_status,
                    last_index_started_at = excluded.last_index_started_at,
                    last_index_finished_at = excluded.last_index_finished_at,
                    last_error_text = excluded.last_error_text
                """,
                (source_name, status, started_at, finished_at, error_text),
            )
            row = conn.execute("SELECT id FROM sources WHERE name = ?", (source_name,)).fetchone()
            return int(row["id"])

    def get_sources(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT name, enabled, config_json, last_index_status, last_index_started_at,
                       last_index_finished_at, last_error_text
                FROM sources
                ORDER BY name
                """
            ).fetchall()
        sources = []
        for row in rows:
            config = {}
            if row["config_json"]:
                try:
                    config = json.loads(row["config_json"])
                except json.JSONDecodeError:
                    config = {}
            sources.append(
                {
                    "name": row["name"],
                    "enabled": bool(row["enabled"]),
                    "config": config,
                    "last_index_status": row["last_index_status"],
                    "last_index_started_at": row["last_index_started_at"],
                    "last_index_finished_at": row["last_index_finished_at"],
                    "last_error_text": row["last_error_text"],
                }
            )
        return sources

    def upsert_source_config(self, source_name: str, config: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sources (name, enabled, config_json)
                VALUES (?, 1, ?)
                ON CONFLICT(name) DO UPDATE SET
                    config_json = excluded.config_json,
                    enabled = 1
                """,
                (source_name, json.dumps(config, sort_keys=True)),
            )

    def get_source_id(self, source_name: str) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM sources WHERE name = ?", (source_name,)).fetchone()
            if row is None:
                raise KeyError(source_name)
            return int(row["id"])

    def upsert_snapshot(
        self,
        source_name: str,
        run_id: int,
        indexed_at: str,
        auctions: Iterable[dict],
        lots: Iterable[dict],
    ) -> dict:
        source_id = self.get_source_id(source_name)
        auction_ids: dict[str, int] = {}
        auction_rows = list(auctions)
        lot_rows = list(lots)

        with self.connect() as conn:
            postal_cache: dict[str, tuple[str, str, float, float] | None] = {}
            for auction in auction_rows:
                postal_code = normalize_postal_code(auction.get("postal_code"))
                coords = None
                if postal_code:
                    coords = postal_cache.get(postal_code)
                    if postal_code not in postal_cache:
                        coords = postal_code_record(postal_code)
                        postal_cache[postal_code] = coords
                conn.execute(
                    """
                    INSERT INTO auctions (
                        source_id, provider_auction_id, title, url, address, city, state, postal_code, country,
                        latitude, longitude, distance_miles, raw_payload_json, indexed_at, updated_at, last_seen_run_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, provider_auction_id) DO UPDATE SET
                        title = excluded.title,
                        url = excluded.url,
                        address = excluded.address,
                        city = excluded.city,
                        state = excluded.state,
                        postal_code = excluded.postal_code,
                        country = excluded.country,
                        latitude = excluded.latitude,
                        longitude = excluded.longitude,
                        distance_miles = excluded.distance_miles,
                        raw_payload_json = excluded.raw_payload_json,
                        indexed_at = excluded.indexed_at,
                        updated_at = excluded.updated_at,
                        last_seen_run_id = excluded.last_seen_run_id
                    """,
                    (
                        source_id,
                        auction["provider_auction_id"],
                        auction["title"],
                        auction["url"],
                        auction.get("address"),
                        auction.get("city"),
                        auction.get("state"),
                        postal_code or auction.get("postal_code"),
                        auction.get("country"),
                        coords[2] if coords else auction.get("latitude"),
                        coords[3] if coords else auction.get("longitude"),
                        auction.get("distance_miles"),
                        json.dumps(auction["raw_payload"], sort_keys=True),
                        indexed_at,
                        indexed_at,
                        run_id,
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM auctions WHERE source_id = ? AND provider_auction_id = ?",
                    (source_id, auction["provider_auction_id"]),
                ).fetchone()
                auction_ids[auction["provider_auction_id"]] = int(row["id"])

            for lot in lot_rows:
                conn.execute(
                    """
                    INSERT INTO lots (
                        source_id, auction_id, provider_lot_id, lot_number, title, condition, description, details,
                        searchable_text, current_bid, shipping_available, url, status, end_time, indexed_at, updated_at,
                        last_seen_run_id, raw_payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, provider_lot_id) DO UPDATE SET
                        auction_id = excluded.auction_id,
                        lot_number = excluded.lot_number,
                        title = excluded.title,
                        condition = excluded.condition,
                        description = excluded.description,
                        details = excluded.details,
                        searchable_text = excluded.searchable_text,
                        current_bid = excluded.current_bid,
                        shipping_available = excluded.shipping_available,
                        url = excluded.url,
                        status = excluded.status,
                        end_time = excluded.end_time,
                        indexed_at = excluded.indexed_at,
                        updated_at = excluded.updated_at,
                        last_seen_run_id = excluded.last_seen_run_id,
                        raw_payload_json = excluded.raw_payload_json
                    """,
                    (
                        source_id,
                        auction_ids[lot["provider_auction_id"]],
                        lot["provider_lot_id"],
                        lot.get("lot_number"),
                        lot["title"],
                        lot.get("condition"),
                        lot.get("description"),
                        lot.get("details"),
                        lot["searchable_text"],
                        lot.get("current_bid"),
                        None if lot.get("shipping_available") is None else int(bool(lot.get("shipping_available"))),
                        lot["url"],
                        lot["status"],
                        lot["end_time"],
                        indexed_at,
                        indexed_at,
                        run_id,
                        json.dumps(lot["raw_payload"], sort_keys=True),
                    ),
                )
            conn.execute("DELETE FROM lots_fts")
            conn.execute(
                """
                INSERT INTO lots_fts(rowid, title, condition, description, details, source)
                SELECT l.id, l.title, l.condition, l.description, l.details, s.name
                FROM lots l
                JOIN sources s ON s.id = l.source_id
                """
            )

        return {"auctions": len(auction_rows), "lots": len(lot_rows)}

    def prune_source_rows(self, source_name: str, run_id: int, window_end: str) -> None:
        source_id = self.get_source_id(source_name)
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM lots WHERE source_id = ? AND (last_seen_run_id IS NULL OR last_seen_run_id != ?)",
                (source_id, run_id),
            )
            conn.execute(
                "DELETE FROM lots WHERE source_id = ? AND end_time > ?",
                (source_id, window_end),
            )
            conn.execute(
                "DELETE FROM lots WHERE source_id = ? AND status != 'open'",
                (source_id,),
            )
            conn.execute(
                """
                DELETE FROM auctions
                WHERE source_id = ?
                  AND id NOT IN (SELECT DISTINCT auction_id FROM lots WHERE source_id = ?)
                """,
                (source_id, source_id),
            )
            conn.execute("DELETE FROM lots_fts")
            conn.execute(
                """
                INSERT INTO lots_fts(rowid, title, condition, description, details, source)
                SELECT l.id, l.title, l.condition, l.description, l.details, s.name
                FROM lots l
                JOIN sources s ON s.id = l.source_id
                """
            )

    def query_results(
        self,
        query: str,
        now: datetime | None = None,
        sort_by: str = "relevance",
        sources: Iterable[str] | None = None,
        ending_within_hours: int | None = None,
        home_postal_code: str | None = None,
        radius_km: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        current = now or utc_now()
        now_iso = to_iso(current)
        max_hours = 24 * 7 if ending_within_hours is None else max(1, int(ending_within_hours))
        window_end = to_iso(current + timedelta(hours=max_hours))
        limit = max(1, min(int(limit or 50), 100))
        offset = max(0, int(offset or 0))
        token_groups = expanded_query_tokens(query)
        source_filters = [source.strip() for source in (sources or []) if source and source.strip()]
        home_postal_code = normalize_postal_code(home_postal_code)
        radius_km = float(radius_km) if radius_km is not None else None
        candidate_ids: list[int] | None = None
        if normalize_text(query):
            try:
                match_query = self._build_fts_match_query(query)
                candidate_ids = self._fts_candidate_ids(match_query, source_filters, window_end, now_iso)
            except Exception:
                candidate_ids = None
        if candidate_ids is not None and not candidate_ids:
            candidate_ids = None
        sql = """
            SELECT
                s.name AS source,
                a.title AS auction_title,
                a.address AS auction_address,
                a.distance_miles AS distance_miles,
                a.latitude AS auction_latitude,
                a.longitude AS auction_longitude,
                a.postal_code AS auction_postal_code,
                l.title AS lot_title,
                l.lot_number AS lot_number,
                l.current_bid AS current_bid,
                l.end_time AS end_time,
                l.status AS status,
                l.condition AS condition,
                l.description AS description,
                l.details AS details,
                l.url AS url,
                l.shipping_available AS shipping_available,
                l.raw_payload_json AS raw_payload_json
            FROM lots l
            JOIN auctions a ON a.id = l.auction_id
            JOIN sources s ON s.id = l.source_id
            WHERE l.status = 'open'
              AND l.end_time >= ?
              AND l.end_time <= ?
        """
        params: list[object] = [now_iso, window_end]
        if source_filters:
            sql += " AND s.name IN (" + ", ".join("?" for _ in source_filters) + ")"
            params.extend(source_filters)
        if candidate_ids is not None:
            if not candidate_ids:
                return [], 0
            sql += " AND l.id IN (" + ", ".join("?" for _ in candidate_ids) + ")"
            params.extend(candidate_ids)
        else:
            for token_group in token_groups:
                sql += " AND (" + " OR ".join("l.searchable_text LIKE ?" for _ in token_group) + ")"
                params.extend(f"%{token}%" for token in token_group)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        results = []
        origin_coords = postal_code_record(home_postal_code) if home_postal_code else None
        for row in rows:
            image_url = _extract_image_url(row["raw_payload_json"], row["url"])
            distance_km = None
            if origin_coords and row["auction_latitude"] is not None and row["auction_longitude"] is not None:
                _, _, origin_lat, origin_lon = origin_coords
                distance_km = distance_between_postal_codes_km(home_postal_code, row["auction_postal_code"]) if row["auction_postal_code"] else None
                if distance_km is None:
                    from geocode import haversine_miles
                    distance_km = haversine_miles((origin_lat, origin_lon), (row["auction_latitude"], row["auction_longitude"])) * 1.609344
            results.append(
                {
                    "source": row["source"],
                    "auction_title": row["auction_title"],
                    "sourceAuction": row["auction_title"],
                    "auction_address": row["auction_address"] or "",
                    "auctionAddress": row["auction_address"] or "",
                    "distance_miles": row["distance_miles"],
                    "distance_km": distance_km,
                    "lot_title": row["lot_title"],
                    "lot_number": row["lot_number"] or "",
                    "current_bid": row["current_bid"],
                    "currentPrice": row["current_bid"],
                    "end_time": row["end_time"],
                    "endTime": row["end_time"],
                    "end_time_iso": row["end_time"],
                    "time_left": format_time_left(row["end_time"], current),
                    "image_url": image_url,
                    "imageUrl": image_url,
                    "shipping_available": None
                    if row["shipping_available"] is None
                    else bool(row["shipping_available"]),
                    "condition": row["condition"] or "",
                    "description": row["description"] or "",
                    "details": row["details"] or "",
                    "url": row["url"],
                    "productUrl": row["url"],
                }
            )
        filtered = filter_and_sort_results(results, query)
        if origin_coords and radius_km is not None:
            filtered = [result for result in filtered if result.get("distance_km") is not None and result["distance_km"] <= radius_km]
        total = len(filtered)
        if sort_by == "relevance":
            return filtered[offset : offset + limit], total

        def sort_key(result: dict) -> tuple:
            current_bid = result.get("current_bid")
            has_price = isinstance(current_bid, (int, float))
            price = float(current_bid) if has_price else 0.0
            end_time_key = parse_iso(result.get("end_time"))
            end_sort = end_time_key if end_time_key is not None else datetime.max.replace(tzinfo=timezone.utc)
            title = result.get("lot_title") or ""
            if sort_by == "ending_soonest":
                return (end_sort, title.lower())
            if sort_by == "price_low_high":
                return (0 if has_price else 1, price, end_sort, title.lower())
            if sort_by == "price_high_low":
                return (0 if has_price else 1, -price, end_sort, title.lower())
            if sort_by == "proximity":
                dist = result.get("distance_km")
                return (0 if dist is not None else 1, dist if dist is not None else float("inf"), end_sort, title.lower())
            return (end_sort, title.lower())

        filtered.sort(key=sort_key)
        return filtered[offset : offset + limit], total

    def _build_fts_match_query(self, query: str) -> str:
        parts = []
        normalized = query_tokens(query)
        if not normalized:
            return ""
        for token in normalized:
            group = [token, *TERM_EXPANSIONS.get(token, [])]
            group_parts = []
            for candidate in group:
                safe = re.sub(r'["*:\'()]+', " ", candidate).strip()
                if not safe:
                    continue
                group_parts.append(f'{safe}*')
                group_parts.append(f'"{safe}"')
            if group_parts:
                parts.append("(" + " OR ".join(group_parts) + ")")
        phrase = normalize_text(query).replace('"', " ")
        if len(normalized) > 1 and phrase:
            parts.insert(0, f'"{phrase}"')
        return " AND ".join(parts)

    def _fts_candidate_ids(self, match_query: str, source_filters: list[str], window_end: str, now_iso: str) -> list[int]:
        if not match_query:
            return []
        sql = """
            SELECT l.id, bm25(lots_fts, 10.0, 3.0, 2.0, 1.0, 0.5) AS score
            FROM lots_fts
            JOIN lots l ON l.id = lots_fts.rowid
            JOIN sources s ON s.id = l.source_id
            WHERE lots_fts MATCH ?
              AND l.status = 'open'
              AND l.end_time >= ?
              AND l.end_time <= ?
        """
        params: list[object] = [match_query, now_iso, window_end]
        if source_filters:
            sql += " AND s.name IN (" + ", ".join("?" for _ in source_filters) + ")"
            params.extend(source_filters)
        sql += " ORDER BY score LIMIT 500"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [int(row["id"]) for row in rows]

    def rebuild_fts_index(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM lots_fts")
            conn.execute(
                """
                INSERT INTO lots_fts(rowid, title, condition, description, details, source)
                SELECT l.id, l.title, l.condition, l.description, l.details, s.name
                FROM lots l
                JOIN sources s ON s.id = l.source_id
                """
            )

    def get_postal_code_location(self, postal_code: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT postal_code, city, province, latitude, longitude FROM postal_codes WHERE postal_code = ?",
                (normalize_postal_code(postal_code),),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def get_metadata(self) -> SearchMetadata:
        with self.connect() as conn:
            last_success = conn.execute(
                """
                SELECT started_at, finished_at, success_summary, source_stats_json, heartbeat_at, progress_total, progress_done, progress_percent, progress_message
                FROM index_runs
                WHERE error_text IS NULL OR error_text = ''
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            latest_lot_indexed = conn.execute("SELECT MAX(indexed_at) AS indexed_at FROM lots").fetchone()
            last_run = conn.execute(
                """
                SELECT started_at, scope, finished_at, success_summary, error_text, heartbeat_at,
                       progress_total, progress_done, progress_percent, progress_message
                FROM index_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        source_stats = {}
        if last_success and last_success["source_stats_json"]:
            try:
                source_stats = json.loads(last_success["source_stats_json"])
            except json.JSONDecodeError:
                source_stats = {}
        indexed_source_count = len(source_stats) if source_stats else None
        indexed_auction_count = None
        indexed_lot_count = None
        if source_stats:
            indexed_auction_count = sum(int(stats.get("auctions", 0) or 0) for stats in source_stats.values())
            indexed_lot_count = sum(int(stats.get("lots", 0) or 0) for stats in source_stats.values())
        indexing = bool(last_run and last_run["finished_at"] is None and last_run["heartbeat_at"])
        stale = bool(last_run and last_run["finished_at"] is None and not indexing)
        return SearchMetadata(
            deploy_commit=None,
            indexed_at=(last_success["finished_at"] if last_success else None) or (latest_lot_indexed["indexed_at"] if latest_lot_indexed else None),
            last_run_status=None if last_run is None else ("error" if last_run["error_text"] else ("stalled" if stale else "success")),
            last_run_finished_at=last_run["finished_at"] if last_run else None,
            last_run_summary=last_run["success_summary"] if last_run else None,
            last_run_duration_seconds=(
                (parse_iso(last_run["finished_at"]) - parse_iso(last_run["started_at"])).total_seconds()
                if last_run and last_run["finished_at"] and parse_iso(last_run["started_at"]) and parse_iso(last_run["finished_at"])
                else None
            ),
            last_success_duration_seconds=(
                (parse_iso(last_success["finished_at"]) - parse_iso(last_success["started_at"])).total_seconds()
                if last_success and last_success["finished_at"] and parse_iso(last_success["started_at"]) and parse_iso(last_success["finished_at"])
                else None
            ),
            progress_total=last_run["progress_total"] if last_run else None,
            progress_done=last_run["progress_done"] if last_run else None,
            progress_percent=last_run["progress_percent"] if last_run else None,
            progress_message=last_run["progress_message"] if last_run else None,
            index_heartbeat_at=last_run["heartbeat_at"] if last_run else None,
            indexed_source_count=indexed_source_count,
            indexed_auction_count=indexed_auction_count,
            indexed_lot_count=indexed_lot_count,
            indexing=indexing,
            current_run_started_at=last_run["started_at"] if indexing and last_run else None,
            current_run_scope=last_run["scope"] if indexing and last_run else None,
            index_stale=stale,
        )

    def last_success_for_scope(self, scope: str) -> datetime | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT finished_at
                FROM index_runs
                WHERE scope = ? AND finished_at IS NOT NULL AND (error_text IS NULL OR error_text = '')
                ORDER BY id DESC
                LIMIT 1
                """,
                (scope,),
            ).fetchone()
        return parse_iso(row["finished_at"]) if row else None
