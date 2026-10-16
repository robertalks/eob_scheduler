# BT725 Low-Tariff Water Heater Controller

Controls your Elektrobock BT725 WiFi thermostat according to your ČEZ
**D45d** tariff:

- **Low tariff**: sets a target temperature. The thermostat's own local
  ambient sensor decides when to actually heat (normal hysteresis
  behavior) — this client doesn't need to know or replicate that logic.
  The target comes from either a fixed value in `config.yaml`, or — with
  `temperature_source: "device"` — whatever temperature is currently set
  on the device (e.g. manually via the official app), so a manual change
  is respected instead of overwritten. See "Temperature source" below.
- **High tariff**: sends a genuine **off** command. This disables the
  output outright — it's not just a low setpoint the ambient could still
  cross, nothing runs during high tariff, period.

Control never depends on the thermostat's ambient temperature, but since
2026-10-16 it *is* read back for statistics -- see "Stats dashboard" below.

## Status: both core commands confirmed against the real device

See the big docstring at the top of `eob_lib.py` for full detail.
In short:

- **Set target temperature** (`opcode 0x28`): confirmed via a capture of
  the official app itself changing its target 15.0C → 16.0C → 17.0C →
  15.0C — produced exactly the expected bytes each time
  (`value = round(temp_C * 2)`).
- **Force off** (`opcode 0x2c`, value `0x00`): confirmed via direct
  observation — sending this made the real relay physically click off.
- **Verified working 2026-09-14**: the off → set-temperature sequence
  (exactly what the scheduler does on a low-tariff transition after a
  high-tariff period) was tested against the real device — the app
  showed the new 19.0C target and the thermostat resumed normal
  operation, not stuck off.

### Verify it actually works before automating

```
python eob_client.py --test <unique_id> <mqtt_password> <target_temp_C>
```

This powers the thermostat off, waits, then sets your target temperature
— exactly the sequence the scheduler uses on a tariff transition. Watch
the app/thermostat display **and** listen for the relay at each step.
The part that specifically needs your eyes: after the "set temperature"
step, confirm the thermostat has resumed normal operation rather than
staying stuck off.

## Architecture

```
eob_tariff.py  -- pure logic: "is it low-tariff right now?" given a config
eob_lib.py     -- reusable driver: talks to the thermostat over its real
                  MQTT protocol (used by both eob_client.py and
                  eob_scheduler.py)
eob_client.py  -- manual testing/diagnostic CLI (--test, --status)
eob_scheduler.py -- daemon: polls eob_tariff.py every minute, sets
                  temperature or powers off only when the tariff state
                  actually changes
eob_stats.py   -- optional: records measured temp / relay state to SQLite
                  and serves a stats web page (started by eob_scheduler.py)
config.yaml    -- your D45d switching times, target temperature, and
                  thermostat connection info
```

## Setup

1. `pip install -r requirements.txt`
2. Find your exact D45d NT/VT switching times:
   https://www.cezdistribuce.cz/cs/pro-zakazniky/potrebuji-vyresit/stavajici-pripojeni/casy-spinani-nizkeho-tarifu
   (you'll need your meter number, EAN, or HDO code/command — printed on the
   HDO receiver in your meter box, or in "Můj ČEZ"). Note ČEZ occasionally
   revises these nationwide (it happened in Oct 2025 and Mar 2026), so check
   back after any such announcement.
3. Copy `config.example.yaml` to `config.yaml` and fill in your VT (high
   tariff) windows, `target_temperature`, your thermostat's `unique_id`,
   and its `mqtt_password` (the latter two captured from your app's MQTT
   CONNECT packet — see `CAPTURE_GUIDE.md` if you ever need to redo this).
   Your actual D45d windows (weekday + weekend/holiday) are already filled
   in as of 2026-09-13 — double check they still match if you're setting
   this up much later.
4. Run `python eob_client.py --test <unique_id> <mqtt_password> <target_temp>`
   with your real target temperature and confirm the off→resume sequence
   works correctly (see "Verify it actually works" above).
5. Test the scheduler without touching the real device:
   `python eob_scheduler.py --dry-run`
6. Run for real: `python eob_scheduler.py`
7. Make it survive reboots (Linux, e.g. Raspberry Pi) — see `systemd/` unit
   file included.

