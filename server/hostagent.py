#!/usr/bin/env python3
"""Root host agent: the only part of the baby monitor that runs as root.

Listens on a Unix socket and performs a fixed whitelist of actions for the web
service. Each connection carries one JSON request and gets one JSON reply:

    {"action": "get_mode"}                      -> {"ok": true, "mode": "standby"}
    {"action": "set_mode", "mode": "observe"}   -> {"ok": true, "mode": "observe"}
    {"action": "reboot"} / {"action": "shutdown"}

Only the standard library is used, so it runs with the system Python.
"""
import argparse
import json
import os
import socketserver
import subprocess
import sys

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
    def __init__(self, run=systemctl):
        self.run = run

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
            self.run('start', target)
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
        raise ValueError(f'unknown action: {action}')


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            request = json.loads(self.rfile.readline(MAX_REQUEST_BYTES))
            reply = dict(ok=True, **self.server.agent.handle(request))
        except subprocess.CalledProcessError as e:
            reply = {'ok': False, 'error': (e.stderr or str(e)).strip()}
        except (ValueError, AttributeError) as e:
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
        server.agent = Agent()
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
