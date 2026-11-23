"""Wi-Fi and hotspot control for the host agent, through NetworkManager (nmcli).

Upstream used dhcpcd/wpa_supplicant/hostapd and a cron job; here everything is a
NetworkManager connection:
- home networks: ordinary Wi-Fi connections (autoconnect);
- the hotspot: connection HOTSPOT (access point, shared IPv4 on 192.168.4.1, WPA2),
  autoconnect off and brought up by the watchdog below.

Watchdog (upstream's ensure_connection.sh, plus a way back): if neither Wi-Fi nor
Ethernet is connected for WATCHDOG_DOWN_TICKS checks, the hotspot comes up. While the
hotspot runs with nobody connected to it, every RETRY_HOME_S the agent tries the
saved home networks for a moment and returns to the hotspot if none is in range,
so the monitor finds its way home after e.g. a router restart.

Standard library only (runs with the system Python as root).
"""
import re
import subprocess
import threading
import time

HOTSPOT = 'babymonitor-hotspot'
HOTSPOT_ADDRESS = '192.168.4.1/24'
WIFI_TYPE = '802-11-wireless'
REGDOM_CONF = '/etc/modprobe.d/babymonitor-regdom.conf'

CONNECT_WAIT_S = 40
SWITCH_WAIT_S = 20            # hotspot up/down; the web service waits 30 s for a reply
WATCHDOG_INTERVAL_S = 60
WATCHDOG_DOWN_TICKS = 2       # ~1-2 min without a network -> hotspot
RETRY_HOME_S = 10 * 60        # idle hotspot -> look for home networks this often
RETRY_HOME_WAIT_S = 45
LOCK_WAIT_S = 2

UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
COUNTRY_RE = re.compile(r'^(00|[A-Z]{2})$')


class Busy(ValueError):
    pass


def run_command(argv, check=True, timeout=90):
    return subprocess.run(argv, check=check, capture_output=True, text=True, timeout=timeout)


def split_terse(line):
    """Split one line of `nmcli -t` output: fields separated by ':', '\\' escapes."""
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == '\\':
            escaped = True
        elif ch == ':':
            fields.append(''.join(current))
            current = []
        else:
            current.append(ch)
    fields.append(''.join(current))
    return fields


def validate_ssid(ssid):
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
        raise ValueError('network name must be 1-32 bytes')
    if any(ord(c) < 32 for c in ssid):
        raise ValueError('network name contains control characters')
    return ssid


def validate_psk(password, allow_empty=False):
    if allow_empty and password in (None, ''):
        return None
    if not isinstance(password, str) or not 8 <= len(password) <= 63 \
            or not all(32 <= ord(c) < 127 for c in password):
        raise ValueError('Wi-Fi password must be 8-63 plain ASCII characters')
    return password


