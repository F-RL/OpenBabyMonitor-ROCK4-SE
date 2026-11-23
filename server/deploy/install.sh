#!/bin/bash
# Install or update the baby monitor on a ROCK 4SE (Radxa Debian bookworm).
#
#   sudo server/deploy/install.sh
#
# Safe to run again: it is also the update command after `git pull`. It keeps your
# settings (/etc/babymonitor/streams.env, the database) and only asks questions for
# things that are not set up yet:
#   - the web password                (or set BM_PASSWORD)
#   - the fallback hotspot password   (or set BM_HOTSPOT_PASSWORD; empty skips it)
#   - the phones' notification password (or set BM_NTFY_PASSWORD; empty skips it)
# Other overrides: MIC=plughw:1,0 (default: first USB card in `arecord -l`),
# MTX_VERSION=v1.x.y (MediaMTX release, default: latest; only used when not installed),
# NTFY_BASE_URL=https://name:8443 (address phones use for notifications, default
# https://<hostname>.local:8443; only used when /etc/babymonitor/ntfy.yml doesn't exist).
set -euo pipefail

REPO=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)
PREFIX=/opt/babymonitor
CONF=/etc/babymonitor
DATA=/var/lib/babymonitor
USER_NAME=babymonitor
DB=$DATA/babymonitor.sqlite3
UNITS=(babymonitor-agent babymonitor-mediamtx babymonitor-ntfy babymonitor-web)
NTFY_TOPIC=babymonitor
NTFY_PHONE_USER=parent

step() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
interactive() { [ -t 0 ]; }

