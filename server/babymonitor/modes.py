"""Main page and the mode API (Standby / Notify / Listen / Observe)."""
from flask import Blueprint, jsonify, render_template, request

from .agent_client import MODES, AgentError, get_agent
from .auth import login_required

bp = Blueprint('modes', __name__)

# Which MediaMTX path the browser should play in each mode (None: nothing to play).
STREAM_PATHS = {'standby': None, 'notify': None, 'listen': 'mic', 'observe': 'cam'}


def mode_state(mode):
    return {'mode': mode, 'stream': STREAM_PATHS[mode]}


@login_required
def index():
    return render_template('index.html', modes=MODES)


@bp.route('/api/mode', methods=['GET'])
@login_required
def get_mode():
    try:
        return jsonify(mode_state(get_agent().request('get_mode')['mode']))
    except AgentError as e:
        return jsonify(error=str(e)), 503


@bp.route('/api/mode', methods=['PUT'])
@login_required
def set_mode():
    mode = (request.get_json(silent=True) or {}).get('mode')
    if mode not in MODES:
        return jsonify(error=f'mode must be one of {", ".join(MODES)}'), 400
    try:
        return jsonify(mode_state(get_agent().request('set_mode', mode=mode)['mode']))
    except AgentError as e:
        return jsonify(error=str(e)), 503
