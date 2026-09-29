"""延误复盘：从事件日志重建「事实变化 → 资源调整 → 最终到场」的完整依据。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .journal import EventStore

_EVENT_LABELS = {
    "FACT_RECORDED": "事实上报",
    "SCHEDULE_FROZEN": "放行锁定",
    "ROUTE_REVISED": "路网修订",
    "CAPACITY_HELD": "运力占用",
    "CAPACITY_RELEASED": "运力释放",
    "REQUEST_DENIED": "申请拒绝",
    "EMERGENCY_RELEASED": "紧急临时占用",
    "EMERGENCY_RATIFIED": "紧急补批",
    "DISPATCH_CONFIRMED": "发车确认",
    "NO_SHOW_RELEASED": "失约释放",
    "ESCALATION_RAISED": "超时升级",
    "ARRIVAL_CONFIRMED": "到场确认",
}

_DOMAIN_LABELS = {
    "fixture": "赛程", "lodging": "驻地", "roster": "名册", "leg": "走廊车程",
    "transit": "公共交通", "resource": "运力池", "screening": "安检窗口", "incident": "事故影响",
}


class Replay:
    def __init__(self, store: EventStore) -> None:
        # 保持日志追加顺序：它就是当时的决策与处置顺序。
        self.events = list(store.all())

    def fact_changes(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for e in self.events:
            if e["event_type"] != "FACT_RECORDED":
                continue
            p = e["payload"]
            if p["fact_type"] == "resource":
                continue
            rows.append({
                "at": e["occurred_at"], "fact_type": p["fact_type"],
                "label": _DOMAIN_LABELS.get(p["fact_type"], p["fact_type"]),
                "ref": p["ref"], "version": p["version"], "owner_role": p["owner_role"],
                "data": p.get("data", {}),
            })
        latest: dict[tuple[str, str], int] = {}
        for row in rows:
            latest[(row["fact_type"], row["ref"])] = row["version"]
        for row in rows:
            row["current"] = latest[(row["fact_type"], row["ref"])] == row["version"]
        return rows

    def request_decisions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in self.events:
            if e["event_type"] not in ("SCHEDULE_FROZEN", "REQUEST_DENIED"):
                continue
            p = e["payload"]
            out.append({
                "at": e["occurred_at"], "event_type": e["event_type"],
                "request_ref": p["request_ref"], "approved": e["event_type"] == "SCHEDULE_FROZEN",
                "basis": p.get("basis"), "plan": p.get("plan"),
                "reason_code": p.get("reason_code"),
                "emergency": p.get("emergency", False),
                "supersedes": p.get("supersedes", []),
            })
        return out

    def capacity_movements(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in self.events:
            if e["event_type"] in ("CAPACITY_HELD", "CAPACITY_RELEASED"):
                p = e["payload"]
                out.append({"at": e["occurred_at"], "event_type": e["event_type"],
                            "request_ref": p["request_ref"], **p})
        return out

    def emergency_track(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in self.events:
            if e["event_type"] in ("EMERGENCY_RELEASED", "EMERGENCY_RATIFIED",
                                   "ESCALATION_RAISED", "NO_SHOW_RELEASED",
                                   "DISPATCH_CONFIRMED", "ARRIVAL_CONFIRMED"):
                out.append({"at": e["occurred_at"], "event_type": e["event_type"],
                            "payload": e["payload"]})
        return out

    def arrival(self, request_ref: str) -> dict[str, Any] | None:
        freeze = next((e for e in self.events
                       if e["event_type"] == "SCHEDULE_FROZEN"
                       and e["payload"]["request_ref"] == request_ref), None)
        arrived = next((e for e in reversed(self.events)
                        if e["event_type"] == "ARRIVAL_CONFIRMED"
                        and e["payload"]["request_ref"] == request_ref), None)
        if freeze is None or arrived is None:
            return None
        planned = datetime.fromisoformat(freeze["payload"]["plan"]["arrives_at"])
        actual = datetime.fromisoformat(arrived["payload"]["arrived_at"])
        delta = int((actual - planned).total_seconds() // 60)
        return {
            "request_ref": request_ref,
            "basis_at_clearance": freeze["payload"]["basis"],
            "planned_arrives_at": planned.isoformat(),
            "actual_arrives_at": actual.isoformat(),
            "delay_minutes": delta,
            "evidence": freeze["payload"]["plan"].get("evidence", []),
            "clearance_event_id": freeze["event_id"],
            "arrival_event_id": arrived["event_id"],
        }

    def timeline(self) -> list[dict[str, str]]:
        return [{"at": e["occurred_at"], "event_type": e["event_type"],
                 "label": _EVENT_LABELS.get(e["event_type"], e["event_type"]),
                 "aggregate_id": e["aggregate_id"], "event_id": e["event_id"]}
                for e in self.events]


def render_text(rep: Replay, request_ref: str | None = None) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("分散赛区通行保障复盘报告")
    lines.append("=" * 72)

    lines.append("\n【一】事实变化（只列各责任方确认的事实版本）")
    for row in rep.fact_changes():
        mark = "现行" if row["current"] else "已被取代"
        summary = _summarize_fact(row["fact_type"], row["data"])
        lines.append(f"  {row['at'][11:16]} {row['label']:<6} {row['ref']:<20} "
                     f"v{row['version']} [{mark}] {{{row['owner_role']}}} {summary}")

    lines.append("\n【二】放行决策（每次放行锁定当时赛程/路网版本）")
    for d in rep.request_decisions():
        if request_ref and d["request_ref"] != request_ref:
            continue
        if d["approved"]:
            plan = d["plan"] or {}
            lines.append(f"  {d['at'][11:16]} 批准 {d['request_ref']} 方式={plan.get('mode')} "
                         f"发车={plan.get('pickup_at')} 到场={plan.get('arrives_at')} "
                         f"安检={plan.get('screening_ref')} 版本锁={d['basis']}"
                         + ("（紧急，待补批）" if d["emergency"] else ""))
            if d["supersedes"]:
                lines.append(f"        取代并释放旧申请：{', '.join(d['supersedes'])}")
        else:
            lines.append(f"  {d['at'][11:16]} 拒绝 {d['request_ref']} 原因={d['reason_code']} "
                         f"版本锁={d['basis']}")

    lines.append("\n【三】资源调整（车辆/司机/无障碍席位的占用与释放）")
    for m in rep.capacity_movements():
        if request_ref and m["request_ref"] != request_ref:
            continue
        verb = "占用" if m["event_type"] == "CAPACITY_HELD" else "释放"
        extra = ""
        if m["event_type"] == "CAPACITY_HELD":
            extra = (f" 窗 {m['window_start'][11:16]}-{m['window_end'][11:16]}"
                     + ("（紧急未批）" if m.get("emergency") else ""))
        else:
            extra = f" 原因={m.get('cause')}"
        lines.append(f"  {m['at'][11:16]} {verb} {m['resource_ref']} x{m['quantity']} "
                     f"<- {m['request_ref']}{extra}")

    lines.append("\n【四】紧急改线与时钟处置")
    for m in rep.emergency_track():
        p = m["payload"]
        ref = p.get("request_ref", "")
        if request_ref and ref != request_ref and ref:
            continue
        detail = ""
        if m["event_type"] == "EMERGENCY_RELEASED":
            detail = f"补批截止 {p['review_due_at'][11:16]}；{p['reason']}"
        elif m["event_type"] == "EMERGENCY_RATIFIED":
            detail = f"批准人 {p['approver']}"
        elif m["event_type"] == "ESCALATION_RAISED":
            detail = f"{p['reason_code']} @ {p['review_due_at'][11:16]}"
        elif m["event_type"] == "NO_SHOW_RELEASED":
            detail = f"宽限至 {p['grace_until'][11:16]} 仍未发车"
        elif m["event_type"] == "DISPATCH_CONFIRMED":
            detail = f"发车 {p['departed_at'][11:16]}"
        elif m["event_type"] == "ARRIVAL_CONFIRMED":
            detail = f"到场 {p['arrived_at'][11:16]}"
        lines.append(f"  {m['at'][11:16]} {_EVENT_LABELS[m['event_type']]:<8} {ref} {detail}".rstrip())

    lines.append("\n【五】最终到场依据")
    targets = [request_ref] if request_ref else sorted({
        e["payload"]["request_ref"] for e in rep.events
        if e["event_type"] == "ARRIVAL_CONFIRMED"})
    for ref in targets:
        a = rep.arrival(ref)
        if a is None:
            lines.append(f"  {ref}: 无到场记录")
            continue
        lines.append(f"  {ref}")
        lines.append(f"    放行事件 {a['clearance_event_id']} → 到场事件 {a['arrival_event_id']}")
        lines.append(f"    锁定版本 {a['basis_at_clearance']}；计划到场 {a['planned_arrives_at'][11:16]}；"
                     f"实际到场 {a['actual_arrives_at'][11:16]}；偏差 {a['delay_minutes']:+d} 分钟")
        lines.append("    规划依据链：")
        for item in a["evidence"]:
            lines.append(f"      - {item}")

    lines.append("\n【附】完整事件时间线")
    for item in rep.timeline():
        lines.append(f"  {item['at'][11:16]} {item['label']:<8} {item['aggregate_id']}  ({item['event_id']})")
    lines.append("=" * 72)
    return "\n".join(lines)


def _summarize_fact(fact_type: str, data: dict[str, Any]) -> str:
    if fact_type == "lodging":
        return f"驻地→{data.get('hotel')}（走廊 {data.get('corridor')}）"
    if fact_type == "fixture":
        return f"到场截止 {str(data.get('deadline'))[11:16]} 开赛 {str(data.get('kickoff'))[11:16]}"
    if fact_type == "transit":
        return f"状态={data.get('status', 'running')} 到 {str(data.get('arrives_at'))[11:16]}"
    if fact_type == "screening":
        return (f"窗口 {str(data.get('open_from'))[11:16]}-{str(data.get('open_to'))[11:16]} "
                f"准入={','.join(data.get('categories_allowed', []))}"
                + ("（运动员专用）" if data.get("athlete_only") else ""))
    if fact_type == "incident":
        return f"{data.get('reason')} +{data.get('delay_minutes')}min"
    if fact_type == "roster":
        return f"{data.get('headcount')} 人，无障碍席位 {data.get('accessible_seats')}"
    if fact_type == "leg":
        return f"常规车程 {data.get('travel_minutes')} 分钟"
    return ""
