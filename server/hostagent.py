#!/usr/bin/env python3
"""Root host agent: the only part of the baby monitor that runs as root.

Listens on a Unix socket and performs a fixed whitelist of actions for the web
service. Each connection carries one JSON request and gets one JSON reply:

    {"action": "get_mode"}                      -> {"ok": true, "mode": "standby"}
    {"action": "set_mode", "mode": "observe"}   -> {"ok": true, "mode": "observe"}
    {"action": "reboot"} / {"action": "shutdown"}
    plus system and network settings, see ACTIONS below.

Only the standard library is used, so it runs with the system Python.
"""
import argparse
import json
import os
import socketserver
import subprocess
import sys
import threading

from hostagent_network import Network
from hostagent_system import System

# Each mode is at most one systemd unit; Standby runs nothing.
MODE_UNITS = {
    'standby': None,
    'notify': 'babymonitor-crydetect.service',
    'listen': 'babymonitor-audiostream.service',
    'observe': 'babymonitor-videostream.service',
}
MAX_REQUEST_BYTES = 4096


def systemctl(*args, check=True):
    return subprocess.run(['systemctl', *args], check=check, capture_output=True, text=True)


class Agent:
    def __init__(self, run=systemctl, network=None, system=None):
        self.run = run
        self.network = network
        self.system = system

    def get_mode(self):
        for mode, unit in MODE_UNITS.items():
            if unit and self.run('is-active', '--quiet', unit, check=False).returncode == 0:
                return mode
        return 'standby'

    def set_mode(self, mode):
        if mode not in MODE_UNITS:
            raise ValueError(f'unknown mode: {mode}')
        target = MODE_UNITS[mode]
        # Stop the other modes first: they share the microphone. Not checked, because
        # stopping a unit that is not installed (yet) fails harmlessly.
        others = [u for u in MODE_UNITS.values() if u and u != target]
        self.run('stop', *others, check=False)
        if target:
            result = self.run('start', target, check=False)
            if result.returncode != 0:
                error = (result.stderr or '').strip()
                if 'not found' in error:
                    raise ValueError(f'{mode} mode is not installed yet ({target} is missing)')
                raise ValueError(error or f'could not start {target}')
        return mode

    def handle(self, request):
        action = request.get('action')
        if action == 'get_mode':
            return {'mode': self.get_mode()}
        if action == 'set_mode':
            return {'mode': self.set_mode(request.get('mode'))}
        if action in ('reboot', 'shutdown'):
            # Reply first; systemd takes the machine down shortly after.
            subprocess.Popen(['systemctl', 'reboot' if action == 'reboot' else 'poweroff'])
            return {}
        if action in SYSTEM_ACTIONS or action in NETWORK_ACTIONS:
            target = self.system if action in SYSTEM_ACTIONS else self.network
            if target is None:
                raise ValueError(f'{action} is not available')
            return (SYSTEM_ACTIONS.get(action) or NETWORK_ACTIONS[action])(target, request)
        raise ValueError(f'unknown action: {action}')


# Settings actions: name -> handler(target, request) returning the reply fields.
# Each handler passes on only the fields it expects; validation happens in the
# System/Network methods.
SYSTEM_ACTIONS = {
    'system_status': lambda s, r: s.status(),
    'list_timezones': lambda s, r: {'timezones': s.timezones()},
    'set_timezone': lambda s, r: s.set_timezone(r.get('timezone')) or {},
    'set_time': lambda s, r: {'changed': s.set_time(r.get('time'))},
    'set_hostname': lambda s, r: s.set_hostname(r.get('hostname')) or {},
}
NETWORK_ACTIONS = {
    'network_status': lambda n, r: n.status(),
    'wifi_scan': lambda n, r: {'networks': n.scan()},
    'wifi_connect': lambda n, r: n.connect(r.get('ssid'), r.get('password')) or {},
    'wifi_forget': lambda n, r: n.forget(r.get('uuid')) or {},
    'set_hotspot': lambda n, r: n.set_hotspot(r.get('ssid'), r.get('password'),
                                              r.get('channel', 0)) or {},
    'start_hotspot': lambda n, r: n.start_hotspot() or {},
    'stop_hotspot': lambda n, r: n.stop_hotspot() or {},
    'set_country': lambda n, r: n.set_country(r.get('country')) or {},
}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            request = json.loads(self.rfile.readline(MAX_REQUEST_BYTES))
            reply = dict(ok=True, **self.server.agent.handle(request))
        except subprocess.CalledProcessError as e:
            reply = {'ok': False, 'error': (e.stderr or str(e)).strip()}
        except (ValueError, AttributeError, TypeError) as e:
            reply = {'ok': False, 'error': str(e)}
        except subprocess.TimeoutExpired as e:
            reply = {'ok': False, 'error': f'timed out: {" ".join(map(str, e.cmd[:3]))}'}
        except OSError as e:
            reply = {'ok': False, 'error': str(e)}
        self.wfile.write(json.dumps(reply).encode() + b'\n')


def serve(socket_path, group):
    import grp  # Unix only; imported here so tests can import this module anywhere

    if os.path.exists(socket_path):
        os.unlink(socket_path)
    # Requests are handled one at a time, so mode switches never overlap.
    with socketserver.UnixStreamServer(socket_path, Handler) as server:
        os.chown(socket_path, 0, grp.getgrnam(group).gr_gid)
        os.chmod(socket_path, 0o660)
        network = Network()
        server.agent = Agent(network=network, system=System())
        threading.Thread(target=network.run_watchdog, daemon=True).start()
        print(f'Listening on {socket_path} (group {group})', file=sys.stderr, flush=True)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--socket', default='/run/babymonitor/agent.sock')
    parser.add_argument('--group', default='babymonitor',
                        help='group allowed to use the socket (the web service user)')
    args = parser.parse_args()
    serve(args.socket, args.group)


if __name__ == '__main__':
    main()
