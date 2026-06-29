from __future__ import annotations

from typing import Callable

from models import ProviderEstimate, ProviderSnapshot
from providers import auction403, hibid, kotn
from shadow_models import ShadowDiscovery, ShadowLotResult, ShadowLotWorkUnit


class SnapshotProviderAdapter:
    """Adapter for existing providers while the shadow pipeline owns orchestration."""

    def __init__(
        self,
        source: str,
        config: dict | None,
        estimator: Callable[[dict | None], ProviderEstimate],
        snapshot_loader: Callable[[dict | None], ProviderSnapshot],
    ):
        self.source = source
        self.config = config or {}
        self._estimator = estimator
        self._snapshot_loader = snapshot_loader
        self._auctions_by_id: dict[str, dict] = {}

    def discover(self) -> ShadowDiscovery:
        estimate = self._estimator(self.config)
        snapshot = self._snapshot_loader(self.config)
        self._auctions_by_id = {auction["provider_auction_id"]: auction for auction in snapshot.auctions}
        work_units = [
            ShadowLotWorkUnit(
                source=self.source,
                provider_lot_id=lot["provider_lot_id"],
                provider_auction_id=lot["provider_auction_id"],
                url=lot.get("url"),
                payload={"auction": self._auctions_by_id.get(lot["provider_auction_id"]), "lot": lot},
            )
            for lot in snapshot.lots
        ]
        lot_total = int(estimate.lots) if estimate.lots is not None else len(work_units)
        return ShadowDiscovery(source=self.source, auctions=snapshot.auctions, lot_total=lot_total, work_units=work_units)

    def fetch_lot(self, work_unit: ShadowLotWorkUnit) -> ShadowLotResult:
        auction = work_unit.payload.get("auction")
        lot = work_unit.payload.get("lot")
        if not auction:
            auction = self._auctions_by_id.get(str(work_unit.provider_auction_id))
        if not auction or not lot:
            raise ValueError(f"{self.source} work unit is missing parsed lot data: {work_unit.provider_lot_id}")
        return ShadowLotResult(auction=auction, lot=lot)


def default_providers(source_configs: dict[str, dict] | None = None) -> list[SnapshotProviderAdapter]:
    configs = source_configs or {}
    kotn_config = dict(configs.get(kotn.SOURCE_NAME, {}))
    kotn_config.setdefault("max_pages", 10000)
    return [
        SnapshotProviderAdapter("HiBid", configs.get("HiBid", {}), hibid.estimate_snapshot, hibid.fetch_snapshot),
        SnapshotProviderAdapter("403 Auction", configs.get("403 Auction", {}), auction403.estimate_snapshot, auction403.fetch_snapshot),
        SnapshotProviderAdapter(kotn.SOURCE_NAME, kotn_config, kotn.estimate_snapshot, kotn.fetch_snapshot),
    ]
