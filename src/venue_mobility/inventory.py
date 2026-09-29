"""运力库存：车辆、司机、无障碍席位三个独立维度。

占用是原子的——任一维度超配则整笔申请被拒绝，不会出现“车占了、司机没占”的半占用。
每条持有以占用事件标识（hold_id）独立记账，同一资源可被同一占用方分事件持有
（首发占用、改线追加占用），归还时按数量冲减最早的持有记录。
重放以 hold_id 去重，重复回执不会二次扣减。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import CapacityExhausted

VEHICLE = "vehicle"
DRIVER = "driver"
ACCESSIBILITY_SEAT = "accessibility_seat"
DIMENSIONS = (VEHICLE, DRIVER, ACCESSIBILITY_SEAT)


@dataclass
class Hold:
    hold_id: str
    receipt_key: str
    resource_ref: str
    quantity: int
    owner_kind: str  # "demand" 或 "emergency"
    owner_id: str


@dataclass
class Inventory:
    _capacity: dict[str, int] = field(default_factory=dict)
    _holds: dict[str, Hold] = field(default_factory=dict)  # key: hold_id

    def apply_pool_snapshot(self, snapshot: dict[str, Any]) -> None:
        pools = snapshot.get("pools", {})
        for ref, body in pools.items():
            self._capacity[ref] = int(body["capacity"])

    def capacity_of(self, resource_ref: str) -> int:
        if resource_ref not in self._capacity:
            raise CapacityExhausted(
                f"运力资源 {resource_ref} 尚未在运力池登记",
                resource_ref=resource_ref,
            )
        return self._capacity[resource_ref]

    def held_of(self, resource_ref: str) -> int:
        return sum(h.quantity for h in self._holds.values() if h.resource_ref == resource_ref)

    def available(self, resource_ref: str) -> int:
        return self.capacity_of(resource_ref) - self.held_of(resource_ref)

    def check(self, items: list[tuple[str, int]]) -> None:
        """原子预检：对所有维度同时核算，任一不足即整体拒绝。"""
        wanted: dict[str, int] = {}
        for resource_ref, quantity in items:
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise CapacityExhausted(
                    f"运力占用数量必须为正整数，收到 {quantity!r}",
                    resource_ref=resource_ref,
                )
            wanted[resource_ref] = wanted.get(resource_ref, 0) + quantity
        for resource_ref, quantity in wanted.items():
            free = self.available(resource_ref)
            if quantity > free:
                raise CapacityExhausted(
                    f"资源 {resource_ref} 余量 {free}，无法满足本次申请 {quantity}",
                    resource_ref=resource_ref,
                )

    def commit(self, hold_id: str, receipt_key: str, owner_kind: str, owner_id: str,
               resource_ref: str, quantity: int) -> Hold:
        """登记一条持有；同一 hold_id 重放原样返回，不二次扣减。"""
        existing = self._holds.get(hold_id)
        if existing is not None:
            if (existing.resource_ref != resource_ref
                    or existing.quantity != quantity
                    or existing.owner_kind != owner_kind
                    or existing.owner_id != owner_id):
                raise CapacityExhausted(
                    f"持有 {hold_id} 的重放记录与既有台账不一致",
                    resource_ref=resource_ref,
                )
            return existing
        hold = Hold(hold_id, receipt_key, resource_ref, quantity, owner_kind, owner_id)
        self._holds[hold_id] = hold
        return hold

    def release(self, resource_ref: str, quantity: int, owner_kind: str, owner_id: str) -> int:
        """归还某占用方在指定资源上的运力，按最早持有逐条冲减；幂等。"""
        if quantity <= 0:
            return 0
        remaining = quantity
        for hold_id in sorted(self._holds):
            if remaining <= 0:
                break
            hold = self._holds[hold_id]
            if (hold.resource_ref != resource_ref or hold.owner_kind != owner_kind
                    or hold.owner_id != owner_id or hold.quantity <= 0):
                continue
            take = min(hold.quantity, remaining)
            hold.quantity -= take
            remaining -= take
            if hold.quantity == 0:
                del self._holds[hold_id]
        return quantity - remaining

    def holds_of(self, owner_kind: str, owner_id: str) -> list[Hold]:
        return [h for h in self._holds.values()
                if h.owner_kind == owner_kind and h.owner_id == owner_id]

    def usage(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ref in sorted(self._capacity):
            held = self.held_of(ref)
            out.append({
                "resource_ref": ref,
                "capacity": self._capacity[ref],
                "held": held,
                "available": self._capacity[ref] - held,
                "holds": [
                    {"hold_id": h.hold_id, "quantity": h.quantity,
                     "owner_kind": h.owner_kind, "owner_id": h.owner_id}
                    for h in sorted(self._holds.values(), key=lambda h: h.hold_id)
                    if h.resource_ref == ref
                ],
            })
        return out
