from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

from search import expanded_query_tokens, filter_and_sort_results


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
            ("progress_total", "INTEGER"),
            ("progress_done", "INTEGER"),
            ("progress_percent", "REAL"),
            ("progress_message", "TEXT"),
        ):
            if column not in index_run_columns:
                conn.execute(f"ALTER TABLE index_runs ADD COLUMN {column} {ddl_type}")

    def start_index_run(self, scope: str, started_at: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO index_runs (started_at, scope, progress_total, progress_done, progress_percent, progress_message) VALUES (?, ?, ?, ?, ?, ?)",
                (started_at, scope, None, None, None, None),
            )
            return int(cursor.lastrowid)

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
                SET progress_total = ?, progress_done = ?, progress_percent = ?, progress_message = ?
                WHERE id = ?
                """,
                (progress_total, progress_done, progress_percent, progress_message, run_id),
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
            for auction in auction_rows:
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
                        auction.get("postal_code"),
                        auction.get("country"),
                        auction.get("latitude"),
                        auction.get("longitude"),
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

    def query_results(
        self,
        query: str,
        now: datetime | None = None,
        sort_by: str = "relevance",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        current = now or utc_now()
        now_iso = to_iso(current)
        window_end = to_iso(current + timedelta(days=7))
        token_groups = expanded_query_tokens(query)
        limit = max(1, min(int(limit or 50), 100))
        offset = max(0, int(offset or 0))
        sql = """
            SELECT
                s.name AS source,
                a.title AS auction_title,
                a.address AS auction_address,
                a.distance_miles AS distance_miles,
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
        for token_group in token_groups:
            sql += " AND (" + " OR ".join("l.searchable_text LIKE ?" for _ in token_group) + ")"
            params.extend(f"%{token}%" for token in token_group)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            image_url = _extract_image_url(row["raw_payload_json"], row["url"])
            results.append(
                {
                    "source": row["source"],
                    "auction_title": row["auction_title"],
                    "sourceAuction": row["auction_title"],
                    "auction_address": row["auction_address"] or "",
                    "auctionAddress": row["auction_address"] or "",
                    "distance_miles": row["distance_miles"],
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
            return (end_sort, title.lower())

        filtered.sort(key=sort_key)
        return filtered[offset : offset + limit], total

    def get_metadata(self) -> SearchMetadata:
        with self.connect() as conn:
            last_success = conn.execute(
                """
                SELECT started_at, finished_at, success_summary, source_stats_json, progress_total, progress_done, progress_percent, progress_message
                FROM index_runs
                WHERE error_text IS NULL OR error_text = ''
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            latest_lot_indexed = conn.execute("SELECT MAX(indexed_at) AS indexed_at FROM lots").fetchone()
            last_run = conn.execute(
                """
                SELECT started_at, scope, finished_at, success_summary, error_text,
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
        indexing = bool(last_run and last_run["finished_at"] is None)
        return SearchMetadata(
            deploy_commit=None,
            indexed_at=(last_success["finished_at"] if last_success else None) or (latest_lot_indexed["indexed_at"] if latest_lot_indexed else None),
            last_run_status=None if last_run is None else ("error" if last_run["error_text"] else "success"),
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
            indexed_source_count=indexed_source_count,
            indexed_auction_count=indexed_auction_count,
            indexed_lot_count=indexed_lot_count,
            indexing=indexing,
            current_run_started_at=last_run["started_at"] if indexing and last_run else None,
            current_run_scope=last_run["scope"] if indexing and last_run else None,
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
