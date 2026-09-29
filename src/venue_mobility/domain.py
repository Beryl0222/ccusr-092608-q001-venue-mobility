"""事实注册表与行程规划：全部由 FACT_RECORDED 事件重建，不保留额外状态。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

# 运动队、交通方、安保方各自只确认自己掌握的事实；媒体不确认事实。
FACT_OWNERS: dict[str, str] = {
    "fixture": "team",       # 赛程（含提前、改期）
    "lodging": "team",       # 驻地（含临时改住处）
    "roster": "team",        # 人员类别与人数
    "leg": "transport",      # 走廊常规车程
    "transit": "transport",  # 公共交通班次（含停运）
    "resource": "transport", # 车辆/司机/无障碍席位运力
    "screening": "security", # 安检窗口（含收紧）
    "incident": "security",  # 事故影响
}

SCHEDULE_DOMAIN = {"fixture", "lodging", "roster"}
NETWORK_DOMAIN = {"leg", "transit", "resource", "screening", "incident"}


@dataclass
class Fact:
    fact_type: str
    ref: str
    version: int
    owner_role: str
    recorded_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    superseded: bool = False


class FactRegistry:
    def __init__(self) -> None:
        self._facts: dict[tuple[str, str], Fact] = {}
        self._history: list[Fact] = []
        self.domain_version = {"schedule": 0, "network": 0}

    def record(self, fact_type: str, ref: str, version: int, owner_role: str,
               recorded_at: datetime, data: Mapping[str, Any]) -> Fact:
        key = (fact_type, ref)
        current = self._facts.get(key)
        if current is not None and version <= current.version:
            raise StaleFact(fact_type, ref, version, current.version)
        if current is not None:
            current.superseded = True
        fact = Fact(fact_type, ref, version, owner_role, recorded_at, dict(data))
        self._facts[key] = fact
        self._history.append(fact)
        domain = "schedule" if fact_type in SCHEDULE_DOMAIN else "network"
        self.domain_version[domain] += 1
        return fact

    def history(self, fact_type: str | None = None) -> list[Fact]:
        rows = self._history if fact_type is None else [f for f in self._history if f.fact_type == fact_type]
        return sorted(rows, key=lambda f: (f.recorded_at, f.fact_type, f.ref, f.version))

    def get(self, fact_type: str, ref: str) -> Fact | None:
        return self._facts.get((fact_type, ref))

    def select(self, fact_type: str) -> list[Fact]:
        return [f for (t, _), f in self._facts.items() if t == fact_type and not f.superseded]

    def basis_snapshot(self) -> list[dict[str, Any]]:
        return [
            {"fact_type": f.fact_type, "ref": f.ref, "version": f.version,
             "owner_role": f.owner_role}
            for f in sorted(self._facts.values(), key=lambda x: (x.fact_type, x.ref, x.version))
            if not f.superseded
        ]


class StaleFact(Exception):
    def __init__(self, fact_type: str, ref: str, version: int, current: int) -> None:
        super().__init__(f"事实 {fact_type}:{ref} 版本 {version} 已过期，当前版本 {current}")
        self.code = "stale_fact"


@dataclass
class Plan:
    mode: str                      # transit | shuttle | denied
    corridor: str
    pickup_at: datetime | None
    arrives_at: datetime | None
    screening_ref: str | None
    resources: dict[str, int]
    accessible: bool
    reason_code: str | None = None
    evidence: list[str] = field(default_factory=list)


class Planner:
    def __init__(self, facts: FactRegistry) -> None:
        self.facts = facts

    def _incident_delay(self, corridor: str, at: datetime) -> tuple[int, list[str]]:
        delay = 0
        hits: list[str] = []
        for inc in self.facts.select("incident"):
            d = inc.data
            if d.get("corridor") != corridor or d.get("status", "active") != "active":
                continue
            if datetime.fromisoformat(d["effective_from"]) <= at < datetime.fromisoformat(d["effective_to"]):
                delay += int(d.get("delay_minutes", 0))
                hits.append(inc.ref)
        return delay, hits

    def _screening_candidates(self, venue: str, category: str) -> list[Fact]:
        out: list[Fact] = []
        for sc in self.facts.select("screening"):
            d = sc.data
            if d.get("venue") != venue or d.get("status", "open") != "open":
                continue
            allowed = set(d.get("categories_allowed", ()))
            if category not in allowed:
                continue
            # 运动员专用通道对其他类别（含媒体）一律不可用。
            if d.get("athlete_only") and category != "athlete":
                continue
            out.append(sc)
        return out

    def plan(self, *, venue: str, corridor: str, category: str, headcount: int,
             accessible_seats: int, deadline: datetime, now: datetime) -> Plan:
        ev: list[str] = []
        legs = {f.data.get("corridor"): f for f in self.facts.select("leg")}
        leg = legs.get(corridor)
        if leg is None:
            return Plan("denied", corridor, None, None, None, {}, False, "unknown_corridor",
                        [f"走廊 {corridor} 没有车程事实"])
        ev.append(f"leg:{leg.ref}@v{leg.version}")

        screenings = self._screening_candidates(venue, category)
        if not screenings:
            return Plan("denied", corridor, None, None, None, {}, False, "no_screening_channel",
                        ev + [f"场馆 {venue} 对类别 {category} 无可用安检通道"])

        def screening_fit(sc: Fact, arrives_at: datetime) -> bool:
            d = sc.data
            opened = datetime.fromisoformat(d["open_from"])
            closed = datetime.fromisoformat(d["open_to"])
            lead = timedelta(minutes=int(d.get("lead_minutes", 0)))
            return arrives_at >= opened and arrives_at + lead <= closed and arrives_at + lead <= deadline

        # 1) 公共交通：取能赶上的最晚一班；停运或不允许该类别/无无障碍设备时跳过。
        best_transit: Fact | None = None
        best_sc: Fact | None = None
        for svc in self.facts.select("transit"):
            d = svc.data
            if d.get("corridor") != corridor or d.get("status", "running") != "running":
                continue
            if category not in set(d.get("categories_allowed", ())):
                continue
            if accessible_seats and not d.get("accessible", False):
                continue
            arrives_at = datetime.fromisoformat(d["arrives_at"])
            sc = next((s for s in screenings if screening_fit(s, arrives_at)), None)
            if sc is None:
                continue
            if best_transit is None or arrives_at > datetime.fromisoformat(best_transit.data["arrives_at"]):
                best_transit, best_sc = svc, sc
        if best_transit is not None and best_sc is not None:
            d = best_transit.data
            ev.append(f"transit:{best_transit.ref}@v{best_transit.version}")
            ev.append(f"screening:{best_sc.ref}@v{best_sc.version}")
            return Plan("transit", corridor, datetime.fromisoformat(d["departs_at"]),
                        datetime.fromisoformat(d["arrives_at"]), best_sc.ref,
                        {}, accessible_seats == 0 or bool(d.get("accessible")), evidence=ev)

        # 2) 接驳车：按事故影响修正车程，挑可在截止前到场且窗口最宽松的安检点。
        base_minutes = int(leg.data.get("travel_minutes"))
        candidate: tuple[datetime, datetime, Fact, int, list[str]] | None = None
        for sc in screenings:
            d = sc.data
            opened = datetime.fromisoformat(d["open_from"])
            closed = datetime.fromisoformat(d["open_to"])
            lead = timedelta(minutes=int(d.get("lead_minutes", 0)))
            arrives_at = min(deadline - lead, closed - lead)
            delay, hits = self._incident_delay(corridor, arrives_at)
            pickup_at = arrives_at - timedelta(minutes=base_minutes + delay)
            if arrives_at < opened or pickup_at < now:
                continue
            if candidate is None or pickup_at > candidate[1]:
                candidate = (arrives_at, pickup_at, sc, delay, hits)
        if candidate is None:
            return Plan("denied", corridor, None, None, None, {}, False, "cannot_make_deadline",
                        ev + ["无安检窗口可容纳该到场时间"])
        arrives_at, pickup_at, sc, delay, hits = candidate
        ev.append(f"screening:{sc.ref}@v{sc.version}")
        if hits:
            ev.append("incident:" + ",".join(hits) + f" +{delay}min")
        bus_seats = int(leg.data.get("bus_seats", 40))
        buses = max(1, math.ceil(headcount / bus_seats))
        resources = {
            f"vehicle:{corridor}": buses,
            f"driver:{corridor}": buses,
            f"aseat:{corridor}": accessible_seats,
        }
        for ref in resources:
            ev.append(f"resource-needed:{ref}={resources[ref]}")
        return Plan("shuttle", corridor, pickup_at, arrives_at, sc.ref, resources,
                    accessible=True, evidence=ev)
