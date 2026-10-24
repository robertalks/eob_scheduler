"""Controller: app-set target, persistence, and how it interacts with tariff
transitions. Uses the mock thermostat; no network."""
import json

import pytest

from eob_lib import MockThermostat
from eob_scheduler import ControlError, Controller


class FakeSchedule:
    def __init__(self, low):
        self.low = low

    def is_low_tariff(self, dt=None):
        return self.low


class DeviceReading(MockThermostat):
    """Mock that also reports a 'current target', like the real device."""

    def __init__(self, reading=None):
        super().__init__()
        self.reading = reading
        self.sent = []

    def set_temperature(self, temp_c):
        super().set_temperature(temp_c)
        self.sent.append(temp_c)

    def power_off(self):
        super().power_off()
        self.sent.append("off")

    def get_current_target_temperature(self):
        return self.reading


def make(tmp_path, low=True, source="config", reading=None, enabled=True):
    sched = FakeSchedule(low)
    client = DeviceReading(reading)
    c = Controller(sched, client, 15.0, source, str(tmp_path / "state.json"),
                   {"enabled": enabled, "token": "secret"})
    return c, sched, client


def test_set_during_low_applies_now_and_persists(tmp_path):
    c, _, client = make(tmp_path)
    c.tick()                                   # -> low tariff, config target 15
    assert client.sent == [15.0]
    r = c.set_target(18.3)                     # rounded to the device's 0.5 steps
    assert r == {"ok": True, "target": 18.5, "applied_now": True}
    assert client.sent[-1] == 18.5
    assert json.load(open(tmp_path / "state.json")) == {"user_target": 18.5}


def test_set_during_high_waits_for_low(tmp_path):
    c, sched, client = make(tmp_path, low=False)
    c.tick()                                   # -> high tariff: off
    r = c.set_target(20)
    assert r["applied_now"] is False and client.sent == ["off"]
    sched.low = True
    c.tick()                                   # next low tariff uses the app target
    assert client.sent[-1] == 20.0


def test_survives_restart(tmp_path):
    c, _, _ = make(tmp_path, low=False)
    c.tick()
    c.set_target(21)
    c2, sched2, client2 = make(tmp_path, low=True)
    assert c2.state.user_target == 21.0
    c2.tick()
    assert client2.sent == [21.0]


def test_device_change_wins_at_next_off(tmp_path):
    # temperature_source=device: a change made in the Elektrobock app while on
    # replaces the app-set target (most recent change wins).
    c, sched, client = make(tmp_path, source="device", reading=17.0)
    c.tick()
    c.set_target(19)
    client.reading = 22.0                      # changed on the device meanwhile
    sched.low = False
    c.tick()
    assert c.state.user_target == 22.0
    assert json.load(open(tmp_path / "state.json"))["user_target"] == 22.0


def test_clear_returns_to_config(tmp_path):
    c, _, client = make(tmp_path)
    c.tick()
    c.set_target(19)
    r = c.clear_target()
    assert r["target"] == 15.0 and client.sent[-1] == 15.0
    assert c.info()["source"] == "config"


def test_validation_and_disabled(tmp_path):
    c, _, _ = make(tmp_path)
    for bad in (4.5, 39.5, "abc", float("nan"), None):
        with pytest.raises(ControlError):
            c.set_target(bad)
    off, _, _ = make(tmp_path, enabled=False)
    with pytest.raises(ControlError) as e:
        off.set_target(18)
    assert e.value.status == 403


def test_token_check(tmp_path):
    c, _, _ = make(tmp_path)
    assert c.check_token("secret")
    assert not c.check_token("wrong") and not c.check_token(None)
    no_token = Controller(FakeSchedule(True), DeviceReading(), 15.0, "config", None,
                          {"enabled": True, "token": ""})
    assert not no_token.enabled and not no_token.check_token("")
