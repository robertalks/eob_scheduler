from datetime import datetime
from zoneinfo import ZoneInfo

from eob_tariff import TariffSchedule

TZ = ZoneInfo("Europe/Prague")

RAW_WINDOWS = [
    {"days": ["all"], "start": "06:00", "end": "08:00"},
    {"days": ["all"], "start": "17:00", "end": "19:00"},
]


def dt(h, m, day=1):
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def test_basic_low_and_high():
    s = TariffSchedule(RAW_WINDOWS, "Europe/Prague")
    assert s.is_low_tariff(dt(3, 0)) is True   # night -> low
    assert s.is_low_tariff(dt(6, 30)) is False  # morning peak -> high
    assert s.is_low_tariff(dt(8, 0)) is True   # boundary: end exclusive -> low
    assert s.is_low_tariff(dt(17, 30)) is False  # evening peak -> high
    assert s.is_low_tariff(dt(20, 0)) is True   # night -> low


def test_weekday_only_window():
    raw = [{"days": ["mon", "tue", "wed", "thu", "fri"], "start": "06:00", "end": "08:00"}]
    s = TariffSchedule(raw, "Europe/Prague")
    monday = datetime(2026, 9, 7, 7, 0, tzinfo=TZ)  # a Monday
    saturday = datetime(2026, 9, 12, 7, 0, tzinfo=TZ)  # a Saturday
    assert s.is_low_tariff(monday) is False
    assert s.is_low_tariff(saturday) is True  # weekend: window doesn't apply


def test_midnight_wrap():
    raw = [{"days": ["all"], "start": "22:00", "end": "02:00"}]
    s = TariffSchedule(raw, "Europe/Prague")
    assert s.is_low_tariff(dt(23, 0)) is False
    assert s.is_low_tariff(dt(1, 0)) is False
    assert s.is_low_tariff(dt(10, 0)) is True


def test_next_change():
    s = TariffSchedule(RAW_WINDOWS, "Europe/Prague")
    change = s.next_change(dt(5, 0))
    assert change.hour == 6 and change.minute == 0


def test_weekday_weekend_keywords():
    raw = [{"days": ["weekday"], "start": "07:00", "end": "08:00"}]
    s = TariffSchedule(raw, "Europe/Prague")
    monday = datetime(2026, 9, 7, 7, 30, tzinfo=TZ)  # Monday
    saturday = datetime(2026, 9, 12, 7, 30, tzinfo=TZ)  # Saturday
    assert s.is_low_tariff(monday) is False  # weekday window applies
    assert s.is_low_tariff(saturday) is True  # weekend: weekday window doesn't apply


def test_holiday_treated_as_weekend():
    # Christmas Day 2026 falls on a Friday -- a "weekday" by date, but
    # should follow the weekend/holiday schedule for D45d purposes.
    raw = [
        {"days": ["weekday"], "start": "07:00", "end": "08:00"},
        {"days": ["weekend"], "start": "12:00", "end": "13:00"},
    ]
    s = TariffSchedule(raw, "Europe/Prague", holiday_country="CZ")
    christmas = datetime(2026, 12, 25, 7, 30, tzinfo=TZ)
    assert christmas.weekday() == 4  # sanity check: it's a Friday
    assert s.is_low_tariff(christmas) is True  # weekday window should NOT apply
    christmas_noon = datetime(2026, 12, 25, 12, 30, tzinfo=TZ)
    assert s.is_low_tariff(christmas_noon) is False  # weekend window SHOULD apply


def test_no_holiday_country_ignores_holidays():
    raw = [{"days": ["weekday"], "start": "07:00", "end": "08:00"}]
    s = TariffSchedule(raw, "Europe/Prague")  # holiday_country not set
    christmas = datetime(2026, 12, 25, 7, 30, tzinfo=TZ)
    assert s.is_low_tariff(christmas) is False  # treated as a plain Friday


def test_users_real_d45d_windows():
    raw = [
        {"days": ["weekday"], "start": "00:10", "end": "01:10"},
        {"days": ["weekday"], "start": "06:56", "end": "07:55"},
        {"days": ["weekday"], "start": "14:11", "end": "15:10"},
        {"days": ["weekday"], "start": "18:26", "end": "19:25"},
        {"days": ["weekend"], "start": "03:36", "end": "04:35"},
        {"days": ["weekend"], "start": "10:06", "end": "11:05"},
        {"days": ["weekend"], "start": "16:06", "end": "17:05"},
        {"days": ["weekend"], "start": "19:36", "end": "20:35"},
    ]
    s = TariffSchedule(raw, "Europe/Prague", holiday_country="CZ")
    monday = datetime(2026, 9, 7, tzinfo=TZ)
    assert s.is_low_tariff(monday.replace(hour=0, minute=5)) is True
    assert s.is_low_tariff(monday.replace(hour=0, minute=30)) is False
    assert s.is_low_tariff(monday.replace(hour=7, minute=0)) is False
    assert s.is_low_tariff(monday.replace(hour=7, minute=30)) is False
    assert s.is_low_tariff(monday.replace(hour=8, minute=0)) is True
    saturday = datetime(2026, 9, 12, tzinfo=TZ)
    assert s.is_low_tariff(saturday.replace(hour=4, minute=0)) is False
    assert s.is_low_tariff(saturday.replace(hour=5, minute=0)) is True


if __name__ == "__main__":
    test_basic_low_and_high()
    test_weekday_only_window()
    test_midnight_wrap()
    test_next_change()
    test_weekday_weekend_keywords()
    test_holiday_treated_as_weekend()
    test_no_holiday_country_ignores_holidays()
    test_users_real_d45d_windows()
    print("All tests passed.")
