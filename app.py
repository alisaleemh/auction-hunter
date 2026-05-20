from __future__ import annotations

import argparse
import atexit
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from indexer import run_index
from scheduler import NightlyIndexer
from store import AuctionStore, DEFAULT_DB_PATH


DB_PATH = Path(os.environ.get("AUCTION_SEARCH_DB", DEFAULT_DB_PATH))
store = AuctionStore(DB_PATH)
app = Flask(__name__)
nightly_indexer: NightlyIndexer | None = None


def run_search(query: str) -> tuple[list[dict], list[str]]:
    return store.query_results(query), []


@app.get("/")
def index():
    query = request.args.get("q", "").strip()
    results, errors = run_search(query) if query else ([], [])
    metadata = store.get_metadata()
    return render_template("index.html", query=query, results=results, errors=errors, metadata=metadata)


@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    results, errors = run_search(query) if query else ([], [])
    metadata = store.get_metadata()
    return jsonify(
        {
            "query": query,
            "count": len(results),
            "results": results,
            "errors": errors,
            "indexed_at": metadata.indexed_at,
            "last_run_status": metadata.last_run_status,
            "last_run_finished_at": metadata.last_run_finished_at,
        }
    )


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
