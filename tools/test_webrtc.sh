#!/bin/bash
# Prototype of a low-latency Observe mode: camera + mic -> H.264 + Opus -> RTSP -> MediaMTX -> WebRTC.
#
# Usage:
#   tools/test_webrtc.sh check     # check GStreamer elements and the MediaMTX install
#   tools/test_webrtc.sh install   # download the latest MediaMTX arm64 release into $MTX_DIR
#   tools/test_webrtc.sh run       # start MediaMTX + the camera/mic pipeline; view in a browser
#   tools/test_webrtc.sh audio     # Listen mode: start MediaMTX + mic-only pipeline
#
# Environment overrides:
#   MIC=plughw:1,0   ALSA capture device (default: first USB card from arecord -l)
#   WIDTH=1280 HEIGHT=720   capture size (the ISP scales from the sensor mode)
#   BPS=2000000      H.264 bitrate in bit/s
#   GOP=30           keyframe interval in frames (a new viewer waits for the next keyframe)
#   FLIP=1           rotate 180 degrees on the sensor (0 to disable)
#   ABPS=64000       Opus bitrate in bit/s for the audio mode
#   MTX_DIR=~/mediamtx   where the MediaMTX binary and its default mediamtx.yml live
#   MTX_VERSION=v1.x.y   pin a release instead of the latest
#
# Ports used by MediaMTX (defaults): 8554 RTSP (pipeline publishes here), 8889 WebRTC page/WHEP,
# 8189/udp WebRTC media, 8888 HLS (for comparison).
set -euo pipefail

VIDEO_DEV=/dev/video0
SENSOR_SUBDEV=/dev/v4l-subdev2
WIDTH=${WIDTH:-1280}
HEIGHT=${HEIGHT:-720}
BPS=${BPS:-2000000}
GOP=${GOP:-30}
FLIP=${FLIP:-1}
MTX_DIR=${MTX_DIR:-$HOME/mediamtx}
MTX_VERSION=${MTX_VERSION:-}
STREAM=cam
AUDIO_STREAM=mic
LOG=/dev/shm/mediamtx.log

have() { gst-inspect-1.0 "$1" >/dev/null 2>&1; }

detect_mic() {
    local card dev
    read -r card dev < <(arecord -l | sed -nE '/USB/ s/^card ([0-9]+):.*device ([0-9]+):.*/\1 \2/p' | head -1)
    [ -n "${card:-}" ] || { echo "No USB capture device found in 'arecord -l'; set MIC=" >&2; exit 1; }
    echo "plughw:$card,$dev"
}

set_orientation() {
    # The flip controls change the Bayer layout, so they must be set before streaming starts.
    v4l2-ctl -d $SENSOR_SUBDEV -c horizontal_flip=$FLIP,vertical_flip=$FLIP
}

install_mediamtx() {
    local api url
    if [ -n "$MTX_VERSION" ]; then
        api=https://api.github.com/repos/bluenviron/mediamtx/releases/tags/$MTX_VERSION
    else
        api=https://api.github.com/repos/bluenviron/mediamtx/releases/latest
    fi
    # Asset names have changed between releases (arm64v8 vs arm64), so match either.
    url=$(curl -fsSL "$api" | grep -oE '"browser_download_url": *"[^"]*linux_arm64[^"]*\.tar\.gz"' \
          | head -1 | sed -E 's/.*"(https[^"]+)"/\1/')
    [ -n "$url" ] || { echo "Could not find a linux arm64 asset at $api" >&2; exit 1; }
    echo "Downloading $url"
    mkdir -p "$MTX_DIR"
    curl -fL "$url" | tar -xz -C "$MTX_DIR"
    "$MTX_DIR/mediamtx" --version || true
    echo "Installed into $MTX_DIR (binary + default mediamtx.yml)"
}

start_mediamtx() {
    [ -x "$MTX_DIR/mediamtx" ] || { echo "MediaMTX not found in $MTX_DIR; run: $0 install" >&2; exit 1; }
    have rtspclientsink || { echo "rtspclientsink missing: sudo apt install gstreamer1.0-rtsp" >&2; exit 1; }
    # MediaMTX reads mediamtx.yml from its working directory; the shipped default accepts any
    # path name for publishing and serves it over RTSP, WebRTC and HLS.
    (cd "$MTX_DIR" && exec ./mediamtx) >"$LOG" 2>&1 &
    MTX_PID=$!
    trap 'kill $MTX_PID 2>/dev/null' EXIT
    sleep 1
    kill -0 $MTX_PID 2>/dev/null || { echo "MediaMTX failed to start:" >&2; cat "$LOG" >&2; exit 1; }
    IP=$(hostname -I | awk '{print $1}')
}

# WebRTC carries Opus, not AAC, so audio is encoded with opusenc at 48 kHz.
audio_branch() {
    echo "alsasrc device=$MIC ! queue ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1" \
         "! opusenc bitrate=${1:-64000} ! queue"
}

case "${1:-}" in
check)
    for e in v4l2src mpph264enc h264parse alsasrc audioconvert audioresample opusenc rtspclientsink; do
        printf '%-15s ' $e; have $e && echo yes || echo NO
    done
    have rtspclientsink || echo "  -> rtspclientsink: sudo apt install gstreamer1.0-rtsp"
    have opusenc        || echo "  -> opusenc: sudo apt install gstreamer1.0-plugins-base"
    echo
    echo "mpph264enc profile values:"
    have mpph264enc && gst-inspect-1.0 mpph264enc | sed -n '/^  profile/,/^  [a-z]/p' | head -12 || true
    echo
    if [ -x "$MTX_DIR/mediamtx" ]; then
        printf 'MediaMTX        '; "$MTX_DIR/mediamtx" --version
    else
        echo "MediaMTX        NOT installed in $MTX_DIR (run: $0 install)"
    fi
    ;;
install)
    install_mediamtx
    ;;
run)
    MIC=${MIC:-$(detect_mic)}
    set_orientation
    start_mediamtx
    echo "Mic: $MIC  Size: ${WIDTH}x$HEIGHT  Bitrate: $BPS  GOP: $GOP  MediaMTX log: $LOG"
    echo "WebRTC: open http://$IP:8889/$STREAM/ in Chrome/Firefox (Ctrl+C to stop)"
    echo "HLS (comparison): http://$IP:8888/$STREAM/"

    # profile=66 (baseline): no B-frames, decodable by every browser's WebRTC stack.
    gst-launch-1.0 -e \
        rtspclientsink name=rtsp location=rtsp://127.0.0.1:8554/$STREAM protocols=tcp \
        v4l2src device=$VIDEO_DEV ! video/x-raw,format=NV12,width=$WIDTH,height=$HEIGHT ! queue \
            ! mpph264enc bps=$BPS gop=$GOP profile=66 ! h264parse config-interval=-1 ! queue ! rtsp. \
        $(audio_branch) ! rtsp.
    ;;
audio)
    # Listen mode: microphone only, no camera.
    MIC=${MIC:-$(detect_mic)}
    start_mediamtx
    echo "Mic: $MIC  Opus: ${ABPS:-64000} bit/s  MediaMTX log: $LOG"
    echo "WebRTC: open http://$IP:8889/$AUDIO_STREAM/ in Chrome/Firefox (Ctrl+C to stop)"
    gst-launch-1.0 -e \
        rtspclientsink name=rtsp location=rtsp://127.0.0.1:8554/$AUDIO_STREAM protocols=tcp \
        $(audio_branch "${ABPS:-64000}") ! rtsp.
    ;;
*)
    sed -n '2,22p' "$0"; exit 1
    ;;
esac
