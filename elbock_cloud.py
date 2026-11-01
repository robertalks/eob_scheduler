"""Fetch a device's MQTT id and password from your Elektrobock account, the
same way the official app does -- so you don't have to capture them from the
app's traffic (see CAPTURE_GUIDE.md).

Flow, taken from the decompiled app:
  1. POST  https://data.elektrobock.cz/api/Auth/login   {"UserName","Password"}
        -> {"AuthToken": <JWT>, "Id": <userId>, "Login", ...}
  2. GET   https://data.elektrobock.cz/api/UsersDevices/<userId>  (Bearer token)
        -> [ { "UniqueIdentifier", "MqttPass", "DeviceType", "Name", ... }, ... ]

The MQTT id is `UniqueIdentifier`; the MQTT password is `MqttPass`. The MQTT
username the scheduler uses is derived from the id (e.g. "PTWIFI_<id>").

Only the standard library is used (urllib). The account password is used once,
in memory, to get a JWT -- it is never written anywhere by this module.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

# Overridable for testing against a local mock server.
BASE_URL = os.environ.get("EOB_DATA_API", "https://data.elektrobock.cz").rstrip("/")

_TIMEOUT = 20


class CloudError(Exception):
    """A login or lookup failure worth showing to the user."""


@dataclass
class CloudDevice:
    unique_id: str
    mqtt_password: str
    name: str
    device_type: str

    @property
    def mqtt_username(self) -> str:
        # The BT725 / PT-WiFi family uses the "PTWIFI" prefix; other device
        # types use a different one, but the scheduler only drives PT-WiFi.
        return f"PTWIFI_{self.unique_id}"


def _ci(d: dict, *names: str, default=None):
    """Case-insensitive lookup -- the API's JSON casing (PascalCase vs
    camelCase) isn't guaranteed, so accept either."""
    if isinstance(d, dict):
        lowered = {k.lower(): v for k, v in d.items()}
        for n in names:
            v = lowered.get(n.lower())
            if v is not None:
                return v
    return default


def _request(path: str, *, method: str, token: str | None = None,
             body: dict | None = None):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise CloudError("Login rejected -- check your email and password.") from e
        raise CloudError(f"Server returned HTTP {e.code} for {path}.") from e
    except urllib.error.URLError as e:
        raise CloudError(f"Could not reach {BASE_URL}: {e.reason}") from e
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise CloudError(f"Unexpected (non-JSON) response from {path}.") from e


def login(email: str, password: str) -> tuple[str, str]:
    """Return (JWT token, account user id)."""
    result = _request("/api/Auth/login", method="POST",
                      body={"UserName": email, "Password": password})
    token = _ci(result or {}, "AuthToken", "authToken", "token", "accessToken")
    user_id = _ci(result or {}, "Id", "id", "userId")
    if not token:
        raise CloudError("Login succeeded but no token was returned.")
    if user_id is None:
        raise CloudError("Login succeeded but no account id was returned.")
    return str(token), str(user_id)


def list_devices(token: str, user_id: str) -> list[CloudDevice]:
    """Return the account's devices, each with its MQTT id and password."""
    result = _request(f"/api/UsersDevices/{user_id}", method="GET", token=token)
    # The endpoint returns a JSON array; tolerate a wrapper object too.
    rows = result if isinstance(result, list) else _ci(result or {}, "devices", "items", default=[])
    devices = []
    for r in rows or []:
        uid = _ci(r, "UniqueIdentifier", "uniqueIdentifier")
        mqtt = _ci(r, "MqttPass", "mqttPass", "mqttPassword")
        if uid is None or mqtt is None:
            continue
        devices.append(CloudDevice(
            unique_id=str(uid).zfill(6),
            mqtt_password=str(mqtt),
            name=str(_ci(r, "Name", "name", default="") or ""),
            device_type=str(_ci(r, "DeviceType", "deviceType", default="") or ""),
        ))
    return devices


def fetch_credentials(email: str, password: str) -> list[CloudDevice]:
    """Log in and return the account's devices (id + MQTT password)."""
    token, user_id = login(email, password)
    return list_devices(token, user_id)
