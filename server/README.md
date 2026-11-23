# Baby monitor server

Flask replacement for upstream's PHP site. Media never passes through Python:

```
browser ──HTTPS──> Caddy ──> gunicorn/Flask (UI, login, /api/*)        :8000
                     └─/stream/* (WHEP, after /auth/stream) ──> MediaMTX :8889
browser <──WebRTC media, UDP 8189──────────────────────────── MediaMTX
Flask ──Unix socket──> hostagent.py (root): start/stop babymonitor-* units, reboot, shutdown
crydetect (Notify) ──SQLite──> Flask ──polled /api/notify──> browser alerts
```

| Path | What |
| --- | --- |
| `babymonitor/` | Flask app: `auth.py` (single password), `modes.py` (mode API + main page), `streams.py` (Caddy forward-auth), `db.py` (SQLite key/value settings), `agent_client.py` |
| `babymonitor/static/whep.js` | Minimal WebRTC (WHEP) player for MediaMTX |
| `babymonitor/notify.py`, `notify_settings.py`, `static/notify.js` | Notify settings API and form, live readings, alerts in the browser |
| `crydetect/` | Notify mode's cry detector (`python -m crydetect --device plughw:1,0`): upstream's features and ONNX networks (`models/`) on onnxruntime |
| `babymonitor/system.py`, `templates/settings.html`, `static/settings.js` | Settings page: Wi-Fi, hotspot, country, time zone, host name, password, restart/shut down |
| `hostagent.py` | Root agent, standard library only; `hostagent_network.py` (nmcli, hotspot watchdog) and `hostagent_system.py` (time zone, clock, host name) |
| `deploy/` | Caddyfile, `mediamtx.yml`, `streams.env`, systemd units |
| `tests/` | `pytest` (no device needed) |

## Development on a PC

No board needed: the fake host agent just remembers the mode.

```bash
cd server
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest

export BM_AGENT_SOCKET=fake BM_INSECURE_COOKIES=1  # Windows: set VAR=value
# Optional: play real streams from the board's MediaMTX (tools/test_webrtc.sh run)
export BM_WHEP_BASE=http://rock-4se:8889
flask --app babymonitor set-password
flask --app babymonitor run --debug
```

## Install on the board (manual, until a setup script exists)

```bash
sudo useradd --system --home /var/lib/babymonitor --groups audio,video babymonitor
sudo apt install caddy gstreamer1.0-rtsp dnsmasq-base iw   # dnsmasq-base: hotspot DHCP
sudo mkdir -p /opt/babymonitor /etc/babymonitor
# The service user owns only its data directory; the code in /opt stays root-owned.
sudo install -d -o babymonitor -g babymonitor -m 750 /var/lib/babymonitor
sudo cp -r server /opt/babymonitor/
sudo python3 -m venv /opt/babymonitor/venv
sudo /opt/babymonitor/venv/bin/pip install -r /opt/babymonitor/server/requirements.txt
# MediaMTX: download the latest arm64 release into /opt/babymonitor/mediamtx
sudo MTX_DIR=/opt/babymonitor/mediamtx bash tools/test_webrtc.sh install
sudo cp server/deploy/mediamtx.yml server/deploy/streams.env /etc/babymonitor/   # edit MIC
sudo cp server/deploy/Caddyfile /etc/caddy/Caddyfile
sudo cp server/deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now babymonitor-agent babymonitor-mediamtx babymonitor-web
sudo systemctl reload caddy
cd /opt/babymonitor/server && sudo -u babymonitor \
    BM_DATABASE=/var/lib/babymonitor/babymonitor.sqlite3 \
    /opt/babymonitor/venv/bin/flask --app babymonitor set-password
```

Then open `https://rock-4se/`.

## Not done yet

- Setup script replacing the steps above.

## Notify mode

`babymonitor-crydetect` records the mic continuously in 2.06 s clips and, per clip,
measures loudness above the background and (unless the clip is quieter than
`min_sound_contrast`) runs the cry network. It alerts when enough of the last few
clips match. It writes the latest reading and alerts to the database; the page polls
`/api/notify` every 2 s and alerts with a banner, beeps (after "Enable alarm" was
tapped), vibration, a system notification where the browser allows it, and by
default switches to Listen. Settings are re-read on every clip.

Differences from upstream, on purpose:

- Probabilities are the network's real ones (`exp` of its LogSoftmax output; upstream
  used `10**`, which exaggerates them). The default certainty threshold is 65%,
  about what upstream's 80% meant.
- "Crying or babbling" only alerts when that share of recent clips is reached
  (upstream alerted on one unsure clip), and "and" really needs both.
- One continuous `arecord` instead of one per clip, so there are no gaps.
- Mic gain is not set (upstream set 100% with `amixer`); set it once with `alsamixer`.

## Settings and the hotspot

`/settings` talks to the host agent, which drives NetworkManager:

- Home networks are ordinary NetworkManager Wi-Fi connections. Joining one runs in
  the background (the browser may be on the hotspot that is about to go away); a
  failed attempt deletes the new profile and brings the hotspot back.
- The hotspot is the connection `babymonitor-hotspot` (WPA2, `192.168.4.1/24`,
  shared IPv4 via dnsmasq). It exists only once a name and password are saved on
  the settings page; until then there is no fallback.
- The agent's watchdog checks every minute: with neither Wi-Fi nor Ethernet
  connected twice in a row it starts the hotspot (upstream: cron every 10 min).
  While the hotspot runs and nobody is connected to it, every 10 minutes it tries
  the saved networks for 45 s and goes back to the hotspot if none answers.
- Wi-Fi country: `iw reg set` plus `/etc/modprobe.d/babymonitor-regdom.conf`.
- The board has no clock battery. The main page sends the browser's time; the agent
  sets the clock from it when NTP hasn't synchronised and it is >30 s off.
- Not ported from upstream: changing the Linux user's password from the web page
  (a stolen web password shouldn't also open SSH).
