import io

from crydetect import push

ENV = {'NTFY_TOKEN': 'tk_abc', 'NTFY_URL': 'http://127.0.0.1:2586/', 'NTFY_TOPIC': 'baby',
       'NTFY_PUBLIC_URL': 'https://rock-4se.local:8443'}


class Opener:
    def __init__(self, fail=None):
        self.requests = []
        self.fail = fail

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.fail:
            raise self.fail
        return io.BytesIO(b'{}')


def test_disabled_without_token():
    assert push.config({})['enabled'] is False
    assert 'not set up' in push.send('crying', push.config({}), opener=Opener())


def test_crying_is_urgent_and_authenticated():
    opener = Opener()
    assert push.send('crying', push.config(ENV), opener=opener) is None
    request = opener.requests[0]
    assert request.full_url == 'http://127.0.0.1:2586/baby'
    assert request.get_method() == 'POST'
    assert request.data == b'Crying detected'
    assert request.get_header('Authorization') == 'Bearer tk_abc'
    assert request.get_header('Priority') == 'urgent'


def test_sound_and_test_priorities():
    opener = Opener()
    push.send('sound', push.config(ENV), opener=opener)
    push.send('test', push.config(ENV), opener=opener)
    assert [r.get_header('Priority') for r in opener.requests] == ['high', 'default']


def test_network_error_is_reported_not_raised():
    error = push.send('crying', push.config(ENV), opener=Opener(fail=OSError('refused')))
    assert 'refused' in error
