# Handover: Porting OpenBabyMonitor to the ROCK 4SE

Nov 15, 2026 · @Florian Ritsvall Losand

## Goal and scope

Port [OpenBabyMonitor](https://github.com/lars-frogner/OpenBabyMonitor) (lars-frogner, unmaintained, built for Raspberry Pi on Raspbian Buster) to a Radxa ROCK 4SE, and rewrite its server as a Python Flask application, run as plain systemd services.

Features to keep from upstream (per its README):

- **Standby**: idle, with a connection-quality test.
- **Notify**: cry detection, either a loudness threshold or a small neural network trained on Google AudioSet (crying / babbling / ambient), with notifications in the browser.
- **Listen**: live audio streaming with waveform/spectrum view.
- **Observe**: live video plus audio, up to 1080p.
- Password-protected web UI over HTTPS (self-signed cert), reachable at `https://<hostname>`.
- Networking: join home Wi-Fi, or run its own access point, switching automatically.
- Settings from the UI: Wi-Fi country, time zone, passwords, reboot and shutdown.

The upstream project's setup scripts and pre-built images will not run on this board as-is; treat it as a reference for features, not as a drop-in install. The web UI will be built new.

## Hardware and OS

The board runs Radxa's Debian image with Rockchip's vendor kernel, not mainline; this decides the camera approach below.

| Item | Value |
| --- | --- |
| Board | Radxa ROCK 4SE (hostname `rock-4se`, user `radxa`) |
| SoC | Rockchip RK3399-T |
| OS | Radxa Debian, bookworm base with `bookworm-backports` enabled |
| Kernel | Vendor (Rockchip BSP) 6.1.115 |
| Camera | Waveshare RPi Camera (F), OV5647 sensor, 15-pin MIPI CSI |
| Sensor on bus | I²C bus 4, address 0x36, probed by the `ov5647` driver |
| Camera overlay | Enabled (sensor probes at boot); managed with `rsetup` |
| Microphone | USB mic; recording with `arecord` verified Nov 15, 2026 |

Package note: `libdrm2` is the backports version (2.4.123-1\~bpo12+1). Any `-dev` package tied to the graphics stack must also come from backports (`apt install -t bookworm-backports …`), or apt reports unmet dependencies.

## Camera status

The camera works at the kernel level and is exposed through V4L2; libcamera and rpicam-apps cannot use it on this kernel. Capture from `/dev/video0` has not yet been tested by the user.

**Why not libcamera / rpicam-apps:** the vendor kernel ships Rockchip's own `rkisp1` driver (v00.01.05). It shares the name with mainline `rkisp1` but has a different media graph (`rkisp1-isp-subdev`, `rkisp1-input-params`, `rkisp1-statistics`, no resizer entities). libcamera's rkisp1 pipeline expects mainline names, so `cam -l` finds no camera. Using libcamera would require a mainline-kernel image plus an OV5647 overlay; that route was set aside.

Media graph (`media-ctl -p -d /dev/media0`): OV5647 (`m00_b_ov5647 4-0036`, SGBRG10 2592×1944 @ 15 fps) → `rockchip-mipi-dphy-rx` → `rkisp1-isp-subdev` → mainpath and selfpath. All links enabled.

| Node | Entity | Role |
| --- | --- | --- |
| `/dev/video0` | rkisp1\_mainpath | Main capture output (use this) |
| `/dev/video1` | rkisp1\_selfpath | Second output, e.g. a preview stream |
| `/dev/video2` | rkisp1\_dmapath | Memory input, unused |
| `/dev/video3` | rkisp1-statistics | Used by 3A |
| `/dev/video4` | rkisp1-input-params | Used by 3A |
| `/dev/v4l-subdev0–2` | ISP, D-PHY, sensor | Subdevices |

`rkisp_3A.service` (Rockchip camera engine) runs at boot and does auto-exposure and white balance through the statistics and params nodes. It must stay running, or images come out dark or green. Discard roughly 30 frames at stream start while exposure settles.

Suggested first tests:

```bash
v4l2-ctl -d /dev/video0 --list-formats-ext
v4l2-ctl -d /dev/video0 --set-fmt-video=width=2592,height=1944,pixelformat=NV12 \
  --stream-mmap --stream-skip=30 --stream-count=1 --stream-to=frame.nv12
ffmpeg -f rawvideo -pix_fmt nv12 -s 2592x1944 -i frame.nv12 frame.png

gst-launch-1.0 v4l2src device=/dev/video0 num-buffers=40 ! \
  video/x-raw,format=NV12,width=1920,height=1080 ! \
  videoconvert ! jpegenc ! multifilesink location=img_%02d.jpg
```

The mainpath has its own scaler, so 1920×1080 or 1280×720 can be requested directly.

## Build history (reference only)

libcamera 0.8.0 (RPi fork) was built into `/usr/local` with GCC but is unused (V4L2 approach); consider removing it. Lessons: graphics `-dev` packages must come from `bookworm-backports`; `c++` is Clang on this system, so use `CC=gcc CXX=g++` for native builds.

## OpenBabyMonitor findings

Repo searched Nov 15, 2026. Modes are mutually exclusive systemd units (`bm_standby`, `bm_listen` = Notify, `bm_audiostream` = Listen, `bm_videostream` = Observe), started by PHP via `sudo systemctl`; so upstream never shares the mic between processes.

Media pipeline (all HLS, files in `/run/shm`, optional AES-128 HLS encryption with a per-start key):

| Mode | Upstream capture | Port impact |
| --- | --- | --- |
| Observe | `control/videostream.py` runs **picam** 1.4.9 (iizukanao, Pi-only, MMAL/OMX) → `streaming/picam/index.m3u8`, H.264 + AAC from `--alsadev`, timestamp overlay; options: 480/720/1080p, fps/vfr, rotation, flips, exposure, metering, ISO, WB | Must be replaced entirely (GStreamer v4l2src + Rockchip encoder). Settings map partly to V4L2/rkisp controls; exposure/WB are owned by `rkisp_3A` |
| Listen | `control/audiostream.py`: ffmpeg `-f alsa plughw:X,Y` mono 8 kHz → libmp3lame → HLS (1 s segments, list 3) | Portable as-is |
| Notify | `control/listen.py` + `detection/features.py`: spawns `arecord` per interval (5 s), mel features (64×128, `python_speech_features`), standardization from `control/standardization.npz` | Portable; arecord works on any ALSA device |
| Frontend | video.js for video, hls.js for audio | New UI; HLS still a fine target (MediaMTX can serve HLS too) |

Cry model: **ONNX**, not TFLite: `control/crynet_small.onnx`, `crynet_large.onnx`, loaded with `cv2.dnn.readNetFromONNX`; output is log10 probs over ambient/bad(crying)/good(babbling). `requirements.txt` pins `numpy==1.21.4` and `opencv-python-headless==4.5.3.56`, neither installable on bookworm's Python 3.11 — use current versions, or swap cv2.dnn for `onnxruntime` (has arm64 wheels). Training code (`detection/neural_net.py`) needs torch; not needed at runtime. IPC with PHP is via files in `control/.comm` (`probabilities.json`, `sound_level.dat`, `notification.txt`). Settings DB is MySQL/MariaDB (`mysql-connector-python`).

Mic handling: `control/mic.py` parses `arecord -l`, stores `hw:card,dev` in `control/.mic/id`, finds an `amixer` capture-volume control and sets gain per mode.

Pi-specific code found, with Debian replacements:

| Location | What it does | Replacement |
| --- | --- | --- |
| `setup_server.sh:135` | Appends `dtoverlay=disable-bt` to `/boot/config.txt` | Drop it; optionally `systemctl disable --now bluetooth` |
| `setup_device.sh:18` | Sources raspi-config functions non-interactively | Replace each `do_*` call it uses (file not yet reviewed) |
| `site/servercontrol/set_country_code.sh:14` | `raspi-config nonint do_wifi_country` | `iw reg set "$CODE"`, plus `cfg80211 ieee80211_regdom` in `/etc/modprobe.d/` |
| `site/servercontrol/set_timezone.sh:20` | `raspi-config nonint do_change_timezone` | `timedatectl set-timezone "$TZ"` |
| `setup_device.sh` | raspi-config `do_hostname`, `do_wifi_country`, `do_change_locale`, `do_change_timezone`, `do_camera 0` (enable camera) | `hostnamectl set-hostname`; `iw reg set`; `localectl set-locale` (+ locale-gen); `timedatectl`; camera via `rsetup` overlay (already done) |
| `site/servercontrol/inform_camera_connected.sh` | `vcgencmd get_camera` → touches a flag file | Check `/dev/video0` exists / `v4l2-ctl --list-devices` shows rkisp1 + ov5647 in `media-ctl` |
| `site/public/monitoring.php:11,23` | `vcgencmd measure_temp`, `get_throttled` | `/sys/class/thermal/thermal_zone*/temp`; throttling: no direct equivalent (cpufreq cur vs max, or drop) |
| `setup_server.sh:322` | Downloads picam binary | Remove |

Other upstream assumptions to expect: a `pi` user (`BM_USER` in `config/setup_config.env`), Buster package names, and Buster-era networking (dhcpcd, wpa\_supplicant, hostapd) that will conflict with NetworkManager on the Radxa image.

## Architecture

Agreed Nov 15, 2026: Flask for control and settings; media never passes through Python. **Updated Nov 16, 2026: streams go through MediaMTX and are played over WebRTC** (measured <1 s latency vs ~8 s with plain HLS). Stream pipelines publish RTSP to MediaMTX (`rtsp://127.0.0.1:8554/<path>`); browsers play via WHEP (MediaMTX :8889, UDP 8189 for media); MediaMTX's HLS (:8888) can be a fallback. Audio must be Opus (WebRTC has no AAC). Viewer auth via MediaMTX `authHTTPAddress` → Flask endpoint.

The browser reaches everything through one HTTPS proxy: Flask serves the UI and API; the proxy forwards WHEP signalling to MediaMTX (media itself flows over UDP 8189); the host agent does the few things that need root.

| systemd unit | Runtime | Device access | Role |
| --- | --- | --- | --- |
| `babymonitor-web.service` | Python + Flask + gunicorn in a venv, SQLite | None | Login, mode switching, settings, cry alerts to the browser (SSE) |
| `babymonitor-crydetect.service` (Notify) | Python + onnxruntime in a venv | `audio` group; opens ALSA mic directly (`arecord`) | Runs threshold or ONNX model, posts events to web |
| `babymonitor-audiostream.service` (Listen) | GStreamer | `audio` group | ALSA → Opus → `rtspclientsink` → MediaMTX (audio-only WebRTC) |
| `babymonitor-videostream.service` (Observe) | GStreamer (`gst-launch-1.0` or small Python wrapper) | `video` + `audio` groups; `/dev/mpp_service` | `v4l2src /dev/video0` → `mpph264enc profile=66` + ALSA → Opus → `rtspclientsink` (proven in `tools/test_webrtc.sh`) |
| `babymonitor-mediamtx.service` | MediaMTX arm64 binary | None | RTSP in, WebRTC/HLS out; always running |
| `caddy.service` (or nginx) | Debian package | Port 443 | TLS with a self-signed or internal certificate; proxies Flask and WHEP |

Mode units are mutually exclusive (`Conflicts=` between them); the web service switches modes by asking the host agent to start a unit (or via a polkit rule limited to `babymonitor-*` units).

Also on the host: `rkisp_3A.service` (needs the vendor ISP libraries), NetworkManager for Wi-Fi client and hotspot (`nmcli`), and a small host agent exposing only whitelisted actions (mode switch, country, time zone, network switch, reboot, shutdown) to the web service over a Unix socket.

Design notes:

- Modes are exclusive (decided Nov 15, 2026), as upstream: Standby runs nothing; Notify runs only crydetect; Listen runs only the audio stream; Observe runs only the audio+video stream. One process holds the mic at a time, so no mic sharing is needed; stop the previous mode's unit before starting the next.
- Settings and state live in SQLite (decided Nov 15, 2026), replacing upstream MariaDB.
- With no containers, the capture pipeline uses Rockchip's hardware-encoder GStreamer plugin and vendor libraries directly. Run web, crydetect and stream units as unprivileged service users; only the host agent runs as root.
- WebRTC media is DTLS-SRTP encrypted, so upstream's AES-128 HLS encryption is not needed.
- Frontend plays WebRTC via a small WHEP client (or MediaMTX `reader.js`); hls.js only if an HLS fallback is kept.

## Next steps and open questions

### Where we left off (end of session Nov 15, 2026)

Media inputs proven: camera (V4L2 + rkisp_3A), sensor flip, hardware H.264 encoder, and a working Observe HLS prototype (`tools/test_observe.sh hls`, plays in VLC). Next session, in order:

1. Confirm the remaining Observe facts: ~~audio present in the HLS stream~~ (confirmed Nov 16, 2026), ~~CPU~~ (~27% of one core, fine), ~~latency~~ (~8 s with HLS; **WebRTC via MediaMTX <1 s, Nov 16, 2026** — adopted).
2. ~~Run `tools/test_crynet.py` on the board~~ (runs; positive cry test passed, Nov 16, 2026).
3. Quick Listen test: `tools/test_webrtc.sh audio` (ALSA → Opus → MediaMTX path `mic`, view `http://<ip>:8889/mic/`). **Works on the board (Nov 16, 2026).**
4. ~~Start the Flask app~~ Skeleton written Nov 16, 2026 in `server/` (see `server/README.md`), 17 pytest tests pass locally (Windows, Python 3.9, fake agent). Not yet run on the board.

### Flask app status (Nov 16, 2026)

- `server/babymonitor/`: single-password login (hash + session secret in SQLite key/value `settings`), `GET/PUT /api/mode` → host agent, `/auth/stream` for Caddy `forward_auth`, page with mode buttons + WHEP player (`static/whep.js`, retries until stream is up, muted fallback + "tap for sound").
- `server/hostagent.py`: root, stdlib only, Unix socket `/run/babymonitor/agent.sock` (group `babymonitor`), actions get_mode/set_mode/reboot/shutdown; set_mode stops other units then starts target.
- `server/deploy/`: Caddyfile (`tls internal`, `/stream/*` → MediaMTX after forward-auth, Location rewrite), `mediamtx.yml` (localhost only + UDP 8189), `streams.env` (MIC etc.), units web/agent/mediamtx/videostream/audiostream. One service user `babymonitor` (audio, video groups).
- Next: deploy to the board and test end to end; then crydetect service + SSE alerts; settings UI (Wi-Fi/hotspot, country, time zone, reboot); setup script. Unverified on board: MediaMTX config keys with latest release, Caddy `header_down` regex syntax on bookworm's Caddy 2.6.

Parked: board power-off (inspect boot `8c1b6a48…` journal tail), suspend fix not yet confirmed applied, camera image tuning.

### Checklist

- [x] Still captured Nov 15, 2026 (1920x1080 NV12 via v4l2-ctl → ffmpeg PNG). Image was upside down (mount; fixed by sensor flip), dark (AE exposing for a blown-out white object, see tuning note), pink cast (Camera (F) is night-vision, no IR-cut filter). Possible UI options: grayscale, WB tweak, exposure compensation.
- [x] Nov 15, 2026: `rkisp_3A` active; `/dev/video0` offers NV12/YUYV/etc. stepwise 32x16–2592x1944 (ISP scales). `/dev/mpp_service` exists (root:video, 0660), so stream services need the `video` group.
- [x] GStreamer 1.22.9 installed with base/good/bad/ugly/libav/alsa, plus Radxa `gstreamer1.0-rockchip1`, `librockchip-mpp1` 1.5.0, `librga2`, `libv4l-rkmpp`, `task-rockchip-gstreamer`. Nothing to install.
- Sensor subdev `/dev/v4l-subdev2` ctrls: horizontal_flip, vertical_flip (set before streaming), exposure 4–1964 (lines; longer needs more vertical_blanking), analogue_gain 16–1023, auto_exposure menu (Manual). Observed under 3A in dim room: exposure 886, gain 56. `/etc/iqfiles` has two ov5647 files (symlinks to `/usr/share/rockchip-iqfiles-rk3399/`): `ov5647_rpi-camera-v1p3_default.xml`, `ov5647_OKDO-5MP_default.xml`. 3A picks `<sensor>_<module-name>_<lens-name>.xml` from DT props `rockchip,camera-module-name` / `-lens-name`. Checked: DT (`/sys/firmware/devicetree/base/i2c@ff3d0000/camera-ov5647@36`) sets module `rpi-camera-v1p3`, lens `default` → matches `ov5647_rpi-camera-v1p3_default.xml`, so tuning IS available (overlay `rock-4-rpi-camera-v1_3.dtbo`). Dark image likely AE exposing for the blown-out white pillow (high-DR scene), not broken 3A. Pink/WB is because the IQ assumes an IR-cut lens. **Image tuning deferred** (backlog: try OKDO IQ by swapping the symlink, manual exposure test, exposure-comp/metering option in UI). 3A logs go to syslog tag `rkisp_3A-0` (`journalctl -t rkisp_3A-0`); `rkisp_3A_server` options: `--mmedia`, `--silent`. Sensor flip (h+v =1) verified Nov 15, 2026: correct 180° rotation, colours OK → use sensor flip, not videoflip.
- [x] Observe prototype **works** (Nov 15, 2026): HLS 720p via mpph264enc plays in VLC. Available: v4l2src, mpph264enc, x264enc, h264parse, alsasrc, voaacenc, avenc_aac (no fdkaacenc), aacparse, hlssink2, mp4mux, videoflip. mpph264enc props include bps/bps-min/bps-max, gop, rc-mode, profile, level, qp-*, header-mode, sei-mode, **rotation, width, height** (encoder-side rotate/scale, useful for 90° option). Audio in HLS stream confirmed Nov 16, 2026. CPU (Nov 16, 2026, 720p hls, one `top` snapshot): gst-launch-1.0 ~27% of one core (~4.5% of 6 cores), RES 22 MB → HW encode is effective, no software-convert bottleneck. HLS latency ~8 s (superseded by WebRTC). (User sourced the script with `.`; advised to run with bash.)
- [x] WebRTC test **works, <1 s latency** (Nov 16, 2026): `tools/test_webrtc.sh check|install|run`. Downloads latest MediaMTX arm64 to `~/mediamtx` (default config), pipeline v4l2src → mpph264enc (profile=66 baseline, gop 30) + alsasrc → opusenc → `rtspclientsink` rtsp://127.0.0.1:8554/cam; view `http://<ip>:8889/cam/` (WebRTC) or `:8888/cam/` (HLS). Needs `gstreamer1.0-rtsp` if rtspclientsink missing. Goal: <1 s vs 8 s HLS.
- Script: `tools/test_observe.sh` with `check` (element list + mpph264enc props), `enc` (300-frame mpph264enc → mp4), `hls` (v4l2src NV12 → mpph264enc → h264parse + alsasrc → AAC → hlssink2 in /dev/shm/obm-hls, served by `python3 -m http.server 8000`, view in VLC). Env: MIC, WIDTH/HEIGHT, BPS, FLIP, OUT.
- [x] USB mic found with `arecord -l`; test clip recorded successfully.
- [x] Repo search: capture methods and stream formats (see OpenBabyMonitor findings).
- [x] Cry model located: ONNX via OpenCV DNN.
- [x] `tools/test_crynet.py` **runs on the board** (Nov 16, 2026): onnxruntime 1.32.0, numpy 2.4.6, Python 3.11 (fine; only upstream's pins in `requirements.txt` fail on 3.11), crynet_large 41.7 ms/inference per 2.06 s clip; live mic `plughw:0,0` in a quiet room → ambient=1.000, bg ≈ -14.6 dB, contrast ≈ 2 dB. Positive test passed Nov 16, 2026 once the right card was used (`plughw:0,0` was wrong: user had read `aplay -l`; always take the card from `arecord -l`, USB line). Recording/cry side considered done. **Upstream bug found:** model ends in `nn.LogSoftmax` (natural log) but `control/listen.py:330` uses `10**out` + renormalise → probabilities sharpened to p^2.3, so upstream's 85% threshold is effectively looser. Test script now uses `np.exp`. Decide for crydetect: true probabilities with a retuned threshold, or replicate upstream for parity.
- [x] `setup_device.sh` raspi-config calls listed.
- [x] Decided: exclusive modes, as upstream.
- [x] Decided: SQLite for settings.
- [x] Decided Nov 16, 2026: MediaMTX + WebRTC (replaces the Nov 15 plain-HLS decision after 8 s HLS latency measured).

Board issues to investigate (seen Nov 15, 2026):

- [ ] **Random power-off** shortly after installing the onnxruntime venv dependencies. Suspects: undersized power supply (RK3399 under load, e.g. pip building/unpacking, needs a solid 5 V/3 A+ USB-C PD supply), thermal shutdown (check heatsink, `/sys/class/thermal/thermal_zone*/temp`), or a kernel crash. After the next boot, check `journalctl -b -1 -e` (enable persistent journal first: `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`) and `dmesg` for under-voltage/thermal messages.
- [ ] **SSH session periodically drops/suspends.** Diagnosed Nov 15, 2026: real system suspend (systemd "will suspend" broadcast in the SSH terminal; board needs a keypress on the attached keyboard to wake). logind.conf/sleep.conf are defaults and sleep targets were not masked, so the idle trigger is likely the desktop power manager. Board is on wired Ethernet (`end0`), not Wi-Fi. Fix given: `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`, plus disable desktop auto-suspend, or boot headless (`systemctl set-default multi-user.target`). Not yet confirmed applied/verified. User `radxa` lacks journal access: `sudo usermod -aG adm,systemd-journal radxa`.
- Power-off stress test (stress-ng + temperature watch) skipped for now by user; persistent journal setup still needed.
- Journal is already persistent (4 boots listed Nov 15, 2026). Board has no RTC battery: boot timestamps start stale until NTP sync. The power-off is probably boot -1 (ended 2026-11-15 17:02:01 UTC, id 8c1b6a48…); its tail (`journalctl -b 8c1b6a4802374e20a978b306bc1487a9 -n 60`) has not been inspected yet. Parked by user to continue the project.
- Last power-off needed a full power cycle (keypress did not wake it), so it is distinct from the idle suspend, unless it was a failed resume. With persistent journal on, read `journalctl -b -1 -n 100` after the next one: ends at `PM: suspend entry` = resume hang; abrupt cut = power supply; panic/thermal = kernel/heat. PSU model/rating and heatsink still unknown.

Open questions for the user:

- Reuse the upstream frontend (HTML/JS)? No. Decided: build a new UI against the Flask API, using upstream only as a feature reference.
- Is the access-point mode needed? Yes. Decided: keep it, with automatic fallback to the device's own hotspot (via NetworkManager) when home Wi-Fi is unavailable.
- Docker Compose or plain systemd services? Decided: plain systemd services, no Docker.
