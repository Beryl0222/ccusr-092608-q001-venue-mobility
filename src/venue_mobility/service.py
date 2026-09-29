"""通行保障主服务：事件溯源的决策内核。

写入路径：命令 -> 领域校验 -> 原子资源核算 -> 事件追加 -> 状态推进。
读取路径：状态按角色投影为可执行但最小披露的行程视图（见 projections 模块）。
所有时间判断都走可控时钟；截止时间以绝对时间落在事件里，进程恢复后期限不平移。
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from .clock import ControllableClock, parse_dt
from .contracts import validate_event
from .errors import (
    DomainRejection,
    EventContractBroken,
    IllegalTransition,
    ReceiptConflict,
    RoleForbidden,
    ValidationRejection,
)
from .facts import FACT_KINDS, FactCatalog, RESOURCE_POOL
from .planner import (
    CONFIRMATION_DOMAINS,
    build_checkpoints,
    gate_access,
    plan_itinerary,
)
from .store import EventStore

RATIFICATION_WINDOW_MINUTES = 30
NOSHOR_WINDOW_MINUTES = 90

ROLES = ("team", "transport", "security", "ops")
CATEGORIES = ("athlete", "media", "official", "staff")

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def fingerprint_request(body: dict[str, Any]) -> str:
    """路线或人数不同 -> 指纹不同；相同标识据此隔离。"""
    material = {
        "origin_ref": body.get("origin_ref"),
        "venue_ref": body.get("venue_ref"),
        "category": body.get("category"),
        "headcount": body.get("headcount"),
        "accessibility_seats": body.get("accessibility_seats"),
        "needed_at": body.get("needed_at"),
        "facts": body.get("facts", {}),
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class RevisionState:
    revision_id: str
    demand_id: str
    reason: str
    effective_at: str
    extra_items: list[tuple[str, int]]
    review_due_at: str | None = None
    status: str = "pending"  # pending / ratified / expired


@dataclass
class DemandState:
    demand_id: str
    request_key: str
    fingerprint: str
    receipt_key: str
    submitted_at: str
    request: dict[str, Any]
    confirmations: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: str = "submitted"  # submitted/frozen/cleared/departed/arrived/released
    schedule_version: int | None = None
    road_version: int | None = None
    clearance: dict[str, Any] | None = None
    revision: RevisionState | None = None
    revision_history: list[dict[str, Any]] = field(default_factory=list)
    departed_at: str | None = None
    no_show_due_at: str | None = None
    arrived_at: str | None = None
    case_id: str | None = None
    isolated_from: str | None = None
    escalations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def requester(self) -> str:
        return str(self.request.get("requester", ""))

    def resource_items(self) -> list[tuple[str, int]]:
        if not self.clearance:
            return []
        return [(r["resource_ref"], int(r["quantity"])) for r in self.clearance.get("resources", [])]


class MobilityService:
    def __init__(self, store: EventStore, clock: ControllableClock) -> None:
        self.store = store
        self.clock = clock
        self.schema = load_schema()
        self.catalog = FactCatalog()
        from .inventory import Inventory
        self.inventory = Inventory()
        self._versions: dict[str, int] = {}
        self._event_ids: set[str] = set()
        self.demands: dict[str, DemandState] = {}
        self._by_request: dict[str, dict[str, str]] = {}  # request_key -> fingerprint -> demand_id
        self._receipts: dict[str, str] = {}  # receipt_key -> demand_id
        self._demand_seq = 0
        self._revision_seq = 0
        self.revisions: dict[str, RevisionState] = {}
        self.cases: dict[str, str] = {}
        if store.events:
            self._replay()

    # ------------------------------------------------------------------ 事件

    def _emit(self, aggregate_type: str, aggregate_id: str, event_type: str,
              payload: dict[str, Any], *, event_id: str | None = None) -> dict[str, Any]:
        version = self._versions.get(aggregate_id, 0) + 1
        eid = event_id or f"{aggregate_id}-v{version}"
        if eid in self._event_ids:
            for existing in self.store.events:
                if existing["event_id"] == eid:
                    return existing
            raise EventContractBroken(f"事件标识冲突但日志中缺失: {eid}")
        event = {
            "event_id": eid,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self.clock.iso(),
            "version": version,
            "payload": payload,
        }
        issues = validate_event(event, self.schema)
        if issues:
            detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
            raise EventContractBroken(f"领域事件未通过契约: {detail}")
        self._versions[aggregate_id] = version
        self._event_ids.add(eid)
        self.store.append(event)
        self.store.save_clock(self.clock.iso())
        return event

    # ------------------------------------------------------------------ 事实

    def register_fact(self, kind: str, key: str, snapshot: dict[str, Any],
                      *, version: int | None = None) -> dict[str, Any]:
        if kind not in FACT_KINDS:
            raise ValidationRejection(f"未知事实类别 {kind}", field="fact_kind")
        if not key or not isinstance(key, str):
            raise ValidationRejection("事实键必须是非空字符串", field="fact_key")
        if not isinstance(snapshot, dict):
            raise ValidationRejection("事实快照必须是 JSON 对象", field="snapshot")
        if version is None:
            try:
                version = self.catalog.latest(kind, key).version + 1
            except Exception:
                version = 1
        edition = self.catalog.register(kind, key, version, snapshot)
        if kind == RESOURCE_POOL:
            self.inventory.apply_pool_snapshot(snapshot)
        return self._emit(
            "fact_catalog", f"fact-{kind}-{key}", "FACT_REGISTERED", edition.as_payload(),
            event_id=f"fact-{kind}-{key}-v{version}",
        )

    # ------------------------------------------------------------------ 申请

    def submit_demand(self, body: dict[str, Any]) -> tuple[DemandState, list[dict[str, Any]], bool]:
        """提交通行申请。返回(申请状态, 新事件列表, 是否隔离副本)。"""
        self._validate_request(body)
        request_key = str(body["request_key"])
        fp = fingerprint_request(body)
        receipt_key = str(body.get("receipt_key") or f"rcpt-{request_key}-{fp}")

        prior_fp = self._by_request.get(request_key, {})
        if fp in prior_fp:
            # 同标识同指纹：重复送达原样返回，不产生事件、不扣减运力。
            if receipt_key in self._receipts and self._receipts[receipt_key] != prior_fp[fp]:
                raise ReceiptConflict(
                    "回执已用于另一份路线/人数不同的申请，不得二次扣减",
                    field="receipt_key",
                )
            return self.demands[prior_fp[fp]], [], False
        if receipt_key in self._receipts:
            raise ReceiptConflict(
                f"回执 {receipt_key} 已被申请 {self._receipts[receipt_key]} 使用，重复回执不得二次扣减",
                field="receipt_key",
            )

        isolated = bool(prior_fp)
        self._demand_seq += 1
        demand_id = f"demand-{self._demand_seq:04d}"
        isolated_from: str | None = None
        new_events: list[dict[str, Any]] = []
        if isolated:
            held_fp = next(iter(prior_fp))
            isolated_from = prior_fp[held_fp]
            new_events.append(self._emit(
                "travel_demand", demand_id, "DEMAND_ISOLATED",
                {
                    "reused_key": request_key,
                    "held_fingerprint": held_fp,
                    "incoming_fingerprint": fp,
                    "reason": "相同申请标识但路线或人数不同，隔离为独立申请",
                },
            ))
        payload = {
            "requester": body["requester"],
            "category": body["category"],
            "headcount": body["headcount"],
            "accessibility_seats": body["accessibility_seats"],
            "origin_ref": body["origin_ref"],
            "venue_ref": body["venue_ref"],
            "needed_at": body["needed_at"],
            "facts": body.get("facts", {}),
            "request_key": request_key,
            "receipt_key": receipt_key,
            "fingerprint": fp,
        }
        if isolated_from:
            payload["isolated_from"] = isolated_from
        new_events.append(self._emit(
            "travel_demand", demand_id, "DEMAND_SUBMITTED", payload,
        ))
        state = DemandState(
            demand_id=demand_id,
            request_key=request_key,
            fingerprint=fp,
            receipt_key=receipt_key,
            submitted_at=self.clock.iso(),
            request=payload,
            isolated_from=isolated_from,
        )
        self._index(state)
        return state, new_events, isolated

    def _index(self, state: DemandState) -> None:
        self.demands[state.demand_id] = state
        self._by_request.setdefault(state.request_key, {})[state.fingerprint] = state.demand_id
        self._receipts[state.receipt_key] = state.demand_id

    def _validate_request(self, body: dict[str, Any]) -> None:
        if not isinstance(body, dict):
            raise ValidationRejection("申请必须是 JSON 对象")
        required = ("request_key", "requester", "category", "headcount",
                    "accessibility_seats", "origin_ref", "venue_ref", "needed_at")
        for name in required:
            if name not in body or body[name] in (None, ""):
                raise ValidationRejection(f"申请缺少字段 {name}", field=name)
        if body["category"] not in CATEGORIES:
            raise ValidationRejection(f"人员类别必须是 {CATEGORIES} 之一", field="category")
        for name in ("headcount", "accessibility_seats"):
            value = body[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationRejection(f"{name} 必须是非负整数", field=name)
        if body["headcount"] <= 0:
            raise ValidationRejection("headcount 必须大于 0", field="headcount")
        if body["accessibility_seats"] > body["headcount"]:
            raise ValidationRejection("无障碍席位数不能超过总人数", field="accessibility_seats")
        parse_dt(body["needed_at"])

    # ------------------------------------------------------------------ 确认

    def confirm_fact(self, demand_id: str, role: str, confirmation_key: str,
                     claims: dict[str, Any]) -> tuple[DemandState, dict[str, Any] | None]:
        state = self._demand(demand_id)
        if role not in CONFIRMATION_DOMAINS:
            raise RoleForbidden(
                f"角色 {role} 不允许确认事实；可确认角色: team/transport/security",
                field="role", demand_id=demand_id,
            )
        if state.status != "submitted":
            raise IllegalTransition(
                f"申请状态为 {state.status}，事实确认通道仅在锁定前开放", demand_id=demand_id)
        if not isinstance(claims, dict) or not claims:
            raise ValidationRejection("确认必须携带非空 claims", field="claims")
        allowed = CONFIRMATION_DOMAINS[role]
        unknown = sorted(set(claims) - allowed)
        if unknown:
            raise RoleForbidden(
                f"{role} 只掌握 {sorted(allowed)}，无权确认 {unknown}",
                field="claims", demand_id=demand_id,
            )
        prior = state.confirmations.get(role)
        if prior is not None and prior["confirmation_key"] == confirmation_key:
            return state, None  # 同一确认键重放：幂等
        event = self._emit(
            "travel_demand", demand_id, "FACT_CONFIRMED",
            {"role": role, "confirmation_key": confirmation_key, "claims": claims},
            event_id=f"{demand_id}-confirm-{role}-{confirmation_key}",
        )
        state.confirmations[role] = {
            "confirmation_key": confirmation_key,
            "claims": copy.deepcopy(claims),
            "at": self.clock.iso(),
        }
        return state, event

    def freeze_schedule(self, demand_id: str) -> tuple[DemandState, dict[str, Any]]:
        state = self._demand(demand_id)
        if state.status != "submitted":
            raise IllegalTransition(
                f"只有待确认申请可以锁定赛程，当前状态 {state.status}", demand_id=demand_id)
        missing = [r for r in ("team", "transport", "security") if r not in state.confirmations]
        if missing:
            raise IllegalTransition(f"三方确认不齐，缺少: {missing}", demand_id=demand_id)
        team_claims = state.confirmations["team"]["claims"]
        for field_name in ("headcount", "accessibility_seats", "origin_ref", "category"):
            if field_name in team_claims and team_claims[field_name] != state.request.get(field_name):
                raise IllegalTransition(
                    f"运动队确认的 {field_name} 与申请不符；事实已变，请以新申请重新提交并隔离",
                    demand_id=demand_id,
                )
        pinned = state.request.get("facts", {})
        schedule_version = int(pinned.get("schedule_version")
                               or self.catalog.latest("schedule", state.request["venue_ref"]).version)
        road_version = int(pinned.get("road_version")
                           or self.catalog.latest("road_network", "primary").version)
        self.catalog.get("schedule", state.request["venue_ref"], schedule_version)
        self.catalog.get("road_network", "primary", road_version)
        event = self._emit(
            "travel_demand", demand_id, "SCHEDULE_FROZEN",
            {"schedule_version": schedule_version, "road_version": road_version},
        )
        state.schedule_version = schedule_version
        state.road_version = road_version
        state.status = "frozen"
        return state, event

    # ------------------------------------------------------------------ 放行

    def issue_clearance(self, demand_id: str) -> tuple[DemandState, list[dict[str, Any]]]:
        state = self._demand(demand_id)
        if state.status == "cleared" and state.clearance is not None:
            return state, []
        if state.status != "frozen":
            raise IllegalTransition(
                f"放行前必须先锁定赛程，当前状态 {state.status}", demand_id=demand_id)
        state, events = self._issue_for_plan(state, self._plan_current(state), phase="initial")
        return state, events

    def _plan_current(self, state: DemandState, *, schedule_version: int | None = None,
                      road_version: int | None = None) -> dict[str, Any]:
        req = state.request
        plan = plan_itinerary(
            self.catalog, self.inventory,
            category=req["category"], headcount=req["headcount"],
            accessibility_seats=req["accessibility_seats"],
            origin_ref=req["origin_ref"], venue_ref=req["venue_ref"],
            needed_at=req["needed_at"],
            schedule_version=schedule_version or state.schedule_version,  # type: ignore[arg-type]
            road_version=road_version or state.road_version,  # type: ignore[arg-type]
            now=self.clock.get(),
        )
        return plan

    def _issue_for_plan(self, state: DemandState, plan: dict[str, Any], *,
                        phase: str, revision_id: str | None = None) -> tuple[DemandState, list[dict[str, Any]]]:
        """占用资源并发行程事件。phase=initial 走首发，ratification 只追加净增量。"""
        events: list[dict[str, Any]] = []
        final_items = [(r["resource_ref"], int(r["quantity"])) for r in plan["resources"]]
        prior = Counter(dict(state.resource_items()))
        if phase == "initial":
            commit_items = final_items
            self.inventory.check(commit_items)  # 三维原子预检
        else:
            commit_items = sorted((Counter(dict(final_items)) - prior).items())
        for resource_ref, quantity in commit_items:
            eid = f"hold-{state.demand_id}-{resource_ref}"
            if phase == "ratification":
                eid += f"-{revision_id}"
            self.inventory.commit(
                eid, state.receipt_key, "demand", state.demand_id,
                resource_ref, quantity,
            )
            events.append(self._emit(
                "capacity_commitment", f"hold-{state.demand_id}", "CAPACITY_HELD",
                {"resource_ref": resource_ref, "quantity": quantity,
                 "receipt_key": state.receipt_key, "demand_id": state.demand_id,
                 "phase": phase, **({"revision_id": revision_id} if revision_id else {})},
                event_id=eid,
            ))
        clearance = {
            "receipt_key": state.receipt_key,
            "schedule_version": plan["schedule_version"],
            "road_version": plan["road_version"],
            "itinerary": {k: plan[k] for k in (
                "mode", "origin_ref", "venue_ref", "gate_ref", "depart_at",
                "arrive_at", "travel_minutes", "route_ref", "route_edges",
                "transit_service", "accessible") if k in plan},
            "checkpoints": build_checkpoints(plan),
            "access": gate_access(self.catalog, plan["gate_ref"]),
            "resources": plan["resources"],
            "issued_at": self.clock.iso(),
            "phase": phase,
            **({"revision_id": revision_id} if revision_id else {}),
            **({"supersedes": {"schedule_version": state.schedule_version,
                               "road_version": state.road_version}} if phase == "ratification" else {}),
        }
        events.append(self._emit(
            "travel_demand", state.demand_id, "CLEARANCE_ISSUED", clearance,
            event_id=(f"{state.demand_id}-clearance-{revision_id}"
                      if revision_id else f"{state.demand_id}-clearance"),
        ))
        state.clearance = clearance
        state.schedule_version = plan["schedule_version"]
        state.road_version = plan["road_version"]
        state.status = "cleared"
        return state, events

    # ------------------------------------------------------------------ 改线

    def emergency_revise(self, demand_id: str, reason: str) -> tuple[RevisionState, list[dict[str, Any]]]:
        state = self._demand(demand_id)
        if state.status != "cleared":
            raise IllegalTransition(
                f"只有已放行未发车的申请可以紧急改线，当前状态 {state.status}", demand_id=demand_id)
        if state.revision is not None and state.revision.status == "pending":
            raise IllegalTransition(
                "已有待批准的紧急改线，请先补齐批准或等待到期处置",
                demand_id=demand_id, revision_id=state.revision.revision_id)
        req = state.request
        latest_schedule = self.catalog.latest("schedule", req["venue_ref"]).version
        latest_road = self.catalog.latest("road_network", "primary").version
        new_plan = self._plan_current(state, schedule_version=latest_schedule,
                                      road_version=latest_road)
        base = Counter(dict(state.resource_items()))
        wanted = Counter({r["resource_ref"]: int(r["quantity"]) for r in new_plan["resources"]})
        # 紧急只占用“最低必要”的增量资源；冗余资源在批准后才归还。
        extra = sorted((wanted - base).items())
        if extra:
            self.inventory.check(extra)

        self._revision_seq += 1
        revision_id = f"rev-{demand_id}-{self._revision_seq:02d}"
        effective_at = self.clock.iso()
        review_due_at = (self.clock.get() + timedelta(minutes=RATIFICATION_WINDOW_MINUTES)).isoformat()
        events: list[dict[str, Any]] = []
        scope = {
            "demand_id": demand_id,
            "from_versions": {"schedule_version": state.schedule_version,
                              "road_version": state.road_version},
            "to_versions": {"schedule_version": latest_schedule, "road_version": latest_road},
            "affected_resources": [{"resource_ref": r, "quantity": q} for r, q in extra],
            "route_ref": new_plan.get("route_ref"),
            "route_edges": new_plan.get("route_edges", []),
            "gate_ref": new_plan["gate_ref"],
            "plan": {k: new_plan[k] for k in (
                "mode", "origin_ref", "venue_ref", "gate_ref", "depart_at",
                "arrive_at", "travel_minutes", "route_ref", "route_edges",
                "transit_service", "accessible", "resources", "needed_at",
                "schedule_version", "road_version") if k in new_plan},
        }
        events.append(self._emit(
            "route_revision", revision_id, "ROUTE_REVISED",
            {"impact_scope": scope, "effective_at": effective_at,
             "review_due_at": review_due_at,
             "reason": reason, "demand_id": demand_id},
        ))
        for resource_ref, quantity in extra:
            self.inventory.commit(
                f"hold-emergency-{revision_id}-{resource_ref}",
                f"emergency-{revision_id}", "emergency", revision_id,
                resource_ref, quantity,
            )
            events.append(self._emit(
                "capacity_commitment", f"hold-emergency-{revision_id}", "EMERGENCY_RELEASED",
                {"revision_id": revision_id, "resource_ref": resource_ref, "quantity": quantity,
                 "review_due_at": review_due_at, "reason": reason, "demand_id": demand_id},
                event_id=f"hold-emergency-{revision_id}-{resource_ref}",
            ))
        revision = RevisionState(
            revision_id=revision_id, demand_id=demand_id, reason=reason,
            effective_at=effective_at, review_due_at=review_due_at,
            extra_items=extra, status="pending",
        )
        state.revision = revision
        self.revisions[revision_id] = revision
        self._open_case(state, f"紧急改线: {reason}")
        return revision, events

    def ratify_revision(self, revision_id: str) -> tuple[DemandState, list[dict[str, Any]]]:
        revision = self.revisions.get(revision_id)
        if revision is None:
            raise IllegalTransition(f"改线 {revision_id} 不存在", revision_id=revision_id)
        if revision.status != "pending":
            raise IllegalTransition(f"改线状态为 {revision.status}，不能重复批准",
                                    revision_id=revision_id)
        if revision.review_due_at and self.clock.get() > parse_dt(revision.review_due_at):
            raise IllegalTransition(
                "已超过补齐批准的限期，该改线已由到期处置释放并升级",
                revision_id=revision_id,
            )
        state = self.demands[revision.demand_id]
        new_plan = self._recover_revision_plan(revision)
        events = self._settle_ratification(state, revision, new_plan)
        events.append(self._emit(
            "route_revision", revision_id, "EMERGENCY_RATIFIED",
            {"revision_id": revision_id, "ratified_at": self.clock.iso()},
        ))
        return state, events

    def _recover_revision_plan(self, revision: RevisionState) -> dict[str, Any]:
        """批准时按锁定的新版本重新规划；恢复后也以日志 impact_scope.plan 为权威。"""
        state = self.demands[revision.demand_id]
        scope = self._revision_scope(revision.revision_id)
        if scope and scope.get("plan"):
            plan = copy.deepcopy(scope["plan"])
            return plan
        return self._plan_current(
            state,
            schedule_version=scope["to_versions"]["schedule_version"] if scope else None,
            road_version=scope["to_versions"]["road_version"] if scope else None,
        )

    def _revision_scope(self, revision_id: str) -> dict[str, Any] | None:
        for event in reversed(self.store.events):
            if event["event_type"] == "ROUTE_REVISED" and event["aggregate_id"] == revision_id:
                return event["payload"]["impact_scope"]
        return None

    def _settle_ratification(self, state: DemandState, revision: RevisionState,
                             plan: dict[str, Any]) -> list[dict[str, Any]]:
        """临时占用转正：先按新方案记账，再释放全部临时占用与原方案冗余，全程不超配。"""
        events: list[dict[str, Any]] = []
        base = Counter(dict(state.resource_items()))
        final = Counter({r["resource_ref"]: int(r["quantity"]) for r in plan["resources"]})
        additional = Counter(final - base)
        provisional = Counter(dict(revision.extra_items))
        # 临时占用即将转为正式占用；预检只需覆盖临持之外的净缺口。
        uncovered = sorted((additional - provisional).items())
        if uncovered:
            self.inventory.check(uncovered)
        # 先重新发行程（内含增量正式占用事件）。
        _, issued = self._issue_for_plan(
            state, plan, phase="ratification", revision_id=revision.revision_id)
        events.extend(issued)
        # 原方案冗余运力归还。
        for resource_ref, quantity in sorted((base - final).items()):
            self.inventory.release(resource_ref, quantity, "demand", state.demand_id)
            events.append(self._emit(
                "capacity_commitment", f"hold-{state.demand_id}", "CAPACITY_RELEASED",
                {"resource_ref": resource_ref, "quantity": quantity,
                 "reason": f"改线 {revision.revision_id} 批准后归还冗余运力",
                 "phase": "ratified", "revision_id": revision.revision_id,
                 "demand_id": state.demand_id},
                event_id=f"release-{state.demand_id}-{resource_ref}-{revision.revision_id}-surplus",
            ))
        # 全部紧急临时占用释放（增量已由正式占用承接，账实相符）。
        for resource_ref, quantity in revision.extra_items:
            self.inventory.release(resource_ref, quantity, "emergency", revision.revision_id)
            events.append(self._emit(
                "capacity_commitment", f"hold-emergency-{revision.revision_id}",
                "CAPACITY_RELEASED",
                {"resource_ref": resource_ref, "quantity": quantity,
                 "reason": f"改线 {revision.revision_id} 批准，临时占用转入正式安排并释放临持账",
                 "phase": "ratified", "revision_id": revision.revision_id,
                 "demand_id": state.demand_id},
                event_id=f"release-emergency-{revision.revision_id}-{resource_ref}-ratified",
            ))
        revision.status = "ratified"
        state.revision_history.append({
            "revision_id": revision.revision_id,
            "reason": revision.reason,
            "effective_at": revision.effective_at,
            "ratified_at": self.clock.iso(),
            "status": "ratified",
            "schedule_version": plan["schedule_version"],
            "road_version": plan["road_version"],
        })
        if state.revision is revision:
            state.revision = None
        return events

    # ------------------------------------------------------------------ 到场

    def depart(self, demand_id: str) -> dict[str, Any]:
        state = self._demand(demand_id)
        if state.status != "cleared":
            raise IllegalTransition(
                f"只有已放行申请可以发车，当前状态 {state.status}", demand_id=demand_id)
        if state.revision is not None and state.revision.status == "pending":
            raise IllegalTransition("紧急改线尚未补齐批准，不能发车", demand_id=demand_id)
        departed_at = self.clock.iso()
        no_show_due = (self.clock.get() + timedelta(minutes=NOSHOR_WINDOW_MINUTES)).isoformat()
        event = self._emit(
            "travel_demand", demand_id, "DEPARTED",
            {"departed_at": departed_at, "no_show_due_at": no_show_due},
        )
        state.departed_at = departed_at
        state.no_show_due_at = no_show_due
        state.status = "departed"
        return event

    def arrive(self, demand_id: str, *, arrived_at: str | None = None) -> list[dict[str, Any]]:
        state = self._demand(demand_id)
        if state.status != "departed":
            raise IllegalTransition(
                f"只有已发车申请可以确认到场，当前状态 {state.status}", demand_id=demand_id)
        moment = parse_dt(arrived_at) if arrived_at else self.clock.get()
        if state.no_show_due_at and moment > parse_dt(state.no_show_due_at):
            raise IllegalTransition("已超过失约释放时限，不能补录到场；请走升级处置",
                                    demand_id=demand_id)
        events = [self._emit(
            "travel_demand", demand_id, "ARRIVAL_CONFIRMED",
            {"arrived_at": moment.isoformat()},
        )]
        for resource_ref, quantity in state.resource_items():
            self.inventory.release(resource_ref, quantity, "demand", state.demand_id)
            events.append(self._emit(
                "capacity_commitment", f"hold-{state.demand_id}", "CAPACITY_RELEASED",
                {"resource_ref": resource_ref, "quantity": quantity,
                 "reason": "队伍已到场，运力归还", "phase": "arrival",
                 "demand_id": state.demand_id},
            ))
        state.arrived_at = moment.isoformat()
        state.status = "arrived"
        state.no_show_due_at = None
        return events

    def escalate(self, demand_id: str, to_role: str, reason: str) -> dict[str, Any]:
        state = self._demand(demand_id)
        if to_role not in ROLES:
            raise ValidationRejection(f"升级对象角色必须是 {ROLES} 之一", field="to_role")
        event = self._emit(
            "travel_demand", demand_id, "ESCALATED",
            {"to_role": to_role, "reason": reason, "at": self.clock.iso()},
        )
        state.escalations.append({"to_role": to_role, "reason": reason, "at": self.clock.iso()})
        self._open_case(state, reason)
        return event

    def _open_case(self, state: DemandState, reason: str) -> str:
        if state.case_id is None:
            state.case_id = f"case-{state.demand_id}"
            self.cases[state.demand_id] = state.case_id
            self._emit(
                "arrival_case", state.case_id, "CASE_OPENED",
                {"demand_id": state.demand_id, "opened_at": self.clock.iso(),
                 "reason": reason},
            )
        return state.case_id

    # ------------------------------------------------------------------ 时钟

    def tick(self, *, minutes: int | None = None, seconds: int | None = None,
             to: str | None = None) -> list[dict[str, Any]]:
        """推进可控时钟并立即结算到期事项（批准到期、失约释放）。"""
        if to is not None:
            self.clock.set(to)
        else:
            self.clock.advance(minutes=minutes or 0, seconds=seconds or 0)
        self.store.save_clock(self.clock.iso())
        return self.due_now()

    def due_now(self) -> list[dict[str, Any]]:
        produced: list[dict[str, Any]] = []
        for revision in list(self.revisions.values()):
            if (revision.status == "pending" and revision.review_due_at
                    and self.clock.get() >= parse_dt(revision.review_due_at)):
                produced.extend(self._expire_revision(revision))
        for state in list(self.demands.values()):
            if (state.status == "departed" and state.no_show_due_at
                    and self.clock.get() >= parse_dt(state.no_show_due_at)):
                produced.extend(self._release_no_show(state))
        return produced

    def _expire_revision(self, revision: RevisionState) -> list[dict[str, Any]]:
        state = self.demands[revision.demand_id]
        events: list[dict[str, Any]] = []
        for resource_ref, quantity in revision.extra_items:
            self.inventory.release(resource_ref, quantity, "emergency", revision.revision_id)
            events.append(self._emit(
                "capacity_commitment", f"hold-emergency-{revision.revision_id}",
                "CAPACITY_RELEASED",
                {"resource_ref": resource_ref, "quantity": quantity,
                 "reason": f"紧急改线 {revision.revision_id} 未在限期内补齐批准，释放临时占用",
                 "phase": "expiry", "revision_id": revision.revision_id,
                 "demand_id": state.demand_id},
            ))
        revision.status = "expired"
        if state.revision is revision:
            state.revision = None
        state.revision_history.append({
            "revision_id": revision.revision_id,
            "reason": revision.reason,
            "effective_at": revision.effective_at,
            "review_due_at": revision.review_due_at,
            "expired_at": self.clock.iso(),
            "status": "expired",
        })
        reason = f"紧急改线 {revision.revision_id} 超时未批准，临时运力已释放，维持原放行安排"
        events.append(self._emit(
            "travel_demand", state.demand_id, "ESCALATED",
            {"to_role": "ops", "reason": reason, "at": self.clock.iso()},
        ))
        state.escalations.append({"to_role": "ops", "reason": reason, "at": self.clock.iso()})
        self._open_case(state, reason)
        return events

    def _release_no_show(self, state: DemandState) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        released_at = self.clock.iso()
        for resource_ref, quantity in state.resource_items():
            self.inventory.release(resource_ref, quantity, "demand", state.demand_id)
            events.append(self._emit(
                "capacity_commitment", f"hold-{state.demand_id}", "NO_SHOW_RELEASED",
                {"resource_ref": resource_ref, "quantity": quantity,
                 "released_at": released_at, "demand_id": state.demand_id},
            ))
        state.status = "released"
        state.no_show_due_at = None
        reason = "发车后超过失约时限未确认到场，运力已释放并升级"
        events.append(self._emit(
            "travel_demand", state.demand_id, "ESCALATED",
            {"to_role": "ops", "reason": reason, "at": released_at},
        ))
        state.escalations.append({"to_role": "ops", "reason": reason, "at": released_at})
        self._open_case(state, reason)
        return events

    # ------------------------------------------------------------------ 查询

    def _demand(self, demand_id: str) -> DemandState:
        state = self.demands.get(demand_id)
        if state is None:
            raise DomainRejection(f"申请 {demand_id} 不存在", code="demand_not_found",
                                  demand_id=demand_id)
        return state

    def fact_refs(self) -> list[dict[str, Any]]:
        return self.catalog.refs()

    def trace(self, demand_id: str) -> list[dict[str, Any]]:
        """与一次申请相关的完整事件链（复盘依据）。"""
        self._demand(demand_id)
        keep = []
        for event in self.store.events:
            payload = event.get("payload", {})
            if event["aggregate_id"] == demand_id:
                keep.append(event); continue
            if payload.get("demand_id") == demand_id:
                keep.append(event); continue
            if event["event_type"] == "CASE_OPENED" and payload.get("demand_id") == demand_id:
                keep.append(event)
            if event["event_type"] == "ROUTE_REVISED" and payload.get("demand_id") == demand_id:
                keep.append(event)
            rev_id = payload.get("revision_id")
            if rev_id and self.revisions.get(rev_id) and self.revisions[rev_id].demand_id == demand_id:
                keep.append(event)
        return keep

    # ------------------------------------------------------------------ 重放

    def _replay(self) -> None:
        for event in self.store.events:
            self._apply(event)

    def _apply(self, event: dict[str, Any]) -> None:
        etype = event["event_type"]
        agg_id = event["aggregate_id"]
        payload = event["payload"]
        self._versions[agg_id] = event["version"]
        self._event_ids.add(event["event_id"])
        at = event["occurred_at"]

        if etype == "FACT_REGISTERED":
            edition = self.catalog.register(
                payload["fact_kind"], payload["fact_key"],
                payload["fact_version"], payload["snapshot"])
            if edition.kind == RESOURCE_POOL:
                self.inventory.apply_pool_snapshot(edition.snapshot)
            return

        if etype == "DEMAND_SUBMITTED":
            seq = int(agg_id.split("-")[1])
            self._demand_seq = max(self._demand_seq, seq)
            state = DemandState(
                demand_id=agg_id,
                request_key=payload["request_key"],
                fingerprint=payload["fingerprint"],
                receipt_key=payload["receipt_key"],
                submitted_at=at,
                request=payload,
                isolated_from=payload.get("isolated_from"),
            )
            self._index(state)
            return

        if etype == "DEMAND_ISOLATED":
            return

        if etype == "ROUTE_REVISED":
            self._revision_seq = max(self._revision_seq, int(agg_id.rsplit("-", 1)[-1]))
            target = self.demands[payload["demand_id"]]
            revision = RevisionState(
                revision_id=agg_id, demand_id=target.demand_id,
                reason=payload.get("reason", ""),
                effective_at=payload["effective_at"],
                review_due_at=payload.get("review_due_at"),
                extra_items=[(r["resource_ref"], r["quantity"])
                             for r in payload["impact_scope"].get("affected_resources", [])],
            )
            self.revisions[agg_id] = revision
            target.revision = revision
            target.revision_history.append({
                "revision_id": agg_id,
                "reason": payload.get("reason", ""),
                "effective_at": payload["effective_at"],
                "review_due_at": payload.get("review_due_at"),
                "status": "pending",
            })
            return

        if etype == "EMERGENCY_RELEASED":
            revision = self.revisions[payload["revision_id"]]
            revision.review_due_at = payload["review_due_at"]
            self.inventory.commit(
                event["event_id"], f"emergency-{revision.revision_id}",
                "emergency", revision.revision_id,
                payload["resource_ref"], payload["quantity"],
            )
            return

        if agg_id in self.demands:
            target: DemandState = self.demands[agg_id]
        elif payload.get("demand_id") in self.demands:
            target = self.demands[payload["demand_id"]]
        else:
            target = None  # type: ignore[assignment]

        if etype == "FACT_CONFIRMED":
            target.confirmations[payload["role"]] = {
                "confirmation_key": payload["confirmation_key"],
                "claims": copy.deepcopy(payload["claims"]),
                "at": at,
            }
        elif etype == "SCHEDULE_FROZEN":
            target.schedule_version = payload["schedule_version"]
            target.road_version = payload["road_version"]
            target.status = "frozen"
        elif etype == "CAPACITY_HELD":
            owner_kind, owner_id, receipt = self._owner_of_hold(agg_id, payload)
            self.inventory.commit(
                event["event_id"], receipt, owner_kind, owner_id,
                payload["resource_ref"], payload["quantity"],
            )
        elif etype == "CAPACITY_RELEASED":
            owner_kind, owner_id, _ = self._owner_of_hold(agg_id, payload)
            self.inventory.release(payload["resource_ref"], payload["quantity"],
                                   owner_kind, owner_id)
            if payload.get("phase") == "expiry" and payload.get("revision_id"):
                rev = self.revisions.get(payload["revision_id"])
                if rev is not None:
                    rev.status = "expired"
                    demand = self.demands.get(rev.demand_id)
                    if demand is not None:
                        if demand.revision is rev:
                            demand.revision = None
                        entry = self._history_entry(demand, rev.revision_id)
                        if entry is not None:
                            entry["status"] = "expired"
                            entry["expired_at"] = at
        elif etype == "NO_SHOW_RELEASED":
            owner_kind, owner_id, _ = self._owner_of_hold(agg_id, payload)
            self.inventory.release(payload["resource_ref"], payload["quantity"],
                                   owner_kind, owner_id)
            if target is not None:
                target.status = "released"
                target.no_show_due_at = None
        elif etype == "CLEARANCE_ISSUED":
            target.clearance = copy.deepcopy(payload)
            target.schedule_version = payload["schedule_version"]
            target.road_version = payload["road_version"]
            target.status = "cleared"
            if payload.get("revision_id"):
                rev = self.revisions.get(payload["revision_id"])
                if rev is not None:
                    rev.status = "ratified"
                    if target.revision is rev:
                        target.revision = None
                entry = self._history_entry(target, payload["revision_id"])
                if entry is not None:
                    entry["status"] = "ratified"
                    entry["ratified_at"] = at
                    entry["schedule_version"] = payload["schedule_version"]
                    entry["road_version"] = payload["road_version"]
        elif etype == "DEPARTED":
            target.departed_at = payload["departed_at"]
            target.no_show_due_at = payload.get("no_show_due_at")
            target.status = "departed"
        elif etype == "ARRIVAL_CONFIRMED":
            target.arrived_at = payload["arrived_at"]
            target.status = "arrived"
            target.no_show_due_at = None
        elif etype == "EMERGENCY_RATIFIED":
            rev = self.revisions.get(payload["revision_id"])
            if rev is not None:
                rev.status = "ratified"
                demand = self.demands.get(rev.demand_id)
                if demand is not None and demand.revision is rev:
                    demand.revision = None
        elif etype == "ESCALATED":
            target.escalations.append({
                "to_role": payload["to_role"],
                "reason": payload["reason"],
                "at": at,
            })
        elif etype == "CASE_OPENED":
            case_demand = self.demands[payload["demand_id"]]
            case_demand.case_id = agg_id
            self.cases[case_demand.demand_id] = agg_id

    @staticmethod
    def _history_entry(state: DemandState, revision_id: str) -> dict[str, Any] | None:
        for entry in state.revision_history:
            if entry.get("revision_id") == revision_id:
                return entry
        return None

    def _owner_of_hold(self, agg_id: str, payload: dict[str, Any]) -> tuple[str, str, str]:
        if agg_id.startswith("hold-emergency-"):
            rev_id = agg_id[len("hold-emergency-"):]
            return "emergency", rev_id, f"emergency-{rev_id}"
        demand_id = agg_id[len("hold-"):]
        return "demand", demand_id, payload.get("receipt_key", f"receipt-{demand_id}")
