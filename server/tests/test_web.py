import pytest

from babymonitor import auth, create_app, db

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


def test_notify_api_requires_login(client):
    assert client.get('/api/notify').status_code == 401
    assert client.get('/api/notify/settings').status_code == 401
    assert client.put('/api/notify/settings', json={}).status_code == 401


def test_notify_settings(client):
    login(client)
    data = client.get('/api/notify/settings').get_json()
    assert data['values']['model'] == 'large_network'
    assert data['spec']['probability_threshold'] == {'type': 'int', 'default': 65,
                                                     'min': 0, 'max': 100}
    response = client.put('/api/notify/settings', json={'probability_threshold': 70})
    assert response.get_json()['values']['probability_threshold'] == 70
    assert client.put('/api/notify/settings', json={'model': 'x'}).status_code == 400
    assert client.put('/api/notify/settings', json=[1]).status_code == 400
    values = client.get('/api/notify/settings').get_json()['values']
    assert values['probability_threshold'] == 70 and values['model'] == 'large_network'


def test_notify_state_and_events(app, client):
    login(client)
    assert client.get('/api/notify').get_json() == {'status': None, 'events': [], 'last_id': 0}
    with app.app_context():
        conn = db.get_db()
        db.add_notify_event(conn, 1.0, 'crying')
        db.write_value(conn, 'runtime', 'notify_status',
                       {'time': 5.0, 'background': 20.0, 'contrast': 3.0, 'probabilities': None})
    # A fresh page skips old alerts.
    first = client.get('/api/notify').get_json()
    assert first['events'] == [] and first['last_id'] == 1
    assert first['status']['contrast'] == 3.0 and first['status']['age'] > 0
    with app.app_context():
        db.add_notify_event(db.get_db(), 2.0, 'sound')
    later = client.get('/api/notify', query_string={'after': 1}).get_json()
    assert [e['kind'] for e in later['events']] == ['sound'] and later['last_id'] == 2
    assert client.get('/api/notify', query_string={'after': 2}).get_json()['events'] == []


def test_settings_page_and_api_require_login(client):
    assert client.get('/settings').status_code == 302
    for path in ('/api/system', '/api/network', '/api/network/scan'):
        assert client.get(path).status_code == 401
    assert client.post('/api/system/reboot').status_code == 401


def test_settings_page(client):
    login(client)
    assert b'Hotspot' in client.get('/settings').data
    assert b'/settings' in client.get('/').data


def test_system_settings_api(client):
    login(client)
    assert client.get('/api/system').get_json()['hostname'] == 'rock-4se'
    assert 'UTC' in client.get('/api/system/timezones').get_json()['timezones']
    assert client.put('/api/system/timezone', json={'timezone': 'UTC'}).status_code == 200
    assert client.get('/api/system').get_json()['timezone'] == 'UTC'
    response = client.put('/api/system/timezone', json={'timezone': 'Mars'})
    assert response.status_code == 400 and 'time zone' in response.get_json()['error']
    assert client.post('/api/system/time', json={'time': 1.8e9}).get_json() == {'changed': False}
    assert client.post('/api/system/reboot').status_code == 200
    assert client.post('/api/system/format-disk').status_code == 404


def test_network_settings_api(client):
    login(client)
    status = client.get('/api/network').get_json()
    assert status['hotspot'] is None and status['saved'][0]['name'] == 'Home'
    assert client.post('/api/network/hotspot/start').status_code == 400  # not set up
    assert client.put('/api/network/hotspot',
                      json={'ssid': 'nursery', 'password': 'longenough', 'channel': 0}
                      ).status_code == 200
    assert client.post('/api/network/hotspot/start').status_code == 200
    assert client.get('/api/network').get_json()['hotspot_active'] is True
    assert client.post('/api/network/connect', json={'ssid': 'Cafe'}).status_code == 200
    saved = client.get('/api/network').get_json()['saved']
    assert [n['name'] for n in saved] == ['Home', 'Cafe']
    assert client.delete(f'/api/network/saved/{saved[1]["uuid"]}').status_code == 200
    assert client.put('/api/network/country', json={'country': 'NO'}).status_code == 200
    assert client.get('/api/network/scan').get_json()['networks'][0]['ssid'] == 'Home'
