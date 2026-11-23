import subprocess

import pytest

import hostagent
import hostagent_network as hn
import hostagent_system as hs

HOME_UUID = '11111111-2222-3333-4444-555555555555'
HOTSPOT_UUID = '66666666-7777-8888-9999-000000000000'


def done(argv, out='', code=0, err=''):
    return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)


class FakeNM:
    """A small simulation of nmcli/iw: one Wi-Fi device, saved connections, a hotspot."""

    def __init__(self, connected='Home', hotspot=True, saved=('Home',), ethernet=False,
                 in_range=('Home', 'Neighbour'), stations=''):
        self.connections = {name: f'{i:08d}-2222-3333-4444-555555555555'
                            for i, name in enumerate(saved)}
        if 'Home' in self.connections:
            self.connections['Home'] = HOME_UUID
        self.hotspot = {'ssid': 'rock-4se', 'psk': 'secret123', 'channel': '0'} if hotspot else None
        self.active = connected  # connection name on wlan0, or None
        self.ethernet = ethernet
        self.in_range = set(in_range)
        self.stations = stations
        self.passwords = {}
        self.calls = []

    def __call__(self, argv, check=True, timeout=90):
        self.calls.append(argv)
        result = self.dispatch(argv)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, argv, result.stdout,
                                                result.stderr)
        return result

    def dispatch(self, argv):
        if argv[:2] == ['iw', 'reg']:
            return done(argv, 'global\ncountry DK: DFS-ETSI\n' if argv[2] == 'get' else '')
        if argv[0] == 'iw':
            return done(argv, self.stations)
        assert argv[0] == 'nmcli'
        args = argv[1:]
        if args[0] == '--wait':
            args = args[2:]
        if args[-1] == 'device' and '-f' in args:
            wifi = 'connected' if self.active else 'disconnected'
            eth = 'connected:Wired' if self.ethernet else 'unavailable:'
            return done(argv, f'wlan0:wifi:{wifi}:{self.active or ""}\nend0:ethernet:{eth}\n'
                              'lo:loopback:connected (externally):lo\n')
        if args[-2:] == ['connection', 'show'] and '-f' in args:
            lines = [f'{n}:{u}:802-11-wireless' for n, u in self.connections.items()]
            if self.hotspot:
                lines.append(f'{hn.HOTSPOT}:{HOTSPOT_UUID}:802-11-wireless')
            lines.append(f'Wired:{HOTSPOT_UUID[:-1]}1:802-3-ethernet')
            return done(argv, '\n'.join(lines) + '\n')
        if args[-3:] == ['connection', 'show', hn.HOTSPOT]:
            if not self.hotspot:
                return done(argv, code=10, err='no such connection')
            h = self.hotspot
            return done(argv, f'802-11-wireless.ssid:{h["ssid"]}\n'
                              f'802-11-wireless-security.psk:{h["psk"]}\n'
                              f'802-11-wireless.channel:{h["channel"]}\n')
        if args[:3] == ['device', 'wifi', 'connect']:
            ssid = args[3]
            pw = args[args.index('password') + 1] if 'password' in args else None
            self.connections[ssid] = 'abcdefab-2222-3333-4444-555555555555'
            if ssid in self.in_range and pw == 'rightpass':
                self.active = ssid
                return done(argv)
            self.active = None
            return done(argv, code=4, err='Error: Connection activation failed: Secrets were required.')
        if args[:2] == ['connection', 'up']:
            name = args[2] if args[2] != 'uuid' else \
                {u: n for n, u in self.connections.items()}[args[3]]
            if name == hn.HOTSPOT or name in self.in_range:
                self.active = name
                return done(argv)
            return done(argv, code=4, err='no network')
        if args[:2] == ['connection', 'down']:
            if self.active == args[2]:
                self.active = None
            return done(argv)
        if args[:2] == ['device', 'connect']:
            for name in self.connections:
                if name in self.in_range:
                    self.active = name
                    return done(argv)
            return done(argv, code=4, err='no suitable connection')
        if args[:3] == ['connection', 'delete', 'uuid']:
            self.connections = {n: u for n, u in self.connections.items() if u != args[3]}
            return done(argv)
        if args[:2] == ['connection', 'add']:
            self.hotspot = {'ssid': args[args.index('802-11-wireless.ssid') + 1],
                            'psk': args[args.index('wifi-sec.psk') + 1],
                            'channel': '0'}
            if '802-11-wireless.channel' in args:
                channel = args[args.index('802-11-wireless.channel') + 1]
                assert channel != '0', 'NetworkManager rejects channel 0'
                self.hotspot['channel'] = channel
            return done(argv)
        if args[:2] == ['connection', 'modify']:
            if args[2] == hn.HOTSPOT:
                channel = args[args.index('802-11-wireless.channel') + 1]
                assert channel != '0', 'NetworkManager rejects channel 0'
                self.hotspot.update(ssid=args[args.index('802-11-wireless.ssid') + 1],
                                    psk=args[args.index('wifi-sec.psk') + 1],
                                    channel=channel or '0')
            return done(argv)
        if 'wifi' in args and 'list' in args:
            return done(argv, ' :Neighbour:40:WPA2\n*:Home:70:WPA2\n :Home:30:WPA2\n'
                              ' ::20:\n :Cafe\\:Bar:10:\n')
        raise AssertionError(f'unexpected command {argv}')


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def network(nm):
    clock = Clock()
    return hn.Network(run=nm, clock=clock, sleep=clock.sleep), clock


