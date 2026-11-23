"""System settings for the host agent: time zone, clock, host name, status.

Standard library only (runs with the system Python as root).
"""
import glob
import re
import socket
import time

from hostagent_network import run_command

HOSTNAME_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')
HOSTS_FILE = '/etc/hosts'
# The board has no RTC battery: without internet (e.g. on its own hotspot) the clock
# starts wherever it stopped. Browsers send their time; it is used if NTP hasn't
# synchronised and the clocks differ by more than this.
CLOCK_TOLERANCE_S = 30


class System:
    def __init__(self, run=run_command, now=time.time, hosts_file=HOSTS_FILE):
        self.run = run
        self.now = now
        self.hosts_file = hosts_file

    def timedatectl_value(self, prop):
        return self.run(['timedatectl', 'show', '-p', prop, '--value'],
                        check=False).stdout.strip()

    def status(self):
        return {
            'hostname': socket.gethostname(),
            'timezone': self.timedatectl_value('Timezone'),
            'ntp_synced': self.timedatectl_value('NTPSynchronized') == 'yes',
            'time': self.now(),
            'uptime_s': read_uptime(),
            'temperature_c': read_temperature(),
        }

    def timezones(self):
        return self.run(['timedatectl', 'list-timezones']).stdout.split()

    def set_timezone(self, timezone):
        if not isinstance(timezone, str) or timezone not in self.timezones():
            raise ValueError('unknown time zone')
        self.run(['timedatectl', 'set-timezone', timezone])

    def set_time(self, timestamp):
        """Set the clock from a browser's clock, unless NTP is in charge or it's close."""
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ValueError('time must be a Unix timestamp')
        if not 1.6e9 < timestamp < 4.1e9:
            raise ValueError('time is implausible')
        if self.timedatectl_value('NTPSynchronized') == 'yes':
            return False
        if abs(timestamp - self.now()) < CLOCK_TOLERANCE_S:
            return False
        self.run(['date', '-s', f'@{int(timestamp)}'])
        return True

    def set_hostname(self, name):
        if not isinstance(name, str) or not HOSTNAME_RE.match(name):
            raise ValueError('host name: 1-63 lowercase letters, digits and hyphens, '
                             'not starting or ending with a hyphen')
        self.run(['hostnamectl', 'set-hostname', name])
        # Debian maps the host name to 127.0.1.1; keep that in step so sudo etc. resolve it.
        try:
            with open(self.hosts_file) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            lines = []
        entry = f'127.0.1.1\t{name}'
        lines = [entry if line.split()[:1] == ['127.0.1.1'] else line for line in lines]
        if entry not in lines:
            lines.append(entry)
        with open(self.hosts_file, 'w') as f:
            f.write('\n'.join(lines) + '\n')


def read_uptime():
    try:
        with open('/proc/uptime') as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def read_temperature():
    temps = []
    for path in glob.glob('/sys/class/thermal/thermal_zone*/temp'):
        try:
            with open(path) as f:
                temps.append(int(f.read().strip()) / 1000)
        except (OSError, ValueError):
            pass
    return round(max(temps), 1) if temps else None
