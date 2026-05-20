from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable

from models import ProviderSnapshot, make_lot_record
from providers import auction403, hibid
from store import AuctionStore, SearchMetadata, to_iso, utc_now


WINDOW_DAYS = 7


def _window_end(now: datetime) -> datetime:
    return now + timedelta(days=WINDOW_DAYS)


def _filter_snapshot(snapshot: ProviderSnapshot, now: datetime) -> ProviderSnapshot:
    end = _window_end(now)
    provider_auction_ids = set()
    filtered_lots = []
    for lot in snapshot.lots:
        end_time = datetime.fromisoformat(lot["end_time"].replace("Z", "+00:00"))
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        end_time = end_time.astimezone(timezone.utc)
        if lot["status"] != "open":
            continue
        if end_time < now or end_time > end:
            continue
        filtered_lots.append(lot)
        provider_auction_ids.add(lot["provider_auction_id"])

    filtered_auctions = [auction for auction in snapshot.auctions if auction["provider_auction_id"] in provider_auction_ids]
    return ProviderSnapshot(source=snapshot.source, auctions=filtered_auctions, lots=filtered_lots)


def run_index(
    store: AuctionStore,
    scope: str = "manual",
    now: datetime | None = None,
    provider_loaders: dict[str, Callable[[], ProviderSnapshot]] | None = None,
) -> dict:
    current = (now or utc_now()).astimezone(timezone.utc)
    started_at = to_iso(current)
    run_id = store.start_index_run(scope=scope, started_at=started_at)
    loaders = provider_loaders or {
        "HiBid": hibid.fetch_snapshot,
        "403 Auction": auction403.fetch_snapshot,
    }

    source_stats: dict[str, dict] = {}
    successful_sources: list[str] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=len(loaders)) as executor:
        future_map = {executor.submit(loader): name for name, loader in loaders.items()}
        for future in as_completed(future_map):
            source_name = future_map[future]
            store.upsert_source_status(source_name, "running", started_at, None, None)
            try:
                snapshot = _filter_snapshot(future.result(), current)
                stats = store.upsert_snapshot(source_name, run_id, started_at, snapshot.auctions, snapshot.lots)
                store.prune_source_rows(source_name, run_id, to_iso(_window_end(current)))
                store.upsert_source_status(source_name, "success", started_at, started_at, None)
                successful_sources.append(source_name)
                source_stats[source_name] = {"status": "success", **stats}
            except Exception as exc:
                error_text = str(exc)
                store.upsert_source_status(source_name, "error", started_at, started_at, error_text)
                source_stats[source_name] = {"status": "error", "error": error_text}
                errors.append(f"{source_name}: {error_text}")

    finished_at = to_iso(utc_now())
    success_count = sum(1 for stats in source_stats.values() if stats["status"] == "success")
    summary = f"{success_count}/{len(loaders)} sources indexed"
    store.finish_index_run(
        run_id=run_id,
        finished_at=finished_at,
        source_stats=source_stats,
        success_summary=summary,
        error_text="; ".join(errors) if errors else None,
    )
    return {"run_id": run_id, "summary": summary, "errors": errors, "source_stats": source_stats}
