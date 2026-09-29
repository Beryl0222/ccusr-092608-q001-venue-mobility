"""测试用事实装配：构造一套自洽的分散赛区事实底座。"""

from __future__ import annotations

from typing import Any

from venue_mobility.service import MobilityService


def register_world(svc: MobilityService, *, pools: dict[str, Any] | None = None,
                   transit: list[dict[str, Any]] | None = None,
                   roads: dict[str, Any] | None = None) -> None:
    svc.register_fact("resource_pool", "primary", {
        "seats_per_vehicle": 15,
        "pools": pools or {
            "vehicle:shuttle": {"capacity": 4},
            "driver:shuttle": {"capacity": 4},
            "accessibility_seat:shuttle": {"capacity": 6},
        },
    })
    svc.register_fact("lodging", "hotel-a", {"lodging_ref": "hotel-a", "road_node": "A"})
    svc.register_fact("lodging", "hotel-b", {"lodging_ref": "hotel-b", "road_node": "B"})
    svc.register_fact("road_network", "primary", roads or {
        "edges": {"e1": {"minutes": 10}, "e2": {"minutes": 20}, "e3": {"minutes": 15}},
        "routes": {
            "r-north": {"start": "A", "end": "V1", "edges": ["e1", "e2"]},
            "r-south": {"start": "B", "end": "V1", "edges": ["e3"]},
        },
        "closed_edges": [], "incidents": {},
    })
    svc.register_fact("schedule", "V1", {"venue_ref": "V1", "road_node": "V1",
                                         "sessions": [{"session_ref": "s1",
                                                       "start": "2026-09-26T15:00:00+08:00"}]})
    svc.register_fact("security_window", "gate-ath", {"gate_ref": "gate-ath", "venue_ref": "V1",
        "allowed_categories": ["athlete"], "open_from": "06:00", "open_to": "22:00", "priority": 1})
    svc.register_fact("security_window", "gate-mix", {"gate_ref": "gate-mix", "venue_ref": "V1",
        "allowed_categories": ["media", "official", "staff", "athlete"],
        "open_from": "06:00", "open_to": "22:00", "priority": 2})
    svc.register_fact("public_transit", "primary", {"services": transit or []})


def request_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "request_key": "RK-1",
        "requester": "team-alpha",
        "category": "athlete",
        "headcount": 30,
        "accessibility_seats": 2,
        "origin_ref": "hotel-a",
        "venue_ref": "V1",
        "needed_at": "2026-09-26T14:00:00+08:00",
    }
    body.update(overrides)
    return body


def confirm_and_freeze(svc: MobilityService, demand_id: str, *,
                       gate: str = "gate-ath", category: str = "athlete",
                       origin: str = "hotel-a", headcount: int = 30,
                       accessibility: int = 2,
                       transport_claims: dict[str, Any] | None = None) -> None:
    svc.confirm_fact(demand_id, "team", f"{demand_id}-team", {
        "headcount": headcount, "accessibility_seats": accessibility,
        "origin_ref": origin, "category": category})
    svc.confirm_fact(demand_id, "transport", f"{demand_id}-transport",
                     transport_claims or {"road_version": 1, "vehicle_ready": True})
    svc.confirm_fact(demand_id, "security", f"{demand_id}-security",
                     {"gate_ref": gate, "access_category": category})
    svc.freeze_schedule(demand_id)
