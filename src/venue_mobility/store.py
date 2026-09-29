"""事件日志：JSONL 追加存储与重放。

事件只追加、不修改；时钟快照作为侧车文件单独持久化。
截止时间以带时区的绝对时间记录在事件载荷里，因此进程恢复后无需平移任何期限。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass
class EventStore:
    path: Path | None = None

    def __post_init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        if self.path is not None:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        self.events.append(json.loads(line))

    def append(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def extend(self, events: Iterable[dict[str, Any]]) -> None:
        for event in events:
            self.append(event)

    def all(self) -> list[dict[str, Any]]:
        return list(self.events)

    def clock_path(self) -> Path | None:
        return self.path.parent / "clock.json" if self.path is not None else None

    def save_clock(self, iso_now: str) -> None:
        target = self.clock_path()
        if target is None:
            return
        target.write_text(
            json.dumps({"now": iso_now}, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def load_clock(self) -> str | None:
        target = self.clock_path()
        if target is None or not target.exists():
            return None
        body = json.loads(target.read_text(encoding="utf-8"))
        return body.get("now")
