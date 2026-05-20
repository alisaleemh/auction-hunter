from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from search import expanded_query_tokens, filter_and_sort_results


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "auction_index.sqlite3"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
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
    indexed_at: str | None
    last_run_status: str | None
    last_run_finished_at: str | None
    last_run_summary: str | None


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


class AuctionStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def start_index_run(self, scope: str, started_at: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO index_runs (started_at, scope) VALUES (?, ?)",
                (started_at, scope),
            )
            return int(cursor.lastrowid)

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
                INSERT INTO sources (name, enabled, last_index_status, last_index_started_at, last_index_finished_at, last_error_text)
                VALUES (?, 1, ?, ?, ?, ?)
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

    def query_results(self, query: str, now: datetime | None = None) -> list[dict]:
        current = now or utc_now()
        now_iso = to_iso(current)
        window_end = to_iso(current + timedelta(days=7))
        token_groups = expanded_query_tokens(query)
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
                l.shipping_available AS shipping_available
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
            results.append(
                {
                    "source": row["source"],
                    "auction_title": row["auction_title"],
                    "auction_address": row["auction_address"] or "",
                    "distance_miles": row["distance_miles"],
                    "lot_title": row["lot_title"],
                    "lot_number": row["lot_number"] or "",
                    "current_bid": row["current_bid"],
                    "end_time": row["end_time"],
                    "end_time_iso": row["end_time"],
                    "time_left": format_time_left(row["end_time"], current),
                    "shipping_available": None
                    if row["shipping_available"] is None
                    else bool(row["shipping_available"]),
                    "condition": row["condition"] or "",
                    "description": row["description"] or "",
                    "details": row["details"] or "",
                    "url": row["url"],
                }
            )
        return filter_and_sort_results(results, query)

    def get_metadata(self) -> SearchMetadata:
        with self.connect() as conn:
            last_success = conn.execute(
                """
                SELECT finished_at, success_summary
                FROM index_runs
                WHERE error_text IS NULL OR error_text = ''
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            last_run = conn.execute(
                """
                SELECT finished_at, success_summary, error_text
                FROM index_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return SearchMetadata(
            indexed_at=last_success["finished_at"] if last_success else None,
            last_run_status=None if last_run is None else ("error" if last_run["error_text"] else "success"),
            last_run_finished_at=last_run["finished_at"] if last_run else None,
            last_run_summary=last_run["success_summary"] if last_run else None,
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
