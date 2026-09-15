"""Driver for the Elektrobock BT725 WiFi thermostat, reverse-engineered
from decrypted captures of the official EOB PT-WiFi app's traffic.

Protocol summary (confirmed from real captures):
  - Transport: real MQTT 3.1.1 over TLS, broker mqtt.elbock.cz:8883
  - MQTT username: "PTWIFI_<uniqueId padded to 6 digits>"
  - MQTT password: a per-device secret ("mqttPass" in the app's terms) --
    it's stable, not per-session, but IS device/account-specific. Yours is
    filled into config.yaml, not hardcoded here.
  - App -> device commands are published (QoS 0, no retain) to
    "PTWIFI/I/<uniqueId>"
  - Device -> app status arrives on "PTWIFI/O/<uniqueId>" (app subscribes
    to this)

Design (as of 2026-09-13): the thermostat has its own local ambient
sensor and does its own on/off (hysteresis) decision-making locally --
that logic is NOT something this client needs to replicate or read back.
This client only needs to push two things, both independently confirmed
against the real device:

SET TARGET TEMPERATURE (confirmed):
    7f ff 00 07 00 02 00 03 01 28 <value>
    <value> = round(temp_C * 2). Confirmed via a capture of the app
    itself setting 15.0C -> 16.0C -> 17.0C -> 15.0C: produced exactly
    0x1e/0x20/0x22/0x1e. Use this for the low-tariff period -- the
    thermostat's own local sensor decides when to actually heat.

POWER OFF (confirmed):
    7f ff 00 07 00 02 00 03 01 2c 00
    Confirmed via direct observation: sending this made the real relay
    physically click off. Use this for the high-tariff period -- it does
    not merely lower a setpoint the ambient could still cross, it
    disables the output outright.

    CONFIRMED 2026-09-14: calling set_temperature() again after this
    reliably resumes normal ambient-based operation -- tested against the
    real device via the off -> set_temperature sequence (the app showed
    the new target and the thermostat resumed normal operation, not
    stuck off).

    (0x2c with other values, e.g. 0x4e, was also observed to force the
    relay on in early testing -- but 0x2c is NOT a continuous
    temperature-setter the way 0x28 is; an earlier version of this file
    wrongly assumed that and was corrected. Only the confirmed 0x00 "off"
    use is exposed here.)

STATUS READ: still supported (request_status()/parse_status()) for
target_temp_c only. Actual/ambient temperature was searched for
extensively across several captures and not found -- most likely because
the ambient sensor loop is entirely local to the device and never
reported over MQTT. Not something this client relies on.
"""
from __future__ import annotations

import logging
import ssl
import threading

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

MQTT_BROKER_HOST = "mqtt.elbock.cz"
MQTT_BROKER_PORT = 8883
MQTT_KEEPALIVE = 15  # matches what the official app used

TOPIC_BASE = "PTWIFI"

# Confirmed from capture: the fixed prefix of the "set" command, then
# [opcode][value].
_SET_PREFIX = bytes.fromhex("7fff00070002000301")
_SET_TEMPERATURE_OPCODE = 0x28  # confirmed: sets target temperature
_POWER_OFF_OPCODE = 0x2C        # confirmed (value 0x00 only): powers off

# Confirmed from capture: publishing this exact payload requests a status
# push from the device.
_STATUS_REQUEST_PAYLOAD = bytes.fromhex("7fff000880020000801b0000")

# Marker for locating the target-temperature byte in a status response.
# `None` marks a position that's genuinely variable (confirmed by
# comparing multiple real captures -- see eob_lib.py history/README for
# how this was found). Everything else here has been constant across
# every sample collected so far:
#   80 02 40 0a [varies] 00 [varies] 01 08 <target_temp_byte>
_STATUS_MARKER_PATTERN = [0x80, 0x02, 0x40, 0x0A, None, 0x00, None, 0x01, 0x08]

