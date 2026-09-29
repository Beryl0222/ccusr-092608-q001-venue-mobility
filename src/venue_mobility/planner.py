"""行程规划：只依据已登记且被锁定的具体版本事实生成可执行行程。

规划器不持有状态，也不碰运力台账——它只回答：在给定赛程/路网版本下，
某类人员从某驻地到某场馆的最低必要出行安排是什么。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from .clock import parse_dt
from .errors import AccessForbidden, FactVersionUnknown, ValidationRejection
from .facts import (
    road_edges,
    security_gate,
    lodging_for,
)

SECURITY_BUFFER_MINUTES = 30
DEFAULT_SEATS_PER_VEHICLE = 15

# 各角色只被允许确认其掌握的事实字段。
CONFIRMATION_DOMAINS: dict[str, frozenset[str]] = {
    "team": frozenset({
        "headcount", "accessibility_seats", "origin_ref", "category", "session_ref",
    }),
    "transport": frozenset({
        "road_version", "vehicle_ready", "transit_service", "depart_at",
    }),
    "security": frozenset({
        "gate_ref", "window_open", "access_category", "incident_clear",
    }),
}


def _route_candidates(road: dict[str, Any]) -> list[dict[str, Any]]:
    routes = road.get("routes", {})
    if isinstance(routes, dict):
        out = []
        for route_ref, body in routes.items():
            out.append({"route_ref": route_ref, **body})
        return out
    return list(routes)


def plan_itinerary(
    catalog,
    inventory,
    *,
    category: str,
    headcount: int,
    accessibility_seats: int,
    origin_ref: str,
    venue_ref: str,
    needed_at: str,
    schedule_version: int,
    road_version: int,
    now: datetime,
) -> dict[str, Any]:
    needed = parse_dt(needed_at)
    schedule = catalog.get("schedule", venue_ref, schedule_version).snapshot
    if schedule.get("venue_ref") != venue_ref:
        raise ValidationRejection(
            f"赛程版本 {schedule_version} 对应场馆为 {schedule.get('venue_ref')}，与申请场馆 {venue_ref} 不一致",
            field="facts.schedule_version",
        )
    road = catalog.get("road_network", "primary", road_version).snapshot
    lodging = lodging_for(catalog, origin_ref)
    origin_node = lodging.get("road_node", origin_ref)
    venue_node = schedule.get("road_node", venue_ref)

    gate = _pick_gate(catalog, venue_ref, category, needed)
    plan = _try_public_transit(
        catalog, category, accessibility_seats, origin_ref, venue_ref,
        origin_node, venue_node, needed, gate,
    )
    if plan is None:
        plan = _plan_shuttle(
            catalog, inventory, road, category, headcount, accessibility_seats,
            origin_ref, venue_ref, origin_node, venue_node, needed, gate,
            road_version,
        )
    plan["schedule_version"] = schedule_version
    plan["road_version"] = road_version
    plan["needed_at"] = needed.isoformat()
    return plan


def _pick_gate(catalog, venue_ref: str, category: str, needed: datetime) -> dict[str, Any]:
    gates = catalog.keys("security_window")
    candidates: list[dict[str, Any]] = []
    for gate_ref in gates:
        gate = security_gate(catalog, gate_ref)
        if gate.get("venue_ref") != venue_ref:
            continue
        allowed = gate.get("allowed_categories", [])
        if category not in allowed:
            continue
        if not _window_covers(gate, needed):
            continue
        candidates.append(gate)
    if not candidates:
        raise AccessForbidden(
            f"场馆 {venue_ref} 在到场截止 {needed.isoformat()} 没有对 {category} 开放的安检窗口；"
            "运动员专用通道不得下发给其他人员类别",
            field="access",
        )
    candidates.sort(key=lambda g: (g.get("priority", 100), g.get("gate_ref", "")))
    return candidates[0]


def _window_covers(gate: dict[str, Any], moment: datetime) -> bool:
    open_from = gate.get("open_from")
    open_to = gate.get("open_to")
    if not open_from or not open_to:
        return True
    local = moment.astimezone(moment.tzinfo)
    hhmm = local.strftime("%H:%M")
    if open_from <= open_to:
        return open_from <= hhmm <= open_to
    return hhmm >= open_from or hhmm <= open_to  # 跨午夜窗口


def _try_public_transit(
    catalog, category, accessibility_seats, origin_ref, venue_ref,
    origin_node, venue_node, needed, gate,
) -> dict[str, Any] | None:
    try:
        transit = catalog.latest("public_transit", "primary").snapshot
    except FactVersionUnknown:
        return None
    deadline = needed - timedelta(minutes=SECURITY_BUFFER_MINUTES)
    best: dict[str, Any] | None = None
    for service in transit.get("services", []):
        if service.get("suspended"):
            continue
        if service.get("from_node") != origin_node or service.get("to_node") != venue_node:
            continue
        if accessibility_seats > 0 and not service.get("accessible"):
            continue
        ride_minutes = int(service.get("arrival_minutes_after", 0))
        for departure in service.get("timetable", []):
            dep = parse_dt(departure)
            arrive = dep + timedelta(minutes=ride_minutes)
            if arrive <= deadline and (best is None or dep > parse_dt(best["depart_at"])):
                best = {
                    "mode": "public_transit",
                    "origin_ref": origin_ref,
                    "venue_ref": venue_ref,
                    "gate_ref": gate["gate_ref"],
                    "transit_service": service["service_ref"],
                    "accessible": bool(service.get("accessible", False)),
                    "depart_at": dep.isoformat(),
                    "arrive_at": arrive.isoformat(),
                    "travel_minutes": ride_minutes,
                    "route_edges": [],
                    "resources": [],
                }
    return best


def _plan_shuttle(
    catalog, inventory, road, category, headcount, accessibility_seats,
    origin_ref, venue_ref, origin_node, venue_node, needed, gate, road_version,
) -> dict[str, Any]:
    edges = road_edges(catalog, road_version)
    chosen: dict[str, Any] | None = None
    for route in _route_candidates(road):
        if route.get("start") != origin_node or route.get("end") != venue_node:
            continue
        route_edges = route.get("edges", [])
        closed = [e for e in route_edges if e not in edges]
        blocked = [
            e for e in route_edges
            if e in edges and (road.get("incidents", {}).get(e, {}).get("blocked"))
        ]
        if closed or blocked or e_id_in(route_edges, road.get("closed_edges", [])):
            continue
        chosen = {"route": route, "edges": route_edges}
        break
    if chosen is None:
        raise AccessForbidden(
            f"路网版本 {road_version} 下 {origin_node}->{venue_node} 无可用接驳路线（封闭或事故阻断）",
            field="road_version",
        )
    travel_minutes = sum(int(edges[e].get("minutes", 0)) for e in chosen["edges"])
    depart = needed - timedelta(minutes=SECURITY_BUFFER_MINUTES + travel_minutes)
    try:
        pool = catalog.latest("resource_pool", "primary").snapshot
    except FactVersionUnknown as exc:
        raise AccessForbidden("尚未登记接驳运力池，无法安排接驳车", field="resources") from exc
    seats = int(pool.get("seats_per_vehicle", DEFAULT_SEATS_PER_VEHICLE))
    vehicles = max(1, math.ceil(headcount / seats))
    resources = [
        {"resource_ref": "vehicle:shuttle", "quantity": vehicles},
        {"resource_ref": "driver:shuttle", "quantity": vehicles},
    ]
    if accessibility_seats > 0:
        resources.append({"resource_ref": "accessibility_seat:shuttle", "quantity": accessibility_seats})
    return {
        "mode": "shuttle",
        "origin_ref": origin_ref,
        "venue_ref": venue_ref,
        "gate_ref": gate["gate_ref"],
        "route_ref": chosen["route"].get("route_ref"),
        "depart_at": depart.isoformat(),
        "arrive_at": (depart + timedelta(minutes=travel_minutes)).isoformat(),
        "travel_minutes": travel_minutes,
        "route_edges": chosen["edges"],
        "resources": resources,
    }


def e_id_in(edges: list[str], closed: list[str]) -> bool:
    return any(e in closed for e in edges)


def build_checkpoints(plan: dict[str, Any], gate: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [
        {"kind": "boarding", "at": plan["origin_ref"], "by": plan["depart_at"]},
        {"kind": "security", "gate_ref": plan["gate_ref"], "by": plan["arrive_at"]},
        {"kind": "arrival", "at": plan["venue_ref"], "by": plan["needed_at"]},
    ]


def gate_access(catalog, gate_ref: str) -> dict[str, Any]:
    gate = security_gate(catalog, gate_ref)
    return {
        "gate_ref": gate_ref,
        "allowed_categories": list(gate.get("allowed_categories", [])),
        "open_from": gate.get("open_from"),
        "open_to": gate.get("open_to"),
    }
