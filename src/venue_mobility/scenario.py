"""复盘场景运行器：按脚本驱动可控时钟与服务，输出完整决策依据。

场景脚本为 JSON：{"title", "start", "steps": [...]}，步骤在全新或既有事件日志上
逐条执行。运行结果既可打印为中文复盘，也可导出为 JSON。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .clock import ControllableClock, parse_dt
from .errors import DomainRejection
from .service import MobilityService
from .store import EventStore


@dataclass
class StepRecord:
    index: int
    op: str
    narrative: str
    events: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ScenarioRunner:
    def __init__(self, store: EventStore, start: str) -> None:
        self.clock = ControllableClock(start)
        self.service = MobilityService(store, self.clock)
        self.alias: dict[str, str] = {}  # 业务别名 -> demand_id
        self.records: list[StepRecord] = []

    # ------------------------------------------------------------------ 执行

    def run(self, scenario: dict[str, Any]) -> list[StepRecord]:
        for index, step in enumerate(scenario.get("steps", []), start=1):
            record = StepRecord(index=index, op=step["op"], narrative="")
            try:
                self._dispatch(step, record)
            except DomainRejection as exc:
                record.error = f"{exc.code}: {exc.message}"
                record.narrative = f"步骤被拒绝：{exc.message}"
            self.records.append(record)
        return self.records

    def _dispatch(self, step: dict[str, Any], record: StepRecord) -> None:
        op = step["op"]
        if op == "fact":
            event = self.service.register_fact(
                step["kind"], step["key"], step["snapshot"],
                version=step.get("version"))
            record.events.append(event["event_id"])
            version = step["snapshot"].get("note") or ""
            record.narrative = (
                f"登记事实 {step['kind']}/{step['key']} 版本 "
                f"{event['payload']['fact_version']}。{version}")
            record.data = {"fact_kind": step["kind"], "fact_key": step["key"],
                           "fact_version": event["payload"]["fact_version"]}
        elif op == "submit":
            state, events, isolated = self.service.submit_demand(step["body"])
            self.alias[step["id"]] = state.demand_id
            record.events.extend(e["event_id"] for e in events)
            note = "（与同名申请路线/人数不同，已隔离）" if isolated else ""
            record.narrative = (
                f"收到 {step['body']['requester']} 的申请 {state.demand_id}{note}："
                f"{step['body']['category']} {step['body']['headcount']} 人"
                f"（无障碍席位 {step['body']['accessibility_seats']}），"
                f"{step['body']['origin_ref']} -> {step['body']['venue_ref']}，"
                f"须于 {step['body']['needed_at']} 到场。")
            record.data = {"demand_id": state.demand_id, "isolated": isolated,
                           "receipt_key": state.receipt_key}
        elif op == "confirm":
            demand_id = self.alias[step["ref"]]
            state, event = self.service.confirm_fact(
                demand_id, step["role"], step["key"], step["claims"])
            if event is not None:
                record.events.append(event["event_id"])
            record.narrative = (
                f"{self._role_cn(step['role'])}就 {demand_id} 确认其掌握的事实 "
                f"{sorted(step['claims'])}（确认键 {step['key']}，幂等重放不重复扣减）。")
        elif op == "freeze":
            state, event = self.service.freeze_schedule(self.alias[step["ref"]])
            record.events.append(event["event_id"])
            record.narrative = (
                f"三方确认齐备，锁定赛程版本 {state.schedule_version}、"
                f"路网版本 {state.road_version}。")
        elif op == "clear":
            state, events = self.service.issue_clearance(self.alias[step["ref"]])
            record.events.extend(e["event_id"] for e in events)
            itin = state.clearance["itinerary"]
            mode_cn = "公共交通" if itin["mode"] == "public_transit" else "接驳车"
            res = "，".join(f"{r['resource_ref']}×{r['quantity']}"
                            for r in state.clearance["resources"]) or "无需占用接驳运力"
            record.narrative = (
                f"放行 {state.demand_id}：采用{mode_cn}，{itin.get('transit_service') or itin.get('route_ref')}，"
                f"{itin['depart_at']} 发车、{itin['arrive_at']} 抵达安检点 "
                f"{itin['gate_ref']}；占用 {res}。")
            record.data = {"clearance": state.clearance}
        elif op == "revise":
            revision, events = self.service.emergency_revise(
                self.alias[step["ref"]], step["reason"])
            record.events.extend(e["event_id"] for e in events)
            extra = "，".join(f"{r}×{q}" for r, q in revision.extra_items) or "无新增运力"
            record.narrative = (
                f"紧急改线 {revision.revision_id}：{step['reason']}；先占用最低必要资源（{extra}），"
                f"须在 {revision.review_due_at} 前补齐批准。")
            record.data = {"revision_id": revision.revision_id,
                           "review_due_at": revision.review_due_at}
        elif op == "ratify":
            demand_id = self.alias[step["ref"]]
            state0 = self.service.demands[demand_id]
            revision_id = step.get("revision_id") or state0.revision.revision_id
            state, events = self.service.ratify_revision(revision_id)
            record.events.extend(e["event_id"] for e in events)
            itin = state.clearance["itinerary"]
            res = "，".join(f"{r['resource_ref']}×{r['quantity']}"
                            for r in state.clearance["resources"]) or "无接驳运力"
            record.narrative = (
                f"运行中心在限期内批准 {revision_id}：行程切换到赛程 "
                f"{state.schedule_version}/路网 {state.road_version}，"
                f"{itin['depart_at']} 发车；正式占用 {res}，临时占用与冗余运力已结清。")
        elif op == "tick":
            minutes = step.get("minutes", 0)
            fired = self.service.tick(minutes=minutes)
            record.events.extend(e["event_id"] for e in fired)
            record.narrative = f"可控时钟推进 {minutes} 分钟至 {self.clock.iso()}。"
            if fired:
                kinds = sorted({e["event_type"] for e in fired})
                record.narrative += f" 到期自动处置：{kinds}。"
        elif op == "depart":
            event = self.service.depart(self.alias[step["ref"]])
            record.events.append(event["event_id"])
            record.narrative = f"{self.alias[step['ref']]} 于 {event['payload']['departed_at']} 发车。"
        elif op == "arrive":
            demand_id = self.alias[step["ref"]]
            events = self.service.arrive(demand_id, arrived_at=step.get("arrived_at"))
            record.events.extend(e["event_id"] for e in events)
            arrived = events[0]["payload"]["arrived_at"]
            record.narrative = f"{demand_id} 确认到场：{arrived}，运力归还。"
            record.data = {"arrived_at": arrived}
        elif op == "escalate":
            event = self.service.escalate(
                self.alias[step["ref"]], step["to_role"], step["reason"])
            record.events.append(event["event_id"])
            record.narrative = f"升级至 {step['to_role']}：{step['reason']}。"
        else:
            raise DomainRejection(f"场景脚本含未知步骤 {op}", code="unknown_step")

    @staticmethod
    def _role_cn(role: str) -> str:
        return {"team": "运动队", "transport": "交通方", "security": "安保方"}.get(role, role)

    # ------------------------------------------------------------------ 复盘

    def build_report(self, title: str) -> dict[str, Any]:
        demand_aliases = {demand_id: alias for alias, demand_id in self.alias.items()}
        demands_report = []
        for demand_id in sorted(self.alias.values()):
            state = self.service.demands[demand_id]
            needed = parse_dt(state.request["needed_at"])
            delay_minutes: int | None = None
            if state.arrived_at:
                delay_minutes = int((parse_dt(state.arrived_at) - needed).total_seconds() // 60)
            demands_report.append({
                "alias": demand_aliases.get(demand_id),
                "demand_id": demand_id,
                "status": state.status,
                "needed_at": state.request["needed_at"],
                "arrived_at": state.arrived_at,
                "delay_minutes": delay_minutes,
                "schedule_version": state.schedule_version,
                "road_version": state.road_version,
                "case_id": state.case_id,
                "revision_history": state.revision_history,
                "trace_event_count": len(self.service.trace(demand_id)),
            })
        return {
            "title": title,
            "now": self.clock.iso(),
            "steps": [
                {"index": r.index, "op": r.op, "narrative": r.narrative,
                 "events": r.events, "data": r.data, "error": r.error}
                for r in self.records
            ],
            "demands": demands_report,
            "inventory": self.service.inventory.usage(),
            "facts": self.service.fact_refs(),
            "event_total": len(self.service.store.events),
        }

    def render_text(self, report: dict[str, Any]) -> str:
        lines: list[str] = []
        lines.append(f"复盘：{report['title']}")
        lines.append(f"复盘时钟终点：{report['now']}；领域事件总数：{report['event_total']}")
        lines.append("")
        lines.append("一、决策时间线（事实变化 → 资源调整 → 到场）")
        for step in report["steps"]:
            marker = "✗" if step["error"] else "·"
            lines.append(f"  {step['index']:>2}. {marker} {step['narrative']}")
            if step["error"]:
                lines.append(f"        拒绝原因：{step['error']}")
        lines.append("")
        lines.append("二、行程结果与延误")
        for d in report["demands"]:
            delay = d["delay_minutes"]
            delay_text = "未到场" if delay is None else (
                f"延误 {delay} 分钟到场" if delay > 0 else f"提前/准时 {-delay} 分钟到场")
            lines.append(
                f"  {d['demand_id']}（{d['alias']}）状态={d['status']}，"
                f"锁定版本 赛程{d['schedule_version']}/路网{d['road_version']}，"
                f"要求到场 {d['needed_at']}，实际 {d['arrived_at'] or '—'}，{delay_text}，"
                f"案卷 {d['case_id'] or '—'}，依据事件 {d['trace_event_count']} 条。")
            for rev in d["revision_history"]:
                lines.append(
                    f"      改线 {rev['revision_id']}：{rev['reason']}；"
                    f"生效 {rev['effective_at']}；"
                    + (f"批准 {rev['ratified_at']}，新版本 赛程{rev['schedule_version']}/路网{rev['road_version']}"
                       if rev.get("status") != "expired" and "ratified_at" in rev
                       else f"未在 {rev.get('review_due_at')} 前批准，{rev.get('expired_at')} 到期释放"))
        lines.append("")
        lines.append("三、运力台账（三维独立、原子占用）")
        for row in report["inventory"]:
            lines.append(
                f"  {row['resource_ref']}: 容量 {row['capacity']}，已占用 {row['held']}，"
                f"余量 {row['available']}")
        lines.append("")
        lines.append("四、事实版本台账")
        for fact in report["facts"]:
            lines.append(f"  {fact['fact_kind']}/{fact['fact_key']}：最新版本 {fact['latest_version']}")
        return "\n".join(lines)


def load_scenario(path: Path) -> dict[str, Any]:
    scenario = json.loads(path.read_text(encoding="utf-8"))
    if "steps" not in scenario or not isinstance(scenario["steps"], list):
        raise DomainRejection("场景脚本必须包含 steps 数组", code="bad_scenario")
    return scenario