# Half-degree encoding: value_byte = round(temp_C * 2). Directly confirmed
# for 10.0-19.0C; the wider range below is a conservative guess beyond
# that -- narrow it down with eob_client.py --test if you rely on values
# near the edges.
TEMP_MIN_C = 5.0
TEMP_MAX_C = 39.0


def temperature_to_byte(temp_c: float) -> int:
    if not (TEMP_MIN_C <= temp_c <= TEMP_MAX_C):
        raise ValueError(
            f"Temperature {temp_c}C is outside the assumed safe range "
            f"[{TEMP_MIN_C}, {TEMP_MAX_C}]C. If you're confident your "
            f"device supports it, adjust TEMP_MIN_C/TEMP_MAX_C in "
            f"eob_lib.py after confirming with eob_client.py --test."
        )
    return int(round(temp_c * 2)) & 0xFF


def _build_set_temperature_command(temp_c: float) -> bytes:
    return _SET_PREFIX + bytes([_SET_TEMPERATURE_OPCODE, temperature_to_byte(temp_c)])


def _build_power_off_command() -> bytes:
    return _SET_PREFIX + bytes([_POWER_OFF_OPCODE, 0x00])


def _find_status_block(payload: bytes) -> int | None:
    """Returns the index of the target-temperature byte, or None if the
    pattern isn't found. `None` entries in _STATUS_MARKER_PATTERN are
    wildcards -- positions confirmed to vary between real captures."""
    plen = len(_STATUS_MARKER_PATTERN)
    for i in range(len(payload) - plen):
        window = payload[i:i + plen]
        if all(expected is None or actual == expected
               for actual, expected in zip(window, _STATUS_MARKER_PATTERN)):
            return i + plen
    return None


def parse_status(payload: bytes) -> dict:
    """Best-effort parse of a status push. Returns 'target_temp_c'
    (confirmed: matches whatever was last set via opcode 0x28). No
    actual/ambient temperature field -- see module docstring."""
    result: dict = {}
    block_start = _find_status_block(payload)
    if block_start is None:
        return result
    block = payload[block_start:block_start + 1]
    if len(block) < 1:
        return result
    result["target_temp_c"] = block[0] / 2.0
    return result


class Bt725Client:
    """Direct MQTT client for one BT725 thermostat."""

    def __init__(self, unique_id: str, mqtt_password: str, client_id: str | None = None):
        self.unique_id = str(unique_id).zfill(6)
        self.username = f"{TOPIC_BASE}_{self.unique_id}"
        self.password = mqtt_password
        self.pub_topic = f"{TOPIC_BASE}/I/{self.unique_id}"
        self.sub_topic = f"{TOPIC_BASE}/O/{self.unique_id}"

        self._client = mqtt.Client(
            client_id=client_id or "",
            protocol=mqtt.MQTTv311,
        )
        self._client.username_pw_set(self.username, self.password)
        self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message

        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._status_event = threading.Event()
        self.last_status_payload: bytes | None = None
        self.last_status: dict = {}

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to %s, subscribing to %s", MQTT_BROKER_HOST, self.sub_topic)
            client.subscribe(self.sub_topic, qos=0)
            self._connected.set()
        else:
            logger.error("MQTT connect failed, rc=%s", rc)

    def _on_subscribe(self, client, userdata, mid, granted_qos):
        # Confirms the broker has actually activated our subscription --
        # only after this can we be sure a device reply won't be missed.
        # Without waiting for this, connect() could return before the
        # subscription is live, silently dropping a fast reply to any
        # request sent immediately afterward (e.g. request_status()).
        self._subscribed.set()

    def _on_message(self, client, userdata, msg):
        logger.debug("Status update on %s: %s", msg.topic, msg.payload.hex())
        self.last_status_payload = msg.payload
        parsed = parse_status(msg.payload)
        if parsed:
            self.last_status.update(parsed)
            self._status_event.set()

    def connect(self, timeout: float = 10.0) -> None:
        self._client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=MQTT_KEEPALIVE)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            self._client.loop_stop()
            raise TimeoutError(f"Could not connect to {MQTT_BROKER_HOST} within {timeout}s")
        if not self._subscribed.wait(timeout):
            self._client.loop_stop()
            raise TimeoutError(
                f"Connected but subscription to {self.sub_topic} was not "
                f"confirmed within {timeout}s"
            )

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
        self._connected.clear()
        self._subscribed.clear()

    def set_temperature(self, temp_c: float) -> None:
        payload = _build_set_temperature_command(temp_c)
        logger.info(
            "Sending SET TEMPERATURE %.1fC to %s: %s",
            temp_c, self.pub_topic, payload.hex(),
        )
        self._client.publish(self.pub_topic, payload, qos=0, retain=False)

    def power_off(self) -> None:
        payload = _build_power_off_command()
        logger.info(
            "Sending POWER OFF to %s: %s",
            self.pub_topic, payload.hex(),
        )
        self._client.publish(self.pub_topic, payload, qos=0, retain=False)

    def request_status(self, timeout: float = 10.0) -> dict:
        """Ask the device for a status push and wait (briefly) for it.
        Returns the parsed dict (see parse_status), or {} on timeout."""
        self._status_event.clear()
        self._client.publish(self.pub_topic, _STATUS_REQUEST_PAYLOAD, qos=0, retain=False)
        if self._status_event.wait(timeout):
            return dict(self.last_status)
        logger.warning("No status response within %.1fs", timeout)
        return {}


