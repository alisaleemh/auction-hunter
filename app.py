from __future__ import annotations

import argparse
import atexit
import logging
import os
import threading
import time
from urllib.parse import urlencode
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from indexer import run_index
from scheduler import NightlyIndexer
from store import AuctionStore, DEFAULT_DB_PATH


DB_PATH = Path(os.environ.get("AUCTION_SEARCH_DB", DEFAULT_DB_PATH))
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
store = AuctionStore(DB_PATH)
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
nightly_indexer: NightlyIndexer | None = None
manual_index_lock = threading.Lock()
deploy_commit = os.environ.get("DEPLOY_COMMIT", "").strip() or None


def metadata_payload() -> dict:
    metadata = store.get_metadata()
    return {
        "deploy_commit": deploy_commit,
        "indexed_at": metadata.indexed_at,
        "last_run_status": metadata.last_run_status,
        "last_run_finished_at": metadata.last_run_finished_at,
        "last_run_summary": metadata.last_run_summary,
        "indexing": metadata.indexing,
        "current_run_started_at": metadata.current_run_started_at,
        "current_run_scope": metadata.current_run_scope,
        "indexed_source_count": metadata.indexed_source_count,
        "indexed_auction_count": metadata.indexed_auction_count,
        "indexed_lot_count": metadata.indexed_lot_count,
        "last_run_duration_seconds": metadata.last_run_duration_seconds,
        "last_success_duration_seconds": metadata.last_success_duration_seconds,
        "progress_total": metadata.progress_total,
        "progress_done": metadata.progress_done,
        "progress_percent": metadata.progress_percent,
        "progress_message": metadata.progress_message,
        "index_heartbeat_at": metadata.index_heartbeat_at,
        "index_stale": metadata.index_stale,
    }


def indexing_history_payload() -> list[dict]:
    return store.get_index_run_history(limit=5)


def source_config_payload() -> list[dict]:
    sources = {source["name"]: source for source in store.get_sources()}
    return [
        {
            "name": "HiBid",
            "label": "HiBid",
            "enabled": True,
            "fields": [
                {"key": "zip_code", "label": "ZIP code", "type": "text", "default": "L9T 8N6"},
                {"key": "miles", "label": "Radius (miles)", "type": "number", "default": 25},
            ],
            "config": sources.get("HiBid", {}).get("config", {}),
        },
        {
            "name": "403 Auction",
            "label": "403 Auction",
            "enabled": True,
            "fields": [],
            "config": sources.get("403 Auction", {}).get("config", {}),
        },
        {
            "name": "King of the North Auction",
            "label": "King of the North Auction",
            "enabled": True,
            "fields": [],
            "config": sources.get("King of the North Auction", {}).get("config", {}),
        },
    ]


def _manual_reindex_worker() -> None:
    try:
        run_index(store, scope="manual")
    finally:
        manual_index_lock.release()
AVAILABLE_SOURCES = ("403 Auction", "HiBid")
ENDING_WINDOW_OPTIONS = (
    ("6", "Ending within 6 hours"),
    ("24", "Ending within 24 hours"),
    ("72", "Ending within 3 days"),
    ("168", "Ending within 7 days"),
)


def _parse_limit(value: str | None, default: int = 50) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _parse_offset(value: str | None, default: int = 0) -> int:
    try:
        return max(0, int(value or default))
    except (TypeError, ValueError):
        return default


def _parse_sources(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [value for value in values if value in AVAILABLE_SOURCES]


def _parse_ending_within(value: str | None) -> int | None:
    try:
        parsed = int(value) if value else None
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _parse_radius_km(value: str | None) -> float | None:
    try:
        parsed = float(value) if value else None
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _build_search_url(
    *,
    query: str,
    sort: str,
    limit: int,
    offset: int,
    sources: list[str] | None = None,
    ending_within: int | None = None,
    home_postal_code: str | None = None,
    radius_km: float | None = None,
) -> str:
    params: list[tuple[str, str]] = []
    if query:
        params.append(("q", query))
    if sort:
        params.append(("sort", sort))
    if limit:
        params.append(("limit", str(limit)))
    if offset > 0:
        params.append(("offset", str(offset)))
    if ending_within:
        params.append(("ending_within", str(ending_within)))
    if home_postal_code:
        params.append(("home_postal_code", home_postal_code))
    if radius_km:
        params.append(("radius_km", str(radius_km)))
    for source in sources or []:
        params.append(("source", source))
    return "/?" + urlencode(params)


def run_search(
    query: str,
    sort_by: str = "relevance",
    limit: int = 50,
    offset: int = 0,
    sources: list[str] | None = None,
    ending_within_hours: int | None = None,
    home_postal_code: str | None = None,
    radius_km: float | None = None,
) -> tuple[list[dict], int, list[str]]:
    results, total = store.query_results(
        query,
        sort_by=sort_by,
        sources=sources,
        ending_within_hours=ending_within_hours,
        home_postal_code=home_postal_code,
        radius_km=radius_km,
        limit=limit,
        offset=offset,
    )
    return results, total, []


@app.get("/")
def index():
    query = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "ending_soonest").strip() or "ending_soonest"
    limit = _parse_limit(request.args.get("limit"), 50)
    offset = _parse_offset(request.args.get("offset"), 0)
    selected_sources = _parse_sources(request.args.getlist("source"))
    ending_within_hours = _parse_ending_within(request.args.get("ending_within"))
    home_postal_code = request.args.get("home_postal_code", "").strip() or None
    radius_km = _parse_radius_km(request.args.get("radius_km"))
    results, total, errors = run_search(
        query,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
        sources=selected_sources,
        ending_within_hours=ending_within_hours,
        home_postal_code=home_postal_code,
        radius_km=radius_km,
    )
    metadata = store.get_metadata()
    return render_template(
        "index.html",
        query=query,
        sort=sort_by,
        limit=limit,
        offset=offset,
        sources=selected_sources,
        ending_within=ending_within_hours,
        home_postal_code=home_postal_code,
        radius_km=radius_km,
        available_sources=AVAILABLE_SOURCES,
        ending_window_options=ENDING_WINDOW_OPTIONS,
        build_search_url=_build_search_url,
        results=results,
        total=total,
        errors=errors,
        metadata=metadata,
        deploy_commit=deploy_commit,
        index_sources=source_config_payload(),
        indexing_history=indexing_history_payload(),
    )


