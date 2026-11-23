"""Settings page and its API: Wi-Fi, hotspot, country, time zone, clock, host name, power.

Everything that changes the system goes through the root host agent, which
validates again; this layer only shapes requests and replies.
"""
from flask import Blueprint, jsonify, render_template, request

from .agent_client import AgentError, get_agent
from .auth import login_required

bp = Blueprint('system', __name__)


def call(action, **params):
    try:
        result = get_agent().request(action, **params)
    except AgentError as e:
        return jsonify(error=str(e)), 503 if e.unreachable else 400
    result.pop('ok', None)
    return jsonify(result)


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.route('/settings')
@login_required
def settings_page():
    return render_template('settings.html')


@bp.route('/api/system')
@login_required
def system_status():
    return call('system_status')


@bp.route('/api/system/timezones')
@login_required
def timezones():
    return call('list_timezones')


@bp.route('/api/system/timezone', methods=['PUT'])
@login_required
def set_timezone():
    return call('set_timezone', timezone=body().get('timezone'))


@bp.route('/api/system/hostname', methods=['PUT'])
@login_required
def set_hostname():
    return call('set_hostname', hostname=body().get('hostname'))


@bp.route('/api/system/time', methods=['POST'])
@login_required
def set_time():
    """Pages send the browser's clock; the agent uses it only if the board's is unset."""
    return call('set_time', time=body().get('time'))


@bp.route('/api/system/<any(reboot, shutdown):action>', methods=['POST'])
@login_required
def power(action):
    return call(action)


@bp.route('/api/network')
@login_required
def network_status():
    return call('network_status')


@bp.route('/api/network/scan')
@login_required
def scan():
    return call('wifi_scan')


@bp.route('/api/network/connect', methods=['POST'])
@login_required
def connect():
    data = body()
    return call('wifi_connect', ssid=data.get('ssid'), password=data.get('password') or None)


@bp.route('/api/network/saved/<uuid>', methods=['DELETE'])
@login_required
def forget(uuid):
    return call('wifi_forget', uuid=uuid)


@bp.route('/api/network/hotspot', methods=['PUT'])
@login_required
def set_hotspot():
    data = body()
    return call('set_hotspot', ssid=data.get('ssid'), password=data.get('password'),
                channel=data.get('channel', 0))


@bp.route('/api/network/hotspot/<any(start, stop):action>', methods=['POST'])
@login_required
def hotspot_switch(action):
    return call(f'{action}_hotspot')


@bp.route('/api/network/country', methods=['PUT'])
@login_required
def set_country():
    return call('set_country', country=body().get('country'))