[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0" >&2; exit 1; }
[ -f "$REPO/server/hostagent.py" ] || { echo "Can't find the repository at $REPO" >&2; exit 1; }

step "Packages"
apt-get update -qq
# dnsmasq-base: DHCP for the hotspot. The Rockchip encoder (gstreamer1.0-rockchip1)
# comes from Radxa's repository and is checked below rather than installed.
# avahi-daemon: the board answers to <hostname>.local.
apt-get install -y -qq caddy python3-venv alsa-utils v4l-utils iw dnsmasq-base curl avahi-daemon \
    gstreamer1.0-tools gstreamer1.0-alsa gstreamer1.0-rtsp \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad >/dev/null
for element in mpph264enc rtspclientsink opusenc alsasrc v4l2src; do
    gst-inspect-1.0 "$element" >/dev/null 2>&1 || warn "GStreamer element $element is missing"
done

step "Service user and directories"
id "$USER_NAME" >/dev/null 2>&1 ||
    useradd --system --home "$DATA" --shell /usr/sbin/nologin --groups audio,video "$USER_NAME"
usermod -aG audio,video "$USER_NAME"
install -d -m 755 "$PREFIX" "$CONF"
# The service user owns only its data; the code in /opt stays root-owned.
install -d -o "$USER_NAME" -g "$USER_NAME" -m 750 "$DATA"

step "Code and Python environment"
rm -rf "$PREFIX/server.new"
cp -r "$REPO/server" "$PREFIX/server.new"
find "$PREFIX/server.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$PREFIX/server"
mv "$PREFIX/server.new" "$PREFIX/server"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q -r "$PREFIX/server/requirements.txt"

step "MediaMTX"
if [ -x "$PREFIX/mediamtx/mediamtx" ]; then
    echo "Installed: $("$PREFIX/mediamtx/mediamtx" --version 2>/dev/null || echo unknown version)"
else
    MTX_DIR="$PREFIX/mediamtx" bash "$REPO/tools/test_webrtc.sh" install
fi

step "Configuration"
install -m 644 "$REPO/server/deploy/mediamtx.yml" "$CONF/mediamtx.yml"
card_exists() { arecord -l 2>/dev/null | grep -q "^card ${1}:"; }
if [ ! -f "$CONF/streams.env" ]; then
    mic=${MIC:-}
    if [ -z "$mic" ]; then
        read -r card dev < <(arecord -l | sed -nE '/USB/ s/^card ([0-9]+):.*device ([0-9]+):.*/\1 \2/p' | head -1) || true
        if [ -n "${card:-}" ]; then
            mic="plughw:$card,$dev"
        else
            mic="plughw:1,0"
            warn "No USB microphone found in 'arecord -l'; using $mic. Fix MIC in $CONF/streams.env"
        fi
    fi
    sed -E "s/^MIC=.*/MIC=$mic/" "$REPO/server/deploy/streams.env" > "$CONF/streams.env"
    echo "Created $CONF/streams.env with MIC=$mic"
else
    mic=$(sed -nE 's/^MIC=//p' "$CONF/streams.env")
    echo "Keeping $CONF/streams.env (MIC=$mic)"
fi
card=$(sed -nE 's/^plughw:([0-9]+),.*/\1/p' <<<"$mic")
if [ -n "$card" ] && ! card_exists "$card"; then
    warn "MIC=$mic: there is no card $card in 'arecord -l'. Fix MIC in $CONF/streams.env"
fi
if [ -f /etc/caddy/Caddyfile ] && ! cmp -s /etc/caddy/Caddyfile "$REPO/server/deploy/Caddyfile" \
        && ! grep -q 'baby monitor' /etc/caddy/Caddyfile; then
    cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-babymonitor
    echo "Saved the previous Caddyfile as /etc/caddy/Caddyfile.before-babymonitor"
fi
install -m 644 "$REPO/server/deploy/Caddyfile" /etc/caddy/Caddyfile

step "Phone notifications (ntfy)"
if [ -x "$PREFIX/ntfy/ntfy" ]; then
    echo "Installed: $PREFIX/ntfy/ntfy"
else
    url=$(curl -fsSL https://api.github.com/repos/binwiederhier/ntfy/releases/latest \
          | grep -oE '"browser_download_url": *"[^"]*linux_arm64\.tar\.gz"' | head -1 \
          | sed -E 's/.*"(https[^"]+)"/\1/')
    [ -n "$url" ] || { echo "Could not find the ntfy arm64 release" >&2; exit 1; }
    echo "Downloading $url"
    install -d -m 755 "$PREFIX/ntfy"
    curl -fsSL "$url" | tar -xz --strip-components=1 -C "$PREFIX/ntfy"
fi
install -d -o "$USER_NAME" -g "$USER_NAME" -m 750 "$DATA/ntfy"
if [ ! -f "$CONF/ntfy.yml" ]; then
    base=${NTFY_BASE_URL:-https://$(hostname).local:8443}
    sed "s|BASE_URL|$base|" "$REPO/server/deploy/ntfy.yml" > "$CONF/ntfy.yml"
    echo "Created $CONF/ntfy.yml (phones use $base)"
fi
public_url=$(sed -nE 's/^base-url: *"?([^"]*)"?.*/\1/p' "$CONF/ntfy.yml")
ntfy_cli() {
    runuser -u "$USER_NAME" -- env NTFY_CONFIG_FILE="$CONF/ntfy.yml" \
        NTFY_AUTH_FILE="$DATA/ntfy/user.db" "$PREFIX/ntfy/ntfy" "$@"
}
ntfy_user_exists() { ntfy_cli user list 2>&1 | grep -qE "^user $1( |$)"; }
ntfy_add_user() {  # name password
    ntfy_user_exists "$1" ||
        printf '%s\n%s\n' "$2" "$2" | NTFY_PASSWORD="$2" ntfy_cli user add "$1" >/dev/null
}
if ! grep -qs '^NTFY_TOKEN=tk_' "$CONF/ntfy.env"; then
    # The detector publishes as 'monitor', which may only write; its password is never used.
    ntfy_add_user monitor "$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
    ntfy_cli access monitor "$NTFY_TOPIC" write-only >/dev/null
    token=$(ntfy_cli token add monitor | grep -oE 'tk_[A-Za-z0-9]+' | head -1)
    [ -n "$token" ] || { echo "Could not create an ntfy token" >&2; exit 1; }
    (umask 027 && printf '%s\n' "NTFY_URL=http://127.0.0.1:2586" "NTFY_TOPIC=$NTFY_TOPIC" \
        "NTFY_TOKEN=$token" "NTFY_PUBLIC_URL=$public_url" "NTFY_PHONE_USER=$NTFY_PHONE_USER" \
        > "$CONF/ntfy.env")
    chgrp "$USER_NAME" "$CONF/ntfy.env"
    echo "Created $CONF/ntfy.env with a publishing token"
fi
sed -i -E "s|^NTFY_PUBLIC_URL=.*|NTFY_PUBLIC_URL=$public_url|" "$CONF/ntfy.env"
if ntfy_user_exists "$NTFY_PHONE_USER"; then
    echo "Phone user '$NTFY_PHONE_USER' exists."
else
    password=${BM_NTFY_PASSWORD:-}
    if [ -z "$password" ] && interactive; then
        echo "Phones log in to the notification server as '$NTFY_PHONE_USER'."
        echo "Choose its password, or press Enter to skip phone notifications for now."
        read -r -s -p "Notification password: " password; echo
    fi
    if [ -n "$password" ]; then
        ntfy_add_user "$NTFY_PHONE_USER" "$password"
        ntfy_cli access "$NTFY_PHONE_USER" "$NTFY_TOPIC" read-only >/dev/null
        echo "Phone user '$NTFY_PHONE_USER' created."
    else
        echo "Skipped: no phone can subscribe until a password is set (run this script again)."
    fi
fi

step "Services"
install -m 644 "$REPO"/server/deploy/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q "${UNITS[@]}"
# Stop a running mode first: its unit file may have changed. The mode starts again
# when chosen on the page.
systemctl stop babymonitor-crydetect babymonitor-audiostream babymonitor-videostream 2>/dev/null || true
systemctl restart "${UNITS[@]}"
systemctl enable -q caddy
systemctl reload-or-restart caddy
# A monitor must never go to sleep (the desktop image suspends when idle).
systemctl mask -q sleep.target suspend.target hibernate.target hybrid-sleep.target

flask_cli() {
    (cd "$PREFIX/server" && runuser -u "$USER_NAME" -- env BM_DATABASE="$DB" \
        "$PREFIX/venv/bin/flask" --app babymonitor "$@")
}

step "Web password"
if flask_cli password-is-set >/dev/null 2>&1; then
    echo "Already set (change it on the settings page)."
elif [ -n "${BM_PASSWORD:-}" ]; then
    flask_cli set-password --password "$BM_PASSWORD"
elif interactive; then
    flask_cli set-password
else
    warn "No web password yet. Set one with: BM_PASSWORD=... sudo $0"
fi

step "Fallback hotspot"
hotspot() { (cd "$PREFIX/server" && python3 -c "$1" "${@:2}"); }
if hotspot 'import sys, hostagent_network as n; sys.exit(n.Network().hotspot_config() is None)'; then
    echo "Already set up (change it on the settings page)."
else
    password=${BM_HOTSPOT_PASSWORD:-}
    if [ -z "$password" ] && interactive; then
        echo "If no known Wi-Fi is in range, the monitor starts its own network named"
        echo "\"$(hostname)\". Choose its password (8-63 characters), or press Enter to skip."
        read -r -s -p "Hotspot password: " password; echo
    fi
    if [ -n "$password" ]; then
        hotspot 'import sys, socket, hostagent_network as n; n.Network().set_hotspot(socket.gethostname(), sys.argv[1])' \
            "$password" && echo "Hotspot \"$(hostname)\" saved."
    else
        echo "Skipped: no fallback until a hotspot is saved on the settings page."
    fi
fi

step "Certificate for phones"
# Phones must trust Caddy's own certificate authority (for the ntfy app, and to stop
# the browser warning). The settings page offers this copy for download.
ca=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
for _ in 1 2 3 4 5; do [ -f "$ca" ] && break; sleep 1; done
if [ -f "$ca" ]; then
    install -o "$USER_NAME" -g "$USER_NAME" -m 644 "$ca" "$DATA/caddy-root.crt"
    echo "Download it on the settings page (Phone notifications)."
else
    warn "Caddy's root certificate isn't there yet: open the web page once, then run this again"
fi

step "Checks"
[ -e /dev/video0 ] || warn "No camera at /dev/video0 (enable the camera overlay with rsetup and reboot)"
systemctl is-active -q rkisp_3A 2>/dev/null || warn "rkisp_3A is not running: the image will have no auto exposure"
for unit in "${UNITS[@]}" caddy; do
    systemctl is-active -q "$unit" || warn "$unit is not running: journalctl -u $unit -n 50"
done

ip=$(hostname -I | awk '{print $1}')
cat <<EOF

Done. Open https://$(hostname).local/ or https://${ip:-<board-ip>}/
The browser warns about the certificate until it trusts the board's certificate
(download it on the settings page). Phone notifications: see Settings on the page.
EOF
