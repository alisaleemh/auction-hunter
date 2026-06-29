from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from models import AuctionRecord, LotRecord
from shadow_models import ShadowLotWorkUnit
from store import to_iso, utc_now


DEFAULT_SHADOW_DB_PATH = Path(__file__).resolve().parent / "data" / "auction_shadow_index.sqlite3"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shadow_index_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    heartbeat_at TEXT NOT NULL,
    discovered_total INTEGER NOT NULL DEFAULT 0,
    queued_count INTEGER NOT NULL DEFAULT 0,
    in_progress_count INTEGER NOT NULL DEFAULT 0,
    indexed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    progress_percent REAL NOT NULL DEFAULT 0,
    validation_success INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shadow_provider_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES shadow_index_runs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    discovered_total INTEGER NOT NULL DEFAULT 0,
    queued_count INTEGER NOT NULL DEFAULT 0,
    in_progress_count INTEGER NOT NULL DEFAULT 0,
    indexed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    progress_percent REAL NOT NULL DEFAULT 0,
    validated INTEGER NOT NULL DEFAULT 0,
    error_text TEXT,
    UNIQUE(run_id, source)
);

CREATE TABLE IF NOT EXISTS shadow_auctions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
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
    UNIQUE(source, provider_auction_id)
);

CREATE TABLE IF NOT EXISTS shadow_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    auction_id INTEGER NOT NULL REFERENCES shadow_auctions(id) ON DELETE CASCADE,
    provider_auction_id TEXT NOT NULL,
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
    raw_payload_json TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_run_id INTEGER,
    UNIQUE(source, provider_lot_id)
);

CREATE TABLE IF NOT EXISTS shadow_lot_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES shadow_index_runs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    provider_lot_id TEXT NOT NULL,
    provider_auction_id TEXT,
    lot_url TEXT,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_text TEXT,
    duration_seconds REAL NOT NULL,
    attempted_at TEXT NOT NULL,
    work_unit_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_index_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    provider_lot_id TEXT NOT NULL,
    provider_auction_id TEXT,
    lot_url TEXT,
    error_text TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    work_unit_json TEXT NOT NULL,
    failed_at TEXT NOT NULL,
    UNIQUE(source, provider_lot_id)
);

CREATE TABLE IF NOT EXISTS shadow_metrics (
    name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(name, source)
);