def test_split_terse_handles_escaped_colons():
    assert hn.split_terse(r' :Cafe\:Bar:10:') == [' ', 'Cafe:Bar', '10', '']


def test_status():
    n, _ = network(FakeNM())
    status = n.status()
    assert status['wifi_device'] == 'wlan0'
    assert [d['device'] for d in status['devices']] == ['wlan0', 'end0']  # no loopback
    assert status['saved'] == [{'name': 'Home', 'uuid': HOME_UUID}]  # no hotspot, no wired
    assert status['hotspot'] == {'ssid': 'rock-4se', 'password': 'secret123', 'channel': 0}
    assert not status['hotspot_active']
    assert status['country'] == 'DK'


def test_scan_merges_access_points_and_skips_hidden():
    n, _ = network(FakeNM())
    assert n.scan() == [
        {'ssid': 'Home', 'signal': 70, 'security': 'WPA2', 'in_use': True},
        {'ssid': 'Neighbour', 'signal': 40, 'security': 'WPA2', 'in_use': False},
        {'ssid': 'Cafe:Bar', 'signal': 10, 'security': '', 'in_use': False},
    ]


def test_connect_new_network():
    nm = FakeNM()
    n, _ = network(nm)
    n.connect('Neighbour', 'rightpass', background=False)
    assert n.last_connect['state'] == 'connected'
    assert nm.active == 'Neighbour' and 'Neighbour' in nm.connections


def test_failed_connect_removes_profile_and_restores_hotspot():
    nm = FakeNM(connected=hn.HOTSPOT)
    n, _ = network(nm)
    n.connect('Neighbour', 'wrongpass', background=False)
    assert n.last_connect['state'] == 'failed' and 'Secrets' in n.last_connect['error']
    assert 'Neighbour' not in nm.connections
    assert nm.active == hn.HOTSPOT


def test_connect_saved_network_uses_its_profile():
    nm = FakeNM(connected=None)
    n, _ = network(nm)
    n.connect('Home', background=False)
    assert nm.active == 'Home'
    assert ['nmcli', '--wait', '40', 'connection', 'up', 'uuid', HOME_UUID, 'ifname', 'wlan0'] \
        in nm.calls


@pytest.mark.parametrize('ssid,password', [('', None), ('x' * 33, None), ('Home', 'short'),
                                           ('Home', 'é' * 10), (None, None)])
def test_connect_validates(ssid, password):
    n, _ = network(FakeNM())
    with pytest.raises(ValueError):
        n.connect(ssid, password, background=False)


def test_forget_only_saved_wifi():
    nm = FakeNM()
    n, _ = network(nm)
    with pytest.raises(ValueError):
        n.forget(HOTSPOT_UUID)  # the hotspot is not a "saved network"
    with pytest.raises(ValueError):
        n.forget('; reboot')
    n.forget(HOME_UUID)
    assert 'Home' not in nm.connections


def test_set_hotspot_creates_then_modifies():
    nm = FakeNM(hotspot=False)
    n, _ = network(nm)
    n.set_hotspot('nursery', 'longenough', 6)
    add = next(c for c in nm.calls if c[1:3] == ['connection', 'add'])
    assert ['802-11-wireless.mode', 'ap'] == add[add.index('802-11-wireless.mode'):][:2]
    assert hn.HOTSPOT_ADDRESS in add and 'autoconnect' in add
    assert nm.hotspot['channel'] == '6'
    n.set_hotspot('nursery2', 'longenough2', 0)
    assert nm.hotspot == {'ssid': 'nursery2', 'psk': 'longenough2', 'channel': '0'}
    nm2 = FakeNM(hotspot=False)
    network(nm2)[0].set_hotspot('nursery', 'longenough', 0)  # automatic on creation
    assert nm2.hotspot['channel'] == '0'
    with pytest.raises(ValueError):
        n.set_hotspot('x', 'short')
    with pytest.raises(ValueError):
        n.set_hotspot('x', 'longenough', 14)


def test_country(tmp_path, monkeypatch):
    conf = tmp_path / 'regdom.conf'
    monkeypatch.setattr(hn, 'REGDOM_CONF', str(conf))
    nm = FakeNM()
    n, _ = network(nm)
    n.set_country('NO')
    assert ['iw', 'reg', 'set', 'NO'] in nm.calls
    assert 'ieee80211_regdom=NO' in conf.read_text()
    for bad in ('no', 'NOR', '', None, 'N;'):
        with pytest.raises(ValueError):
            n.set_country(bad)


