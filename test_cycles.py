"""StatsStore.cycles(): reconstructing heating cycles from readings."""
import time

from eob_stats import StatsStore
from eob_tariff import TariffSchedule


def store(tmp_path, interval=60, low=True):
    # Empty high-tariff windows -> always low; an all-day window -> always high.
    windows = [] if low else [{"days": ["all"], "start": "00:00", "end": "23:59"}]
    sched = TariffSchedule(windows, "Europe/Prague")
    return StatsStore(str(tmp_path / "s.db"), sched, interval)


def add(st, ts, relay, temp, target=15.0):
    st.add({"measured_temp_c": temp, "target_temp_c": target, "relay_on": relay}, ts=ts)


def test_simple_cycle(tmp_path):
    st = store(tmp_path)
    base = int(time.time()) - 3600
    for off, relay, temp in [
        (0, False, 13.5), (60, False, 13.4), (120, True, 13.4),
        (180, True, 14.0), (240, True, 14.6), (300, False, 15.0), (360, False, 14.9),
    ]:
        add(st, base + off, relay, temp)
    cy = st.cycles(hours=48)["cycles"]
    assert len(cy) == 1
    c = cy[0]
    assert c["start"] == base + 120 and c["end"] == base + 300
    assert c["seconds"] == 180
    assert c["start_temp"] == 13.4 and c["end_temp"] == 15.0
    assert c["low_tariff"] is True
    assert c["ongoing"] is False and c["gap"] is False


def test_ongoing_cycle(tmp_path):
    st = store(tmp_path)
    now = int(time.time())
    for off, relay, temp in [
        (-300, False, 13.0), (-240, False, 13.0), (-180, True, 13.0),
        (-120, True, 13.5), (-60, True, 14.0), (0, True, 14.3),
    ]:
        add(st, now + off, relay, temp)
    c = st.cycles(hours=48)["cycles"][0]
    assert c["ongoing"] is True and c["end"] is None and c["end_temp"] is None
    assert c["start"] == now - 180 and c["start_temp"] == 13.0
    assert abs(c["seconds"] - 180) <= 2


def test_gap_truncates_and_splits(tmp_path):
    st = store(tmp_path)
    base = int(time.time()) - 7200
    add(st, base, False, 13.0)
    add(st, base + 60, True, 13.0)
    add(st, base + 120, True, 13.5)
    add(st, base + 1120, True, 18.0)   # ~17 min gap: old cycle is closed, a new one starts
    add(st, base + 1180, False, 18.2)
    cy = st.cycles(hours=48)["cycles"]
    assert len(cy) == 2
    newest, older = cy
    assert older["start"] == base + 60 and older["end"] == base + 120 and older["gap"] is True
    assert newest["start"] == base + 1120 and newest["end"] == base + 1180 and newest["gap"] is False


def test_high_tariff_recorded(tmp_path):
    st = store(tmp_path, low=False)
    base = int(time.time()) - 3600
    add(st, base, False, 13.0)
    add(st, base + 60, True, 13.0)
    add(st, base + 120, False, 14.0)
    assert st.cycles(hours=48)["cycles"][0]["low_tariff"] is False


def test_window_excludes_old(tmp_path):
    st = store(tmp_path)
    old = int(time.time()) - 50 * 3600
    add(st, old, False, 13.0)
    add(st, old + 60, True, 13.0)
    add(st, old + 120, False, 14.0)
    assert st.cycles(hours=48)["cycles"] == []


def test_true_start_before_window(tmp_path):
    # A cycle that began before the window but ended inside it keeps its real start.
    st = store(tmp_path)
    now = int(time.time())
    since = now - 3600          # 1 h window
    add(st, since - 300, False, 12.0)
    add(st, since - 240, True, 12.0)   # started 4 min before the window
    add(st, since - 60, True, 13.0)
    add(st, since + 120, False, 14.0)  # ended inside it
    c = st.cycles(hours=1)["cycles"][0]
    assert c["start"] == since - 240 and c["start_temp"] == 12.0
    assert c["end"] == since + 120


def test_limit(tmp_path):
    st = store(tmp_path)
    base = int(time.time()) - 20000
    t = base
    for _ in range(5):
        add(st, t, False, 13.0); t += 60
        add(st, t, True, 13.0); t += 60
        add(st, t, False, 14.0); t += 60
    assert len(st.cycles(hours=48, limit=2)["cycles"]) == 2
