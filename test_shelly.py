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
    assert abs(total_kwh - 33.6) < 0.05


def test_shelly_store_daily_prior_row(tmp_path):
    """Prior row before window is used as baseline; delta credited to t1's day."""
    tz = ZoneInfo("Europe/Prague")
    store = ShellyStore(str(tmp_path / "s.db"))
    update_s = 3600

    def at(day, h):
        return int(datetime(2026, 11, day, h, 0, tzinfo=tz).timestamp())

    # One reading the day before the window, then two on the target day
    store.add(at(9, 23), "dev", 10.0, 5.0)   # prior: 5.0 kWh before window
    store.add(at(10, 1), "dev", 10.0, 5.4)   # +0.4 kWh → credited to Nov 10
    store.add(at(10, 2), "dev", 10.0, 5.9)   # +0.5 kWh → credited to Nov 10

    now = datetime(2026, 11, 10, 12, 0, tzinfo=tz)
    result = store.daily("dev", 1, tz, update_s, now=now)
    assert len(result) == 1
    assert abs(result[0]["kwh"] - 0.9) < 0.001


def test_shelly_store_daily_no_synthetic_baseline_large_counter(tmp_path):
    """No synthetic zero baseline when first reading has a large counter value."""
    tz = ZoneInfo("Europe/Prague")
    store = ShellyStore(str(tmp_path / "s.db"))
    update_s = 60

    def at(h, m):
        return int(datetime(2026, 11, 10, h, m, tzinfo=tz).timestamp())

    # Device counter already at 500 kWh when first seen (e.g. after DB wipe)
    store.add(at(1, 0), "dev", 10.0, 500.0)
    store.add(at(2, 0), "dev", 10.0, 500.3)  # +0.3 kWh

    now = datetime(2026, 11, 10, 12, 0, tzinfo=tz)
    result = store.daily("dev", 1, tz, update_s, now=now)
    assert len(result) == 1
    # Only the delta between the two readings should be counted, not 500 kWh
    assert abs(result[0]["kwh"] - 0.3) < 0.001


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


def test_energy_wh_in_intervals(tmp_path):
    """energy_wh_in_intervals sums deltas proportionally within given intervals."""
    tz = ZoneInfo("Europe/Prague")
    store = ShellyStore(str(tmp_path / "s.db"))

    def at(h, m=0):
        return int(datetime(2026, 11, 10, h, m, tzinfo=tz).timestamp())

    # 1 kWh per hour rate; readings every 30 min
    store.add(at(0), "dev", 2000.0, 0.0)
    store.add(at(0, 30), "dev", 2000.0, 0.5)   # +0.5 kWh = 500 Wh
    store.add(at(1), "dev", 2000.0, 1.0)        # +0.5 kWh = 500 Wh
    store.add(at(1, 30), "dev", 2000.0, 1.5)    # +0.5 kWh = 500 Wh
    store.add(at(2), "dev", 2000.0, 2.0)        # +0.5 kWh = 500 Wh

    # Heater was on from 00:30 to 01:30 (3600s)
    intervals = [(at(0, 30), at(1, 30))]
    result = store.energy_wh_in_intervals("dev", intervals)
    # Should capture the two full 30-min deltas fully inside the interval = 1000 Wh
    assert abs(result - 1000.0) < 1.0


def test_energy_wh_in_intervals_partial(tmp_path):
    """Partial overlap: a delta straddling an interval boundary is proportioned."""
    tz = ZoneInfo("Europe/Prague")
    store = ShellyStore(str(tmp_path / "s.db"))

    def at(h, m=0):
        return int(datetime(2026, 11, 10, h, m, tzinfo=tz).timestamp())

    # One delta: 00:00 → 02:00, 2 kWh = 2000 Wh
    store.add(at(0), "dev", 1000.0, 0.0)
    store.add(at(2), "dev", 1000.0, 2.0)

    # Interval covers exactly the first half: 00:00 → 01:00
    intervals = [(at(0), at(1))]
    result = store.energy_wh_in_intervals("dev", intervals)
    # Overlap = 3600s out of 7200s span → 50% → 1000 Wh
    assert abs(result - 1000.0) < 1.0


def test_energy_wh_in_intervals_no_prior_row(tmp_path):
    """Newly installed device: first reading already has a large counter value.
    All energy from that first reading onward should be counted."""
    tz = ZoneInfo("Europe/Prague")
    store = ShellyStore(str(tmp_path / "s.db"))

    def at(h, m=0):
        return int(datetime(2026, 11, 10, h, m, tzinfo=tz).timestamp())

    # Device installed mid-day, counter already at 21.39 kWh from factory use
    store.add(at(8), "dev", 2000.0, 21.39)
    store.add(at(9), "dev", 2000.0, 21.60)   # +0.21 kWh = 210 Wh
    store.add(at(10), "dev", 2000.0, 21.80)  # +0.20 kWh = 200 Wh

    # Heater on the whole day 00:00–24:00
    intervals = [(at(0), at(23, 59))]
    result = store.energy_wh_in_intervals("dev", intervals)
    # Should count from 0 → 21.80 kWh = 21800 Wh (first reading baseline = 0)
    assert abs(result - 21800.0) < 1.0


def test_energy_wh_in_intervals_first_reading_is_prior(tmp_path):
    """First-ever reading falls before the query window (prior row), but has no
    row before it — its counter value should be treated as starting from 0."""
    tz = ZoneInfo("Europe/Prague")
    store = ShellyStore(str(tmp_path / "s.db"))

    def at(day, h, m=0):
        return int(datetime(2026, 11, day, h, m, tzinfo=tz).timestamp())

    # Device first seen on Nov 20 at 14:00 with counter already at 21.39 kWh
    # (accumulated since installation, before first poll)
    store.add(at(20, 14), "dev", 2000.0, 21.39)   # first ever reading
    store.add(at(20, 15), "dev", 2000.0, 21.42)   # +0.03 kWh = 30 Wh
    store.add(at(21, 10), "dev", 2000.0, 21.69)   # +0.27 kWh = 270 Wh

    # Heater on all of Nov 20 and Nov 21
    intervals = [(at(20, 0), at(20, 23, 59)), (at(21, 0), at(21, 23, 59))]
    result = store.energy_wh_in_intervals("dev", intervals)
    # 0→21.39 baseline delta: fully within Nov 20 interval = 21390 Wh
    # at(20,14)→at(20,15): 30 Wh, fully within Nov 20 interval
    # at(20,15)→at(21,10): 270 Wh, spans both intervals — full delta counted across both
    assert abs(result - (21390.0 + 30.0 + 270.0)) < 1.0



    store = ShellyStore(str(tmp_path / "s.db"))
    assert store.energy_wh_in_intervals("dev", []) == 0.0
    # No readings
    assert store.energy_wh_in_intervals("dev", [(1000, 2000)]) == 0.0
