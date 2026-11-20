"""Shelly power meter integration.

Polls one or more Shelly devices (Gen1 or Gen2/3) over HTTP on a per-device
schedule defined in config.yaml under `power_meters:`. Readings are stored in
the existing stats SQLite DB (new `power_readings` table). Exposes:

  GET  /api/power/status          current readings per device
  GET  /api/power/daily?days=14   daily kWh per device
  POST /api/power/refresh         force an immediate poll (no auth required)

If `power_meters:` is absent or empty, the feature is completely inactive.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_TIMEOUT = 5  # seconds for all Shelly HTTP requests


def _parse_update_interval(value) -> int:
    """Parse update interval: "30m" -> 1800, "60m" -> 3600, 5 -> 5 (seconds)."""
    s = str(value).strip()
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    return int(s)


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read())


class ShellyDevice:
    """Tracks one configured Shelly device: gen detection, polling, storage.

    Supported device families (auto-detected at first poll):
      "gen1_switch"  — Gen1 plug/switch: GET /status → meters[0].{power, total}
      "gen2_switch"  — Gen2/3 plug/switch: GET /rpc/Switch.GetStatus?id=0 → {apower, aenergy}
      "gen2_em"      — Gen2/3 energy monitor (e.g. Pro 3EM):
                         GET /rpc/EM.GetStatus?id=0     → total_act_power (W)
                         GET /rpc/EMData.GetStatus?id=0 → total_act (Wh)
    """

    def __init__(self, name: str, address: str, update_seconds: int):
        self.name = name
        self.address = address
        self.update_seconds = update_seconds
        self._kind: str | None = None  # one of the family strings above
        self._lock = threading.Lock()
        self._last_reading: dict | None = None  # {"ts", "power_w", "total_kwh"}
        self._timer: threading.Timer | None = None
        self._stopped = False

    def _detect(self) -> str | None:
        """Probe the device and return its family string, or None on failure."""
        base = f"http://{self.address}"

        # --- Gen2/3: /rpc/ namespace is present ---
        try:
            _fetch_json(f"{base}/rpc/Shelly.GetDeviceInfo")
            # It's Gen2/3. Now distinguish switch vs energy monitor.
            try:
                data = _fetch_json(f"{base}/rpc/Switch.GetStatus?id=0")
                if "apower" in data or "aenergy" in data:
                    logger.info("shelly[%s]: detected Gen2/3 switch/plug", self.name)
                    return "gen2_switch"
            except Exception:
                pass
            try:
                data = _fetch_json(f"{base}/rpc/EM.GetStatus?id=0")
                if "total_act_power" in data:
                    logger.info("shelly[%s]: detected Gen2/3 energy monitor (EM)", self.name)
                    return "gen2_em"
            except Exception:
                pass
            logger.warning("shelly[%s]: Gen2/3 device at %s but no recognised power endpoint "
                           "(tried Switch.GetStatus and EM.GetStatus)", self.name, self.address)
            return None
        except Exception as exc:
            logger.debug("shelly[%s]: not Gen2/3 (%s), trying Gen1", self.name, exc)

        # --- Gen1: /status with meters ---
        try:
            data = _fetch_json(f"{base}/status")
            if "meters" in data:
                logger.info("shelly[%s]: detected Gen1 switch/plug", self.name)
                return "gen1_switch"
            logger.debug("shelly[%s]: /status responded but no meters key — keys: %s",
                         self.name, list(data.keys()))
        except Exception as exc:
            logger.debug("shelly[%s]: Gen1 probe failed: %s", self.name, exc)

        logger.warning("shelly[%s]: could not detect device type at %s — "
                       "check the address and that the device is reachable.",
                       self.name, self.address)
        return None

    def _detect_and_read(self) -> dict | None:
        """Auto-detect device family on first call, then read current state.
        Returns {"power_w": float, "total_kwh": float} or None on failure."""
        if self._kind is None:
            self._kind = self._detect()
        if self._kind is None:
            return None

        base = f"http://{self.address}"
        try:
            if self._kind == "gen2_switch":
                data = _fetch_json(f"{base}/rpc/Switch.GetStatus?id=0")
                power_w = float(data.get("apower") or 0.0)
                total_kwh = float((data.get("aenergy") or {}).get("total") or 0.0) / 1000.0
            elif self._kind == "gen2_em":
                em = _fetch_json(f"{base}/rpc/EM.GetStatus?id=0")
                emd = _fetch_json(f"{base}/rpc/EMData.GetStatus?id=0")
                power_w = float(em.get("total_act_power") or 0.0)
                total_kwh = float(emd.get("total_act") or 0.0) / 1000.0
            else:  # gen1_switch
                data = _fetch_json(f"{base}/status")
                meters = data.get("meters") or [{}]
                power_w = float(meters[0].get("power") or 0.0)
                # Gen1 total is in Watt-minutes
                total_kwh = float(meters[0].get("total") or 0.0) / 60.0 / 1000.0
            return {"power_w": power_w, "total_kwh": total_kwh}
        except Exception as exc:
            logger.warning("shelly[%s]: read failed: %s", self.name, exc)
            # Reset kind so the next poll re-detects (handles firmware reboots).
            self._kind = None
            return None

    def poll(self, store: "ShellyStore") -> None:
        """Read device, store result, schedule next poll."""
        reading = self._detect_and_read()
        ts = int(time.time())
        if reading is not None:
            store.add(ts, self.name, reading["power_w"], reading["total_kwh"])
            with self._lock:
                self._last_reading = {"ts": ts, **reading}
            logger.debug("shelly[%s]: %.1f W, %.4f kWh total", self.name,
                         reading["power_w"], reading["total_kwh"])
        self._schedule_next(store)

    def _schedule_next(self, store: "ShellyStore") -> None:
        with self._lock:
            if self._stopped:
                return
            self._timer = threading.Timer(self.update_seconds, self.poll, args=(store,))
            self._timer.daemon = True
            self._timer.start()

    def start(self, store: "ShellyStore") -> None:
        with self._lock:
            self._stopped = False
        # First poll fires immediately
        t = threading.Thread(target=self.poll, args=(store,), daemon=True)
        t.start()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def force_refresh(self, store: "ShellyStore") -> None:
        """Cancel pending timer and poll immediately."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        t = threading.Thread(target=self.poll, args=(store,), daemon=True)
        t.start()

    def last_reading(self) -> dict | None:
        with self._lock:
            return dict(self._last_reading) if self._last_reading else None


