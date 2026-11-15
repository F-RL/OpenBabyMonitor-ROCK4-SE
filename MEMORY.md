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

libcamera (Raspberry Pi fork, version 0.8.0) was built from source into `/usr/local` with GCC and compiled successfully; it is not needed for the chosen V4L2 approach. rpicam-apps was not built. Consider removing the `/usr/local` libcamera install later so it does not confuse other software.

| Problem | Fix applied |
| --- | --- |
| No prebuilt rpicam-apps outside Raspberry Pi OS | Build from source (later abandoned) |
| `libdrm-dev` unmet dependency on `libdrm2` | Install from `bookworm-backports` |
| meson: `ipas` does not accept `auto` | Dropped `-Dipas=auto`; IPAs follow enabled pipelines |
| ninja: `build.ninja` missing | meson setup had failed; rerun and read the error |
| `libevent_pthreads` not found | `apt install libevent-dev` |
| Clang `-Wc99-designator` error under `-Werror` | Build with GCC: `CC=gcc CXX=g++ meson setup …` |
| `GLES2/gl2.h` missing (software ISP GPU path) | `apt install libgles-dev libegl-dev` |

On this system `c++` points to Clang. Use GCC explicitly for any native C/C++ builds.

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

Agreed Nov 15, 2026: Flask for control and settings; media never passes through Python. Streams are plain HLS files written to a tmpfs directory (e.g. `/run/babymonitor/stream/`) and served directly by the HTTPS proxy. No MediaMTX (dropped Nov 15, 2026; revisit only if HLS latency, typically several seconds, proves unacceptable, e.g. for WebRTC).

The browser reaches everything through one HTTPS proxy: Flask serves the UI and API; the proxy serves HLS files as static content (behind the Flask login, e.g. Caddy `forward_auth` / nginx `auth_request`); the host agent does the few things that need root.

| systemd unit | Runtime | Device access | Role |
| --- | --- | --- | --- |
| `babymonitor-web.service` | Python + Flask + gunicorn in a venv, SQLite | None | Login, mode switching, settings, cry alerts to the browser (SSE) |
| `babymonitor-crydetect.service` (Notify) | Python + onnxruntime in a venv | `audio` group; opens ALSA mic directly (`arecord`) | Runs threshold or ONNX model, posts events to web |
| `babymonitor-audiostream.service` (Listen) | ffmpeg | `audio` group | ALSA → MP3 → HLS, as upstream `audiostream.py` |
| `babymonitor-videostream.service` (Observe) | GStreamer (`gst-launch-1.0` or small Python wrapper) | `video` + `audio` groups; `/dev/mpp_service` | `v4l2src /dev/video0` → `mpph264enc` (fallback `x264enc`) + ALSA → AAC → `hlssink2` |
| `caddy.service` (or nginx) | Debian package | Port 443 | TLS with a self-signed or internal certificate; serves HLS dir |

Mode units are mutually exclusive (`Conflicts=` between them); the web service switches modes by asking the host agent to start a unit (or via a polkit rule limited to `babymonitor-*` units).

Also on the host: `rkisp_3A.service` (needs the vendor ISP libraries), NetworkManager for Wi-Fi client and hotspot (`nmcli`), and a small host agent exposing only whitelisted actions (mode switch, country, time zone, network switch, reboot, shutdown) to the web service over a Unix socket.

Design notes:

- Modes are exclusive (decided Nov 15, 2026), as upstream: Standby runs nothing; Notify runs only crydetect; Listen runs only the audio stream; Observe runs only the audio+video stream. One process holds the mic at a time, so no mic sharing is needed; stop the previous mode's unit before starting the next.
- Settings and state live in SQLite (decided Nov 15, 2026), replacing upstream MariaDB.
- With no containers, the capture pipeline uses Rockchip's hardware-encoder GStreamer plugin and vendor libraries directly. Run web, crydetect and stream units as unprivileged service users; only the host agent runs as root.
- Upstream encrypts HLS (AES-128, per-start key). Over HTTPS with an authenticated proxy this is optional; decide later.
- Frontend can play HLS with hls.js (video and audio); Safari plays it natively.

## Next steps and open questions

The first job is to prove the media inputs on the host before writing any server code.

- [ ] Capture a still and a short video from `/dev/video0` with the commands above; confirm `systemctl status rkisp_3A` is active and the image is well exposed.
- [ ] Check for the hardware H.264 encoder: `ls /dev/mpp_service` and `gst-inspect-1.0 | grep -i mpp` (look for `mpph264enc`). Fall back to `x264enc` at 720p if absent.
- [x] USB mic found with `arecord -l`; test clip recorded successfully.
- [x] Repo search: capture methods and stream formats (see OpenBabyMonitor findings).
- [x] Cry model located: ONNX via OpenCV DNN.
- [ ] Run `tools/test_crynet.py` on the board (copy `config control detection tools` to `~/obm`; venv `~/obm-venv` with `onnxruntime numpy scipy python_speech_features`). First without args (random input, timing), then `--device plughw:<card>,<dev>` for the live Notify path. The venv deps were installed, but the script has not been run yet (board powered off, see below).
- [x] `setup_device.sh` raspi-config calls listed.
- [x] Decided: exclusive modes, as upstream.
- [x] Decided: SQLite for settings.
- [x] Decided: plain HLS (ffmpeg / GStreamer `hlssink2`), no MediaMTX.
- [ ] On the board: check `gst-inspect-1.0 hlssink2` and an AAC encoder (`avenc_aac` or `voaacenc`) are available.

Board issues to investigate (seen Nov 15, 2026):

- [ ] **Random power-off** shortly after installing the onnxruntime venv dependencies. Suspects: undersized power supply (RK3399 under load, e.g. pip building/unpacking, needs a solid 5 V/3 A+ USB-C PD supply), thermal shutdown (check heatsink, `/sys/class/thermal/thermal_zone*/temp`), or a kernel crash. After the next boot, check `journalctl -b -1 -e` (enable persistent journal first: `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`) and `dmesg` for under-voltage/thermal messages.
- [ ] **SSH session periodically drops/suspends.** Likely system suspend or Wi-Fi power saving. Check `systemctl status sleep.target suspend.target`; fix with `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`, and disable Wi-Fi power save (`nmcli connection modify <conn> 802-11-wireless.powersave 2`). Client side: `ServerAliveInterval 30` in `~/.ssh/config`. A baby monitor must never sleep, so masking suspend is required anyway.

Open questions for the user:

- Reuse the upstream frontend (HTML/JS)? No. Decided: build a new UI against the Flask API, using upstream only as a feature reference.
- Is the access-point mode needed? Yes. Decided: keep it, with automatic fallback to the device's own hotspot (via NetworkManager) when home Wi-Fi is unavailable.
- Docker Compose or plain systemd services? Decided: plain systemd services, no Docker.
