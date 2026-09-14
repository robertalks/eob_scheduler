"""Daemon: polls the tariff schedule and drives the thermostat accordingly.

During low tariff: sets the configured target temperature and lets the
thermostat's own local ambient sensor decide when to actually heat.
During high tariff: sends a genuine force-off command, so nothing runs
regardless of ambient temperature.

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


def run_once(
    schedule: TariffSchedule,
    client,
    target_temp: float,
    last_state,
):
    """Check current tariff state, push a change to the thermostat if
    needed. Returns the new last_state to remember (either the float
    target_temp, or the string 'off')."""
    is_low = schedule.is_low_tariff()
    desired_state = target_temp if is_low else "off"
    if desired_state != last_state:
        logging.info(
            "Tariff state changed -> %s (%s)",
            f"set target temperature = {target_temp:.1f}C" if is_low else "force OFF",
            "low tariff" if is_low else "high tariff",
        )
        try:
            if is_low:
                client.set_temperature(target_temp)
            else:
                client.force_off()
        except NotImplementedError as e:
            logging.error("Thermostat driver not implemented yet: %s", e)
        except Exception:
            logging.exception("Failed to update thermostat; will retry next poll")
            return last_state  # don't update last_state, so we retry
        else:
            return desired_state
    return last_state


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

    target_temp = float(config["target_temperature"])

    if args.dry_run:
        config = dict(config)
        config["thermostat"] = {"mode": "mock"}
        logging.info("Running in --dry-run mode: no real device will be contacted.")
    client = build_client(config)

    poll_interval = config.get("poll_interval_seconds", 60)
    last_state = None

    if args.once:
        last_state = run_once(schedule, client, target_temp, last_state)
        logging.info("Current state: %s", last_state)
        return

    logging.info(
        "Starting scheduler loop (poll every %ss). Target temp=%.1fC on low "
        "tariff, OFF on high tariff. Ctrl+C to stop.",
        poll_interval, target_temp,
    )
    try:
        while True:
            last_state = run_once(schedule, client, target_temp, last_state)
            time_module.sleep(poll_interval)
    except KeyboardInterrupt:
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
