from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import replace
import logging
import threading
import time
from typing import Any

from shadow_models import AuctionProvider, IndexRepository, ShadowLotResult, ShadowLotWorkUnit
from store import to_iso, utc_now


logger = logging.getLogger(__name__)


class IndexRunner:
    def __init__(
        self,
        repository: IndexRepository,
        providers: list[AuctionProvider],
        *,
        global_workers: int = 32,
        per_provider_workers: int = 8,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
    ):
        self.repository = repository
        self.providers = providers
        self.global_workers = max(1, int(global_workers))
        self.per_provider_workers = max(1, int(per_provider_workers))
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_seconds = max(0.0, float(backoff_seconds))

    def run(self, scope: str = "manual") -> dict[str, Any]:
        run_id = self.repository.start_run(scope)
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat_loop, args=(run_id, heartbeat_stop), daemon=True)
        heartbeat.start()
        status = "success"
        try:
            for provider in self.providers:
                try:
                    self._run_provider(run_id, provider)
                except Exception as exc:
                    status = "error"
                    logger.exception("shadow provider failed source=%s error=%s", provider.source, exc)
                    self.repository.update_provider_progress(
                        run_id,
                        provider.source,
                        status="error",
                        finished_at=to_iso(utc_now()),
                        error_text=str(exc),
                        validated=0,
                    )
            summary = self.repository.run_summary(run_id)
            has_partial_provider = any(provider.get("status") == "partial" for provider in summary.get("providers", []))
            if has_partial_provider or not summary.get("validation_success"):
                status = "partial" if status == "success" else status
            return {"run_id": run_id, "status": status, "summary": summary}
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
            self.repository.finish_run(run_id, status)

    def retry_failures(self, source: str | None = None) -> dict[str, Any]:
        failures = self.repository.list_failures(source)
        if not failures:
            return {"run_id": None, "retried": 0, "indexed": 0, "failed": 0}

        providers = {provider.source: provider for provider in self.providers}
        retry_units: dict[str, list[ShadowLotWorkUnit]] = {}
        for failure in failures:
            failure_source = failure["source"]
            if failure_source not in providers:
                continue
            retry_units.setdefault(failure_source, []).append(
                ShadowLotWorkUnit(
                    source=failure_source,
                    provider_lot_id=str(failure["provider_lot_id"]),
                    provider_auction_id=failure["provider_auction_id"],
                    url=failure["lot_url"],
                    payload=self._decode_payload(failure["work_unit_json"]),
                )
            )

        run_id = self.repository.start_run("retry-failures")
        retried = indexed = failed = 0
        try:
            for failure_source, units in retry_units.items():
                provider = providers[failure_source]
                self.repository.start_provider_run(run_id, failure_source)
                self.repository.update_provider_progress(
                    run_id,
                    failure_source,
                    discovered_total=len(units),
                    queued_count=len(units),
                    status="running",
                )
                stats = self._process_units(run_id, provider, units)
                retried += len(units)
                indexed += stats["indexed"]
                failed += stats["failed"]
                self.repository.update_provider_progress(
                    run_id,
                    failure_source,
                    status="success" if stats["failed"] == 0 else "partial",
                    finished_at=to_iso(utc_now()),
                    queued_count=0,
                    in_progress_count=0,
                    indexed_count=stats["indexed"],
                    failed_count=stats["failed"],
                    retry_count=stats["retried"],
                    validated=1 if stats["indexed"] + stats["failed"] == len(units) else 0,
                    progress_percent=100.0,
                )
            status = "success" if failed == 0 else "partial"
            return {"run_id": run_id, "retried": retried, "indexed": indexed, "failed": failed}
        finally:
            self.repository.finish_run(run_id, "success" if failed == 0 else "partial")

    def _run_provider(self, run_id: int, provider: AuctionProvider) -> None:
        source = provider.source
        self.repository.start_provider_run(run_id, source)
        discovery = provider.discover()
        for auction in discovery.auctions:
            self.repository.upsert_auction(run_id, source, auction)
        queued = len(discovery.work_units)
        self.repository.update_provider_progress(
            run_id,
            source,
            discovered_total=discovery.lot_total,
            queued_count=queued,
            status="running",
            progress_percent=0.0,
        )
        stats = self._process_units(run_id, provider, discovery.work_units, discovered_total=discovery.lot_total)
        validated = 1 if stats["indexed"] + stats["failed"] == discovery.lot_total else 0
        status = "success" if stats["failed"] == 0 and validated else "partial"
        self.repository.update_provider_progress(
            run_id,
            source,
            status=status,
            finished_at=to_iso(utc_now()),
            queued_count=0,
            in_progress_count=0,
            indexed_count=stats["indexed"],
            failed_count=stats["failed"],
            retry_count=stats["retried"],
            validated=validated,
            progress_percent=self._percent(stats["indexed"] + stats["failed"], discovery.lot_total),
        )

    def _process_units(
        self,
        run_id: int,
        provider: AuctionProvider,
        work_units: list[ShadowLotWorkUnit],
        *,
        discovered_total: int | None = None,
    ) -> dict[str, int]:
        indexed = failed = retried = in_progress = completed = 0
        provider_running = 0
        pending = list(work_units)
        futures: dict[Future[tuple[ShadowLotWorkUnit, int, ShadowLotResult]], tuple[ShadowLotWorkUnit, int]] = {}

        def submit_available(executor: ThreadPoolExecutor) -> None:
            nonlocal provider_running, in_progress
            while pending and len(futures) < self.global_workers and provider_running < self.per_provider_workers:
                unit = pending.pop(0)
                provider_running += 1
                in_progress += 1
                futures[executor.submit(self._attempt_lot, run_id, provider, unit, 1)] = (unit, 1)

        total = discovered_total if discovered_total is not None else len(work_units)
        self.repository.update_provider_progress(
            run_id,
            provider.source,
            queued_count=len(pending),
            in_progress_count=0,
            indexed_count=0,
            failed_count=0,
            retry_count=0,
            progress_percent=0.0,
        )

        with ThreadPoolExecutor(max_workers=self.global_workers) as executor:
            submit_available(executor)
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    unit, attempt = futures.pop(future)
                    provider_running -= 1
                    in_progress -= 1
                    try:
                        _, _, result = future.result()
                    except Exception as exc:
                        if attempt < self.max_attempts:
                            retried += 1
                            if self.backoff_seconds:
                                time.sleep(self.backoff_seconds * attempt)
                            provider_running += 1
                            in_progress += 1
                            futures[executor.submit(self._attempt_lot, run_id, provider, unit, attempt + 1)] = (unit, attempt + 1)
                            continue
                        failed += 1
                        completed += 1
                        self.repository.record_failure(run_id, provider.source, unit, str(exc), attempt)
                    else:
                        indexed += 1
                        completed += 1
                        self.repository.upsert_lot(run_id, provider.source, result.auction, result.lot)
                        self.repository.clear_failure(provider.source, unit.provider_lot_id)

                    self.repository.update_provider_progress(
                        run_id,
                        provider.source,
                        queued_count=len(pending),
                        in_progress_count=in_progress,
                        indexed_count=indexed,
                        failed_count=failed,
                        retry_count=retried,
                        progress_percent=self._percent(completed, total),
                    )
                submit_available(executor)

        return {"indexed": indexed, "failed": failed, "retried": retried}

    def _attempt_lot(
        self,
        run_id: int,
        provider: AuctionProvider,
        work_unit: ShadowLotWorkUnit,
        attempt_number: int,
    ) -> tuple[ShadowLotWorkUnit, int, ShadowLotResult]:
        started = time.perf_counter()
        try:
            result = provider.fetch_lot(work_unit)
        except Exception as exc:
            self.repository.record_lot_attempt(
                run_id,
                provider.source,
                work_unit,
                attempt_number,
                "error",
                time.perf_counter() - started,
                str(exc),
            )
            raise
        self.repository.record_lot_attempt(
            run_id,
            provider.source,
            replace(work_unit, provider_lot_id=result.lot["provider_lot_id"]),
            attempt_number,
            "success",
            time.perf_counter() - started,
            None,
        )
        return work_unit, attempt_number, result

    def _heartbeat_loop(self, run_id: int, stop: threading.Event) -> None:
        while not stop.wait(15):
            self.repository.refresh_run_heartbeat(run_id)

    def _percent(self, done: int, total: int | None) -> float:
        if not total:
            return 100.0 if done else 0.0
        return min(100.0, round((done / total) * 100, 1))

    def _decode_payload(self, raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        import json

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
