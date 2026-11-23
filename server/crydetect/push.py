"""Push alerts to phones through the board's own ntfy server (babymonitor-ntfy).

Configured by /etc/babymonitor/ntfy.env (written by server/deploy/install.sh), which
the crydetect and web units load:
    NTFY_URL=http://127.0.0.1:2586     where to publish (ntfy itself, not via Caddy)
    NTFY_TOPIC=babymonitor
    NTFY_TOKEN=tk_...                  write-only token of the ntfy user 'monitor'
    NTFY_PUBLIC_URL=https://rock-4se.local:8443   what phones subscribe to
    NTFY_PHONE_USER=parent             read-only ntfy user for the phones
Without NTFY_TOKEN, pushing is off. Standard library only.
"""
import logging
import os
import threading
import urllib.request

log = logging.getLogger('crydetect.push')

TITLE = 'Baby monitor'
TEXTS = {
    'crying': 'Crying detected',
    'babbling': 'Babbling detected',
    'crying_and_babbling': 'Crying and babbling detected',
    'crying_or_babbling': 'Baby sounds detected',
    'sound': 'Loud sound detected',
    'test': 'Test notification: phone alerts work.',
}
TIMEOUT_S = 10


def config(environ=os.environ):
    token = environ.get('NTFY_TOKEN', '')
    return {
        'enabled': bool(token),
        'url': environ.get('NTFY_URL', 'http://127.0.0.1:2586').rstrip('/'),
        'topic': environ.get('NTFY_TOPIC', 'babymonitor'),
        'token': token,
        'public_url': environ.get('NTFY_PUBLIC_URL', ''),
        'phone_user': environ.get('NTFY_PHONE_USER', 'parent'),
    }


def build_request(cfg, kind):
    urgent = kind in ('crying', 'crying_and_babbling', 'crying_or_babbling')
    return urllib.request.Request(
        f'{cfg["url"]}/{cfg["topic"]}',
        data=TEXTS.get(kind, kind).encode(),
        method='POST',
        headers={
            'Authorization': f'Bearer {cfg["token"]}',
            'Title': TITLE,
            # 5 (urgent) can be set to break through Do Not Disturb in the app.
            'Priority': 'urgent' if urgent else 'high' if kind != 'test' else 'default',
            'Tags': 'baby' if kind != 'sound' else 'loud_sound',
        })


def send(kind, cfg=None, opener=urllib.request.urlopen):
    """Publish one alert; return None on success or an error message."""
    cfg = cfg or config()
    if not cfg['enabled']:
        return 'phone notifications are not set up (no NTFY_TOKEN)'
    try:
        with opener(build_request(cfg, kind), timeout=TIMEOUT_S) as response:
            response.read()
        return None
    except Exception as e:  # network errors, HTTP errors: report, never crash detection
        return f'could not reach the notification server: {e}'


def send_in_background(kind, cfg=None):
    """For the detector: publishing must never hold up the next recording."""
    def work():
        error = send(kind, cfg)
        if error:
            log.warning('Push failed: %s', error)
    threading.Thread(target=work, daemon=True).start()
