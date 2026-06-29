from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import threading
import logging
from typing import Callable

from models import ProviderEstimate, ProviderSnapshot
from providers import auction403, hibid, kotn
from repositories import AuctionIndexRepository
from store import to_iso, utc_now


WINDOW_DAYS = 7
logger = logging.getLogger(__name__)


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
    store: AuctionIndexRepository,
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
    source_progress: dict[str, dict] = {
        name: {"total": None, "estimated": None, "done": 0, "indexed": 0, "status": "queued"} for name in loaders
    }
    for source_name, estimator in estimators.items():
        try:
            estimate = estimator()
            source_progress[source_name] = {
                "total": estimate.lots,
                "estimated": estimate.lots,
                "done": 0,
                "indexed": 0,
                "status": "estimated",
            }
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
                    "estimated": source_progress.get(source_name, {}).get("estimated"),
                    "done": 0,
                    "indexed": 0,
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
                    snapshot = _filter_snapshot(future.result(), current)
                    stats = store.upsert_snapshot(source_name, run_id, started_at, snapshot.auctions, snapshot.lots)
                    store.prune_source_rows(source_name, run_id, to_iso(_window_end(current)))
                    store.upsert_source_status(source_name, "success", started_at, started_at, None)
                    successful_sources.append(source_name)
                    source_stats[source_name] = {"status": "success", **stats}
                    estimated = source_progress.get(source_name, {}).get("estimated")
                    indexed = int(stats.get("lots") or 0)
                    source_progress[source_name] = {
                        "total": estimated if estimated is not None else indexed,
                        "estimated": estimated,
                        "done": indexed,
                        "indexed": indexed,
                        "status": "success",
                        "validated": estimated == indexed if estimated is not None else None,
                    }
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
                    source_progress[source_name] = {
                        "total": source_progress.get(source_name, {}).get("estimated"),
                        "estimated": source_progress.get(source_name, {}).get("estimated"),
                        "done": 0,
                        "indexed": 0,
                        "status": "error",
                        "validated": False,
                    }
                    errors.append(f"{source_name}: {error_text}")
                    logger.exception("index source error run_id=%s source=%s error=%s", run_id, source_name, error_text)
                completed += 1
                finished_total = sum(int(stats.get("total") or 0) for stats in source_progress.values() if stats.get("total") is not None)
                finished_done = sum(int(stats.get("done") or 0) for stats in source_progress.values())
                progress_percent = round((finished_done / finished_total) * 100, 1) if finished_total else round((completed / len(loaders)) * 100, 1)
                validation_summary = []
                for name, stats in source_progress.items():
                    if stats.get("estimated") is None:
                        continue
                    validation_summary.append(f"{name}: {stats.get('indexed', 0)}/{stats.get('estimated')} validated")
                store.update_index_run_progress(
                    run_id,
                    progress_total=finished_total or len(loaders),
                    progress_done=finished_done,
                    progress_percent=progress_percent,
                    progress_message=(
                        f"Indexed {finished_done}/{finished_total or len(loaders)} items"
                        + (f"; {'; '.join(validation_summary)}" if validation_summary else "")
                    ),
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
