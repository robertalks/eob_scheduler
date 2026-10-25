from datetime import datetime
from zoneinfo import ZoneInfo

from eob_lib import iter_frames, parse_status
from eob_stats import StatsStore
from eob_tariff import TariffSchedule

# Real reply from the device (2026-10-16): 14.7C measured, 15.0C target,
# relay off.
REAL_FRAME = bytes.fromhex("8002400a0e000701081e00210029")


def test_real_frame():
    s = parse_status(REAL_FRAME)
    assert s["measured_temp_c"] == 14.7
    assert s["target_temp_c"] == 15.0
    assert s["relay_on"] is False
    assert s["off_mode"] is False


def test_wrapped_in_multimessage():
    payload = bytes.fromhex("7fff4013") + REAL_FRAME + bytes.fromhex("801b40011e")
    assert [(hi, lo) for hi, lo, _ in iter_frames(payload)] == [(0x80, 0x02), (0x80, 0x1B)]
    assert parse_status(payload)["measured_temp_c"] == 14.7


def test_relay_on_and_negative():
    p = bytearray(REAL_FRAME)
    p[4 + 6] |= 0x40          # relay on
    p[4 + 4] |= 0x80          # negative temperature
    s = parse_status(bytes(p))
    assert s["relay_on"] is True
    assert s["measured_temp_c"] == -14.7


def test_off_reports_zero_target():
    p = bytearray(REAL_FRAME)
    p[4 + 5] = 0x00
    assert parse_status(bytes(p))["target_temp_c"] == 0.0


def test_junk_before_frame_still_parses():
    assert parse_status(b"\x00\xff" + REAL_FRAME)["measured_temp_c"] == 14.7


def test_daily_split_by_tariff(tmp_path):
    tz = ZoneInfo("Europe/Prague")
    sched = TariffSchedule([{"days": ["all"], "start": "06:00", "end": "08:00"}],
                           "Europe/Prague")
    store = StatsStore(str(tmp_path / "s.db"), sched, interval=60)
    # A fixed ordinary day (no DST transition). Build timestamps from explicit
    # wall-clock times, not midnight + minutes*60 -- the latter is an hour off
    # on a DST changeover day.
    def at(h, m):
        return int(datetime(2026, 11, 10, h, m, tzinfo=tz).timestamp())
    # relay on 05:00-07:00 (1 h low, then 1 h inside the 06:00-08:00 high window)
    ts = at(5, 0)
    while ts <= at(7, 0):
        store.add({"measured_temp_c": 20.0, "target_temp_c": 22.0,
                   "relay_on": ts < at(7, 0)}, ts=ts)
        ts += 60
    day = {d["day"]: d for d in
           store.daily(1, now=datetime(2026, 11, 10, 12, 0, tzinfo=tz))}["2026-11-10"]
    assert day["low"] == 60 and day["high"] == 60


def test_daily_across_dst_fallback(tmp_path):
    # Autumn fall-back 2026 in Prague is the night of Oct 25 (clocks go back at
    # 03:00). Midnight itself is unambiguous, so a cycle straddling it must be
    # split 30/30 -- which only works if the day boundary uses the real epoch of
    # local midnight, not a fixed 86400 step.
    tz = ZoneInfo("Europe/Prague")
    store = StatsStore(str(tmp_path / "s.db"), TariffSchedule([], "Europe/Prague"), 60)

    def at(d, h, m):
        return int(datetime(2026, 10, d, h, m, tzinfo=tz).timestamp())

    ts, end = at(24, 23, 30), at(25, 0, 30)
    while ts <= end:
        store.add({"measured_temp_c": 50.0, "target_temp_c": 55.0, "relay_on": ts < end}, ts=ts)
        ts += 60
    days = {d["day"]: d for d in
            store.daily(3, now=datetime(2026, 10, 25, 12, 0, tzinfo=tz))}
    assert days["2026-10-24"]["low"] == 30
    assert days["2026-10-25"]["low"] == 30
