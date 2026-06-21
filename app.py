from __future__ import annotations

import argparse
import atexit
import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from indexer import run_index
from scheduler import NightlyIndexer
from store import AuctionStore, DEFAULT_DB_PATH


DB_PATH = Path(os.environ.get("AUCTION_SEARCH_DB", DEFAULT_DB_PATH))
store = AuctionStore(DB_PATH)
app = Flask(__name__)
nightly_indexer: NightlyIndexer | None = None
manual_index_lock = threading.Lock()


def metadata_payload() -> dict:
    metadata = store.get_metadata()
    return {
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
    }


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
    ]


def _manual_reindex_worker() -> None:
    try:
        run_index(store, scope="manual")
    finally:
        manual_index_lock.release()


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


def run_search(query: str, sort_by: str = "relevance", limit: int = 50, offset: int = 0) -> tuple[list[dict], int, list[str]]:
    results, total = store.query_results(query, sort_by=sort_by, limit=limit, offset=offset)
    return results, total, []


@app.get("/")
def index():
    query = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "ending_soonest").strip() or "ending_soonest"
    limit = _parse_limit(request.args.get("limit"), 50)
    offset = _parse_offset(request.args.get("offset"), 0)
    results, total, errors = run_search(query, sort_by=sort_by, limit=limit, offset=offset)
    metadata = store.get_metadata()
    return render_template(
        "index.html",
        query=query,
        sort=sort_by,
        limit=limit,
        offset=offset,
        results=results,
        total=total,
        errors=errors,
        metadata=metadata,
        index_sources=source_config_payload(),
    )


@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "relevance").strip() or "relevance"
    limit = _parse_limit(request.args.get("limit"), 50)
    offset = _parse_offset(request.args.get("offset"), 0)
    results, total, errors = run_search(query, sort_by=sort_by, limit=limit, offset=offset)
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
        }
    )


@app.get("/api/status")
def api_status():
    return jsonify(metadata_payload())


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
    serve_parser = subparsers.add_parser("serve", help="Run the web app and nightly scheduler")
    serve_parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5001")))
    args = parser.parse_args()

    if args.command == "index":
        result = run_index(store, scope="manual")
        print(result["summary"])
        if result["errors"]:
            print("; ".join(result["errors"]))
        return

    port = getattr(args, "port", int(os.environ.get("PORT", "5001")))
    serve(port)


if __name__ == "__main__":
    main()
