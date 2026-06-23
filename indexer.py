from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import threading
import logging
from typing import Callable

from models import ProviderEstimate, ProviderSnapshot, make_lot_record
from providers import auction403, hibid, kotn
from store import AuctionStore, SearchMetadata, to_iso, utc_now


logger = logging.getLogger(__name__)


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

    def hibid_estimator() -> ProviderEstimate:
        return hibid.estimate_snapshot(source_configs.get("HiBid", {}))

    def auction403_loader() -> ProviderSnapshot:
        return auction403.fetch_snapshot(source_configs.get("403 Auction", {}))

    def auction403_estimator() -> ProviderEstimate:
        return auction403.estimate_snapshot(source_configs.get("403 Auction", {}))

    def kotn_loader() -> ProviderSnapshot:
        return kotn.fetch_snapshot(source_configs.get("King of the North Auction", {}))

    def kotn_estimator() -> ProviderEstimate:
        return kotn.estimate_snapshot(source_configs.get("King of the North Auction", {}))

    default_loaders = {
        "HiBid": hibid_loader,
        "403 Auction": auction403_loader,
        "King of the North Auction": kotn_loader,
    }
    loaders = provider_loaders or default_loaders
    estimators = {
        "HiBid": hibid_estimator,
        "403 Auction": auction403_estimator,
        "King of the North Auction": kotn_estimator,
    } if loaders is default_loaders else {}

    source_stats: dict[str, dict] = {}
    source_progress: dict[str, dict] = {name: {"total": None, "done": 0, "status": "queued"} for name in loaders}
    for source_name, estimator in estimators.items():
        try:
            estimate = estimator()
            source_progress[source_name] = {"total": estimate.lots, "done": 0, "status": "estimated"}
        except Exception as exc:
            logger.warning("index estimate failed source=%s error=%s", source_name, exc)
    successful_sources: list[str] = []
    errors: list[str] = []
    estimated_total = sum(int(stats.get("total") or 0) for stats in source_progress.values() if stats.get("total") is not None)
    store.update_index_run_progress(
        run_id,
        progress_total=estimated_total or None,
        progress_done=0,
        progress_percent=0.0 if estimated_total else None,
        progress_message=f"Estimated {estimated_total} items across {len(loaders)} sources" if estimated_total else f"Fetching {len(loaders)} sources",
        source_progress=source_progress,
    )
    logger.info("index run start run_id=%s scope=%s sources=%s", run_id, scope, list(loaders))
    heartbeat_stop = threading.Event()

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(15):
            store.refresh_index_run_heartbeat(run_id)

    heartbeat_thread = threading.Thread(target=heartbeat_loop, name="auction-index-heartbeat", daemon=True)
    heartbeat_thread.start()

    try:
        with ThreadPoolExecutor(max_workers=len(loaders)) as executor:
            future_map = {executor.submit(loader): name for name, loader in loaders.items()}
            completed = 0
            for future in as_completed(future_map):
                source_name = future_map[future]
                store.upsert_source_status(source_name, "running", started_at, None, None)
                source_progress[source_name] = {
                    "total": source_progress.get(source_name, {}).get("total"),
                    "done": 0,
                    "status": "running",
                }
                store.update_index_run_progress(
                    run_id,
                    progress_total=estimated_total or None,
                    progress_done=sum(int(stats.get("done") or 0) for stats in source_progress.values()),
                    progress_percent=None,
                    progress_message=f"Fetching {source_name}",
                    source_progress=source_progress,
                )
                logger.info("index source start run_id=%s source=%s", run_id, source_name)
                try:
                    snapshot = future.result()
                    stats = store.upsert_snapshot(source_name, run_id, started_at, snapshot.auctions, snapshot.lots)
                    store.prune_source_rows(source_name, run_id, to_iso(current))
                    store.upsert_source_status(source_name, "success", started_at, started_at, None)
                    successful_sources.append(source_name)
                    source_stats[source_name] = {"status": "success", **stats}
                    source_progress[source_name] = {"total": stats.get("lots", 0), "done": stats.get("lots", 0), "status": "success"}
                    logger.info(
                        "index source success run_id=%s source=%s auctions=%s lots=%s",
                        run_id,
                        source_name,
                        stats.get("auctions"),
                        stats.get("lots"),
                    )
                except Exception as exc:
                    error_text = str(exc)
                    store.upsert_source_status(source_name, "error", started_at, started_at, error_text)
                    source_stats[source_name] = {"status": "error", "error": error_text}
                    source_progress[source_name] = {"total": None, "done": 0, "status": "error"}
                    errors.append(f"{source_name}: {error_text}")
                    logger.exception("index source error run_id=%s source=%s error=%s", run_id, source_name, error_text)
                completed += 1
                finished_total = sum(int(stats.get("total") or 0) for stats in source_progress.values() if stats.get("total") is not None)
                finished_done = sum(int(stats.get("done") or 0) for stats in source_progress.values())
                progress_percent = round((finished_done / finished_total) * 100, 1) if finished_total else round((completed / len(loaders)) * 100, 1)
                store.update_index_run_progress(
                    run_id,
                    progress_total=finished_total or len(loaders),
                    progress_done=finished_done,
                    progress_percent=progress_percent,
                    progress_message=f"Indexed {finished_done}/{finished_total or len(loaders)} items",
                    source_progress=source_progress,
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
    logger.info("index run done run_id=%s summary=%s errors=%s", run_id, summary, errors)
    return {"run_id": run_id, "summary": summary, "errors": errors, "source_stats": source_stats}
