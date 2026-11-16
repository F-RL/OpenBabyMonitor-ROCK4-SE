# Baby monitor server

Flask replacement for upstream's PHP site. Media never passes through Python:

```
browser ──HTTPS──> Caddy ──> gunicorn/Flask (UI, login, /api/*)        :8000
                     └─/stream/* (WHEP, after /auth/stream) ──> MediaMTX :8889
browser <──WebRTC media, UDP 8189──────────────────────────── MediaMTX
Flask ──Unix socket──> hostagent.py (root): start/stop babymonitor-* units, reboot, shutdown
```

| Path | What |
| --- | --- |
| `babymonitor/` | Flask app: `auth.py` (single password), `modes.py` (mode API + main page), `streams.py` (Caddy forward-auth), `db.py` (SQLite key/value settings), `agent_client.py` |
| `babymonitor/static/whep.js` | Minimal WebRTC (WHEP) player for MediaMTX |
| `hostagent.py` | Root agent, standard library only |
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
sudo apt install caddy gstreamer1.0-rtsp
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

- `babymonitor-crydetect.service` (Notify) and cry alerts to the browser (SSE).
- Settings UI: Wi-Fi / hotspot, country, time zone, reboot/shutdown buttons.
- Setup script replacing the steps above.
