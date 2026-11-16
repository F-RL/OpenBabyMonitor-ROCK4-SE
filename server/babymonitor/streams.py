"""Access control for the media streams.

The HTTPS proxy (Caddy `forward_auth`) calls /auth/stream before forwarding a WHEP
request to MediaMTX. The browser's session cookie comes along because the player
talks to the same origin, so a logged-in browser gets 204 and anyone else 401.
MediaMTX itself only listens on localhost (see deploy/mediamtx.yml).
"""
from flask import Blueprint

from .auth import is_logged_in

bp = Blueprint('streams', __name__)


@bp.route('/auth/stream')
def check_stream_access():
    return ('', 204) if is_logged_in() else ('', 401)
