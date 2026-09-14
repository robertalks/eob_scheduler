"""Manual testing/diagnostic CLI for the BT725. All the reusable protocol
code lives in eob_lib.py -- this file is just the command-line tool.

Usage:
    python eob_client.py --test <unique_id> <mqtt_password> <target_temp_C>
        Tests the full off -> set_temperature sequence eob_scheduler.py
        actually uses: forces off, waits, sets the target temp, waits.
        Watch the app / thermostat display AND listen for the relay at
        each step to confirm both the off command and the
        resume-after-off behavior work as expected.

    python eob_client.py --status <unique_id> <mqtt_password> [known_actual_temp_C]
        Diagnostic tool: dumps the raw status payload and lists every
        byte that could plausibly be a half-degree temperature, flagging
        any close to a known real temperature you supply.
"""
from __future__ import annotations

import logging
import sys
import time

from eob_lib import Bt725Client


def run_status(unique_id: str, mqtt_password: str, known_temp: float | None) -> None:
    client = Bt725Client(unique_id, mqtt_password)
    client.connect()
    status = client.request_status()
    payload = client.last_status_payload or b""
    print(f"Raw status payload: {payload.hex()}")
    print(f"Parsed (confirmed fields only): {status}")
    print("\nByte-by-byte candidates (offset: byte -> if_half_degree_temp):")
    for i, byte in enumerate(payload):
        temp_guess = byte / 2.0
        marker = ""
        if known_temp is not None and abs(temp_guess - known_temp) < 0.3:
            marker = "  <-- close to your known actual temp!"
        if 6 <= byte <= 80:
            print(f"  offset {i:3d}: 0x{byte:02x} ({byte:3d}) -> {temp_guess:5.1f}C{marker}")
    client.disconnect()


def run_test(unique_id: str, mqtt_password: str, target_temp: float) -> None:
    client = Bt725Client(unique_id, mqtt_password)
    client.connect()

    print("Requesting current status...")
    status = client.request_status()
    print(f"Status: {status}")

    print("Forcing OFF in 3s (this is what happens on a high-tariff transition)...")
    time.sleep(3)
    client.force_off()
    print("Sent. Check the app/display AND listen for the relay. Waiting 8s...")
    time.sleep(8)

    print(f"Now setting target temperature to {target_temp}C (this is what happens on a low-tariff transition)...")
    client.set_temperature(target_temp)
    print(
        "Sent. THIS IS THE PART THAT NEEDS YOUR CONFIRMATION: check the app "
        "shows the new target AND that normal ambient-based operation has "
        "resumed (not stuck 'off'). Waiting 8s..."
    )
    time.sleep(8)

    client.disconnect()
    print("Done.")


def _usage() -> None:
    print(
        "Usage: python eob_client.py --test <unique_id> <mqtt_password> <target_temp_C>\n"
        "   or: python eob_client.py --status <unique_id> <mqtt_password> [known_actual_temp_C]"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) >= 2 and sys.argv[1] == "--status":
        if len(sys.argv) not in (4, 5):
            _usage()
            sys.exit(1)
        known_temp = float(sys.argv[4]) if len(sys.argv) == 5 else None
        run_status(sys.argv[2], sys.argv[3], known_temp)
        sys.exit(0)

    if len(sys.argv) != 5 or sys.argv[1] != "--test":
        _usage()
        sys.exit(1)

    run_test(sys.argv[2], sys.argv[3], float(sys.argv[4]))
