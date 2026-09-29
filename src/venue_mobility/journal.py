"""事件日志：只追加、可重放。事件标识保证业务幂等，恢复后状态一致。"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


class EventStore:
    """JSONL 事件日志。同一 event_id 永远只接受一次。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._events: list[dict[str, Any]] = []
        self._seen: set[str] = set()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        event = json.loads(line)
                        self._events.append(event)
                        self._seen.add(event["event_id"])

    def append(self, event: Mapping[str, Any]) -> bool:
        """追加事件；event_id 已存在时返回 False，不做二次扣减。"""
        event_id = event["event_id"]
        if event_id in self._seen:
            return False
        record = dict(event)
        self._events.append(record)
        self._seen.add(event_id)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return True

    def all(self) -> list[dict[str, Any]]:
        return list(self._events)

    def stream(self) -> Iterator[dict[str, Any]]:
        return iter(self._events)

    def by_aggregate(self, aggregate_type: str) -> Iterable[dict[str, Any]]:
        return (e for e in self._events if e["aggregate_type"] == aggregate_type)

    def seen_receipts(self, prefix: str | None = None) -> set[str]:
        if prefix is None:
            return set(self._seen)
        return {eid for eid in self._seen if eid.startswith(prefix)}

    def next_version(self, aggregate_id: str) -> int:
        versions = [e["version"] for e in self._events if e["aggregate_id"] == aggregate_id]
        return (max(versions) + 1) if versions else 1
