# Capturing the real thermostat API calls

Goal: watch what the official **EOB PT-WiFi** app sends when you change a
setting, so we can replicate it in `eob_lib.py`. You're only inspecting
traffic between your own phone and your own device/account — this is normal
reverse-engineering of a device you own, not "hacking" anything remote.

## Option A — Local control mode (try this first, easiest)

The BT725 manual describes a "control within range of your router" mode,
distinct from the cloud/server mode. If the app talks to the thermostat
directly over your LAN in that mode, it's very likely **plain HTTP**
(no TLS to defeat), which makes this trivial to capture.

1. In the EOB PT-WiFi app, switch the thermostat's connection to local mode
   (not "SERVER"). The manual has the exact menu path for finding/setting
   the thermostat's local IP.
2. On a computer connected to the **same WiFi network**, install
   [Wireshark](https://www.wireshark.org/).
3. Start a capture on your WiFi interface with a filter for the
   thermostat's IP, e.g.: `host 192.168.1.XX` (replace with the real IP).
4. In the app, change something harmless and reversible — e.g. bump the
   target temperature by 0.5°C, then set it back, or flip AUTO/MANU.
5. Stop the capture. Look for HTTP requests (Wireshark: `Follow > HTTP
   Stream`). Note the request method, URL/path, headers, and body for each
   action you performed.

If it's HTTP, you're basically done — paste me the request(s) and I'll
write `eob_lib.py` immediately.

## Option B — Cloud/server mode (if local mode isn't usable)

If the app always talks to Elektrobock's server even in local mode, you'll
need to intercept TLS traffic instead:

1. Install [mitmproxy](https://mitmproxy.org/) on a computer on the same
   network as your phone.
2. Configure your phone's WiFi to use that computer as an HTTP proxy
   (Settings → WiFi → your network → Proxy → Manual → computer's IP, port
   8080).
3. On the phone, open a browser and go to `mitm.it`, then install the
   mitmproxy CA certificate for your OS (Android may require enabling it
   under Settings → Security → install from storage, and on newer Android
   versions installing it as a **user** certificate is enough since this
   is just for your own testing).
4. With mitmproxy running (`mitmproxy` or `mitmweb` for a browser UI),
   open the EOB PT-WiFi app and perform the same harmless action as above
   (nudge temperature, flip mode, etc.).
5. In mitmweb, find the request(s) to an Elektrobock domain (something like
   `*.elbock.cz` or `*.elektrobock.cz` — mitmproxy will show you the actual
   host). Note:
   - Full URL and method (GET/POST/PUT)
   - Request headers (especially `Authorization`, cookies, or any token)
   - Request body (likely JSON)
   - The response body, so we know how to parse status back

**Important:** if login uses a password, note that a session token/cookie
is probably what authorizes later requests — capture the login request too
so we can automate re-authentication.

## What to send me

For each captured action (e.g. "turn relay on", "turn relay off", "read
current temperature"), paste:
```
METHOD  https://full/url/path
Headers: { ... }
Body: { ... }
Response: { ... }
```
I'll turn that directly into working code in `eob_lib.py`.