class Network:
    def __init__(self, run=run_command, clock=time.monotonic, sleep=time.sleep):
        self.run = run
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.RLock()
        self.last_connect = None   # result of the latest connect request, for the UI
        self.down_ticks = 0
        self.hotspot_idle_since = None

    # --- Reading state ---

    def nmcli(self, *args, check=True, wait=None):
        """Run nmcli; `wait` caps how long it waits for (de)activation (default 90 s)."""
        prefix = ['--wait', str(wait)] if wait is not None else []
        return self.run(['nmcli', *prefix, *args], check=check)

    def devices(self):
        out = self.nmcli('-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device').stdout
        devices = []
        for line in out.splitlines():
            if line:
                device, kind, state, connection = (split_terse(line) + [''] * 4)[:4]
                devices.append({'device': device, 'type': kind, 'state': state,
                                'connection': connection})
        return devices

    def wifi_device(self, devices=None):
        for d in devices if devices is not None else self.devices():
            if d['type'] == 'wifi':
                return d['device']
        return None

    def saved(self):
        out = self.nmcli('-t', '-f', 'NAME,UUID,TYPE', 'connection', 'show').stdout
        networks = []
        for line in out.splitlines():
            if line:
                name, uuid, kind = (split_terse(line) + [''] * 3)[:3]
                if kind == WIFI_TYPE and name != HOTSPOT:
                    networks.append({'name': name, 'uuid': uuid})
        return networks

    def hotspot_config(self):
        result = self.nmcli('-s', '-t', '-f',
                            '802-11-wireless.ssid,802-11-wireless-security.psk,'
                            '802-11-wireless.channel',
                            'connection', 'show', HOTSPOT, check=False)
        if result.returncode != 0:
            return None
        values = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition(':')
            values[key] = value.replace('\\:', ':').replace('\\\\', '\\')
        channel = values.get('802-11-wireless.channel', '0')
        return {
            'ssid': values.get('802-11-wireless.ssid', ''),
            'password': values.get('802-11-wireless-security.psk', ''),
            'channel': int(channel) if channel.isdigit() else 0,
        }

    @staticmethod
    def is_up(device):
        return device['state'].startswith('connected') and device['type'] in ('wifi', 'ethernet') \
            and device['connection'] != HOTSPOT

    def status(self):
        devices = self.devices()
        return {
            'wifi_device': self.wifi_device(devices),
            'devices': [d for d in devices if d['type'] in ('wifi', 'ethernet')],
            'hotspot_active': any(d['connection'] == HOTSPOT for d in devices),
            'hotspot': self.hotspot_config(),
            'saved': self.saved(),
            'connect': self.last_connect,
            'country': self.country(),
        }

    def scan(self):
        args = ['-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list']
        result = self.nmcli(*args, '--rescan', 'yes', check=False)
        if result.returncode != 0:  # e.g. the radio can't scan while it is a hotspot
            result = self.nmcli(*args, '--rescan', 'no')
        best = {}
        for line in result.stdout.splitlines():
            in_use, ssid, signal, security = (split_terse(line) + [''] * 4)[:4]
            if not ssid:
                continue
            entry = {'ssid': ssid, 'signal': int(signal) if signal.isdigit() else 0,
                     'security': security, 'in_use': in_use.strip() == '*'}
            old = best.get(ssid)
            if old is None or entry['signal'] > old['signal']:
                entry['in_use'] = entry['in_use'] or bool(old and old['in_use'])
                best[ssid] = entry
            elif entry['in_use']:
                old['in_use'] = True
        return sorted(best.values(), key=lambda e: -e['signal'])

    def country(self):
        result = self.run(['iw', 'reg', 'get'], check=False)
        match = re.search(r'^country (\w\w):', result.stdout or '', re.MULTILINE)
        return match.group(1) if match else None

    # --- Changing state (one change at a time) ---

    def locked(self):
        if not self.lock.acquire(timeout=LOCK_WAIT_S):
            raise Busy('the network is busy switching, try again in a minute')
        return self.lock

    def connect(self, ssid, password=None, background=True):
        """Join a network. Runs in the background: the browser may be connected through
        the hotspot that this is about to replace, so it couldn't get the answer anyway.
        The result shows up as `connect` in status()."""
        validate_ssid(ssid)
        password = validate_psk(password, allow_empty=True)
        with self.locked():
            self.last_connect = {'ssid': ssid, 'state': 'connecting', 'error': None}
        if background:
            threading.Thread(target=self._connect, args=(ssid, password), daemon=True).start()
        else:
            self._connect(ssid, password)

    def _connect(self, ssid, password):
        with self.lock:
            devices = self.devices()
            device = self.wifi_device(devices)
            hotspot_was_active = any(d['connection'] == HOTSPOT for d in devices)
            existing = [n for n in self.saved() if n['name'] == ssid]
            if existing:
                uuid = existing[0]['uuid']
                if password:
                    self.nmcli('connection', 'modify', 'uuid', uuid,
                               'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', password)
                result = self.nmcli('connection', 'up', 'uuid', uuid, 'ifname', device,
                                    check=False, wait=CONNECT_WAIT_S)
            else:
                cmd = ['device', 'wifi', 'connect', ssid]
                if password:
                    cmd += ['password', password]
                result = self.nmcli(*cmd, 'ifname', device, check=False, wait=CONNECT_WAIT_S)
            if result.returncode == 0:
                self.last_connect = {'ssid': ssid, 'state': 'connected', 'error': None}
                self.down_ticks = 0
                return
            error = (result.stderr or result.stdout or '').strip() or 'could not connect'
            if not existing:  # don't keep a profile with a wrong password
                for n in self.saved():
                    if n['name'] == ssid:
                        self.nmcli('connection', 'delete', 'uuid', n['uuid'], check=False)
            if hotspot_was_active:
                self.nmcli('connection', 'up', HOTSPOT, check=False, wait=SWITCH_WAIT_S)
            self.last_connect = {'ssid': ssid, 'state': 'failed', 'error': error}

    def forget(self, uuid):
        if not isinstance(uuid, str) or not UUID_RE.match(uuid):
            raise ValueError('invalid network id')
        with self.locked():
            if uuid not in [n['uuid'] for n in self.saved()]:
                raise ValueError('no such saved network')
            self.nmcli('connection', 'delete', 'uuid', uuid)

    def set_hotspot(self, ssid, password, channel=0):
        validate_ssid(ssid)
        validate_psk(password)
        if not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel <= 13:
            raise ValueError('channel must be 0 (automatic) or 1-13')
        settings = ['802-11-wireless.ssid', ssid,
                    '802-11-wireless.channel', str(channel),
                    'wifi-sec.psk', password]
        with self.locked():
            devices = self.devices()
            if self.hotspot_config() is None:
                device = self.wifi_device(devices)
                if device is None:
                    raise ValueError('this device has no Wi-Fi')
                self.nmcli('connection', 'add', 'type', 'wifi', 'ifname', device,
                           'con-name', HOTSPOT, 'autoconnect', 'no',
                           '802-11-wireless.mode', 'ap', '802-11-wireless.band', 'bg',
                           'ipv4.method', 'shared', 'ipv4.addresses', HOTSPOT_ADDRESS,
                           'ipv6.method', 'disabled',
                           'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.proto', 'rsn',
                           'wifi-sec.pairwise', 'ccmp', 'wifi-sec.group', 'ccmp', *settings)
            else:
                self.nmcli('connection', 'modify', HOTSPOT, *settings)
                if any(d['connection'] == HOTSPOT for d in devices):
                    self.nmcli('connection', 'up', HOTSPOT, wait=SWITCH_WAIT_S)  # apply now

    def start_hotspot(self):
        with self.locked():
            if self.hotspot_config() is None:
                raise ValueError('set a hotspot name and password first')
            self.nmcli('connection', 'up', HOTSPOT, wait=SWITCH_WAIT_S)

    def stop_hotspot(self):
        """Leave the hotspot and let NetworkManager join a saved network in range."""
        with self.locked():
            device = self.wifi_device()
            # 8 + 20 s: stays under the web service's 30 s wait for a reply.
            self.nmcli('connection', 'down', HOTSPOT, check=False, wait=8)
            if device:
                self.nmcli('device', 'connect', device, check=False, wait=SWITCH_WAIT_S)
            self.down_ticks = 0

    def set_country(self, code):
        if not isinstance(code, str) or not COUNTRY_RE.match(code):
            raise ValueError('country must be a two-letter code such as DK')
        with self.locked():
            self.run(['iw', 'reg', 'set', code])
            with open(REGDOM_CONF, 'w') as f:
                f.write(f'# Written by the baby monitor: Wi-Fi regulatory domain\n'
                        f'options cfg80211 ieee80211_regdom={code}\n')

    # --- Watchdog ---

    def watchdog_tick(self):
        if not self.lock.acquire(blocking=False):
            return  # a user-requested change is in progress
        try:
            devices = self.devices()
            device = self.wifi_device(devices)
            if device is None or self.hotspot_config() is None:
                return
            if any(self.is_up(d) for d in devices):
                self.down_ticks = 0
                self.hotspot_idle_since = None
                return
            if not any(d['connection'] == HOTSPOT for d in devices):
                self.hotspot_idle_since = None
                self.down_ticks += 1
                if self.down_ticks >= WATCHDOG_DOWN_TICKS:
                    self.nmcli('connection', 'up', HOTSPOT, check=False, wait=SWITCH_WAIT_S)
                    self.down_ticks = 0
                return
            # The hotspot is up. Look for home networks now and then, but never while
            # someone is connected to it.
            stations = self.run(['iw', 'dev', device, 'station', 'dump'], check=False).stdout
            now = self.clock()
            if stations.strip():
                self.hotspot_idle_since = None
                return
            if self.hotspot_idle_since is None:
                self.hotspot_idle_since = now
                return
            if now - self.hotspot_idle_since < RETRY_HOME_S or not self.saved():
                return
            self.hotspot_idle_since = None
            self.nmcli('connection', 'down', HOTSPOT, check=False, wait=SWITCH_WAIT_S)
            self.nmcli('device', 'connect', device, check=False, wait=SWITCH_WAIT_S)
            deadline = self.clock() + RETRY_HOME_WAIT_S
            while self.clock() < deadline:
                if any(self.is_up(d) for d in self.devices()):
                    return
                self.sleep(5)
            self.nmcli('connection', 'up', HOTSPOT, check=False, wait=SWITCH_WAIT_S)
        finally:
            self.lock.release()

    def run_watchdog(self):
        while True:
            self.sleep(WATCHDOG_INTERVAL_S)
            try:
                self.watchdog_tick()
            except Exception as e:  # keep watching whatever happens
                print(f'watchdog: {e}', flush=True)
