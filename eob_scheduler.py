"""Daemon: polls the tariff schedule and drives the thermostat accordingly.

During low tariff: sets a target temperature and lets the thermostat's
own local ambient sensor decide when to actually heat. The target comes
from one of two sources, controlled by config.yaml's `temperature_source`:
  - "config" (default): always use the fixed `target_temperature` value.
  - "device": use whatever temperature was ACTUALLY RUNNING right before
    the most recent high-tariff (off) period started -- captured by
    reading the device's current target right as we're about to powers it
    off, then reapplying that same snapshot on the next resume. This
    deliberately does NOT pick up changes made through the app while the
    device was off (that state isn't "what was running", so it's
    intentionally ignored) -- only a change made while genuinely on, that
    was still in effect at the moment of the next off-transition, counts.
    `target_temperature` still acts as a fallback if no snapshot is
    available yet (e.g. the very first run, before any off-transition has
    happened, or a failed status request).

    IMPORTANT (found 2026-09-15): the device reports 0.0C as its live
    target_temp_c reading whenever it's genuinely off -- this isn't a
    read failure, it's a real value that just means "off", and using it
    as a target crashes (0.0C is outside the valid range). So every live
    read in this file is checked with _is_plausible_target() before
    being trusted; a 0.0C (or otherwise out-of-range) reading is treated
    exactly like "no reading available" and falls through to the next
    fallback instead.

During high tariff: sends a genuine power-off command, so nothing runs
regardless of ambient temperature. This uses a different underlying
command than set_temperature, so the device's own "last target" memory
*would* survive being turned off in principle -- but in practice, a live
read while off just returns 0.0C (see above), so this client never
relies on reading live status while the device is known to be off; it
only reads right before forcing off (while still genuinely on) or as a
best-effort guess on a true cold start, always through the same
plausibility check.

Usage:
    python eob_scheduler.py                 # run for real, using config.yaml
    python eob_scheduler.py --dry-run       # log what it would do, touch nothing
    python eob_scheduler.py --config other.yaml
    python eob_scheduler.py --once          # check once and exit (for cron)

If config.yaml has `stats: enabled: true`, the scheduler also records the
thermostat's measured temperature / relay state and serves a small stats
dashboard over HTTP -- see eob_stats.py. It reuses the scheduler's own
MQTT connection and TariffSchedule.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import math
import os
import sys
import threading
import time as time_module
from dataclasses import dataclass, replace

import yaml

import eob_stats
from eob_lib import build_client, TEMP_MAX_C, TEMP_MIN_C
from eob_tariff import TariffSchedule


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_file: str, dry_run: bool) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s]%(message)s"
        if dry_run
        else "%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


def _is_plausible_target(temp: float | None) -> bool:
    """A live target_temp_c reading is only meaningful if it's a real
    value the device could actually be set to. 0.0C (and anything below
    TEMP_MIN_C) means the device is off, not "targeting 0 degrees" --
    treat that the same as no reading at all."""
    return temp is not None and temp >= TEMP_MIN_C


@dataclass
class SchedulerState:
    last_is_low: bool | None = None
    # Snapshot of the device's target temperature, taken right as it was
    # switched off (i.e. what was genuinely running beforehand). Only
    # meaningful/used when temperature_source == "device".
    last_on_target: float | None = None
    # Low-tariff target set from EOBApp / the control API. When set, it wins
    # over both temperature sources; persisted across restarts (state_file).
    user_target: float | None = None


def run_once(
    schedule: TariffSchedule,
    client,
    fallback_temp: float,
    temperature_source: str,
    state: SchedulerState,
) -> SchedulerState:
    """Check current tariff state, push a change to the thermostat if the
    low/high tariff state actually changed. Returns the new state."""
    is_low = schedule.is_low_tariff()
    if is_low == state.last_is_low:
        return state

    if is_low:
        if state.user_target is not None:
            target_temp = state.user_target
            logging.info("Using the target set from the app: %.1fC.", target_temp)
        elif temperature_source == "device":
            if state.last_on_target is not None:
                target_temp = state.last_on_target
                logging.info(
                    "temperature_source=device: reapplying %.1fC snapshotted "
                    "from before the last off period.", target_temp,
                )
            else:
                # No snapshot yet -- try a live read as a best-effort
                # guess, but only trust it if it's a plausible real value
                # (see _is_plausible_target: 0.0C means the device is
                # currently off, not a genuine target to reapply).
                current = client.get_current_target_temperature()
                if _is_plausible_target(current):
                    target_temp = current
                    logging.info(
                        "temperature_source=device: no snapshot yet, read "
                        "%.1fC currently on the device, using that.",
                        current,
                    )
                else:
                    target_temp = fallback_temp
                    logging.warning(
                        "temperature_source=device: no snapshot and no "
                        "plausible live reading available (got %s), "
                        "falling back to config target_temperature=%.1fC.",
                        f"{current:.1f}C" if current is not None else "none",
                        fallback_temp,
                    )
        else:
            target_temp = fallback_temp
        logging.info(
            "Tariff state changed -> low tariff: set target temperature = %.1fC",
            target_temp,
        )
        try:
            client.set_temperature(target_temp)
        except NotImplementedError as e:
            logging.error("Thermostat driver not implemented yet: %s", e)
            return state
        except Exception:
            logging.exception("Failed to update thermostat; will retry next poll")
            return state
        return replace(state, last_is_low=True)

    else:
        new_snapshot = state.last_on_target
        new_user_target = state.user_target
        if temperature_source == "device":
            current = client.get_current_target_temperature()
            if _is_plausible_target(current):
                new_snapshot = current
                if new_user_target is not None and abs(current - new_user_target) >= 0.25:
                    # Changed on the device (e.g. in the Elektrobock app) after
                    # it was set from EOBApp: the most recent change wins.
                    logging.info(
                        "Target was changed on the device (%.1fC -> %.1fC); "
                        "following it.", new_user_target, current,
                    )
                    new_user_target = current
                logging.info(
                    "temperature_source=device: snapshotting %.1fC (what was "
                    "running) before forcing off.", current,
                )
            else:
                logging.info(
                    "temperature_source=device: no plausible reading to "
                    "snapshot before forcing off (got %s) -- keeping "
                    "previous snapshot (%s).",
                    f"{current:.1f}C" if current is not None else "none",
                    f"{state.last_on_target:.1f}C" if state.last_on_target is not None else "none",
                )
        logging.info("Tariff state changed -> high tariff: power OFF")
        try:
            client.power_off()
        except NotImplementedError as e:
            logging.error("Thermostat driver not implemented yet: %s", e)
            return state
        except Exception:
            logging.exception("Failed to update thermostat; will retry next poll")
            return state
        return replace(state, last_is_low=False, last_on_target=new_snapshot,
                       user_target=new_user_target)


class ControlError(Exception):
    """A rejected control request; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Controller:
    """Owns the scheduler state, shared by the scheduler loop and the control
    API (eob_stats web server, used by EOBApp). One lock serializes them, so a
    tariff transition and an app request can never interleave their commands
    to the thermostat.

    The app-set target (state.user_target) is persisted to `state_file` so it
    survives restarts -- including a restart during high tariff, when the
    device itself reports 0.0C and can't tell us what to resume with.
    """

    STEP = 0.5  # the device encodes half degrees

    def __init__(self, schedule, client, fallback_temp: float, temperature_source: str,
                 state_file: str | None, control_cfg: dict | None = None):
        self.schedule = schedule
        self.client = client
        self.fallback_temp = fallback_temp
        self.temperature_source = temperature_source
        self.state_file = state_file
        cfg = control_cfg or {}
        self.token = str(cfg.get("token") or "")
        self.enabled = bool(cfg.get("enabled", False)) and bool(self.token)
        if cfg.get("enabled") and not self.token:
            logging.warning("control.enabled is set but control.token is empty -- "
                            "remote control stays disabled.")
        # Config can only narrow the range eob_lib considers safe, never widen it.
        self.min_temp = max(TEMP_MIN_C, float(cfg.get("min_temperature", TEMP_MIN_C)))
        self.max_temp = min(TEMP_MAX_C, float(cfg.get("max_temperature", TEMP_MAX_C)))
        self.lock = threading.Lock()
        self.state = SchedulerState(user_target=self._load())
        if self.state.user_target is not None:
            logging.info("Loaded app-set target %.1fC from %s.",
                         self.state.user_target, self.state_file)
        self.on_change = None  # set by eob_stats: refresh the dashboard soon

    # --- persistence
    def _load(self) -> float | None:
        if not self.state_file or not os.path.exists(self.state_file):
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                v = json.load(f).get("user_target")
            return None if v is None else float(v)
        except Exception:
            logging.exception("Could not read %s; ignoring it.", self.state_file)
            return None

    def _save(self) -> None:
        if not self.state_file:
            return
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"user_target": self.state.user_target}, f)
            os.replace(tmp, self.state_file)  # atomic: never a half-written file
        except Exception:
            logging.exception("Could not write %s.", self.state_file)

    # --- scheduler loop
    def tick(self) -> None:
        with self.lock:
            before = self.state.user_target
            self.state = run_once(self.schedule, self.client, self.fallback_temp,
                                  self.temperature_source, self.state)
            if self.state.user_target != before:
                self._save()

    # --- control API
    def check_token(self, presented: str | None) -> bool:
        return bool(self.token) and presented is not None and \
            hmac.compare_digest(presented.encode(), self.token.encode())

    def set_target(self, value) -> dict:
        if not self.enabled:
            raise ControlError("Remote control is disabled in config.yaml.", 403)
        try:
            temp = float(value)
        except (TypeError, ValueError):
            raise ControlError("target must be a number")
        if not math.isfinite(temp):
            raise ControlError("target must be a number")
        temp = round(temp / self.STEP) * self.STEP
        if not (self.min_temp <= temp <= self.max_temp):
            raise ControlError(
                f"target must be between {self.min_temp:.1f} and {self.max_temp:.1f} C")
        with self.lock:
            applied_now = False
            if self.state.last_is_low:
                # Low tariff right now: apply immediately. During high tariff the
                # heater stays forced off; run_once applies it at the next NT.
                self._send(temp)
                applied_now = True
            self.state = replace(self.state, user_target=temp)
            self._save()
        logging.info("Target set from the app: %.1fC (%s).", temp,
                     "applied now" if applied_now else "applies at next low tariff")
        self._changed()
        return {"ok": True, "target": temp, "applied_now": applied_now}

    def clear_target(self) -> dict:
        """Back to the configured behaviour (temperature_source / target_temperature)."""
        if not self.enabled:
            raise ControlError("Remote control is disabled in config.yaml.", 403)
        with self.lock:
            self.state = replace(self.state, user_target=None)
            self._save()
            target = self._default_target()
            applied_now = False
            if self.state.last_is_low and target is not None:
                self._send(target)
                applied_now = True
        logging.info("App-set target cleared; back to %s.", self.temperature_source)
        self._changed()
        return {"ok": True, "target": target, "applied_now": applied_now}

    def _send(self, temp: float) -> None:
        try:
            self.client.set_temperature(temp)
        except Exception as e:
            logging.exception("Sending %.1fC to the thermostat failed", temp)
            raise ControlError(f"Couldn't send it to the thermostat: {e}", 502)

    def _default_target(self) -> float | None:
        if self.temperature_source == "device" and self.state.last_on_target is not None:
            return self.state.last_on_target
        return self.fallback_temp

    def info(self) -> dict:
        """For /api/status: what the app needs to offer the control."""
        with self.lock:
            user = self.state.user_target
            low_target = user if user is not None else self._default_target()
        return {
            "enabled": self.enabled,
            "min": self.min_temp,
            "max": self.max_temp,
            "step": self.STEP,
            # The temperature low-tariff periods run at, and where it comes from.
            "target": low_target,
            "source": "app" if user is not None else self.temperature_source,
        }

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                logging.exception("on_change hook failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use a mock thermostat client and just log intended actions.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once and exit, instead of looping forever.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_file", "scheduler.log"), args.dry_run)

    schedule = TariffSchedule(
        raw_windows=config["high_tariff_windows"],
        timezone=config.get("timezone", "Europe/Prague"),
        holiday_country=config.get("holiday_country"),
    )

    fallback_temp = float(config["target_temperature"])
    temperature_source = config.get("temperature_source", "config")
    if temperature_source not in ("config", "device"):
        raise ValueError(
            f"temperature_source must be 'config' or 'device', got {temperature_source!r}"
        )

    if args.dry_run:
        config = dict(config)
        config["thermostat"] = {"mode": "mock"}
        logging.info("Running in --dry-run mode: no real device will be contacted.")
    client = build_client(config)

    poll_interval = config.get("poll_interval_seconds", 60)
    controller = Controller(
        schedule, client, fallback_temp, temperature_source,
        state_file=config.get("state_file", "eob_state.json"),
        control_cfg=config.get("control"),
    )

    if args.once:
        controller.tick()
        logging.info("Current state: %s",
                     "low tariff" if controller.state.last_is_low else "high tariff (off)")
        return

    stats = eob_stats.start_from_config(config, client, schedule, controller)
    if stats is None:
        logging.info("Stats/dashboard disabled (set stats.enabled in config.yaml).")

    logging.info(
        "Starting scheduler loop (poll every %ss). temperature_source=%s "
        "(fallback=%.1fC). OFF on high tariff. Ctrl+C to stop.",
        poll_interval, temperature_source, fallback_temp,
    )
    try:
        while True:
            controller.tick()
            time_module.sleep(poll_interval)
    except KeyboardInterrupt:
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
