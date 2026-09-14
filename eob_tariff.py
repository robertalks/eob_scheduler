"""Pure tariff-window logic. No network, no hardware — easy to test.

A "high tariff window" is defined by a day-set and a start/end HH:MM time.
Anything not covered by a high-tariff window is treated as low tariff (NT),
i.e. the time the water heater is allowed to run.

Day sets support:
  - "all"                          every day
  - "weekday"                      Mon-Fri, EXCLUDING public holidays
  - "weekend"                      Sat-Sun, OR a public holiday on any
                                    weekday (matches how D45d actually
                                    treats holidays -- same schedule as
                                    weekends)
  - literal day names ("mon".."sun") for anything that doesn't follow the
    weekday/weekend split (holidays are ignored for these)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class TariffWindow:
    days: frozenset[str]  # subset of DAY_NAMES, "all", "weekday", "weekend"
    start: time
    end: time

    def covers(self, dt: datetime, is_holiday: bool) -> bool:
        if "all" not in self.days:
            day_name = DAY_NAMES[dt.weekday()]
            is_weekend_like = dt.weekday() >= 5 or is_holiday
            day_matches = (
                day_name in self.days
                or ("weekday" in self.days and not is_weekend_like)
                or ("weekend" in self.days and is_weekend_like)
            )
            if not day_matches:
                return False
        t = dt.time()
        if self.start <= self.end:
            # normal window, e.g. 06:00-08:00
            return self.start <= t < self.end
        else:
            # wraps midnight, e.g. 22:00-02:00
            return t >= self.start or t < self.end


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def load_windows(raw_windows: list[dict]) -> list[TariffWindow]:
    windows = []
    for w in raw_windows:
        days = frozenset(d.lower() for d in w["days"])
        windows.append(
            TariffWindow(
                days=days,
                start=_parse_hhmm(w["start"]),
                end=_parse_hhmm(w["end"]),
            )
        )
    return windows


class TariffSchedule:
    def __init__(
        self,
        raw_windows: list[dict],
        timezone: str,
        holiday_country: str | None = None,
    ):
        self.windows = load_windows(raw_windows)
        self.tz = ZoneInfo(timezone)
        self._holidays = None
        if holiday_country:
            import holidays as holidays_lib  # optional dependency

            # API differs across versions of the `holidays` package:
            # country_holidays() (newer) vs CountryHoliday() (older).
            if hasattr(holidays_lib, "country_holidays"):
                self._holidays = holidays_lib.country_holidays(holiday_country)
            else:
                self._holidays = holidays_lib.CountryHoliday(holiday_country)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def _is_holiday(self, dt: datetime) -> bool:
        if self._holidays is None:
            return False
        return dt.date() in self._holidays

    def is_low_tariff(self, dt: datetime | None = None) -> bool:
        """True if `dt` (default: now) falls in a low-tariff (NT) period."""
        dt = dt or self.now()
        is_holiday = self._is_holiday(dt)
        for window in self.windows:
            if window.covers(dt, is_holiday):
                return False  # inside a HIGH tariff window
        return True  # not in any high-tariff window -> low tariff

    def next_change(self, dt: datetime | None = None, horizon_days: int = 2):
        """Return the datetime of the next NT/VT transition after `dt`,
        by scanning minute-by-minute. Good enough for a UI/log message;
        not meant for high-precision scheduling (the daemon polls instead).
        """
        from datetime import timedelta

        dt = dt or self.now()
        current_state = self.is_low_tariff(dt)
        t = dt
        end = dt + timedelta(days=horizon_days)
        step = timedelta(minutes=1)
        while t < end:
            t += step
            if self.is_low_tariff(t) != current_state:
                return t
        return None
