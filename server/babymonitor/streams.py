"""Hooks the HTTPS proxy (Caddy) calls: stream access control and certificate approval.

Before forwarding a WHEP request to MediaMTX, Caddy's `forward_auth` calls
/auth/stream. The browser's session cookie comes along because the player talks to
the same origin, so a logged-in browser gets 204 and anyone else 401. MediaMTX
itself only listens on localhost (see deploy/mediamtx.yml).
"""
import ipaddress
import socket

from flask import Blueprint, request

from .auth import is_logged_in

bp = Blueprint('streams', __name__)


@bp.route('/auth/stream')
def check_stream_access():
    return ('', 204) if is_logged_in() else ('', 401)


def is_own_name(domain):
    """True for IP addresses and the board's own host names."""
    try:
        ipaddress.ip_address(domain)
        return True
    except ValueError:
        pass
    hostname = socket.gethostname().lower()
    return domain.lower() in ('localhost', hostname, f'{hostname}.local')


@bp.route('/auth/tls-ask')
def approve_certificate():
    """Caddy's on-demand TLS asks here before issuing a certificate for a name.

    The board is reached by IP (which changes between home Wi-Fi and its own
    hotspot) or by host name, so certificates are issued on first use, but only
    for those, never for arbitrary names someone points at the device.
    """
    return ('', 200) if is_own_name(request.args.get('domain', '')) else ('', 403)
