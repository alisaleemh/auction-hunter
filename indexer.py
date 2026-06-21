from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import threading
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
    source_configs = {source["name"]: source.get("config") or {} for source in store.get_sources()}

    def hibid_loader() -> ProviderSnapshot:
        return hibid.fetch_snapshot(source_configs.get("HiBid", {}))

    def auction403_loader() -> ProviderSnapshot:
        return auction403.fetch_snapshot(source_configs.get("403 Auction", {}))

    loaders = provider_loaders or {
        "HiBid": hibid_loader,
        "403 Auction": auction403_loader,
    }

    source_stats: dict[str, dict] = {}
    successful_sources: list[str] = []
    errors: list[str] = []
    heartbeat_stop = threading.Event()

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(15):
            store.refresh_index_run_heartbeat(run_id)

    heartbeat_thread = threading.Thread(target=heartbeat_loop, name="auction-index-heartbeat", daemon=True)
    heartbeat_thread.start()
    store.update_index_run_progress(run_id, progress_total=len(loaders), progress_done=0, progress_percent=0.0, progress_message="Starting index")

    try:
        with ThreadPoolExecutor(max_workers=len(loaders)) as executor:
            future_map = {executor.submit(loader): name for name, loader in loaders.items()}
            completed = 0
            for future in as_completed(future_map):
                source_name = future_map[future]
                store.upsert_source_status(source_name, "running", started_at, None, None)
                try:
                    store.update_index_run_progress(
                        run_id,
                        progress_total=len(loaders),
                        progress_done=completed,
                        progress_percent=round((completed / len(loaders)) * 100, 1) if len(loaders) else 0.0,
                        progress_message=f"Fetching {source_name}",
                    )
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
                completed += 1
                store.update_index_run_progress(
                    run_id,
                    progress_total=len(loaders),
                    progress_done=completed,
                    progress_percent=round((completed / len(loaders)) * 100, 1) if len(loaders) else 100.0,
                    progress_message=f"Indexed {completed}/{len(loaders)} sources",
                )
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)

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
