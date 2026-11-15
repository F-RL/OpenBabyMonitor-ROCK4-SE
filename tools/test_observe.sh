#!/bin/bash
# Prototype of the Observe mode pipeline: camera + mic -> H.264 + AAC -> HLS.
#
# Usage:
#   tools/test_observe.sh check    # list which GStreamer elements are available
#   tools/test_observe.sh enc      # 10 s video-only test into enc_test.mp4
#   tools/test_observe.sh hls      # live HLS into $OUT, served over HTTP on :8000
#
# Environment overrides:
#   MIC=plughw:1,0   ALSA capture device (default: first USB card from arecord -l)
#   WIDTH=1280 HEIGHT=720   capture size (the ISP scales from the sensor mode)
#   BPS=2000000      H.264 bitrate in bit/s
#   FLIP=1           rotate 180 degrees on the sensor (0 to disable)
#   OUT=/dev/shm/obm-hls   output directory for the playlist and segments
set -euo pipefail

VIDEO_DEV=/dev/video0
SENSOR_SUBDEV=/dev/v4l-subdev2
WIDTH=${WIDTH:-1280}
HEIGHT=${HEIGHT:-720}
BPS=${BPS:-2000000}
FLIP=${FLIP:-1}
OUT=${OUT:-/dev/shm/obm-hls}
SEGMENT_SECONDS=2
GOP=30   # keyframe interval in frames; must give a keyframe at least every SEGMENT_SECONDS

have() { gst-inspect-1.0 "$1" >/dev/null 2>&1; }

detect_mic() {
    local card dev
    read -r card dev < <(arecord -l | sed -nE '/USB/ s/^card ([0-9]+):.*device ([0-9]+):.*/\1 \2/p' | head -1)
    [ -n "${card:-}" ] || { echo "No USB capture device found in 'arecord -l'; set MIC=" >&2; exit 1; }
    echo "plughw:$card,$dev"
}

aac_encoder() {
    for e in voaacenc avenc_aac fdkaacenc; do
        have $e && { echo $e; return; }
    done
    echo "No AAC encoder found (voaacenc, avenc_aac, fdkaacenc)" >&2; exit 1
}

set_orientation() {
    # The flip controls change the Bayer layout, so they must be set before streaming starts.
    v4l2-ctl -d $SENSOR_SUBDEV -c horizontal_flip=$FLIP,vertical_flip=$FLIP
}

video_src() {
    echo "v4l2src device=$VIDEO_DEV ${1:-} ! video/x-raw,format=NV12,width=$WIDTH,height=$HEIGHT ! queue"
}

video_enc() {
    echo "mpph264enc bps=$BPS gop=$GOP ! h264parse config-interval=-1"
}

case "${1:-}" in
check)
    for e in v4l2src mpph264enc x264enc h264parse alsasrc audioconvert audioresample \
             voaacenc avenc_aac fdkaacenc aacparse hlssink2 mp4mux videoflip; do
        printf '%-14s ' $e; have $e && echo yes || echo NO
    done
    echo
    echo "mpph264enc properties:"
    have mpph264enc && gst-inspect-1.0 mpph264enc | sed -n '/Element Properties/,$p' | grep -E '^  [a-z]' || true
    ;;
enc)
    set_orientation
    echo "Encoding 300 frames to enc_test.mp4 (watch CPU in another shell with: top)"
    time gst-launch-1.0 -e $(video_src num-buffers=300) ! $(video_enc) ! mp4mux ! filesink location=enc_test.mp4
    ls -l enc_test.mp4
    ;;
hls)
    MIC=${MIC:-$(detect_mic)}
    AAC=$(aac_encoder)
    set_orientation
    rm -rf "$OUT"; mkdir -p "$OUT"
    echo "Mic: $MIC  AAC: $AAC  Size: ${WIDTH}x$HEIGHT  Output: $OUT"
    (cd "$OUT" && exec python3 -m http.server 8000 >/dev/null 2>&1) &
    HTTP_PID=$!
    trap 'kill $HTTP_PID 2>/dev/null' EXIT
    echo "Open http://$(hostname):8000/index.m3u8 in VLC (Media > Open Network Stream). Ctrl+C to stop."
    gst-launch-1.0 -e \
        hlssink2 name=hls location="$OUT/segment%05d.ts" playlist-location="$OUT/index.m3u8" \
            target-duration=$SEGMENT_SECONDS playlist-length=3 max-files=6 \
        $(video_src) ! $(video_enc) ! hls.video \
        alsasrc device="$MIC" ! queue ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 \
            ! $AAC bitrate=64000 ! aacparse ! queue ! hls.audio
    ;;
*)
    sed -n '2,15p' "$0"; exit 1
    ;;
esac
