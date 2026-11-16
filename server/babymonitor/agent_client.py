"""Client for the root host agent (server/hostagent.py).

The agent listens on a Unix socket and accepts one JSON request per connection:
    {"action": "set_mode", "mode": "observe"}  ->  {"ok": true, "mode": "observe"}
With AGENT_SOCKET = 'fake' an in-process stand-in is used, for development on a PC.
"""
import json
import socket

from flask import current_app

MODES = ('standby', 'notify', 'listen', 'observe')
TIMEOUT_S = 15  # starting a stream unit can take a few seconds


class AgentError(Exception):
    pass


class SocketAgent:
    def __init__(self, path):
        self.path = path

    def request(self, action, **params):
        payload = json.dumps(dict(action=action, **params)).encode() + b'\n'
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(TIMEOUT_S)
                s.connect(self.path)
                s.sendall(payload)
                s.shutdown(socket.SHUT_WR)
                response = b''.join(iter(lambda: s.recv(4096), b''))
        except OSError as e:
            raise AgentError(f'host agent unreachable: {e}') from e
        try:
            result = json.loads(response)
        except ValueError as e:
            raise AgentError('invalid reply from host agent') from e
        if not result.get('ok'):
            raise AgentError(result.get('error', 'host agent refused the request'))
        return result


class FakeAgent:
    """Remembers the mode in memory and otherwise does nothing."""

    def __init__(self):
        self.mode = 'standby'

    def request(self, action, **params):
        if action == 'get_mode':
            return {'ok': True, 'mode': self.mode}
        if action == 'set_mode':
            if params.get('mode') not in MODES:
                raise AgentError(f'unknown mode: {params.get("mode")}')
            self.mode = params['mode']
            return {'ok': True, 'mode': self.mode}
        if action in ('reboot', 'shutdown'):
            return {'ok': True}
        raise AgentError(f'unknown action: {action}')


def get_agent():
    return current_app.extensions['babymonitor_agent']


def init_app(app):
    path = app.config['AGENT_SOCKET']
    app.extensions['babymonitor_agent'] = FakeAgent() if path == 'fake' else SocketAgent(path)
