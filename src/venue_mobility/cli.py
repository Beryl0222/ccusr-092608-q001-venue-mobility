"""命令行入口：契约校验、延误复盘、HTTP 服务。

- validate：对领域事件做基础契约校验（原行为，输出 valid 或逐行问题）。
- replay：运行复盘场景脚本，打印“事实变化 → 资源调整 → 到场”的完整依据，
  可选 --export 导出结构化 JSON；可用 --data-dir 将事件日志落盘后再重放验证恢复。
- serve：启动按角色最小披露的 HTTP 服务。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts import validate_event
from .errors import DomainRejection
from .scenario import ScenarioRunner, load_scenario
from .store import EventStore


def _cmd_validate(args: argparse.Namespace) -> int:
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    raw = json.loads(Path(args.event).read_text(encoding="utf-8"))
    events = raw if isinstance(raw, list) else [raw]
    issues_all = []
    for event in events:
        for issue in validate_event(event, schema):
            issues_all.append((event.get("event_id", "?"), issue))
    if not issues_all:
        print("valid")
        return 0
    for event_id, issue in issues_all:
        print(f"{event_id}\t{issue.field}\t{issue.code}\t{issue.message}")
    return 1


def _cmd_replay(args: argparse.Namespace) -> int:
    scenario = load_scenario(Path(args.scenario))
    store = EventStore(Path(args.data_dir) / "events.jsonl" if args.data_dir else None)
    runner = ScenarioRunner(store, scenario["start"])
    runner.run(scenario)
    report = runner.build_report(scenario.get("title", "未命名复盘"))

    if args.export:
        Path(args.export).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(runner.render_text(report))
    if args.export:
        print(f"\n结构化复盘已导出：{args.export}")
    failures = [s for s in report["steps"] if s["error"]]
    # 被领域规则预期拒绝的步骤（如并发超配）在脚本里以 expect_error 标注。
    rejected_steps = {
        index for index, step in enumerate(scenario.get("steps", []), start=1)
        if step.get("expect_error")
    }
    unexpected = [s for s in failures if s["index"] not in rejected_steps]
    if unexpected:
        print(f"\n存在未预期的失败步骤：{[s['index'] for s in unexpected]}", file=sys.stderr)
        return 1
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .httpapi import serve
    httpd = serve(Path(args.data_dir), args.host, args.port)
    print(f"venue-mobility 服务已启动: http://{args.host}:{args.port}  数据目录 {args.data_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="venue_mobility",
                                     description="分散赛区通行保障台")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验领域事件契约")
    p_validate.add_argument("schema")
    p_validate.add_argument("event")
    p_validate.set_defaults(func=_cmd_validate)

    p_replay = sub.add_parser("replay", help="运行延误复盘场景")
    p_replay.add_argument("scenario", help="场景脚本 JSON")
    p_replay.add_argument("--export", help="导出结构化复盘 JSON")
    p_replay.add_argument("--data-dir", help="将事件落盘到此目录（验证持久化）")
    p_replay.add_argument("--quiet", action="store_true")
    p_replay.set_defaults(func=_cmd_replay)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--data-dir", default=".run")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DomainRejection as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
