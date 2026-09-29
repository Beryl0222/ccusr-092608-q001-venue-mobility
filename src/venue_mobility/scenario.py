"""端到端延误场景：改住处、赛程提前、公交停运、安检收紧、事故、紧急改线到最终到场。"""

from __future__ import annotations

from typing import Any

from .app import Hub


def _t(h: int, m: int = 0) -> str:
    return f"2026-09-26T{h:02d}:{m:02d}:00+08:00"


def build_scenario(hub: Hub) -> list[dict[str, Any]]:
    """在给定 Hub 上按时间轴推进完整案例，返回每一步的可读记录。"""
    svc = hub.clock
    service = hub.service
    trace: list[dict[str, Any]] = []

    def step(title: str, result: Any) -> None:
        trace.append({"at": svc.now.isoformat(), "step": title, "result": result})

    def advance(until: str) -> None:
        svc.advance(until=until)
        outcome = service.advance(svc.now)
        for action in outcome["actions"]:
            step(f"时钟推进自动处置 {action['request_ref']}", action)

    def fact(receipt: str, fact_type: str, ref: str, version: int, owner: str, data: dict[str, Any]) -> None:
        step(f"事实上报 {fact_type}:{ref} v{version}（{owner}）",
             service.record_fact({"receipt": receipt, "fact_type": fact_type, "ref": ref,
                                  "version": version, "owner_role": owner, "data": data}, svc.now))

    # 08:00 初始事实 --------------------------------------------------------
    fact("f-leg-n-1", "leg", "north", 1, "transport",
         {"corridor": "north", "travel_minutes": 35, "bus_seats": 40})
    fact("f-leg-s-1", "leg", "south", 1, "transport",
         {"corridor": "south", "travel_minutes": 45, "bus_seats": 40})
    fact("f-metro-n-1", "transit", "metro-north", 1, "transport",
         {"corridor": "north", "departs_at": _t(11, 20), "arrives_at": _t(12, 30),
          "categories_allowed": ["athlete", "media"], "accessible": True})
    fact("f-metro-s-1", "transit", "metro-south", 1, "transport",
         {"corridor": "south", "departs_at": _t(11, 10), "arrives_at": _t(12, 40),
          "categories_allowed": ["athlete", "media"], "accessible": True})
    for ref, cap in (("vehicle:north", 2), ("driver:north", 2), ("aseat:north", 4),
                     ("vehicle:south", 2), ("driver:south", 2), ("aseat:south", 4)):
        kind, corridor = ref.split(":")
        fact(f"f-res-{corridor}-{kind}", "resource", ref, 1, "transport", {"capacity": cap})
    fact("f-gate-ath-1", "screening", "gate-east-athlete", 1, "security",
         {"venue": "gymnasium-east", "open_from": _t(11, 0), "open_to": _t(14, 0),
          "lead_minutes": 15, "categories_allowed": ["athlete"], "athlete_only": True})
    fact("f-gate-gen-1", "screening", "gate-east-general", 1, "security",
         {"venue": "gymnasium-east", "open_from": _t(11, 0), "open_to": _t(14, 0),
          "lead_minutes": 15, "categories_allowed": ["team_official", "media"]})
    fact("f-gate-med-1", "screening", "gate-east-media", 1, "security",
         {"venue": "gymnasium-east", "open_from": _t(10, 0), "open_to": _t(14, 30),
          "lead_minutes": 15, "categories_allowed": ["media"]})
    fact("f-fixture-1", "fixture", "match-m1", 1, "team",
         {"venue": "gymnasium-east", "deadline": _t(14, 0), "kickoff": _t(14, 30)})
    fact("f-lodge-1", "lodging", "stay-alpha", 1, "team",
         {"team_ref": "team-alpha", "hotel": "north-lodge", "corridor": "north"})
    fact("f-roster-1", "roster", "roster-alpha", 1, "team",
         {"team_ref": "team-alpha", "category": "athlete", "headcount": 38,
          "accessible_seats": 2})

    # 08:10 首次放行：地铁可直达，锁定 schedule/network 版本 ------------------
    advance(_t(8, 10))
    r1 = service.submit_request({
        "receipt": "q-r1", "request_ref": "REQ-ALPHA-1", "team_ref": "team-alpha",
        "venue": "gymnasium-east", "corridor": "north", "category": "athlete",
        "headcount": 38, "accessible_seats": 2, "deadline": _t(14, 0),
    }, svc.now)
    step("放行 REQ-ALPHA-1（原计划：公共交通）", r1)

    # 09:00 事实集中变化 ----------------------------------------------------
    advance(_t(9, 0))
    fact("f-lodge-2", "lodging", "stay-alpha", 2, "team",
         {"team_ref": "team-alpha", "hotel": "south-residence", "corridor": "south"})
    fact("f-fixture-2", "fixture", "match-m1", 2, "team",
         {"venue": "gymnasium-east", "deadline": _t(11, 30), "kickoff": _t(12, 0)})
    fact("f-metro-s-2", "transit", "metro-south", 2, "transport",
         {"corridor": "south", "status": "suspended", "reason": "公共交通提前停运",
          "departs_at": _t(11, 10), "arrives_at": _t(12, 40),
          "categories_allowed": ["athlete", "media"], "accessible": True})
    fact("f-gate-ath-2", "screening", "gate-east-athlete", 2, "security",
         {"venue": "gymnasium-east", "open_from": _t(9, 30), "open_to": _t(11, 20),
          "lead_minutes": 15, "categories_allowed": ["athlete"], "athlete_only": True,
          "change": "安检窗口收紧"})
    fact("f-inc-1", "incident", "incident-south-1", 1, "security",
         {"corridor": "south", "status": "active", "reason": "主干道事故封闭一条车道",
          "effective_from": _t(9, 0), "effective_to": _t(12, 30), "delay_minutes": 25})

    # 09:10 紧急改线：先占最低必要资源，20 分钟内须补批 ---------------------
    advance(_t(9, 10))
    r2 = service.submit_request({
        "receipt": "q-r2", "request_ref": "REQ-ALPHA-2", "team_ref": "team-alpha",
        "venue": "gymnasium-east", "corridor": "south", "category": "athlete",
        "headcount": 38, "accessible_seats": 2, "deadline": _t(11, 30),
        "emergency": True, "reason": "运动员改住南线、赛程提前且南线地铁停运",
        "supersedes": ["REQ-ALPHA-1"],
    }, svc.now)
    step("紧急放行 REQ-ALPHA-2（接驳车，待补批，释放原 REQ-ALPHA-1）", r2)

    # 误用运动员通道的媒体建议：查运动员行程应被拒绝 ------------------------
    media_view = None
    try:
        service.itinerary("REQ-ALPHA-2", "media")
    except Exception as exc:  # noqa: BLE001
        media_view = f"已拦截：{exc}"
    step("媒体尝试查看运动员通道行程", media_view)

    # 媒体自身请求走媒体专用安检点，看不到运力细节 -------------------------
    r3 = service.submit_request({
        "receipt": "q-r3", "request_ref": "REQ-PRESS-1", "team_ref": "press-pool",
        "venue": "gymnasium-east", "corridor": "south", "category": "media",
        "headcount": 6, "accessible_seats": 0, "deadline": _t(12, 0),
    }, svc.now)
    step("放行 REQ-PRESS-1（媒体通道，隔离披露）", r3)

    # 09:12 第三支队伍同样依赖南线接驳车，且时间窗重叠：拒绝超配 -------------
    advance(_t(9, 12))
    r4 = service.submit_request({
        "receipt": "q-r4", "request_ref": "REQ-BETA-1", "team_ref": "team-beta",
        "venue": "gymnasium-east", "corridor": "south", "category": "athlete",
        "headcount": 30, "accessible_seats": 1, "deadline": _t(11, 30),
    }, svc.now)
    step("放行 REQ-BETA-1（南线第三辆接驳车，时间窗重叠）", r4)

    # 09:20 交通值班经理补齐批准 ------------------------------------------
    advance(_t(9, 20))
    step("紧急占用补批 REQ-ALPHA-2",
         service.ratify("REQ-ALPHA-2", {"receipt": "a-r2", "approver": "transport-duty-manager"}, svc.now))

    # 09:55 准点发车 ------------------------------------------------------
    advance(_t(9, 55))
    step("发车确认 REQ-ALPHA-2",
         service.confirm_dispatch("REQ-ALPHA-2", {"receipt": "d-r2", "departed_at": _t(9, 55)}, svc.now))

    # 11:12 到场（事故导致比计划 11:05 晚 7 分钟，但赶上 11:30 截止） ------
    advance(_t(11, 12))
    step("到场确认 REQ-ALPHA-2",
         service.confirm_arrival("REQ-ALPHA-2", {"receipt": "v-r2", "arrived_at": _t(11, 12)}, svc.now))
    return trace