## Temperature source: fixed config value, or read from the device

`temperature_source` in `config.yaml` controls where the low-tariff
target temperature comes from:

- **`"config"`** (default): always use `target_temperature` from the
  config file. If you manually change the temperature via the app during
  a low-tariff period, it'll be overwritten back to this fixed value the
  next time the scheduler applies a change (at the next high→low
  transition).
- **`"device"`**: snapshots whatever temperature was *actually running*
  right before the most recent high-tariff (off) period started — read
  right as the scheduler is about to power it off — and reapplies that
  same snapshot on the next resume. A manual change you make via the app
  **while it's genuinely on** is respected and carried forward; a change
  made **while it's off** (during high tariff) is deliberately ignored,
  since that isn't "what was running" — the device's status would
  reflect it, but the scheduler intentionally doesn't sample status
  during the off period, only right at the moment it switches off.
  `target_temperature` is still used as a fallback if no snapshot exists
  yet (e.g. the very first run, before any off-transition has happened).

This works because `power_off` (used during high tariff) and
`set_temperature` (used during low tariff) are different underlying
commands — turning the device off doesn't erase its memory of the last
temperature you set, so that value can be read back later.

## Safety notes

- `--dry-run` logs what it *would* do without calling the thermostat at all.
  Always test with this first.
- The scheduler only sends a command when the tariff state changes (not
  every poll), so you won't hammer the device.
- If the thermostat becomes unreachable, the scheduler logs the failure and
  retries next cycle — it never assumes success.
- Temperatures outside the current 5.0–39.0C assumed range are rejected
  rather than silently clamped or sent as-is, specifically so an untested
  value never reaches the real device by accident.
- D45d treats public holidays the same as weekends. `holiday_country: "CZ"`
  in config.yaml makes the scheduler aware of this automatically (via the
  `holidays` package) — a "weekday" window won't fire on a holiday, and a
  "weekend" window will. Set it to `null`/omit it if you'd rather ignore
  holidays entirely (not recommended for D45d).

## Stats dashboard

With `stats.enabled: true` in `config.yaml` (see `config.example.yaml`),
`eob_scheduler.py` also polls the thermostat's status every
`stats.poll_interval_seconds` over its existing MQTT connection and
records measured temperature, target, relay on/off and the current
tariff into `stats.db_file` (SQLite). It serves a page at
`http://<host>:<stats.web.port>/` (default port 8080) with:

- current measured/target temperature, relay state and for how long,
  current tariff and the next tariff change
- a 6 h / 24 h / 7 d temperature chart with heater-on periods and
  low-tariff windows shaded
- heater on-time per day, split into low / high tariff -- a quick check
  that heating really only happens in NT

JSON is available at `/api/status`, `/api/history?hours=24` and
`/api/daily?days=14`. Relay switches are also logged
(`Heater relay switched ON/OFF ...`). The page has no authentication --
keep it on your LAN, or set `stats.web.host: "127.0.0.1"`.
`--dry-run` uses a simulated thermostat so you can preview the page.

## Reading ambient/current temperature (solved 2026-10-16)

The measured temperature *is* in the status reply: the 80 02 frame's
first byte is whole degrees and the low nibble of its third byte is
tenths (e.g. `0e .. 07` = 14.7C). It was missed before because every
search assumed the half-degree encoding the target uses. The layout comes
from the official app (ThermSettingsDataLoader) and is documented in
`eob_lib.py`; `parse_status()` now returns `measured_temp_c` and
`relay_on` alongside `target_temp_c`. The original notes are kept below
for history.

### Original notes (before 2026-10-16)

An earlier version of this project tried to log the thermostat's actual
ambient temperature alongside the target. After extensive testing across
several packet captures — including two different status-read requests
and multiple byte-encoding hypotheses — nothing in the MQTT traffic
matched a real known room temperature. The most likely explanation: the
ambient sensor loop is entirely local to the device (it needs to keep
working even if the internet is down), and simply isn't reported back
over MQTT at all.

This isn't a blocker for the current design — the thermostat's local
hysteresis logic handles the ambient comparison correctly on its own,
regardless of whether this client can see that number. If you want to
pick this investigation back up later, `eob_client.py --status` still
exists as a diagnostic tool for dumping and searching raw status payloads.
