"""Time source for relative-date context (Europe/Kyiv)."""

from datetime import datetime
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")


class Clock:
    def now(self) -> datetime:
        return datetime.now(KYIV)