def test_watchdog_starts_hotspot_after_two_misses():
    nm = FakeNM(connected=None)
    n, _ = network(nm)
    n.watchdog_tick()
    assert nm.active is None
    n.watchdog_tick()
    assert nm.active == hn.HOTSPOT


def test_watchdog_leaves_wired_and_unconfigured_alone():
    for nm in (FakeNM(connected=None, ethernet=True), FakeNM(connected=None, hotspot=False)):
        n, _ = network(nm)
        for _ in range(5):
            n.watchdog_tick()
        assert nm.active is None


def test_watchdog_goes_home_from_idle_hotspot():
    nm = FakeNM(connected=hn.HOTSPOT)
    n, clock = network(nm)
    n.watchdog_tick()  # idle hotspot noticed
    clock.now += hn.RETRY_HOME_S - 1
    n.watchdog_tick()
    assert nm.active == hn.HOTSPOT
    clock.now += 2
    n.watchdog_tick()
    assert nm.active == 'Home'


def test_watchdog_returns_to_hotspot_when_home_is_gone():
    nm = FakeNM(connected=hn.HOTSPOT, in_range=())
    n, clock = network(nm)
    n.watchdog_tick()
    clock.now += hn.RETRY_HOME_S + 1
    n.watchdog_tick()
    assert nm.active == hn.HOTSPOT
    assert ['nmcli', '--wait', '20', 'device', 'connect', 'wlan0'] in nm.calls


def test_watchdog_never_drops_hotspot_with_clients():
    nm = FakeNM(connected=hn.HOTSPOT, stations='Station aa:bb:cc:dd:ee:ff (on wlan0)\n')
    n, clock = network(nm)
    for _ in range(5):
        n.watchdog_tick()
        clock.now += hn.RETRY_HOME_S
    assert nm.active == hn.HOTSPOT
    assert not any(c[1:3] == ['connection', 'down'] for c in nm.calls)


# --- System ---

class FakeTimedatectl:
    def __init__(self, ntp='no'):
        self.ntp = ntp
        self.calls = []

    def __call__(self, argv, check=True, timeout=90):
        self.calls.append(argv)
        if argv[:2] == ['timedatectl', 'show']:
            return done(argv, {'Timezone': 'Europe/Copenhagen\n', 'NTPSynchronized': self.ntp}
                        [argv[3]])
        if argv[:2] == ['timedatectl', 'list-timezones']:
            return done(argv, 'Europe/Copenhagen\nEurope/Oslo\nUTC\n')
        return done(argv)


def test_set_time_only_when_needed():
    run = FakeTimedatectl()
    system = hs.System(run=run, now=lambda: 1.7e9)
    assert system.set_time(1.7e9 + 5) is False  # close enough
    assert system.set_time(1.8e9) is True
    assert ['date', '-s', '@1800000000'] in run.calls
    assert hs.System(run=FakeTimedatectl(ntp='yes'), now=lambda: 1.7e9).set_time(1.8e9) is False
    for bad in ('1.8e9', None, True, 5):
        with pytest.raises(ValueError):
            system.set_time(bad)


def test_set_timezone_validates():
    run = FakeTimedatectl()
    system = hs.System(run=run)
    system.set_timezone('Europe/Oslo')
    assert ['timedatectl', 'set-timezone', 'Europe/Oslo'] in run.calls
    with pytest.raises(ValueError):
        system.set_timezone('Mars/Olympus')


def test_set_hostname_updates_hosts(tmp_path):
    hosts = tmp_path / 'hosts'
    hosts.write_text('127.0.0.1\tlocalhost\n127.0.1.1\trock-4se\n')
    run = FakeTimedatectl()
    system = hs.System(run=run, hosts_file=str(hosts))
    system.set_hostname('nursery')
    assert ['hostnamectl', 'set-hostname', 'nursery'] in run.calls
    assert hosts.read_text() == '127.0.0.1\tlocalhost\n127.0.1.1\tnursery\n'
    for bad in ('Nursery', '-x', 'a b', 'x' * 64, '', None):
        with pytest.raises(ValueError):
            system.set_hostname(bad)


def test_agent_dispatches_settings_actions():
    nm = FakeNM()
    agent = hostagent.Agent(network=hn.Network(run=nm), system=hs.System(run=FakeTimedatectl()))
    assert agent.handle({'action': 'list_timezones'})['timezones'][-1] == 'UTC'
    assert agent.handle({'action': 'wifi_scan'})['networks'][0]['ssid'] == 'Home'
    assert agent.handle({'action': 'set_timezone', 'timezone': 'UTC'}) == {}
    with pytest.raises(ValueError):
        hostagent.Agent().handle({'action': 'wifi_scan'})  # no network configured
