"""资源台账：车辆、司机、无障碍席位按时间窗占用，全部可由事件重放重建。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class Hold:
    request_ref: str
    resource_ref: str
    quantity: int
    window_start: datetime
    window_end: datetime
    emergency: bool = False
    ratified: bool = False
    review_due_at: datetime | None = None
    depart_grace_until: datetime | None = None
    released: bool = False


@dataclass
class _Pool:
    capacity: int = 0
    holds: list[Hold] = field(default_factory=list)


class ResourceLedger:
    def __init__(self) -> None:
        # resource_ref -> 池子；同时按类别 vehicle/driver/accessible_seat 汇总。
        self._pools: dict[str, _Pool] = {}
        self._requests: dict[str, list[Hold]] = {}

    def register_pool(self, resource_ref: str, capacity: int) -> None:
        pool = self._pools.setdefault(resource_ref, _Pool())
        pool.capacity = max(pool.capacity, int(capacity))

    def holds_for(self, request_ref: str) -> list[Hold]:
        return list(self._requests.get(request_ref, ()))

    def active_holds(self, resource_ref: str) -> list[Hold]:
        return [h for h in self._pools.get(resource_ref, _Pool()).holds if not h.released]

    def available(self, resource_ref: str, start: datetime, end: datetime,
                  ignore_request: str | None = None, include_unratified: bool = True) -> int:
        pool = self._pools.get(resource_ref)
        if pool is None:
            return 0
        # 占用按时间窗切分，取窗内并发占用的最大值。
        cuts = sorted({start, end} | {
            t for h in pool.holds if not h.released and h.request_ref != ignore_request
            and (include_unratified or h.ratified or not h.emergency)
            for t in (h.window_start, h.window_end)
            if start < t < end
        })
        worst = 0
        for a, b in zip(cuts, cuts[1:]):
            midpoint = a + (b - a) / 2
            used = sum(
                h.quantity for h in pool.holds
                if not h.released and h.request_ref != ignore_request
                and (include_unratified or h.ratified or not h.emergency)
                and h.window_start <= midpoint < h.window_end
            )
            worst = max(worst, used)
        return pool.capacity - worst

    def can_hold(self, resources: Mapping[str, int], start: datetime, end: datetime,
                 ignore_request: str | None = None) -> str | None:
        """普通占用：任一资源在时间窗内超配即返回 resource_ref，否则 None。"""
        for resource_ref, quantity in resources.items():
            if resource_ref not in self._pools:
                return resource_ref
            if self.available(resource_ref, start, end, ignore_request) < quantity:
                return resource_ref
        return None

    def add_hold(self, request_ref: str, resource_ref: str, quantity: int,
                 start: datetime, end: datetime, emergency: bool,
                 review_due_at: datetime | None, depart_grace_until: datetime | None) -> Hold:
        hold = Hold(
            request_ref=request_ref, resource_ref=resource_ref, quantity=int(quantity),
            window_start=start, window_end=end, emergency=emergency,
            ratified=not emergency, review_due_at=review_due_at,
            depart_grace_until=depart_grace_until,
        )
        self._pools.setdefault(resource_ref, _Pool()).holds.append(hold)
        self._requests.setdefault(request_ref, []).append(hold)
        return hold

    def ratify(self, request_ref: str) -> list[Hold]:
        changed: list[Hold] = []
        for hold in self._requests.get(request_ref, ()):
            if hold.emergency and not hold.ratified:
                object.__setattr__(hold, "ratified", True)
                changed.append(hold)
        return changed

    def release_one(self, request_ref: str, resource_ref: str) -> Hold | None:
        for hold in self._requests.get(request_ref, ()):
            if hold.resource_ref == resource_ref and not hold.released:
                object.__setattr__(hold, "released", True)
                return hold
        return None

    def release(self, request_ref: str) -> list[Hold]:
        changed: list[Hold] = []
        for hold in self._requests.get(request_ref, ()):
            if not hold.released:
                object.__setattr__(hold, "released", True)
                changed.append(hold)
        return changed

    def overdue_emergency(self, now: datetime) -> list[Hold]:
        return [
            h for pool in self._pools.values() for h in pool.holds
            if h.emergency and not h.ratified and not h.released
            and h.review_due_at is not None and now >= h.review_due_at
        ]

    def missed_departures(self, now: datetime) -> list[Hold]:
        return [
            h for pool in self._pools.values() for h in pool.holds
            if not h.emergency or h.ratified
            if not h.released and h.depart_grace_until is not None and now >= h.depart_grace_until
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            ref: {
                "capacity": pool.capacity,
                "active": sum(h.quantity for h in pool.holds if not h.released),
            }
            for ref, pool in sorted(self._pools.items())
        }
