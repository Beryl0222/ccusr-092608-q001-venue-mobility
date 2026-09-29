"""HTTP 接口：按角色返回可执行且不过度披露的行程。

仅使用标准库。鉴权采用请求头：
- X-Role: team | transport | security | ops
- X-Identity: team 角色下的队伍标识（需与申请 requester 一致）

命令型接口由运行中心（ops）或职责相关角色执行；查询接口按角色投影，
team 只能看本队行程，且返回内容只包含可执行信息，不含运力台账与其他方事实。
状态持久化在 --data-dir 指向的 JSONL 事件日志与时钟侧车中。
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .clock import ControllableClock
from .errors import DomainRejection, RoleForbidden
from .projections import list_demands, project_demand
from .service import MobilityService
from .store import EventStore

DEFAULT_START = "2026-09-26T00:00:00+08:00"


class Runtime:
    """持有单例服务与写锁；命令串行化，避免并发申请超配。"""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = EventStore(data_dir / "events.jsonl")
        saved = self.store.load_clock()
        self.clock = ControllableClock(saved or DEFAULT_START)
        self.service = MobilityService(self.store, self.clock)
        self.lock = threading.RLock()

    def reset(self, start: str = DEFAULT_START) -> None:
        target = self.store.path
        if target is not None and target.exists():
            target.unlink()
        clock_file = self.store.clock_path()
        if clock_file is not None and clock_file.exists():
            clock_file.unlink()
        self.store = EventStore(target)
        self.clock = ControllableClock(start)
        self.service = MobilityService(self.store, self.clock)


def build_handler(runtime: Runtime) -> type[BaseHTTPRequestHandler]:
    service = runtime.service

    class Handler(BaseHTTPRequestHandler):
        server_version = "VenueMobility/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        # ------------------------------------------------------------ 工具

        def _send_json(self, status: int, body: Any) -> None:
            raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise DomainRejection(f"请求体不是合法 JSON: {exc}", code="bad_json")
            if not isinstance(body, dict):
                raise DomainRejection("请求体必须是 JSON 对象", code="bad_json")
            return body

        def _role(self) -> str | None:
            return self.headers.get("X-Role")

        def _identity(self) -> str | None:
            return self.headers.get("X-Identity")

        def _require_role(self, *roles: str) -> str:
            role = self._role()
            if role not in roles:
                raise RoleForbidden(
                    f"该接口要求角色 {'/'.join(roles)}，收到 {role!r}", field="X-Role")
            return role

        def _handle(self, fn: Callable[[], Any]) -> None:
            try:
                with runtime.lock:
                    result = fn()
            except DomainRejection as exc:
                status = 403 if exc.code == "role_forbidden" else (
                    404 if exc.code == "demand_not_found" else 422)
                self._send_json(status, {"error": exc.to_dict()})
            except Exception as exc:  # noqa: BLE001 - 边界统一兜底
                self._send_json(500, {"error": {"code": "internal", "message": str(exc)}})
            else:
                if result is None:
                    result = {"ok": True, "now": runtime.clock.iso()}
                self._send_json(200, result)

        # ------------------------------------------------------------ 路由

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            m_demand = re.fullmatch(r"/demands/(demand-\d+)", path)

            def handle() -> Any:
                if path == "/health":
                    return {"ok": True, "now": runtime.clock.iso(),
                            "demands": len(service.demands)}
                if path == "/facts":
                    self._require_role("transport", "security", "ops")
                    return {"facts": service.fact_refs(), "now": runtime.clock.iso()}
                if path == "/usage":
                    self._require_role("transport", "ops")
                    return {"usage": service.inventory.usage(), "now": runtime.clock.iso()}
                if path == "/demands":
                    role = self._role()
                    if role is None:
                        raise RoleForbidden("查询行程必须携带 X-Role", field="X-Role")
                    return {"demands": list_demands(service, role, identity=self._identity()),
                            "now": runtime.clock.iso()}
                if m_demand:
                    role = self._role()
                    if role is None:
                        raise RoleForbidden("查询行程必须携带 X-Role", field="X-Role")
                    demand_id = m_demand.group(1)
                    view = project_demand(service, demand_id, role, identity=self._identity())
                    return {"trip": view, "now": runtime.clock.iso()}
                m_trace = re.fullmatch(r"/demands/(demand-\d+)/trace", path)
                if m_trace:
                    self._require_role("ops")
                    return {"demand_id": m_trace.group(1),
                            "events": service.trace(m_trace.group(1)),
                            "now": runtime.clock.iso()}
                raise DomainRejection(f"无此路径: {path}", code="not_found")

            self._handle(handle)

        def do_POST(self) -> None:  # noqa: N802
            self._route_post()

        def _route_post(self) -> None:
            path = urlparse(self.path).path
            m_confirm = re.fullmatch(r"/demands/(demand-\d+)/confirmations", path)
            m_freeze = re.fullmatch(r"/demands/(demand-\d+)/freeze", path)
            m_clear = re.fullmatch(r"/demands/(demand-\d+)/clearance", path)
            m_revise = re.fullmatch(r"/demands/(demand-\d+)/emergency-revise", path)
            m_ratify = re.fullmatch(r"/revisions/(rev-demand-\d+-\d+)/ratify", path)
            m_depart = re.fullmatch(r"/demands/(demand-\d+)/depart", path)
            m_arrive = re.fullmatch(r"/demands/(demand-\d+)/arrive", path)
            m_escalate = re.fullmatch(r"/demands/(demand-\d+)/escalate", path)

            def handle() -> Any:
                body = self._read_json()
                if path == "/admin/facts":
                    self._require_role("ops")
                    event = service.register_fact(
                        body["kind"], body["key"], body["snapshot"],
                        version=body.get("version"))
                    return {"ok": True, "event_id": event["event_id"],
                            "now": runtime.clock.iso()}
                if path == "/demands":
                    state, events, isolated = service.submit_demand(body)
                    return {"demand_id": state.demand_id, "isolated": isolated,
                            "receipt_key": state.receipt_key,
                            "fingerprint": state.fingerprint,
                            "new_events": [e["event_type"] for e in events],
                            "now": runtime.clock.iso()}
                if m_confirm:
                    demand_id = m_confirm.group(1)
                    role = body.get("role", "")
                    self._require_role(role)  # 头角色必须与确认角色一致
                    state, event = service.confirm_fact(
                        demand_id, role, body["confirmation_key"], body.get("claims", {}))
                    return {"demand_id": demand_id, "status": state.status,
                            "confirmations": sorted(state.confirmations),
                            "idempotent": event is None,
                            "now": runtime.clock.iso()}
                if m_freeze:
                    self._require_role("ops")
                    state, event = service.freeze_schedule(m_freeze.group(1))
                    return {"demand_id": state.demand_id, "status": state.status,
                            "schedule_version": state.schedule_version,
                            "road_version": state.road_version}
                if m_clear:
                    self._require_role("ops")
                    state, events = service.issue_clearance(m_clear.group(1))
                    return {"demand_id": state.demand_id, "status": state.status,
                            "clearance_event": f"{state.demand_id}-clearance",
                            "schedule_version": state.schedule_version,
                            "road_version": state.road_version,
                            "itinerary": state.clearance["itinerary"],
                            "resources": state.clearance["resources"]}
                if m_revise:
                    self._require_role("ops")
                    reason = str(body.get("reason", ""))
                    revision, events = service.emergency_revise(m_revise.group(1), reason)
                    return {"revision_id": revision.revision_id,
                            "review_due_at": revision.review_due_at,
                            "minimum_extra_resources": [
                                {"resource_ref": r, "quantity": q}
                                for r, q in revision.extra_items],
                            "events": [e["event_type"] for e in events]}
                if m_ratify:
                    self._require_role("ops")
                    state, events = service.ratify_revision(m_ratify.group(1))
                    return {"demand_id": state.demand_id, "status": state.status,
                            "schedule_version": state.schedule_version,
                            "road_version": state.road_version,
                            "resources": state.clearance["resources"],
                            "events": [e["event_type"] for e in events]}
                if m_depart:
                    self._require_role("transport", "ops")
                    event = service.depart(m_depart.group(1))
                    return {"ok": True, "event_id": event["event_id"]}
                if m_arrive:
                    demand_id = m_arrive.group(1)
                    state = service._demand(demand_id)
                    role = self._role()
                    if role not in ("team", "ops"):
                        raise RoleForbidden("只有本队或运行中心可以确认到场", field="X-Role")
                    if role == "team" and self._identity() not in (None, state.requester):
                        raise RoleForbidden("只能确认本队到场", field="X-Identity")
                    events = service.arrive(demand_id, arrived_at=body.get("arrived_at"))
                    return {"ok": True, "events": [e["event_type"] for e in events]}
                if m_escalate:
                    self._require_role("ops")
                    event = service.escalate(m_escalate.group(1),
                                             body["to_role"], body["reason"])
                    return {"ok": True, "event_id": event["event_id"]}
                if path == "/clock/tick":
                    self._require_role("ops")
                    events = service.tick(minutes=body.get("minutes"),
                                          seconds=body.get("seconds"),
                                          to=body.get("to"))
                    return {"now": runtime.clock.iso(),
                            "due_events": [e["event_id"] for e in events]}
                raise DomainRejection(f"无此路径: {path}", code="not_found")

            self._handle(handle)

    return Handler


def serve(data_dir: Path, host: str, port: int) -> ThreadingHTTPServer:
    runtime = Runtime(data_dir)
    handler = build_handler(runtime)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.runtime = runtime  # type: ignore[attr-defined]
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description="分散赛区通行保障 HTTP 服务")
    parser.add_argument("--data-dir", default=".run", help="事件日志与时钟目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    httpd = serve(Path(args.data_dir), args.host, args.port)
    print(f"venue-mobility 服务已启动: http://{args.host}:{args.port}  数据目录 {args.data_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
