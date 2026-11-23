"""Notify mode API: settings, and the cry detector's live readings and alerts.

The cry detector (crydetect/, run by babymonitor-crydetect.service) writes its latest
measurement to the `runtime` table and alerts to `notify_events`; the browser polls
GET /api/notify every couple of seconds while Notify is active. Polling instead of
server-sent events keeps gunicorn's few threads free (an open tab would hold one).
"""
import time

from flask import Blueprint, jsonify, request

from . import notify_settings
from .auth import login_required
from .db import get_db, get_setting, last_notify_event_id, notify_events_after, read_value, \
    set_setting

bp = Blueprint('notify', __name__)

STATUS_KEY = 'notify_status'


def load_settings():
    return notify_settings.merged(get_setting(notify_settings.SETTINGS_KEY))


@bp.route('/api/notify/settings', methods=['GET'])
@login_required
def get_settings():
    return jsonify(values=load_settings(), spec=notify_settings.describe())


@bp.route('/api/notify/settings', methods=['PUT'])
@login_required
def put_settings():
    changes = request.get_json(silent=True)
    if not isinstance(changes, dict):
        return jsonify(error='expected a JSON object of settings'), 400
    try:
        settings = notify_settings.updated(load_settings(), changes)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    set_setting(notify_settings.SETTINGS_KEY, settings)
    return jsonify(values=settings, spec=notify_settings.describe())


@bp.route('/api/notify', methods=['GET'])
@login_required
def get_state():
    """Latest reading plus alerts newer than ?after=<id>.

    Without `after` no old alerts are returned, only `last_id` to continue from, so a
    freshly opened page doesn't replay alerts from earlier in the night.
    """
    conn = get_db()
    status = read_value(conn, 'runtime', STATUS_KEY)
    if status is not None:
        status['age'] = round(time.time() - status['time'], 1)
    after = request.args.get('after', type=int)
    if after is None:
        events = []
        last_id = last_notify_event_id(conn)
    else:
        events = notify_events_after(conn, after)
        last_id = events[-1]['id'] if events else after
    return jsonify(status=status, events=events, last_id=last_id)
