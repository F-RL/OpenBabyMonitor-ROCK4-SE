# Handover: Porting OpenBabyMonitor to the ROCK 4SE

Started Nov 15, 2026 · last updated Nov 16, 2026 · @Florian Ritsvall Losand

## Goal and scope

Port [OpenBabyMonitor](https://github.com/lars-frogner/OpenBabyMonitor) (unmaintained, Raspberry Pi / Buster) to a Radxa ROCK 4SE, with the PHP site replaced by a Flask app, all as plain systemd services (no Docker). Upstream is a feature reference only; the UI is new.

Features to keep: **Standby** (idle, connection test) · **Notify** (cry detection: loudness threshold or ONNX net, crying/babbling/ambient, browser alerts) · **Listen** (live audio) · **Observe** (live video + audio, up to 1080p) · password-protected HTTPS UI · home Wi-Fi with automatic fallback to its own hotspot (NetworkManager) · settings: Wi-Fi country, time zone, password, reboot/shutdown.

## Hardware and OS

| Item | Value |
| --- | --- |
| Board | Radxa ROCK 4SE, RK3399-T (hostname `rock-4se`, user `radxa`, wired `end0`) |
| OS / kernel | Radxa Debian bookworm + `bookworm-backports`; Rockchip vendor kernel 6.1.115; Python 3.11 |
| Camera | Waveshare RPi Camera (F), OV5647 (I²C 4, 0x36), overlay `rock-4-rpi-camera-v1_3.dtbo` via `rsetup` |
| Microphone | USB mic. Take the card from **`arecord -l`** (not `aplay -l`; card 0 is not the mic) |
| GStreamer | 1.22.9 + Radxa `gstreamer1.0-rockchip1` (`mpph264enc`), `librockchip-mpp1`; `gstreamer1.0-rtsp` for `rtspclientsink` |

Packaging lessons: graphics `-dev` packages must come from backports (`libdrm2` is the bpo version); `c++` is Clang, use `CC=gcc CXX=g++`. An unused libcamera 0.8.0 build sits in `/usr/local` (could be removed).

## Media findings (Nov 15–16, 2026, all verified on the board)

- **Camera = V4L2, not libcamera.** The vendor `rkisp1` driver has a different media graph from mainline, so libcamera finds nothing. Capture from `/dev/video0` (rkisp1 mainpath, NV12, ISP scales to any size up to 2592×1944). `rkisp_3A.service` must stay running (AE/AWB); discard ~30 frames at start. `/dev/mpp_service` and the subdevs need the `video` group.
- **Orientation:** set `horizontal_flip=1,vertical_flip=1` on `/dev/v4l-subdev2` before streaming (180°, colours OK). `mpph264enc` can also rotate/scale.
- **Image tuning deferred:** image is dark in high-contrast scenes (AE exposing for bright objects) and pink (night-vision module, no IR-cut; IQ file `ov5647_rpi-camera-v1p3_default.xml` assumes IR-cut). Ideas: try the OKDO IQ file, exposure compensation / grayscale option in the UI. 3A logs: `journalctl -t rkisp_3A-0`.
- **Encoding:** 720p `mpph264enc` + audio uses ~27% of one core (hardware encode works).
- **Latency:** HLS gave ~8 s → **switched to WebRTC via MediaMTX: <1 s** for both Observe and Listen (`tools/test_webrtc.sh run|audio`). WebRTC needs Opus audio and H.264 baseline (`profile=66`).
- **Cry model:** upstream `control/crynet_{small,large}.onnx` runs under onnxruntime 1.32 / numpy 2.4 (upstream's pinned `requirements.txt` doesn't install on 3.11; ignore it). Large model: 42 ms per 2.06 s clip. Positive test with a crying clip passed. **Upstream bug:** the net ends in `LogSoftmax` (natural log) but `control/listen.py:330` uses `10**out`, sharpening probabilities to p^2.3, so its 85% threshold is looser than it looks. `tools/test_crynet.py` uses `np.exp`. **Open decision** for crydetect: true probabilities with a retuned threshold, or replicate upstream.

Test scripts in `tools/`: `test_observe.sh` (HLS prototype, historical), `test_webrtc.sh` (`check|install|run|audio`, installs MediaMTX to `~/mediamtx` or `$MTX_DIR`), `test_crynet.py` (`--device plughw:<card>,0`, venv `~/obm-venv`).

## Upstream notes still relevant

- Modes are exclusive systemd units, so the mic is never shared. Notify upstream: `control/listen.py` + `detection/features.py` (`arecord` per interval, 64×128 mel features via `python_speech_features`, `control/standardization.npz`, amplification 10). Mic gain per mode via `amixer` (`control/mic.py`).
- Pi-specific replacements for later settings work: Wi-Fi country `iw reg set` (+ `cfg80211 ieee80211_regdom`); time zone `timedatectl set-timezone`; hostname `hostnamectl`; locale `localectl`; temperature `/sys/class/thermal/thermal_zone*/temp` (no throttling equivalent); camera present = `/dev/video0` + ov5647 in `media-ctl`. Drop `dtoverlay=disable-bt` and the picam download. Upstream assumes user `pi` and dhcpcd/wpa_supplicant/hostapd, which conflict with NetworkManager.

## Architecture (decided)

```
browser ─HTTPS─> Caddy ─> gunicorn/Flask :8000 (UI, login, /api/*)
                  └ /stream/* ─(forward_auth /auth/stream)─> MediaMTX :8889 (WHEP)
browser <── WebRTC media, UDP 8189 ── MediaMTX <── RTSP :8554 ── GStreamer mode units
Flask ─Unix socket─> hostagent.py (root): start/stop babymonitor-* units, reboot, shutdown
```

- Media never passes through Python. Settings in SQLite. One unprivileged service user `babymonitor` (groups audio, video); only the host agent is root.
- Modes are exclusive: Standby = nothing; Notify = `babymonitor-crydetect`; Listen = `babymonitor-audiostream` (MediaMTX path `mic`); Observe = `babymonitor-videostream` (path `cam`). `babymonitor-mediamtx` always runs.
- Caddy uses on-demand `tls internal` certificates, approved by Flask `/auth/tls-ask` (IP addresses and the board's own names only), so `https://<ip>/` works, including on the hotspot. Browsers warn until Caddy's root CA is trusted.
- WebRTC is DTLS-SRTP encrypted, so upstream's AES-128 HLS encryption is dropped.

## Flask server (`server/`, see `server/README.md`)

- `babymonitor/`: single-password login (hash + session secret in SQLite `settings`), `GET/PUT /api/mode` via the host agent, `/auth/stream` and `/auth/tls-ask` for Caddy, main page with mode buttons and a WHEP player (`static/whep.js`; retries while a stream starts, reconnects, muted fallback with "tap to turn on sound"). CLI: `flask --app babymonitor set-password`.
- `hostagent.py`: stdlib only, socket `/run/babymonitor/agent.sock` (group `babymonitor`); `get_mode`, `set_mode` (stops other units, then starts the target; missing unit → "<mode> mode is not installed yet"), `reboot`, `shutdown`.
- `deploy/`: Caddyfile, `mediamtx.yml` (localhost only + UDP 8189), `streams.env` (MIC, size, bitrates, flip), units for web, agent, mediamtx, videostream, audiostream.
- Tests: `cd server && pytest` (20 pass; fake agent via `BM_AGENT_SOCKET=fake`, no board needed). No Node on the dev PC, so JS is untested locally.
- Code lives root-owned in `/opt/babymonitor`; data in `/var/lib/babymonitor` (owned by `babymonitor`). `.gitattributes` forces LF everywhere.

### Nov 16, 2026 board deployment, issues fixed

1. "Permission denied" starting Flask: the app created `instance/` inside root-owned `/opt` → now only the DB directory is created; install step adds `/var/lib/babymonitor`.
2. `SSL_ERROR_INTERNAL_ERROR_ALERT` when opening by IP → on-demand TLS (above).
3. Observe "dependency job failed": MediaMTX binary was never installed (`203/EXEC`; README step was only a comment) → copied from `~/mediamtx`; README now has a real command.
4. Observe/Listen black and silent with "URL constructor: /stream/cam/whep is not a valid URL": `whep.js` bug resolving the session URL against a relative URL → fixed in `4d0a850` (pushed). WHEP signalling through Caddy + forward-auth + MediaMTX was shown to work.
5. Notify: `babymonitor-crydetect.service` doesn't exist yet → agent now gives a clear message.

## Next steps

1. **Pending user test:** after `git pull`, `sudo cp -r server /opt/babymonitor/`, restarting `babymonitor-agent babymonitor-web` and a hard reload, check that Observe and Listen play in the UI. If still black: `journalctl -u babymonitor-mediamtx` / `-u babymonitor-videostream`, and check UDP 8189. Still unverified: `mediamtx.yml` keys with the installed MediaMTX version; Caddy `header_down` regex on bookworm's Caddy 2.6.
2. Notify: `babymonitor-crydetect.service` (port `listen.py` logic onto onnxruntime, settle the probability decision above) and cry alerts to the browser (SSE).
3. Settings UI: Wi-Fi/hotspot (nmcli), country, time zone, password change (API exists), reboot/shutdown.
4. Setup script replacing the manual README steps.

Parked board issues (from Nov 15, 2026):

- **Random power-off** needing a full power cycle. Suspects: power supply (needs solid 5 V/3 A+), heat, kernel crash. Journal is persistent; inspect `journalctl -b 8c1b6a4802374e20a978b306bc1487a9 -n 60` (likely the power-off boot) or `journalctl -b -1 -n 100` after the next one: ends at `PM: suspend entry` = resume hang, abrupt cut = power, panic/thermal = kernel/heat. No RTC battery, so early timestamps are stale.
- **Idle suspend** drops SSH (desktop power manager). Fix given but not confirmed applied: `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`, or boot headless (`systemctl set-default multi-user.target`). Give `radxa` journal access: `sudo usermod -aG adm,systemd-journal radxa`.
