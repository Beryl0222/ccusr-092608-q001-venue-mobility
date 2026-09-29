"""可控时钟：所有领域时间判断都经过它，推进只能显式发生。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .errors import ValidationRejection


def parse_dt(value: str | datetime) -> datetime:
    """解析时间字符串，拒绝无时区时间。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationRejection(f"无法解析时间: {value}", field="time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationRejection("时间必须携带时区", field="time")
    return parsed


@dataclass
class ControllableClock:
    """测试与复盘用时钟；不读取系统时钟。"""

    now: datetime

    def __init__(self, start: str | datetime) -> None:
        self.now = parse_dt(start)

    def get(self) -> datetime:
        return self.now

    def set(self, value: str | datetime) -> datetime:
        self.now = parse_dt(value)
        return self.now

    def advance(self, *, minutes: int = 0, seconds: int = 0) -> datetime:
        self.now += timedelta(minutes=minutes, seconds=seconds)
        return self.now

    def iso(self) -> str:
        return self.now.isoformat()

    def snapshot(self) -> dict[str, Any]:
        return {"now": self.iso()}
