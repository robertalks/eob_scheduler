"""Tests for shelly.py: interval parsing, store daily delta logic, poller config."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from shelly import ShellyPoller, ShellyStore, _parse_update_interval


def test_parse_update_interval():
    assert _parse_update_interval("30m") == 1800
    assert _parse_update_interval("60m") == 3600
    assert _parse_update_interval("1h") == 3600
    assert _parse_update_interval("5") == 5
    assert _parse_update_interval(120) == 120


def test_shelly_store_daily_delta(tmp_path):
    """Counter reset mid-day must be skipped, not subtracted.
    The first reading's value is counted from 0 (no prior row = device started at 0)."""
    tz = ZoneInfo("Europe/Prague")
    store = ShellyStore(str(tmp_path / "s.db"))
    update_s = 60

    def at(h, m):
        return int(datetime(2026, 11, 10, h, m, tzinfo=tz).timestamp())

    readings = [
        (at(1, 0), 0.5),
        (at(2, 0), 1.0),   # +0.5
        (at(3, 0), 1.8),   # +0.8
        (at(4, 0), 0.1),   # reset — skip
        (at(5, 0), 0.6),   # +0.5
    ]
    for ts, kwh in readings:
        store.add(ts, "dev", 100.0, kwh)

    now = datetime(2026, 11, 10, 12, 0, tzinfo=tz)
    result = store.daily("dev", 1, tz, update_s, now=now)
    assert len(result) == 1
    # 0.5 (first reading from 0) + 0.5 + 0.8 + 0.5 (reset skipped) = 2.3
    assert abs(result[0]["kwh"] - 2.3) < 0.001


def test_shelly_store_daily_14_days(tmp_path):
    """14 days of hourly readings → correct day count and sums."""
    tz = ZoneInfo("Europe/Prague")
    store = ShellyStore(str(tmp_path / "s.db"))
    update_s = 3600  # 1h interval

    base = datetime(2026, 11, 7, 0, 0, tzinfo=tz)
    total = 0.0
    for day in range(14):
        for hour in range(24):
            dt = base + timedelta(days=day, hours=hour)
            total += 0.1  # 0.1 kWh per hour = 2.4 kWh per day
            store.add(int(dt.timestamp()), "dev", 100.0, round(total, 4))

    # Set now to end of last day of data so the 14-day window aligns with data
    now = base + timedelta(days=13, hours=23, minutes=59)
    result = store.daily("dev", 14, tz, update_s, now=now)
    assert len(result) == 14
    total_kwh = sum(e["kwh"] for e in result)
    # 14 days × 24 h × 0.1 kWh/h = 33.6 (first reading counted from 0)
    assert abs(total_kwh - 33.6) < 0.01


def test_shelly_poller_from_config_none():
    assert ShellyPoller.from_config({}, "/tmp/x.db", ZoneInfo("Europe/Prague")) is None
    assert ShellyPoller.from_config({"power_meters": []}, "/tmp/x.db", ZoneInfo("Europe/Prague")) is None


def test_shelly_poller_from_config(tmp_path):
    config = {
        "power_meters": [
            {"name": "Fridge", "address": "192.168.1.10", "update": "30m"},
            {"name": "Heater", "address": "192.168.1.11", "update": "60m"},
        ]
    }
    poller = ShellyPoller.from_config(config, str(tmp_path / "s.db"), ZoneInfo("Europe/Prague"))
    assert poller is not None
    assert len(poller._devices) == 2
    fridge, heater = poller._devices
    assert fridge.name == "Fridge"
    assert fridge.address == "192.168.1.10"
    assert fridge.update_seconds == 1800
    assert heater.name == "Heater"
    assert heater.update_seconds == 3600