# --- ThermostatClient-compatible wrapper, for eob_scheduler.py -------------

class ThermostatClient:
    """Base interface eob_scheduler.py expects."""

    def set_temperature(self, temp_c: float) -> None:
        raise NotImplementedError

    def power_off(self) -> None:
        raise NotImplementedError

    def get_last_state(self):
        """Returns either a float (last target temp) or the string 'off'."""
        raise NotImplementedError

    def get_current_target_temperature(self) -> float | None:
        """Reads the device's OWN idea of its current target temperature
        (e.g. whatever the user last set via the official app), regardless
        of what this client last sent. Returns None if unavailable."""
        raise NotImplementedError


class MockThermostat(ThermostatClient):
    def __init__(self):
        self._state = None

    def set_temperature(self, temp_c: float) -> None:
        logger.info("[MOCK] set_temperature(%.1fC)", temp_c)
        self._state = temp_c

    def power_off(self) -> None:
        logger.info("[MOCK] power_off()")
        self._state = "off"

    def get_last_state(self):
        return self._state

    def get_current_target_temperature(self) -> float | None:
        return None  # no real device to read from in mock mode


class Bt725LiveThermostat(ThermostatClient):
    """Adapts Bt725Client to the ThermostatClient interface, connecting
    once and reusing the MQTT session for the life of the scheduler."""

    def __init__(self, unique_id: str, mqtt_password: str):
        self._client = Bt725Client(unique_id, mqtt_password)
        self._client.connect()
        self._state = None

    def set_temperature(self, temp_c: float) -> None:
        self._client.set_temperature(temp_c)
        self._state = temp_c

    def power_off(self) -> None:
        self._client.power_off()
        self._state = "off"

    def get_last_state(self):
        return self._state

    def get_current_target_temperature(self) -> float | None:
        status = self._client.request_status()
        return status.get("target_temp_c")


def build_client(config: dict) -> ThermostatClient:
    t_cfg = config["thermostat"]
    mode = t_cfg.get("mode", "live")
    if mode == "mock":
        return MockThermostat()
    if mode == "live":
        return Bt725LiveThermostat(
            unique_id=t_cfg["unique_id"],
            mqtt_password=t_cfg["mqtt_password"],
        )
    raise ValueError(f"Unknown thermostat mode: {mode!r}")
