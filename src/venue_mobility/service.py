"""通行保障决策服务：版本锁定、角色事实、幂等放行、冲突隔离、紧急改线、失约与升级。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from .domain import FACT_OWNERS, FactRegistry, Planner, StaleFact
from .journal import EventStore
from .resources import ResourceLedger

RATIFICATION_WINDOW = timedelta(minutes=20)
ARRIVAL_BUFFER = timedelta(minutes=20)
DEPART_GRACE = timedelta(minutes=10)


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class RequestRecord:
    request_ref: str
    team_ref: str = ""
    fingerprint: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    basis: dict[str, int] = field(default_factory=dict)
    category: str = ""
    headcount: int = 0
    emergency: bool = False
    ratified: bool = False
    review_due_at: datetime | None = None
    denied_reason: str | None = None
    dispatched: bool = False
    arrived: bool = False
    released: bool = False
    escalated: bool = False
    supersedes: list[str] = field(default_factory=list)
    receipt: str = ""


class MobilityService:
    def __init__(self, store: EventStore, ledger: ResourceLedger | None = None,
                 facts: FactRegistry | None = None) -> None:
        self.store = store
        self.ledger = ledger or ResourceLedger()
        self.facts = facts or FactRegistry()
        self._receipts: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, RequestRecord] = {}
        self._denials: dict[str, list[dict[str, Any]]] = {}
        self._replay()

    # ------------------------------------------------------------------ 重放

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str,
              payload: Mapping[str, Any], event_id: str, occurred_at: datetime) -> dict[str, Any]:
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at.isoformat(),
            "version": self.store.next_version(aggregate_id),
            "payload": dict(payload),
        }
        if not self.store.append(event):
            raise ServiceError("duplicate_event", f"事件 {event_id} 已存在", 409)
        self._apply(event)
        return event

    def _apply(self, e: Mapping[str, Any]) -> None:
        et = e["event_type"]
        p = e.get("payload", {})
        at = datetime.fromisoformat(e["occurred_at"])
        if et == "FACT_RECORDED":
            try:
                self.facts.record(p["fact_type"], p["ref"], int(p["version"]),
                                  p["owner_role"], at, p.get("data", {}))
            except StaleFact:
                pass
            if p.get("fact_type") == "resource":
                self.ledger.register_pool(p["ref"], int(p["data"].get("capacity", 0)))
            if "receipt" in p:
                self._receipts[p["receipt"]] = {"kind": "fact", "event_id": e["event_id"]}
        elif et == "ROUTE_REVISED":
            if "receipt" in p:
                self._receipts.setdefault(p["receipt"], {"kind": "route_revised", "event_id": e["event_id"]})
        elif et in ("SCHEDULE_FROZEN", "REQUEST_DENIED"):
            ref = p["request_ref"]
            receipt = p.get("receipt")
            if et == "REQUEST_DENIED":
                self._denials.setdefault(ref, []).append(dict(p))
            rec = self.requests.get(ref)
            # 标识相同但内容不同的隔离拒绝不得覆盖既有获批记录。
            if et == "REQUEST_DENIED" and rec is not None and rec.plan:
                if receipt:
                    self._receipts.setdefault(receipt, {
                        "kind": "clearance", "approved": False, "request_ref": ref,
                        "event_id": e["event_id"], "reason_code": p.get("reason_code")})
                return
            if rec is None:
                rec = RequestRecord(request_ref=ref)
                self.requests[ref] = rec
            if receipt:
                rec.receipt = receipt
            rec.team_ref = p.get("team_ref", rec.team_ref)
            rec.category = p.get("category", rec.category)
            rec.headcount = int(p.get("headcount", rec.headcount or 0))
            if p.get("fingerprint"):
                rec.fingerprint = dict(p["fingerprint"])
            if p.get("basis"):
                rec.basis = dict(p["basis"])
            if et == "SCHEDULE_FROZEN":
                rec.plan = dict(p.get("plan", {}))
                rec.emergency = bool(p.get("emergency", False))
                rec.ratified = not rec.emergency
                rec.denied_reason = None
                due = p.get("review_due_at")
                rec.review_due_at = datetime.fromisoformat(due) if due else None
                rec.supersedes = list(p.get("supersedes", ()))
            else:
                rec.denied_reason = p.get("reason_code")
            if receipt:
                self._receipts[receipt] = {
                    "kind": "clearance", "approved": et == "SCHEDULE_FROZEN",
                    "request_ref": ref, "event_id": e["event_id"],
                    "reason_code": p.get("reason_code"),
                }
        elif et == "CAPACITY_HELD":
            self.ledger.add_hold(
                p["request_ref"], p["resource_ref"], int(p["quantity"]),
                datetime.fromisoformat(p["window_start"]),
                datetime.fromisoformat(p["window_end"]),
                bool(p.get("emergency", False)),
                datetime.fromisoformat(p["review_due_at"]) if p.get("review_due_at") else None,
                datetime.fromisoformat(p["depart_grace_until"]) if p.get("depart_grace_until") else None,
            )
        elif et == "EMERGENCY_RATIFIED":
            self.ledger.ratify(p["request_ref"])
            rec = self.requests.get(p["request_ref"])
            if rec is not None:
                rec.ratified = True
            if p.get("receipt"):
                self._receipts.setdefault(p["receipt"],
                    {"kind": "ratify", "request_ref": p["request_ref"], "event_id": e["event_id"]})
        elif et == "DISPATCH_CONFIRMED":
            for h in self.ledger.holds_for(p["request_ref"]):
                object.__setattr__(h, "depart_grace_until", None)
            rec = self.requests.get(p["request_ref"])
            if rec is not None:
                rec.dispatched = True
            if p.get("receipt"):
                self._receipts.setdefault(p["receipt"],
                    {"kind": "dispatch", "request_ref": p["request_ref"], "event_id": e["event_id"]})
        elif et == "CAPACITY_RELEASED":
            self.ledger.release_one(p["request_ref"], p["resource_ref"])
            rec = self.requests.get(p["request_ref"])
            if rec is not None and p.get("final"):
                rec.released = True
        elif et == "NO_SHOW_RELEASED":
            rec = self.requests.get(p["request_ref"])
            if rec is not None:
                rec.released = True
        elif et == "ESCALATION_RAISED":
            rec = self.requests.get(p["request_ref"])
            if rec is not None:
                rec.escalated = True
                rec.ratified = True   # 终止未决状态，资源已释放
        elif et == "ARRIVAL_CONFIRMED":
            rec = self.requests.get(p["request_ref"])
            if rec is not None:
                rec.arrived = True
                rec.released = True
            if p.get("receipt"):
                self._receipts.setdefault(p["receipt"],
                    {"kind": "arrival", "request_ref": p["request_ref"], "event_id": e["event_id"]})

    def _replay(self) -> None:
        for e in self.store.all():
            self._apply(e)

    # ------------------------------------------------------------------ 事实

    def record_fact(self, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        receipt = _require(body, "receipt")
        if receipt in self._receipts:
            return {"idempotent": True, **self._receipts[receipt]}
        fact_type = _require(body, "fact_type")
        owner = _require(body, "owner_role")
        ref = _require(body, "ref")
        expected_owner = FACT_OWNERS.get(fact_type)
        if expected_owner is None:
            raise ServiceError("unknown_fact_type", f"未登记的事实类型 {fact_type}")
        if owner != expected_owner:
            raise ServiceError("owner_mismatch",
                               f"{fact_type} 事实只能由 {expected_owner} 确认，收到 {owner}", 403)
        version = body.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ServiceError("version_invalid", "事实版本必须是正整数")
        data = body.get("data")
        if not isinstance(data, dict):
            raise ServiceError("data_invalid", "事实数据必须是对象")
        current = self.facts.get(fact_type, ref)
        if current is not None and version <= current.version:
            raise ServiceError("stale_fact",
                               f"事实 {fact_type}:{ref} 版本 {version} 已过期，当前 {current.version}", 409)
        payload = {"receipt": receipt, "fact_type": fact_type, "owner_role": owner,
                   "ref": ref, "version": version, "data": data}
        agg_type = "travel_demand" if fact_type in ("fixture", "lodging", "roster") else \
                   "capacity_commitment" if fact_type == "resource" else "route_revision"
        agg_id = f"fact-{fact_type}-{ref}"
        emitted = [self._emit("FACT_RECORDED", agg_type, agg_id, payload,
                              f"fact-{receipt}", now)]
        if fact_type == "incident":
            route_payload = {
                "receipt": receipt,
                "impact_scope": data.get("corridor", ref),
                "effective_at": data.get("effective_from", now.isoformat()),
                "incident_ref": ref, "reason": data.get("reason", "事故影响"),
            }
            emitted.append(self._emit("ROUTE_REVISED", "route_revision", f"incident-{ref}",
                                      route_payload, f"route-{receipt}", now))
        return {"idempotent": False, "event_id": emitted[0]["event_id"], "recorded": len(emitted)}

    # ------------------------------------------------------------------ 放行

    def submit_request(self, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        receipt = _require(body, "receipt")
        prior = self._receipts.get(receipt)
        if prior is not None:
            # 重复回执：原样返回既有结论，不二次扣减任何资源。
            return {"idempotent": True, **prior}
        ref = _require(body, "request_ref")
        team_ref = _require(body, "team_ref")
        venue = _require(body, "venue")
        corridor = _require(body, "corridor")
        category = _require(body, "category")
        headcount = _nonneg_int(body, "headcount")
        accessible_seats = _nonneg_int(body, "accessible_seats")
        deadline = _parse_dt(_require(body, "deadline"))
        emergency = bool(body.get("emergency", False))
        supersedes = list(body.get("supersedes", ()))

        fingerprint = {"venue": venue, "corridor": corridor, "category": category,
                       "headcount": headcount, "accessible_seats": accessible_seats,
                       "deadline": deadline.isoformat()}
        existing = self.requests.get(ref)
        if existing is not None and existing.fingerprint and existing.fingerprint != fingerprint:
            # 标识相同但路线/人数/时间不同：隔离，拒绝并保留原请求与既有占用。
            self._emit("REQUEST_DENIED", "travel_demand", ref, {
                "receipt": receipt, "request_ref": ref, "team_ref": team_ref,
                "category": category, "headcount": headcount,
                "fingerprint": fingerprint, "basis": self._basis(),
                "reason_code": "identity_conflict",
                "message": "相同申请标识对应不同路线或人数，已隔离",
            }, f"deny-{receipt}", now)
            return {"idempotent": False, "approved": False, "request_ref": ref,
                    "reason_code": "identity_conflict"}
        if existing is not None and existing.plan and not existing.denied_reason:
            # 同一申请、同一指纹已有结论：回放原结论，绝不二次扣减。
            return {"idempotent": True, "approved": True, "request_ref": ref,
                    "mode": existing.plan.get("mode"), "plan": existing.plan,
                    "basis": existing.basis, "status": self._status(existing),
                    "original_receipt": existing.receipt or None}

        plan = Planner(self.facts).plan(
            venue=venue, corridor=corridor, category=category, headcount=headcount,
            accessible_seats=accessible_seats, deadline=deadline, now=now)
        basis = self._basis()

        if plan.mode == "denied":
            self._emit("REQUEST_DENIED", "travel_demand", ref, {
                "receipt": receipt, "request_ref": ref, "team_ref": team_ref,
                "category": category, "headcount": headcount,
                "fingerprint": fingerprint, "basis": basis,
                "reason_code": plan.reason_code, "evidence": plan.evidence,
            }, f"deny-{receipt}", now)
            return {"idempotent": False, "approved": False, "request_ref": ref,
                    "reason_code": plan.reason_code, "basis": basis, "evidence": plan.evidence}

        window_start = plan.pickup_at
        window_end = plan.arrives_at + ARRIVAL_BUFFER
        assert window_start is not None

        if plan.mode == "shuttle":
            blocking = None if emergency else self.ledger.can_hold(plan.resources, window_start, window_end, ref)
            if blocking is not None:
                self._emit("REQUEST_DENIED", "travel_demand", ref, {
                    "receipt": receipt, "request_ref": ref, "team_ref": team_ref,
                    "category": category, "headcount": headcount,
                    "fingerprint": fingerprint, "basis": basis,
                    "reason_code": "overcapacity", "blocking_resource": blocking,
                    "evidence": plan.evidence,
                }, f"deny-{receipt}", now)
                return {"idempotent": False, "approved": False, "request_ref": ref,
                        "reason_code": "overcapacity", "blocking_resource": blocking, "basis": basis}

        review_due = (now + RATIFICATION_WINDOW) if emergency else None
        freeze_payload = {
            "receipt": receipt, "request_ref": ref, "team_ref": team_ref,
            "venue": venue, "category": category, "headcount": headcount,
            "accessible_seats": accessible_seats, "fingerprint": fingerprint,
            "basis": basis, "plan": self._plan_json(plan),
            "emergency": emergency, "supersedes": supersedes,
        }
        if review_due is not None:
            freeze_payload["review_due_at"] = review_due.isoformat()
        self._emit("SCHEDULE_FROZEN", "travel_demand", ref, freeze_payload,
                   f"freeze-{receipt}", now)

        if emergency:
            # 紧急改线：先占用最低必要资源，批准截止时间已锁定。
            self._emit("EMERGENCY_RELEASED", "capacity_commitment", ref, {
                "request_ref": ref, "receipt": receipt,
                "review_due_at": review_due.isoformat(),  # type: ignore[union-attr]
                "reason": str(body.get("reason", "紧急改线")),
            }, f"emrel-{receipt}", now)

        for old_ref in supersedes:
            self._release_request(old_ref, now, cause=f"superseded-by-{ref}", final=True)

        held: list[dict[str, Any]] = []
        if plan.mode == "shuttle":
            grace = window_start + DEPART_GRACE
            for resource_ref, quantity in plan.resources.items():
                if quantity <= 0:
                    continue
                payload = {
                    "request_ref": ref, "receipt": receipt,
                    "resource_ref": resource_ref, "quantity": quantity,
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "emergency": emergency,
                }
                if emergency:
                    payload["review_due_at"] = review_due.isoformat()  # type: ignore[union-attr]
                    payload["reason"] = str(body.get("reason", "紧急改线"))
                payload["depart_grace_until"] = grace.isoformat()
                self._emit("CAPACITY_HELD", "capacity_commitment", ref, payload,
                           f"hold-{receipt}-{resource_ref.replace(':', '-')}", now)
                held.append({"resource_ref": resource_ref, "quantity": quantity})

        return {
            "idempotent": False, "approved": True, "request_ref": ref,
            "mode": plan.mode, "plan": self._plan_json(plan), "basis": basis,
            "held": held, "emergency": emergency,
            "review_due_at": review_due.isoformat() if review_due else None,
        }

    def ratify(self, ref: str, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        receipt = _require(body, "receipt")
        if receipt in self._receipts:
            return {"idempotent": True, **self._receipts[receipt]}
        rec = self._require_request(ref)
        if not rec.emergency:
            raise ServiceError("not_emergency", "该申请不是紧急改线，无需补批")
        if rec.escalated:
            raise ServiceError("ratification_window_closed",
                               "补批截止已过，紧急占用已升级并释放", 409)
        if rec.ratified:
            raise ServiceError("already_ratified", "紧急占用已完成批准", 409)
        approver = _require(body, "approver")
        self._emit("EMERGENCY_RATIFIED", "capacity_commitment", ref, {
            "receipt": receipt, "request_ref": ref, "approver": approver,
            "ratified_at": now.isoformat(),
        }, f"ratify-{receipt}", now)
        return {"idempotent": False, "ratified": True, "request_ref": ref}

    def confirm_dispatch(self, ref: str, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        receipt = _require(body, "receipt")
        if receipt in self._receipts:
            return {"idempotent": True, **self._receipts[receipt]}
        rec = self._require_request(ref)
        if rec.denied_reason or rec.released:
            raise ServiceError("request_not_active", "申请未获批或已释放，不能发车", 409)
        if rec.emergency and not rec.ratified:
            raise ServiceError("ratification_required", "紧急改线尚未补齐批准，不能发车", 409)
        departed_at = _parse_dt(body.get("departed_at", now.isoformat()))
        self._emit("DISPATCH_CONFIRMED", "capacity_commitment", ref, {
            "receipt": receipt, "request_ref": ref, "departed_at": departed_at.isoformat(),
        }, f"dispatch-{receipt}", now)
        return {"idempotent": False, "dispatched": True, "request_ref": ref}

    def confirm_arrival(self, ref: str, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        receipt = _require(body, "receipt")
        if receipt in self._receipts:
            return {"idempotent": True, **self._receipts[receipt]}
        rec = self._require_request(ref)
        if not rec.dispatched:
            raise ServiceError("not_dispatched", "尚未确认发车，不能登记到场")
        arrived_at = _parse_dt(body.get("arrived_at", now.isoformat()))
        self._emit("ARRIVAL_CONFIRMED", "arrival_case", ref, {
            "receipt": receipt, "request_ref": ref, "arrived_at": arrived_at.isoformat(),
            "planned_arrives_at": rec.plan.get("arrives_at"),
        }, f"arrive-{receipt}", now)
        self._release_request(ref, now, cause="arrival", final=True)
        return {"idempotent": False, "arrived": True, "request_ref": ref,
                "arrived_at": arrived_at.isoformat()}

    # --------------------------------------------------------- 时钟驱动的处置

    def advance(self, now: datetime) -> dict[str, Any]:
        """时钟推进后处理到期事项：未补齐批准的紧急占用升级，失约车辆释放。"""
        actions: list[dict[str, Any]] = []
        for hold in list(self.ledger.overdue_emergency(now)):
            key = hold.review_due_at.isoformat().replace(":", "").replace("-", "")
            if f"escal-{hold.request_ref}-{key}" not in self.store.seen_receipts():
                ref = hold.request_ref
                self._emit("ESCALATION_RAISED", "capacity_commitment", ref, {
                    "request_ref": ref, "reason_code": "emergency_not_ratified_in_time",
                    "review_due_at": hold.review_due_at.isoformat(),  # type: ignore[union-attr]
                    "escalated_at": now.isoformat(),
                }, f"escal-{ref}-{key}", now)
                self._release_request(ref, now, cause="escalation", final=True)
                actions.append({"request_ref": ref, "action": "escalated_and_released"})
        for hold in list(self.ledger.missed_departures(now)):
            key = hold.depart_grace_until.isoformat().replace(":", "").replace("-", "")
            eid = f"noshow-{hold.request_ref}-{key}"
            if eid not in self.store.seen_receipts():
                ref = hold.request_ref
                rec = self.requests.get(ref)
                if rec is not None and rec.dispatched:
                    continue
                self._emit("NO_SHOW_RELEASED", "capacity_commitment", ref, {
                    "request_ref": ref, "released_at": now.isoformat(),
                    "grace_until": hold.depart_grace_until.isoformat(),  # type: ignore[union-attr]
                }, eid, now)
                self._release_request(ref, now, cause="no_show", final=False)
                actions.append({"request_ref": ref, "action": "no_show_released"})
        return {"now": now.isoformat(), "actions": actions}

    def _release_request(self, ref: str, now: datetime, *, cause: str, final: bool) -> None:
        for hold in self.ledger.holds_for(ref):
            if hold.released:
                continue
            eid = f"rel-{cause}-{ref}-{hold.resource_ref.replace(':', '-')}-{hold.window_start.isoformat().replace(':', '').replace('-', '')}"
            if eid in self.store.seen_receipts():
                self.ledger.release_one(ref, hold.resource_ref)
                continue
            self._emit("CAPACITY_RELEASED", "capacity_commitment", ref, {
                "request_ref": ref, "resource_ref": hold.resource_ref,
                "quantity": hold.quantity, "cause": cause, "final": final,
                "released_at": now.isoformat(),
            }, eid, now)

    # ------------------------------------------------------------------ 投影

    def itinerary(self, ref: str, role: str) -> dict[str, Any]:
        """按角色返回可执行而不过度披露的行程。"""
        rec = self.requests.get(ref)
        if rec is None:
            raise ServiceError("not_found", f"申请 {ref} 不存在", 404)
        plan = rec.plan
        base: dict[str, Any] = {"request_ref": ref, "status": self._status(rec)}
        if role == "team":
            base.update({
                "team_ref": rec.team_ref, "mode": plan.get("mode"),
                "corridor": plan.get("corridor"),
                "pickup_at": plan.get("pickup_at"), "arrives_at": plan.get("arrives_at"),
                "screening_ref": plan.get("screening_ref"),
                "accessible": plan.get("accessible"),
                "emergency": rec.emergency, "ratified": rec.ratified,
                "basis": rec.basis, "reason_code": rec.denied_reason,
            })
        elif role == "transport":
            if plan.get("mode") == "shuttle":
                base.update({
                    "corridor": plan.get("corridor"),
                    "pickup_at": plan.get("pickup_at"), "arrives_at": plan.get("arrives_at"),
                    "headcount": rec.headcount,
                    "resources": plan.get("resources"),
                    "review_due_at": rec.review_due_at.isoformat() if rec.review_due_at else None,
                    "emergency": rec.emergency, "ratified": rec.ratified,
                })
            else:
                base.update({"mode": plan.get("mode"), "note": "该行程不占用接驳运力"})
        elif role == "security":
            base.update({
                "screening_ref": plan.get("screening_ref"),
                "arrives_at": plan.get("arrives_at"),
                "category": rec.category, "headcount": rec.headcount,
                "corridor": plan.get("corridor"),
            })
        elif role == "media":
            if rec.category != "media":
                raise ServiceError("forbidden", "媒体只能查看本类别的通行指引", 403)
            base.update({
                "mode": plan.get("mode"), "corridor": plan.get("corridor"),
                "arrives_at": plan.get("arrives_at"),
                "screening_ref": plan.get("screening_ref"),
                "reason_code": rec.denied_reason,
            })
        elif role == "operator":
            # 赛事运行中心需要完整依据；其他角色拿不到这些字段。
            base.update({
                "team_ref": rec.team_ref, "category": rec.category,
                "headcount": rec.headcount, "plan": plan, "basis": rec.basis,
                "emergency": rec.emergency, "ratified": rec.ratified,
                "review_due_at": rec.review_due_at.isoformat() if rec.review_due_at else None,
                "supersedes": rec.supersedes, "reason_code": rec.denied_reason,
            })
        else:
            raise ServiceError("forbidden", f"角色 {role} 无行程查看权限", 403)
        return base

    def list_requests(self, role: str, team_ref: str | None = None) -> list[dict[str, Any]]:
        out = []
        for ref, rec in sorted(self.requests.items()):
            if role == "media" and rec.category != "media":
                continue
            if team_ref and rec.team_ref != team_ref:
                continue
            out.append({"request_ref": ref, "team_ref": rec.team_ref,
                        "category": rec.category, "status": self._status(rec),
                        "emergency": rec.emergency})
        return out

    @staticmethod
    def _status(rec: RequestRecord) -> str:
        if rec.arrived:
            return "arrived"
        if rec.escalated:
            return "escalated"
        if rec.released:
            return "released"
        if rec.denied_reason:
            return f"denied:{rec.denied_reason}"
        if rec.dispatched:
            return "dispatched"
        if rec.emergency and not rec.ratified:
            return "held_emergency_pending_ratification"
        return "held"

    def _require_request(self, ref: str) -> RequestRecord:
        rec = self.requests.get(ref)
        if rec is None:
            raise ServiceError("not_found", f"申请 {ref} 不存在", 404)
        return rec

    def _basis(self) -> dict[str, int]:
        return dict(self.facts.domain_version)

    @staticmethod
    def _plan_json(plan: Any) -> dict[str, Any]:
        return {
            "mode": plan.mode, "corridor": plan.corridor,
            "pickup_at": plan.pickup_at.isoformat() if plan.pickup_at else None,
            "arrives_at": plan.arrives_at.isoformat() if plan.arrives_at else None,
            "screening_ref": plan.screening_ref, "resources": dict(plan.resources),
            "accessible": plan.accessible, "evidence": list(plan.evidence),
        }


def _require(body: Mapping[str, Any], key: str) -> Any:
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ServiceError("missing_field", f"缺少必填字段 {key}")
    return value


def _nonneg_int(body: Mapping[str, Any], key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ServiceError("invalid_field", f"{key} 必须是非负整数")
    return value


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise ServiceError("invalid_datetime", f"时间 {value} 无法解析")
    if dt.tzinfo is None:
        raise ServiceError("timezone_required", "时间必须携带时区")
    return dt
