"""版本化事实目录：赛程、路网、驻地、公交班次、运力池、安检窗口。

服务每次放行只引用已登记事实的具体版本；事实升级不追溯改写历史放行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import FactVersionUnknown

SCHEDULE = "schedule"
ROAD_NETWORK = "road_network"
LODGING = "lodging"
PUBLIC_TRANSIT = "public_transit"
RESOURCE_POOL = "resource_pool"
SECURITY_WINDOW = "security_window"

FACT_KINDS = (
    SCHEDULE,
    ROAD_NETWORK,
    LODGING,
    PUBLIC_TRANSIT,
    RESOURCE_POOL,
    SECURITY_WINDOW,
)


@dataclass
class FactEdition:
    kind: str
    key: str
    version: int
    snapshot: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "fact_kind": self.kind,
            "fact_key": self.key,
            "fact_version": self.version,
            "snapshot": self.snapshot,
        }


@dataclass
class FactCatalog:
    """按 (kind,key) 维护版本序列；重建时从 FACT_REGISTERED 事件追加。"""

    _editions: dict[tuple[str, str], list[FactEdition]] = field(default_factory=dict)

    def register(self, kind: str, key: str, version: int, snapshot: dict[str, Any]) -> FactEdition:
        editions = self._editions.setdefault((kind, key), [])
        for edition in editions:
            if edition.version == version:
                # 重放同一事实：快照必须一致，否则事件日志本身矛盾。
                if edition.snapshot != snapshot:
                    raise FactVersionUnknown(
                        f"事实 {kind}/{key} 版本 {version} 的快照前后不一致",
                        field=f"facts.{kind}.{key}",
                    )
                return edition
        if editions and version != editions[-1].version + 1:
            raise FactVersionUnknown(
                f"事实 {kind}/{key} 的新版本必须接续 {editions[-1].version}",
                field=f"facts.{kind}.{key}",
            )
        edition = FactEdition(kind, key, version, snapshot)
        editions.append(edition)
        return edition

    def get(self, kind: str, key: str, version: int) -> FactEdition:
        editions = self._editions.get((kind, key))
        if editions:
            for edition in editions:
                if edition.version == version:
                    return edition
        raise FactVersionUnknown(
            f"事实 {kind}/{key} 版本 {version} 未登记",
            field=f"facts.{kind}.{key}@{version}",
        )

    def latest(self, kind: str, key: str) -> FactEdition:
        editions = self._editions.get((kind, key))
        if not editions:
            raise FactVersionUnknown(f"事实 {kind}/{key} 未登记任何版本", field=f"facts.{kind}.{key}")
        return editions[-1]

    def keys(self, kind: str) -> list[str]:
        return sorted(key for (kind_, key) in self._editions if kind_ == kind)

    def refs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (kind, key), editions in sorted(self._editions.items()):
            out.append({"fact_kind": kind, "fact_key": key, "latest_version": editions[-1].version})
        return out


def _latest_snapshot(catalog: FactCatalog, kind: str, key: str) -> dict[str, Any]:
    try:
        return catalog.latest(kind, key).snapshot
    except FactVersionUnknown:
        return {}


def road_edge_open(catalog: FactCatalog, road_version: int, edge_id: str) -> bool:
    """某条路段在给定路网版本下是否可通行。"""
    road = catalog.get(ROAD_NETWORK, "primary", road_version).snapshot
    closed = set(road.get("closed_edges", []))
    incidents = road.get("incidents", {})
    return edge_id not in closed and not incidents.get(edge_id, {}).get("blocked", False)


def road_edges(catalog: FactCatalog, road_version: int) -> dict[str, dict[str, Any]]:
    road = catalog.get(ROAD_NETWORK, "primary", road_version).snapshot
    return dict(road.get("edges", {}))


def transit_options(catalog: FactCatalog) -> list[dict[str, Any]]:
    snap = _latest_snapshot(catalog, PUBLIC_TRANSIT, "primary")
    return list(snap.get("services", []))


def lodging_for(catalog: FactCatalog, lodging_ref: str) -> dict[str, Any]:
    snap = _latest_snapshot(catalog, LODGING, lodging_ref)
    if not snap:
        raise FactVersionUnknown(f"驻地 {lodging_ref} 未登记", field="origin_ref")
    return snap


def security_gate(catalog: FactCatalog, gate_ref: str) -> dict[str, Any]:
    snap = _latest_snapshot(catalog, SECURITY_WINDOW, gate_ref)
    if not snap:
        raise FactVersionUnknown(f"安检点 {gate_ref} 未登记", field="gate_ref")
    return snap
