"""角色投影：同一事实底座，对不同角色输出可执行且不过度披露的行程。

- team（运动队）：只看本队：登车/安检/到场三个时间点、通道与类别、无障碍席位落实；
  不暴露车辆/司机台账、路网边、其他队伍。
- transport（交通方）：看运力与路线：车型数量、司机数、无障碍席位、路线边、发车时间；
  不暴露安检窗口对其他类别的安排。
- security（安保方）：看安检侧：通道、适用类别、到场窗口、人数；不暴露驻地、发车时间、司机。
- ops（运行中心）：完整决策依据：锁定版本、确认、占用回执、改线历史、升级与案卷。
"""

from __future__ import annotations

from typing import Any

from .errors import RoleForbidden
from .service import DemandState, MobilityService

ROLES = ("team", "transport", "security", "ops")


def project_demand(service: MobilityService, demand_id: str, role: str,
                   *, identity: str | None = None) -> dict[str, Any]:
    if role not in ROLES:
        raise RoleForbidden(f"未知角色 {role}", field="role", demand_id=demand_id)
    state = service._demand(demand_id)
    if role == "team":
        if identity is not None and identity != state.requester:
            raise RoleForbidden(
                "运动队只能查看本队行程，不得查询其他队伍",
                field="identity", demand_id=demand_id,
            )
        return _team_view(state)
    if role == "transport":
        return _transport_view(state)
    if role == "security":
        return _security_view(state)
    return _ops_view(service, state)


def _base(state: DemandState) -> dict[str, Any]:
    return {
        "demand_id": state.demand_id,
        "status": state.status,
        "category": state.request["category"],
        "headcount": state.request["headcount"],
        "accessibility_seats": state.request["accessibility_seats"],
        "venue_ref": state.request["venue_ref"],
        "needed_at": state.request["needed_at"],
    }


def _itinerary(state: DemandState) -> dict[str, Any]:
    return state.clearance["itinerary"] if state.clearance else {}


def _team_view(state: DemandState) -> dict[str, Any]:
    view = _base(state)
    view["requester"] = state.requester
    itin = _itinerary(state)
    if state.clearance:
        view["itinerary"] = {
            "mode": itin.get("mode"),
            "depart_at": itin.get("depart_at"),
            "arrive_at": itin.get("arrive_at"),
            "boarding_at": state.request.get("origin_ref"),
        }
        if itin.get("transit_service"):
            view["itinerary"]["transit_service"] = itin["transit_service"]
            view["itinerary"]["accessible"] = bool(itin.get("accessible"))
        view["access"] = {
            "gate_ref": state.clearance["access"]["gate_ref"],
            "your_category": state.request["category"],
            "open_from": state.clearance["access"].get("open_from"),
            "open_to": state.clearance["access"].get("open_to"),
        }
        view["checkpoints"] = state.clearance["checkpoints"]
        accessible_seats = state.request["accessibility_seats"]
        accessible_by_shuttle = accessible_seats <= sum(
            r["quantity"] for r in state.clearance["resources"]
            if r["resource_ref"] == "accessibility_seat:shuttle"
        )
        accessible_by_transit = itin.get("mode") == "public_transit" and itin.get("accessible")
        view["accessibility_confirmed"] = (
            accessible_seats == 0 or accessible_by_shuttle or accessible_by_transit
        )
        view["schedule_locked"] = {
            "schedule_version": state.schedule_version,
            "road_version": state.road_version,
        }
    else:
        view["confirmations_pending"] = [
            r for r in ("team", "transport", "security") if r not in state.confirmations
        ]
    if state.departed_at:
        view["departed_at"] = state.departed_at
    if state.arrived_at:
        view["arrived_at"] = state.arrived_at
    if state.isolated_from:
        view["notice"] = "本申请因路线或人数与同名申请不同，已隔离为独立行程"
    return view


def _transport_view(state: DemandState) -> dict[str, Any]:
    view = _base(state)
    view["origin_ref"] = state.request["origin_ref"]
    itin = _itinerary(state)
    if state.clearance:
        view["itinerary"] = {
            "mode": itin.get("mode"),
            "depart_at": itin.get("depart_at"),
            "arrive_at": itin.get("arrive_at"),
            "route_ref": itin.get("route_ref"),
            "route_edges": itin.get("route_edges", []),
            "transit_service": itin.get("transit_service"),
        }
        view["gate_ref"] = itin.get("gate_ref")
        view["resource_manifest"] = [
            {"resource_ref": r["resource_ref"], "quantity": r["quantity"]}
            for r in state.clearance["resources"]
        ]
        view["schedule_locked"] = {
            "schedule_version": state.schedule_version,
            "road_version": state.road_version,
        }
    if state.departed_at:
        view["departed_at"] = state.departed_at
    if state.no_show_due_at:
        view["no_show_due_at"] = state.no_show_due_at
    rev = state.revision
    if rev is not None and rev.status == "pending":
        view["pending_revision"] = {
            "revision_id": rev.revision_id,
            "review_due_at": rev.review_due_at,
            "extra_resources": [
                {"resource_ref": r, "quantity": q} for r, q in rev.extra_items
            ],
        }
    return view


def _security_view(state: DemandState) -> dict[str, Any]:
    view = _base(state)
    if state.clearance:
        view["gate_ref"] = state.clearance["access"]["gate_ref"]
        view["access"] = {
            "gate_ref": state.clearance["access"]["gate_ref"],
            "allowed_categories": state.clearance["access"]["allowed_categories"],
            "open_from": state.clearance["access"].get("open_from"),
            "open_to": state.clearance["access"].get("open_to"),
        }
        view["arrival_window"] = {
            "arrive_at": _itinerary(state).get("arrive_at"),
            "needed_at": state.request["needed_at"],
        }
        view["screening_load"] = state.request["headcount"]
    return view


def _ops_view(service: MobilityService, state: DemandState) -> dict[str, Any]:
    view = _base(state)
    view.update({
        "requester": state.requester,
        "request_key": state.request_key,
        "receipt_key": state.receipt_key,
        "fingerprint": state.fingerprint,
        "isolated_from": state.isolated_from,
        "confirmations": state.confirmations,
        "clearance": state.clearance,
        "revision_history": state.revision_history,
        "escalations": state.escalations,
        "case_id": state.case_id,
        "submitted_at": state.submitted_at,
    })
    if state.revision is not None:
        view["pending_revision"] = {
            "revision_id": state.revision.revision_id,
            "review_due_at": state.revision.review_due_at,
            "status": state.revision.status,
        }
    if state.departed_at:
        view["departed_at"] = state.departed_at
        view["no_show_due_at"] = state.no_show_due_at
    if state.arrived_at:
        view["arrived_at"] = state.arrived_at
    return view


def list_demands(service: MobilityService, role: str, *, identity: str | None = None) -> list[dict[str, Any]]:
    if role not in ROLES:
        raise RoleForbidden(f"未知角色 {role}", field="role")
    rows: list[dict[str, Any]] = []
    for demand_id in sorted(service.demands):
        state = service.demands[demand_id]
        if role == "team" and identity is not None and state.requester != identity:
            continue
        rows.append({
            "demand_id": demand_id,
            "status": state.status,
            "category": state.request["category"],
            "venue_ref": state.request["venue_ref"],
            "needed_at": state.request["needed_at"],
            "has_pending_revision": state.revision is not None
            and state.revision.status == "pending",
        })
    return rows