@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "relevance").strip() or "relevance"
    limit = _parse_limit(request.args.get("limit"), 50)
    offset = _parse_offset(request.args.get("offset"), 0)
    selected_sources = _parse_sources(request.args.getlist("source"))
    ending_within_hours = _parse_ending_within(request.args.get("ending_within"))
    home_postal_code = request.args.get("home_postal_code", "").strip() or None
    radius_km = _parse_radius_km(request.args.get("radius_km"))
    results, total, errors = run_search(
        query,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
        sources=selected_sources,
        ending_within_hours=ending_within_hours,
        home_postal_code=home_postal_code,
        radius_km=radius_km,
    )
    metadata = metadata_payload()
    return jsonify(
        {
            "query": query,
            "count": len(results),
            "total": total,
            "offset": offset,
            "results": results,
            "errors": errors,
            "sort": sort_by,
            "limit": limit,
            **metadata,
            "sources": selected_sources,
            "ending_within": ending_within_hours,
            "home_postal_code": home_postal_code,
            "radius_km": radius_km,
        }
    )


@app.get("/api/status")
def api_status():
    return jsonify({**metadata_payload(), "indexing_history": indexing_history_payload()})


@app.get("/api/index-config")
def api_index_config():
    return jsonify({"sources": source_config_payload()})


@app.post("/api/index-config")
def api_index_config_update():
    payload = request.get_json(silent=True) or {}
    sources = payload.get("sources") or {}
    if not isinstance(sources, dict):
        return jsonify({"error": "sources must be an object"}), 400
    updated = []
    for source_name, config in sources.items():
        if not isinstance(config, dict):
            continue
        store.upsert_source_config(source_name, config)
        updated.append(source_name)
    return jsonify({"status": "ok", "updated": updated, **{"sources": source_config_payload()}})


@app.post("/api/reindex")
def api_reindex():
    metadata = store.get_metadata()
    if metadata.indexing or manual_index_lock.locked():
        return jsonify({"status": "running", **metadata_payload()}), 409
    if not manual_index_lock.acquire(blocking=False):
        return jsonify({"status": "running", **metadata_payload()}), 409
    worker = threading.Thread(target=_manual_reindex_worker, name="auction-index-manual", daemon=True)
    worker.start()
    for _ in range(20):
        if store.get_metadata().indexing:
            break
        time.sleep(0.05)
    response_payload = metadata_payload()
    response_payload["indexing"] = True
    response_payload["current_run_scope"] = response_payload["current_run_scope"] or "manual"
    return jsonify({"status": "started", **response_payload}), 202


def serve(port: int) -> None:
    global nightly_indexer
    nightly_indexer = NightlyIndexer(store)
    nightly_indexer.start()
    atexit.register(lambda: nightly_indexer.stop() if nightly_indexer else None)
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    app.run(debug=debug, host="0.0.0.0", port=port, use_reloader=debug)


def main() -> None:
    parser = argparse.ArgumentParser(description="Auction search tool")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("index", help="Rebuild the local index")
    subparsers.add_parser("rebuild-fts", help="Rebuild the SQLite FTS index")
    serve_parser = subparsers.add_parser("serve", help="Run the web app and nightly scheduler")
    serve_parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5001")))
    args = parser.parse_args()

    if args.command == "index":
        result = run_index(store, scope="manual")
        print(result["summary"])
        if result["errors"]:
            print("; ".join(result["errors"]))
        return
    if args.command == "rebuild-fts":
        store.rebuild_fts_index()
        print("rebuilt FTS index")
        return

    port = getattr(args, "port", int(os.environ.get("PORT", "5001")))
    serve(port)


if __name__ == "__main__":
    main()
