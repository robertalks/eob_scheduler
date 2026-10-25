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

STATUS READ (request_status()/parse_status()):
    The status request is a multi-message (7f ff) wrapping two queries,
    80 02 ("therm settings") and 80 1b ("desired temp"). The device
    answers with frames of the same shape: [type_hi, type_lo, len_hi,
    len_lo, payload...], where len_hi carries a 0x40 flag (real length =
    (len_hi & 0x3f) << 8 | len_lo).

    The 80 02 payload (10 bytes, layout taken from the official app's
    ThermSettingsDataLoader, confirmed 2026-10-16 against the real device:
    0e 00 07 01 08 1e 00 21 00 29 -> 14.7C measured, 15.0C target, relay off):
        p[0]           measured temp, whole degrees
        p[2] & 0x0f    measured temp, tenths      -> 14 + 0.7 = 14.7C
        p[4] bit 7     measured temp is negative
        p[4] bit 0     auto (program) mode
        p[5]           target temp * 2            (0x00 while forced off)
        p[6] bit 6     relay/output ON
        p[6] bit 2     off mode
        p[6] bit 7     temperature sensor error
    The ambient temperature was missed in earlier captures because it is
    NOT half-degree encoded like the target -- it's degrees + a tenths
    nibble.
"""
from __future__ import annotations

import logging
import ssl
import threading
import time

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

# Status frame type (response to the 80 02 query inside the request above).
_THERM_SETTINGS_TYPE = (0x80, 0x02)
_MULTI_MESSAGE_TYPE = (0x7F, 0xFF)

# Legacy fallback: marker for locating the target-temperature byte in a
# status response, used only if frame parsing finds no 80 02 frame.
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


def iter_frames(data: bytes):
    """Yield (type_hi, type_lo, payload) for every frame in `data`,
    descending into 7f ff multi-message wrappers."""
    i = 0
    while i + 4 <= len(data):
        hi, lo = data[i], data[i + 1]
        n = (data[i + 2] & 0x3F) << 8 | data[i + 3]  # 0x40 in len_hi is a flag
        body = data[i + 4:i + 4 + n]
        if len(body) < n:
            return  # truncated / not a frame stream
        if (hi, lo) == _MULTI_MESSAGE_TYPE:
            yield from iter_frames(body)
        else:
            yield hi, lo, body
        i += 4 + n


def _bit(byte: int, n: int) -> bool:
    return bool(byte >> n & 1)


def parse_therm_settings(p: bytes) -> dict:
    """Decode an 80 02 status payload (see module docstring)."""
    if len(p) < 7 or p[0] == 0xFF:
        return {}  # 0xFF = other device layouts (BT722 etc.), not ours
    measured = p[0] + (p[2] & 0x0F) / 10
    if _bit(p[4], 7):
        measured = -measured
    return {
        "target_temp_c": p[5] / 2.0,
        "measured_temp_c": round(measured, 1),
        "relay_on": _bit(p[6], 6),
        "auto_mode": _bit(p[4], 0),
        "off_mode": _bit(p[6], 2),
        "sensor_error": _bit(p[6], 7),
    }


def parse_status(payload: bytes) -> dict:
    """Parse a status push. Returns 'target_temp_c' (confirmed: matches
    whatever was last set via opcode 0x28; 0.0 while forced off) plus
    'measured_temp_c', 'relay_on', 'auto_mode', 'off_mode',
    'sensor_error' when the 80 02 frame is present."""
    for hi, lo, body in iter_frames(payload):
        if (hi, lo) == _THERM_SETTINGS_TYPE:
            parsed = parse_therm_settings(body)
            if parsed:
                return parsed
    # Frame stream didn't line up: look for an 80 02 header anywhere.
    for i in range(len(payload) - 4):
        if payload[i] == 0x80 and payload[i + 1] == 0x02:
            n = (payload[i + 2] & 0x3F) << 8 | payload[i + 3]
            if 8 <= n <= 32 and i + 4 + n <= len(payload):
                parsed = parse_therm_settings(payload[i + 4:i + 4 + n])
                if parsed:
                    return parsed
    # Legacy fallback (target only).
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

    # On a miss, re-publish and wait this much longer before giving up. Status
    # requests go out at QoS 0 with no retain, so a request sent during a brief
    # device/broker disconnect is simply dropped and no reply ever comes -- a
    # longer single wait can't recover that, but a re-publish usually can,
    # because the device has reconnected by then.
    RETRY_WAIT = 5.0

    def __init__(self, unique_id: str, mqtt_password: str, client_id: str | None = None,
                 request_timeout: float = 15.0, request_retries: int = 1):
        self.unique_id = str(unique_id).zfill(6)
        self.username = f"{TOPIC_BASE}_{self.unique_id}"
        self.password = mqtt_password
        self.pub_topic = f"{TOPIC_BASE}/I/{self.unique_id}"
        self.sub_topic = f"{TOPIC_BASE}/O/{self.unique_id}"
        self.request_timeout = float(request_timeout)
        self.request_retries = max(0, int(request_retries))

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
        # The scheduler and the stats collector share this client from
        # different threads; serialize request/response round trips.
        self._request_lock = threading.Lock()
        self.last_status_payload: bytes | None = None
        self.last_status: dict = {}
        self.last_status_time: float | None = None

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
            self.last_status_time = time.time()
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

    def request_status(self, timeout: float | None = None,
                       retries: int | None = None) -> dict:
        """Ask the device for a status push and wait for it, re-publishing a
        few times if nothing comes back (QoS 0, so a request can be lost).
        Returns the parsed dict (see parse_status), or {} if every attempt
        timed out. Logs the final miss at DEBUG -- the stats collector decides
        when a run of misses is worth a WARNING."""
        timeout = self.request_timeout if timeout is None else float(timeout)
        retries = self.request_retries if retries is None else max(0, int(retries))
        with self._request_lock:
            for attempt in range(retries + 1):
                wait = timeout if attempt == 0 else min(timeout, self.RETRY_WAIT)
                self._status_event.clear()
                self._client.publish(self.pub_topic, _STATUS_REQUEST_PAYLOAD, qos=0, retain=False)
                if self._status_event.wait(wait):
                    return dict(self.last_status)
                if attempt < retries:
                    logger.debug("No status in %.1fs, re-publishing (attempt %d/%d)",
                                 wait, attempt + 2, retries + 1)
        logger.debug("No status response after %d attempt(s)", retries + 1)
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

    def get_status(self) -> dict:
        """Full live status (see parse_status): measured_temp_c,
        target_temp_c, relay_on, ... Returns {} if unavailable."""
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

    def get_status(self) -> dict:
        # Crude simulation so --dry-run has something to show on the
        # stats page: heats while a target is set, cools while off.
        now = time.time()
        temp = getattr(self, "_sim_temp", 14.0)
        dt = min(now - getattr(self, "_sim_t", now), 600) / 60
        self._sim_t = now
        target = self._state if isinstance(self._state, float) else 0.0
        on = getattr(self, "_sim_on", False)
        if target and temp < target - 0.5:
            on = True
        elif not target or temp >= target + 0.3:
            on = False
        self._sim_on = on
        self._sim_temp = temp = round(temp + (0.15 if on else -0.02) * dt, 2)
        return {"target_temp_c": target, "measured_temp_c": round(temp, 1),
                "relay_on": on, "auto_mode": False,
                "off_mode": self._state == "off", "sensor_error": False}


class Bt725LiveThermostat(ThermostatClient):
    """Adapts Bt725Client to the ThermostatClient interface, connecting
    once and reusing the MQTT session for the life of the scheduler."""

    def __init__(self, unique_id: str, mqtt_password: str,
                 request_timeout: float = 15.0, request_retries: int = 1):
        self._client = Bt725Client(unique_id, mqtt_password,
                                    request_timeout=request_timeout,
                                    request_retries=request_retries)
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

    def get_status(self) -> dict:
        return self._client.request_status()


def build_client(config: dict) -> ThermostatClient:
    t_cfg = config["thermostat"]
    mode = t_cfg.get("mode", "live")
    if mode == "mock":
        return MockThermostat()
    if mode == "live":
        return Bt725LiveThermostat(
            unique_id=t_cfg["unique_id"],
            mqtt_password=t_cfg["mqtt_password"],
            request_timeout=float(t_cfg.get("request_timeout_seconds", 15.0)),
            request_retries=int(t_cfg.get("request_retries", 1)),
        )
    raise ValueError(f"Unknown thermostat mode: {mode!r}")
