import pytest

from babymonitor import auth, create_app

PASSWORD = 'correct horse'


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, 'FAILED_LOGIN_DELAY_S', 0)
    app = create_app({
        'TESTING': True,
        'DATABASE': str(tmp_path / 'test.sqlite3'),
        'AGENT_SOCKET': 'fake',
    })
    with app.app_context():
        auth.set_password(PASSWORD)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, password=PASSWORD):
    return client.post('/login', data={'password': password})


def test_pages_require_login(client):
    response = client.get('/')
    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_api_requires_login(client):
    assert client.get('/api/mode').status_code == 401


def test_wrong_password_is_rejected(client):
    response = login(client, 'nope')
    assert response.status_code == 200
    assert b'Wrong password' in response.data
    assert client.get('/api/mode').status_code == 401


def test_login_and_logout(client):
    assert login(client).status_code == 302
    assert client.get('/').status_code == 200
    client.post('/logout')
    assert client.get('/api/mode').status_code == 401


def test_login_does_not_redirect_offsite(client):
    response = client.post('/login?next=//evil.example', data={'password': PASSWORD})
    assert response.headers['Location'] == '/'


def test_mode_switching(client):
    login(client)
    assert client.get('/api/mode').get_json() == {'mode': 'standby', 'stream': None}
    response = client.put('/api/mode', json={'mode': 'observe'})
    assert response.get_json() == {'mode': 'observe', 'stream': 'cam'}
    assert client.get('/api/mode').get_json()['mode'] == 'observe'
    assert client.put('/api/mode', json={'mode': 'listen'}).get_json()['stream'] == 'mic'


def test_unknown_mode_is_rejected(client):
    login(client)
    assert client.put('/api/mode', json={'mode': 'party'}).status_code == 400


def test_stream_auth(client):
    assert client.get('/auth/stream').status_code == 401
    login(client)
    assert client.get('/auth/stream').status_code == 204


def test_change_password(client):
    login(client)
    assert client.post('/api/password', json={'current': 'x', 'new': 'whatever1'}).status_code == 403
    assert client.post('/api/password', json={'current': PASSWORD, 'new': 'short'}).status_code == 400
    assert client.post('/api/password',
                       json={'current': PASSWORD, 'new': 'new password'}).status_code == 200
    client.post('/logout')
    assert login(client, 'new password').status_code == 302


def test_secret_key_persists(tmp_path):
    config = {'TESTING': True, 'DATABASE': str(tmp_path / 'k.sqlite3'), 'AGENT_SOCKET': 'fake'}
    assert create_app(config).secret_key == create_app(config).secret_key


def test_tls_ask_allows_ips_and_own_hostname(client, monkeypatch):
    monkeypatch.setattr('socket.gethostname', lambda: 'rock-4se')
    for domain in ('192.168.1.50', '10.42.0.1', 'fe80::1', 'localhost', 'rock-4se',
                   'ROCK-4SE.local'):
        assert client.get('/auth/tls-ask', query_string={'domain': domain}).status_code == 200
    for domain in ('example.com', 'rock-4se.example.com', ''):
        assert client.get('/auth/tls-ask', query_string={'domain': domain}).status_code == 403
