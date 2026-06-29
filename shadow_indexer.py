from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from flask import Flask, Response, jsonify

from shadow_providers import default_providers
from shadow_runner import IndexRunner
from shadow_store import DEFAULT_SHADOW_DB_PATH, ShadowIndexStore
from store import AuctionStore, DEFAULT_DB_PATH


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def _source_configs() -> dict[str, dict]:
    try:
        store = AuctionStore(DEFAULT_DB_PATH)
        return {source["name"]: source.get("config") or {} for source in store.get_sources() if source.get("enabled", True)}
    except Exception as exc:
        logger.warning("shadow indexer could not read existing source config: %s", exc)
        return {}


def build_runner(db_path: Path | str = DEFAULT_SHADOW_DB_PATH) -> IndexRunner:
    repository = ShadowIndexStore(db_path)
    providers = default_providers(_source_configs())
    return IndexRunner(
        repository,
        providers,
        global_workers=int(os.getenv("SHADOW_INDEX_GLOBAL_WORKERS", "32")),
        per_provider_workers=int(os.getenv("SHADOW_INDEX_PROVIDER_WORKERS", "8")),
        max_attempts=int(os.getenv("SHADOW_INDEX_MAX_ATTEMPTS", "3")),
        backoff_seconds=float(os.getenv("SHADOW_INDEX_BACKOFF_SECONDS", "1")),
    )


def create_app(db_path: Path | str = DEFAULT_SHADOW_DB_PATH) -> Flask:
    app = Flask(__name__)
    store = ShadowIndexStore(db_path)

    @app.get("/health")
    def health():
        metrics = store.latest_metrics()
        active = metrics.get(("auction_shadow_index_run_active", ""), 0)
        return jsonify({"ok": True, "active": bool(active), "db_path": str(store.db_path)})

    @app.get("/metrics")
    def metrics():
        lines = []
        values = store.latest_metrics()
        for (name, source), value in values.items():
            if source:
                escaped = source.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{name}{{source="{escaped}"}} {value:g}')
            else:
                lines.append(f"{name} {value:g}")
        return Response("\n".join(lines) + ("\n" if lines else ""), mimetype="text/plain; version=0.0.4")

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone shadow auction indexer")
    parser.add_argument("--db", default=str(DEFAULT_SHADOW_DB_PATH), help="shadow SQLite database path")
    subcommands = parser.add_subparsers(dest="command")

    run_parser = subcommands.add_parser("run", help="run a full shadow index")
    run_parser.add_argument("--scope", default="manual")

    retry_parser = subcommands.add_parser("retry-failures", help="retry final failed lots")
    retry_parser.add_argument("--source")

    serve_parser = subcommands.add_parser("serve", help="serve health and metrics endpoints")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", default=int(os.getenv("SHADOW_INDEXER_PORT", "5002")), type=int)

    args = parser.parse_args(argv)
    command = args.command or "run"

    if command == "serve":
        create_app(args.db).run(host=args.host, port=args.port)
        return 0

    runner = build_runner(args.db)
    if command == "retry-failures":
        result = runner.retry_failures(source=args.source)
    else:
        result = runner.run(scope=args.scope)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