class ShellyStore:
    """SQLite persistence for power readings. Shares the stats db file."""

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS power_readings(
              ts INTEGER NOT NULL,
              device TEXT NOT NULL,
              power_w REAL,
              total_kwh REAL,
              PRIMARY KEY (ts, device)
            );
        """)

    def add(self, ts: int, device: str, power_w: float, total_kwh: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO power_readings VALUES(?,?,?,?)",
                (ts, device, power_w, total_kwh))
            self._db.commit()

    def latest(self, device: str) -> tuple | None:
        """Returns (ts, power_w, total_kwh) or None."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, power_w, total_kwh FROM power_readings "
                "WHERE device=? ORDER BY ts DESC LIMIT 1", (device,)).fetchall()
        return rows[0] if rows else None

    def daily(self, device: str, days: int, tz, update_seconds: int,
              now: datetime | None = None) -> list[dict]:
        """Delta-based daily kWh for the last `days` days.
        Counter resets (total decreasing) are skipped, not subtracted.
        `now` overrides the clock (for tests)."""
        base = (now or datetime.now(tz)).astimezone(tz)
        today = base.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=days - 1)
        # Pull a little before start to catch the first delta of the first day
        since = int(start.timestamp()) - update_seconds

        with self._lock:
            rows = self._db.execute(
                "SELECT ts, total_kwh FROM power_readings "
                "WHERE device=? AND ts>=? ORDER BY ts", (device, since)).fetchall()

        out = {}
        for d in range(days):
            k = (start + timedelta(days=d)).strftime("%Y-%m-%d")
            out[k] = 0.0

        for (t0, kwh0), (t1, kwh1) in zip(rows, rows[1:]):
            if kwh1 <= kwh0:
                continue  # counter reset or no change: skip
            delta = kwh1 - kwh0
            day_key = datetime.fromtimestamp(t0, tz).strftime("%Y-%m-%d")
            if day_key in out:
                out[day_key] += delta

        return [{"day": k, "kwh": round(v, 4)} for k, v in out.items()]


class ShellyPoller:
    """Owns N ShellyDevice instances, each polling on its own schedule."""

    def __init__(self, devices: list[ShellyDevice], store: ShellyStore, tz):
        self._devices = devices
        self._store = store
        self._tz = tz

    @classmethod
    def from_config(cls, config: dict, db_path: str, tz) -> "ShellyPoller | None":
        meters = config.get("power_meters") or []
        if not meters:
            return None
        store = ShellyStore(db_path)
        devices = []
        for m in meters:
            name = str(m["name"])
            address = str(m["address"])
            update_seconds = _parse_update_interval(m.get("update", "60m"))
            devices.append(ShellyDevice(name, address, update_seconds))
        return cls(devices, store, tz)

    def start(self) -> None:
        for dev in self._devices:
            dev.start(self._store)

    def stop(self) -> None:
        for dev in self._devices:
            dev.stop()

    def refresh(self, name: str | None = None) -> dict:
        """Force-poll one or all devices. Returns immediately (poll runs in background)."""
        targets = ([d for d in self._devices if d.name == name] if name
                   else list(self._devices))
        if name and not targets:
            return {"error": f"Unknown device: {name!r}"}
        for dev in targets:
            dev.force_refresh(self._store)
        refreshed = [d.name for d in targets]
        return {"ok": True, "refreshed": refreshed}

    def status(self) -> dict:
        now = int(time.time())
        result = []
        for dev in self._devices:
            # Try in-memory first; fall back to DB
            reading = dev.last_reading()
            if reading is None:
                row = self._store.latest(dev.name)
                if row:
                    reading = {"ts": row[0], "power_w": row[1], "total_kwh": row[2]}
            if reading:
                age = now - reading["ts"]
                ok = age < 3 * dev.update_seconds
                result.append({
                    "name": dev.name,
                    "address": dev.address,
                    "update_seconds": dev.update_seconds,
                    "last_seen": reading["ts"],
                    "age": age,
                    "power_w": reading["power_w"],
                    "total_kwh": reading["total_kwh"],
                    "ok": ok,
                })
            else:
                result.append({
                    "name": dev.name,
                    "address": dev.address,
                    "update_seconds": dev.update_seconds,
                    "last_seen": None,
                    "age": None,
                    "power_w": None,
                    "total_kwh": None,
                    "ok": False,
                })
        return {"devices": result}

    def daily(self, days: int) -> dict:
        result = []
        for dev in self._devices:
            days_data = self._store.daily(dev.name, days, self._tz, dev.update_seconds, now=None)
            result.append({"name": dev.name, "days": days_data})
        return {"devices": result}
