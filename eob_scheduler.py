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

During high tariff: sends a genuine power-off command, so nothing runs
regardless of ambient temperature. This uses a different underlying
command than set_temperature, so the device's own "last target" memory
survives being turned off -- that's what makes "device" mode possible.

Usage:
    python eob_scheduler.py                 # run for real, using config.yaml
    python eob_scheduler.py --dry-run       # log what it would do, touch nothing
    python eob_scheduler.py --config other.yaml
    python eob_scheduler.py --once          # check once and exit (for cron)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time as time_module
from dataclasses import dataclass

import yaml

from eob_lib import build_client
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


@dataclass
class SchedulerState:
    last_is_low: bool | None = None
    # Snapshot of the device's target temperature, taken right as it was
    # switched off (i.e. what was genuinely running beforehand). Only
    # meaningful/used when temperature_source == "device".
    last_on_target: float | None = None


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
        if temperature_source == "device":
            if state.last_on_target is not None:
                target_temp = state.last_on_target
                logging.info(
                    "temperature_source=device: reapplying %.1fC snapshotted "
                    "from before the last off period.", target_temp,
                )
            else:
                # No snapshot yet (e.g. the very first run) -- a live read
                # from the device is a better guess than the hardcoded
                # config fallback, so try that first.
                current = client.get_current_target_temperature()
                if current is not None:
                    target_temp = current
                    logging.info(
                        "temperature_source=device: no snapshot yet, read "
                        "%.1fC currently on the device, using that.",
                        current,
                    )
                else:
                    target_temp = fallback_temp
                    logging.warning(
                        "temperature_source=device: no snapshot and no live "
                        "reading available, falling back to config "
                        "target_temperature=%.1fC.", fallback_temp,
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
        return SchedulerState(last_is_low=True, last_on_target=state.last_on_target)

    else:
        new_snapshot = state.last_on_target
        # Only trust a live read here if we KNOW we're coming from a
        # genuinely-managed low-tariff period (state.last_is_low is
        # True) -- if this is a cold start into an already-high-tariff
        # period (state.last_is_low is None), the device could have been
        # off for a while already, and a "current" read at this moment
        # doesn't reflect "what was running right before going off" at
        # all. Snapshotting it anyway was the bug: it read whatever the
        # device reports while idle (observed as 0.0C) and wrongly
        # treated that as a real value to reapply later.

        if temperature_source == "device" and state.last_is_low is True:
            current = client.get_current_target_temperature()
            if current is not None:
                new_snapshot = current
                logging.info(
                    "temperature_source=device: snapshotting %.1fC (what was "
                    "running) before forcing off.", current,
                )
            else:
                logging.warning(
                    "temperature_source=device: could not read current "
                    "temperature before forcing off; keeping previous "
                    "snapshot (%s).",
                    f"{state.last_on_target:.1f}C" if state.last_on_target is not None else "none",
                )
        elif temperature_source == "device":
            logging.info(
                "temperature_source=device: starting up already in a "
                "high-tariff period, so not trusting a snapshot read here "
                "(nothing genuinely just turned off) -- the next low-tariff "
                "resume will read a live value or use the config fallback "
                "instead."
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
        return SchedulerState(last_is_low=False, last_on_target=new_snapshot)


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
    state = SchedulerState()

    if args.once:
        state = run_once(schedule, client, fallback_temp, temperature_source, state)
        logging.info("Current state: %s", "low tariff" if state.last_is_low else "high tariff (off)")
        return

    logging.info(
        "Starting scheduler loop (poll every %ss). temperature_source=%s "
        "(fallback=%.1fC). OFF on high tariff. Ctrl+C to stop.",
        poll_interval, temperature_source, fallback_temp,
    )
    try:
        while True:
            state = run_once(schedule, client, fallback_temp, temperature_source, state)
            time_module.sleep(poll_interval)
    except KeyboardInterrupt:
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