CREATE INDEX IF NOT EXISTS idx_shadow_lots_source ON shadow_lots(source);
CREATE INDEX IF NOT EXISTS idx_shadow_attempts_run_source ON shadow_lot_attempts(run_id, source);
CREATE INDEX IF NOT EXISTS idx_shadow_failures_source ON shadow_index_failures(source);
"""


class ShadowIndexStore:
    def __init__(self, db_path: Path | str = DEFAULT_SHADOW_DB_PATH):
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
            conn.executescript(SCHEMA)

    def start_run(self, scope: str) -> int:
        now = to_iso(utc_now())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO shadow_index_runs (scope, status, started_at, heartbeat_at)
                VALUES (?, 'running', ?, ?)
                """,
                (scope, now, now),
            )
            self._set_metric(conn, "auction_shadow_index_run_active", "", 1)
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str) -> None:
        now = to_iso(utc_now())
        summary = self.run_summary(run_id)
        started_at = summary.get("started_at")
        duration = 0.0
        if started_at:
            from store import parse_iso

            started = parse_iso(started_at)
            if started:
                duration = max(0.0, (utc_now() - started).total_seconds())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE shadow_index_runs
                SET status = ?, finished_at = ?, heartbeat_at = ?
                WHERE id = ?
                """,
                (status, now, now, run_id),
            )
            self._set_metric(conn, "auction_shadow_index_run_active", "", 0)
            self._set_metric(conn, "auction_shadow_index_duration_seconds", "", duration)

    def refresh_run_heartbeat(self, run_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE shadow_index_runs SET heartbeat_at = ? WHERE id = ?", (to_iso(utc_now()), run_id))

    def start_provider_run(self, run_id: int, source: str) -> None:
        now = to_iso(utc_now())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO shadow_provider_runs (run_id, source, status, started_at)
                VALUES (?, ?, 'running', ?)
                ON CONFLICT(run_id, source) DO UPDATE SET status = 'running', started_at = excluded.started_at
                """,
                (run_id, source, now),
            )
            self._rollup_run_locked(conn, run_id)

    def update_provider_progress(self, run_id: int, source: str, **progress: Any) -> None:
        allowed = {
            "status",
            "finished_at",
            "discovered_total",
            "queued_count",
            "in_progress_count",
            "indexed_count",
            "failed_count",
            "retry_count",
            "progress_percent",
            "validated",
            "error_text",
        }
        fields = {key: value for key, value in progress.items() if key in allowed}
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        with self.connect() as conn:
            conn.execute(f"UPDATE shadow_provider_runs SET {assignments} WHERE run_id = ? AND source = ?", (*values, run_id, source))
            self._update_provider_metrics_locked(conn, run_id, source)
            self._rollup_run_locked(conn, run_id)

    def upsert_auction(self, run_id: int, source: str, auction: AuctionRecord) -> None:
        now = to_iso(utc_now())
        with self.connect() as conn:
            self._upsert_auction_locked(conn, run_id, source, auction, now)

    def upsert_lot(self, run_id: int, source: str, auction: AuctionRecord, lot: LotRecord) -> None:
        now = to_iso(utc_now())
        with self.connect() as conn:
            auction_id = self._upsert_auction_locked(conn, run_id, source, auction, now)
            conn.execute(
                """
                INSERT INTO shadow_lots (
                    source, auction_id, provider_auction_id, provider_lot_id, lot_number, title, condition,
                    description, details, searchable_text, current_bid, shipping_available, url, status,
                    end_time, raw_payload_json, indexed_at, updated_at, last_seen_run_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, provider_lot_id) DO UPDATE SET
                    auction_id = excluded.auction_id,
                    provider_auction_id = excluded.provider_auction_id,
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
                    raw_payload_json = excluded.raw_payload_json,
                    indexed_at = excluded.indexed_at,
                    updated_at = excluded.updated_at,
                    last_seen_run_id = excluded.last_seen_run_id
                """,
                (
                    source,
                    auction_id,
                    lot["provider_auction_id"],
                    lot["provider_lot_id"],
                    lot.get("lot_number") or "",
                    lot["title"],
                    lot.get("condition") or "",
                    lot.get("description") or "",
                    lot.get("details") or "",
                    lot.get("searchable_text") or "",
                    lot.get("current_bid"),
                    None if lot.get("shipping_available") is None else int(bool(lot.get("shipping_available"))),
                    lot["url"],
                    lot["status"],
                    lot["end_time"],
                    json.dumps(lot.get("raw_payload") or {}, sort_keys=True),
                    now,
                    now,
                    run_id,
                ),
            )

    def record_lot_attempt(
        self,
        run_id: int,
        source: str,
        work_unit: ShadowLotWorkUnit,
        attempt_number: int,
        status: str,
        duration_seconds: float,
        error_text: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO shadow_lot_attempts (
                    run_id, source, provider_lot_id, provider_auction_id, lot_url, attempt_number,
                    status, error_text, duration_seconds, attempted_at, work_unit_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source,
                    work_unit.provider_lot_id,
                    work_unit.provider_auction_id,
                    work_unit.url,
                    attempt_number,
                    status,
                    error_text,
                    duration_seconds,
                    to_iso(utc_now()),
                    json.dumps(work_unit.payload, sort_keys=True),
                ),
            )

    def record_failure(
        self,
        run_id: int,
        source: str,
        work_unit: ShadowLotWorkUnit,
        error_text: str,
        attempts: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO shadow_index_failures (
                    run_id, source, provider_lot_id, provider_auction_id, lot_url, error_text,
                    attempts, work_unit_json, failed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, provider_lot_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    provider_auction_id = excluded.provider_auction_id,
                    lot_url = excluded.lot_url,
                    error_text = excluded.error_text,
                    attempts = excluded.attempts,
                    work_unit_json = excluded.work_unit_json,
                    failed_at = excluded.failed_at
                """,
                (
                    run_id,
                    source,
                    work_unit.provider_lot_id,
                    work_unit.provider_auction_id,
                    work_unit.url,
                    error_text,
                    attempts,
                    json.dumps(work_unit.payload, sort_keys=True),
                    to_iso(utc_now()),
                ),
            )

    def clear_failure(self, source: str, provider_lot_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM shadow_index_failures WHERE source = ? AND provider_lot_id = ?", (source, provider_lot_id))

    def list_failures(self, source: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM shadow_index_failures"
        params: tuple[Any, ...] = ()
        if source:
            sql += " WHERE source = ?"
            params = (source,)
        sql += " ORDER BY failed_at, id"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def run_summary(self, run_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM shadow_index_runs WHERE id = ?", (run_id,)).fetchone()
            providers = conn.execute("SELECT * FROM shadow_provider_runs WHERE run_id = ? ORDER BY source", (run_id,)).fetchall()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        result["providers"] = [dict(provider) for provider in providers]
        return result

    def latest_metrics(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT name, source, value FROM shadow_metrics ORDER BY name, source").fetchall()
        return {(row["name"], row["source"]): row["value"] for row in rows}

    def _upsert_auction_locked(self, conn: sqlite3.Connection, run_id: int, source: str, auction: AuctionRecord, now: str) -> int:
        conn.execute(
            """
            INSERT INTO shadow_auctions (
                source, provider_auction_id, title, url, address, city, state, postal_code,
                country, latitude, longitude, distance_miles, raw_payload_json, indexed_at,
                updated_at, last_seen_run_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, provider_auction_id) DO UPDATE SET
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
                source,
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
                json.dumps(auction.get("raw_payload") or {}, sort_keys=True),
                now,
                now,
                run_id,
            ),
        )
        row = conn.execute("SELECT id FROM shadow_auctions WHERE source = ? AND provider_auction_id = ?", (source, auction["provider_auction_id"])).fetchone()
        return int(row["id"])

    def _rollup_run_locked(self, conn: sqlite3.Connection, run_id: int) -> None:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(discovered_total), 0) AS discovered_total,
                COALESCE(SUM(queued_count), 0) AS queued_count,
                COALESCE(SUM(in_progress_count), 0) AS in_progress_count,
                COALESCE(SUM(indexed_count), 0) AS indexed_count,
                COALESCE(SUM(failed_count), 0) AS failed_count,
                COALESCE(SUM(retry_count), 0) AS retry_count,
                MIN(CASE WHEN validated THEN 1 ELSE 0 END) AS validation_success
            FROM shadow_provider_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        discovered = int(row["discovered_total"] or 0)
        indexed = int(row["indexed_count"] or 0)
        failed = int(row["failed_count"] or 0)
        percent = min(100.0, round(((indexed + failed) / discovered) * 100, 1)) if discovered else 0.0
        validation = int(bool(discovered and indexed + failed == discovered and row["validation_success"]))
        conn.execute(
            """
            UPDATE shadow_index_runs
            SET heartbeat_at = ?, discovered_total = ?, queued_count = ?, in_progress_count = ?,
                indexed_count = ?, failed_count = ?, retry_count = ?, progress_percent = ?,
                validation_success = ?
            WHERE id = ?
            """,
            (
                to_iso(utc_now()),
                discovered,
                int(row["queued_count"] or 0),
                int(row["in_progress_count"] or 0),
                indexed,
                failed,
                int(row["retry_count"] or 0),
                percent,
                validation,
                run_id,
            ),
        )

    def _update_provider_metrics_locked(self, conn: sqlite3.Connection, run_id: int, source: str) -> None:
        row = conn.execute("SELECT * FROM shadow_provider_runs WHERE run_id = ? AND source = ?", (run_id, source)).fetchone()
        if row is None:
            return
        self._set_metric(conn, "auction_shadow_lots_discovered_total", source, row["discovered_total"])
        self._set_metric(conn, "auction_shadow_lots_indexed_total", source, row["indexed_count"])
        self._set_metric(conn, "auction_shadow_lots_failed_total", source, row["failed_count"])
        self._set_metric(conn, "auction_shadow_lot_retries_total", source, row["retry_count"])
        self._set_metric(conn, "auction_shadow_index_validation_success", source, 1 if row["validated"] else 0)

    def _set_metric(self, conn: sqlite3.Connection, name: str, source: str, value: float) -> None:
        conn.execute(
            """
            INSERT INTO shadow_metrics (name, source, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name, source) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (name, source, float(value), to_iso(utc_now())),
        )
