"""命令行入口：契约校验、场景推演、延误复盘、HTTP 服务。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts import validate_event


def _cmd_validate(args: argparse.Namespace) -> int:
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def _cmd_scenario(args: argparse.Namespace) -> int:
    from .app import Hub
    from .replay import Replay, render_text
    from .scenario import build_scenario

    hub = Hub(args.state)
    trace = build_scenario(hub)
    if args.json:
        payload = {"trace": trace, "events": hub.store.all()}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        _write(args.json_out, text)
    else:
        print("推演步骤：")
        for item in trace:
            result = item["result"]
            summary = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            print(f"  [{item['at'][11:16]}] {item['step']} -> {summary}")
        print()
        print(render_text(Replay(hub.store), args.request))
        if args.state:
            print(f"\n状态已持久化至 {args.state}，可用 replay 子命令复盘。")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from .journal import EventStore
    from .replay import Replay, render_text

    if args.events:
        store = EventStore(Path(args.events))
    elif args.state:
        store = EventStore(Path(args.state) / "events.jsonl")
    else:
        print("必须给出 --state 目录或 --events 事件文件", file=sys.stderr)
        return 2
    rep = Replay(store)
    if args.json:
        payload = {
            "fact_changes": rep.fact_changes(),
            "decisions": rep.request_decisions(),
            "capacity": rep.capacity_movements(),
            "emergency_track": rep.emergency_track(),
            "arrivals": [rep.arrival(r) for r in sorted({
                e["payload"]["request_ref"] for e in rep.events
                if e["event_type"] == "ARRIVAL_CONFIRMED"})],
            "timeline": rep.timeline(),
        }
        _write(args.json_out, json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(rep, args.request))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .app import make_server

    server = make_server(args.host, args.port, args.state)
    print(f"listening on {args.host}:{args.port} state={args.state or '<memory>'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _write(target: str | None, text: str) -> None:
    if target:
        Path(target).write_text(text, encoding="utf-8")
        print(f"written to {target}")
    else:
        print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="分散赛区通行保障台")
    sub = parser.add_subparsers(dest="command")

    p_val = sub.add_parser("validate", help="校验单个领域事件")
    p_val.add_argument("schema")
    p_val.add_argument("event")
    p_val.set_defaults(func=_cmd_validate)

    common_state = argparse.ArgumentParser(add_help=False)
    common_state.add_argument("--state", default=None, help="持久化目录（clock.json + events.jsonl）")

    p_sc = sub.add_parser("scenario", parents=[common_state], help="推演完整延误场景")
    p_sc.add_argument("--request", default=None, help="复盘只聚焦该申请")
    p_sc.add_argument("--json", action="store_true", help="输出 JSON 而非文本报告")
    p_sc.add_argument("--json-out", default=None, help="JSON 输出文件路径")
    p_sc.set_defaults(func=_cmd_scenario)

    p_rp = sub.add_parser("replay", help="从事件日志复盘")
    p_rp.add_argument("--state", default=None)
    p_rp.add_argument("--events", default=None, help="直接指定 events.jsonl")
    p_rp.add_argument("--request", default=None)
    p_rp.add_argument("--json", action="store_true")
    p_rp.add_argument("--json-out", default=None)
    p_rp.set_defaults(func=_cmd_replay)

    p_sv = sub.add_parser("serve", parents=[common_state], help="启动 HTTP 服务")
    p_sv.add_argument("--host", default="127.0.0.1")
    p_sv.add_argument("--port", type=int, default=8080)
    p_sv.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # 兼容旧用法：python -m venue_mobility.cli <schema.json> <event.json>
    if len(argv) == 2 and not argv[0].startswith("-") and Path(argv[0]).exists():
        ns = argparse.Namespace(schema=argv[0], event=argv[1])
        return _cmd_validate(ns)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
