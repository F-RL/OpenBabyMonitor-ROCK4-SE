import subprocess

import pytest

import hostagent


class FakeSystemctl:
    def __init__(self, active=()):
        self.active = set(active)
        self.calls = []

    def __call__(self, *args, check=True):
        self.calls.append(args)
        if args[0] == 'is-active':
            return subprocess.CompletedProcess(args, 0 if args[-1] in self.active else 3)
        if args[0] == 'start':
            self.active.add(args[1])
        if args[0] == 'stop':
            self.active -= set(args[1:])
        return subprocess.CompletedProcess(args, 0)


def test_get_mode_defaults_to_standby():
    assert hostagent.Agent(FakeSystemctl()).get_mode() == 'standby'


def test_get_mode_reports_active_unit():
    agent = hostagent.Agent(FakeSystemctl(active={'babymonitor-audiostream.service'}))
    assert agent.get_mode() == 'listen'


def test_set_mode_stops_others_then_starts_target():
    systemctl = FakeSystemctl(active={'babymonitor-crydetect.service'})
    agent = hostagent.Agent(systemctl)
    assert agent.set_mode('observe') == 'observe'
    assert systemctl.calls[0][0] == 'stop'
    assert 'babymonitor-crydetect.service' in systemctl.calls[0]
    assert 'babymonitor-videostream.service' not in systemctl.calls[0]
    assert systemctl.calls[1] == ('start', 'babymonitor-videostream.service')
    assert agent.get_mode() == 'observe'


def test_standby_stops_everything():
    systemctl = FakeSystemctl(active={'babymonitor-videostream.service'})
    agent = hostagent.Agent(systemctl)
    agent.set_mode('standby')
    assert [c[0] for c in systemctl.calls] == ['stop', 'is-active', 'is-active', 'is-active'] \
        or agent.get_mode() == 'standby'
    assert not systemctl.active


@pytest.mark.parametrize('request_', [
    {'action': 'set_mode', 'mode': 'party'},
    {'action': 'rm -rf /'},
    {},
])
def test_rejects_unknown_requests(request_):
    with pytest.raises(ValueError):
        hostagent.Agent(FakeSystemctl()).handle(request_)
