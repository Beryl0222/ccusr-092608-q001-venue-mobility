"""可控时钟：所有业务时间从这里取，进程重启后延续同一时间轴。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_CST = timezone(timedelta(hours=8))


class Clock:
    def __init__(self, path: Path | None = None, start: datetime | None = None) -> None:
        self.path = path
        self._now: datetime | None = None
        if path is not None and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self._now = datetime.fromisoformat(data["now"])
        if self._now is None:
            self._now = start or datetime(2026, 9, 26, 8, 0, tzinfo=_CST)
        if self._now.tzinfo is None:
            raise ValueError("时钟起点必须携带时区")
        self._save()

    @property
    def now(self) -> datetime:
        return self._now  # type: ignore[return-value]

    def advance(self, minutes: float = 0, until: str | datetime | None = None) -> datetime:
        target = self._now  # type: ignore[assignment]
        if until is not None:
            new_time = until if isinstance(until, datetime) else datetime.fromisoformat(until)
            if new_time.tzinfo is None:
                raise ValueError("推进目标时间必须携带时区")
            if new_time < target:
                raise ValueError("时钟只能向前推进")
            target = new_time
        if minutes:
            target = target + timedelta(minutes=float(minutes))
        if until is None and not minutes:
            raise ValueError("必须给出推进分钟数或目标时间")
        self._now = target
        self._save()
        return self._now

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"now": self._now.isoformat()}, ensure_ascii=False), encoding="utf-8")
