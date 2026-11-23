"""Client for the root host agent (server/hostagent.py).

The agent listens on a Unix socket and accepts one JSON request per connection:
    {"action": "set_mode", "mode": "observe"}  ->  {"ok": true, "mode": "observe"}
With AGENT_SOCKET = 'fake' an in-process stand-in is used, for development on a PC.
"""
import json
import socket
import time
import uuid

from flask import current_app

MODES = ('standby', 'notify', 'listen', 'observe')
TIMEOUT_S = 30  # starting a stream unit or switching networks takes a while


class AgentError(Exception):
    """The agent refused a request, or (unreachable=True) could not be asked at all."""

    def __init__(self, message, unreachable=False):
        super().__init__(message)
        self.unreachable = unreachable


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
            raise AgentError(f'host agent unreachable: {e}', unreachable=True) from e
        try:
            result = json.loads(response)
        except ValueError as e:
            raise AgentError('invalid reply from host agent', unreachable=True) from e
        if not result.get('ok'):
            raise AgentError(result.get('error', 'host agent refused the request'))
        return result


class FakeAgent:
    """Development stand-in: remembers changes in memory and otherwise does nothing."""

    TIMEZONES = ['Europe/Copenhagen', 'Europe/London', 'Europe/Oslo', 'UTC']

    def __init__(self):
        self.mode = 'standby'
        self.timezone = 'Europe/Copenhagen'
        self.hostname = 'rock-4se'
        self.country = 'DK'
        self.hotspot = None
        self.hotspot_active = False
        self.saved = [{'name': 'Home', 'uuid': str(uuid.uuid4())}]
        self.connected = 'Home'
        self.last_connect = None

    def request(self, action, **params):
        handler = getattr(self, f'do_{action}', None)
        if handler is None:
            raise AgentError(f'unknown action: {action}')
        return dict(ok=True, **(handler(**params) or {}))

    def do_get_mode(self):
        return {'mode': self.mode}

    def do_set_mode(self, mode=None):
        if mode not in MODES:
            raise AgentError(f'unknown mode: {mode}')
        self.mode = mode
        return {'mode': mode}

    def do_reboot(self):
        pass

    def do_shutdown(self):
        pass

    def do_system_status(self):
        return {'hostname': self.hostname, 'timezone': self.timezone, 'ntp_synced': True,
                'time': time.time(), 'uptime_s': 3600, 'temperature_c': 45.0}

    def do_list_timezones(self):
        return {'timezones': self.TIMEZONES}

    def do_set_timezone(self, timezone=None):
        if timezone not in self.TIMEZONES:
            raise AgentError('unknown time zone')
        self.timezone = timezone

    def do_set_time(self, time=None):
        return {'changed': False}

    def do_set_hostname(self, hostname=None):
        if not hostname or not hostname.replace('-', '').isalnum():
            raise AgentError('invalid host name')
        self.hostname = hostname

    def do_network_status(self):
        wifi = {'device': 'wlan0', 'type': 'wifi', 'state': 'connected',
                'connection': 'babymonitor-hotspot' if self.hotspot_active else self.connected}
        return {'wifi_device': 'wlan0',
                'devices': [wifi, {'device': 'end0', 'type': 'ethernet',
                                   'state': 'unavailable', 'connection': ''}],
                'hotspot_active': self.hotspot_active, 'hotspot': self.hotspot,
                'saved': self.saved, 'connect': self.last_connect, 'country': self.country}

    def do_wifi_scan(self):
        return {'networks': [
            {'ssid': 'Home', 'signal': 80, 'security': 'WPA2', 'in_use': True},
            {'ssid': 'Neighbour', 'signal': 35, 'security': 'WPA2 WPA3', 'in_use': False},
            {'ssid': 'Cafe', 'signal': 20, 'security': '', 'in_use': False}]}

    def do_wifi_connect(self, ssid=None, password=None):
        if not ssid:
            raise AgentError('network name must be 1-32 bytes')
        if ssid not in [n['name'] for n in self.saved]:
            self.saved.append({'name': ssid, 'uuid': str(uuid.uuid4())})
        self.connected, self.hotspot_active = ssid, False
        self.last_connect = {'ssid': ssid, 'state': 'connected', 'error': None}

    def do_wifi_forget(self, uuid=None):
        if uuid not in [n['uuid'] for n in self.saved]:
            raise AgentError('no such saved network')
        self.saved = [n for n in self.saved if n['uuid'] != uuid]

    def do_set_hotspot(self, ssid=None, password=None, channel=0):
        if not ssid or not password or len(password) < 8:
            raise AgentError('Wi-Fi password must be 8-63 plain ASCII characters')
        self.hotspot = {'ssid': ssid, 'password': password, 'channel': channel}

    def do_start_hotspot(self):
        if self.hotspot is None:
            raise AgentError('set a hotspot name and password first')
        self.hotspot_active = True

    def do_stop_hotspot(self):
        self.hotspot_active = False

    def do_set_country(self, country=None):
        if not country or len(country) != 2 or not country.isupper():
            raise AgentError('country must be a two-letter code such as DK')
        self.country = country


def get_agent():
    return current_app.extensions['babymonitor_agent']


def init_app(app):
    path = app.config['AGENT_SOCKET']
    app.extensions['babymonitor_agent'] = FakeAgent() if path == 'fake' else SocketAgent(path)
