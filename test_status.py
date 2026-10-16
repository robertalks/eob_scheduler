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
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    # relay on 05:00-07:00 (1 h low + 1 h high), one reading per minute
    for m in range(5 * 60, 7 * 60 + 1):
        ts = int(today.timestamp()) + m * 60
        store.add({"measured_temp_c": 20.0, "target_temp_c": 22.0,
                   "relay_on": m < 7 * 60}, ts=ts)
    day = store.daily(1)[0]
    assert day["low"] == 60 and day["high"] == 60
