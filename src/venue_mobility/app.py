"""HTTP 接口：事实上报、放行、紧急改线、批准、发车、到场与角色行程投影。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .clock import Clock
from .journal import EventStore
from .service import MobilityService, ServiceError


class Hub:
    """进程内全部状态，受同一把锁保护。"""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.lock = threading.RLock()
        if state_dir is None:
            self.clock = Clock()
            self.store = EventStore()
        else:
            base = Path(state_dir)
            base.mkdir(parents=True, exist_ok=True)
            self.clock = Clock(base / "clock.json")
            self.store = EventStore(base / "events.jsonl")
        self.service = MobilityService(self.store)

    def rebuild_after_restart(self) -> None:
        # 时钟与日志均落盘，构造时已完成重放。
        self.service = MobilityService(self.store)


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    hub: Hub  # 由 make_server 注入到类上

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError("invalid_json", f"请求体不是合法 JSON：{exc}", 400)
        if not isinstance(body, dict):
            raise ServiceError("invalid_body", "请求体必须是 JSON 对象")
        return body

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            with self.hub.lock:
                if parsed.path == "/requests":
                    q = parse_qs(parsed.query)
                    role = (q.get("role") or [""])[0]
                    team_ref = (q.get("team_ref") or [None])[0]
                    if role not in ("team", "transport", "security", "media", "operator"):
                        raise ServiceError("forbidden", "缺少有效 role 查询参数", 403)
                    _json_response(self, 200, {"requests": self.hub.service.list_requests(role, team_ref)})
                elif parsed.path.startswith("/requests/") and parsed.path.endswith("/itinerary"):
                    ref = parsed.path.split("/")[2]
                    q = parse_qs(parsed.query)
                    role = (q.get("role") or [""])[0]
                    _json_response(self, 200, self.hub.service.itinerary(ref, role))
                elif parsed.path == "/resources":
                    _json_response(self, 200, {"now": self.hub.clock.now.isoformat(),
                                               "pools": self.hub.service.ledger.snapshot()})
                elif parsed.path == "/events":
                    _json_response(self, 200, {"events": self.hub.store.all()})
                elif parsed.path == "/clock":
                    _json_response(self, 200, {"now": self.hub.clock.now.isoformat()})
                else:
                    raise ServiceError("not_found", f"无此路径 {parsed.path}", 404)
        except ServiceError as exc:
            _json_response(self, exc.status, {"error": exc.code, "message": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            with self.hub.lock:
                now = self.hub.clock.now
                svc = self.hub.service
                if parsed.path == "/facts":
                    result = svc.record_fact(body, now)
                elif parsed.path == "/requests":
                    result = svc.submit_request(body, now)
                elif parsed.path.startswith("/requests/"):
                    parts = parsed.path.strip("/").split("/")
                    if len(parts) != 3:
                        raise ServiceError("not_found", f"无此路径 {parsed.path}", 404)
                    ref, action = parts[1], parts[2]
                    if action == "ratify":
                        result = svc.ratify(ref, body, now)
                    elif action == "dispatch":
                        result = svc.confirm_dispatch(ref, body, now)
                    elif action == "arrival":
                        result = svc.confirm_arrival(ref, body, now)
                    else:
                        raise ServiceError("not_found", f"无此动作 {action}", 404)
                elif parsed.path == "/clock/advance":
                    target = self.hub.clock.advance(
                        minutes=body.get("minutes", 0), until=body.get("until"))
                    result = svc.advance(target)
                else:
                    raise ServiceError("not_found", f"无此路径 {parsed.path}", 404)
                _json_response(self, 200, {"now": self.hub.clock.now.isoformat(), **result})
        except ServiceError as exc:
            _json_response(self, exc.status, {"error": exc.code, "message": str(exc)})


def make_server(host: str, port: int, state_dir: str | Path | None = None) -> ThreadingHTTPServer:
    hub = Hub(state_dir)

    class _Handler(Handler):
        pass

    _Handler.hub = hub
    server = ThreadingHTTPServer((host, port), _Handler)
    server.hub = hub  # type: ignore[attr-defined]
    return server


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="分散赛区通行保障台 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state", default=None, help="持久化目录；缺省为纯内存")
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.state)
    print(f"listening on {args.host}:{args.port} state={args.state or '<memory>'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
