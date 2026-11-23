"""SQLite storage, shared with the cry detector (crydetect/).

settings:      user settings, key/value with JSON-encoded values
runtime:       state written by the cry detector (latest measurement), same format
notify_events: alerts raised by the cry detector, read by the browser via the web service
"""
import json
import secrets
import sqlite3

import click
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notify_events (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    kind TEXT NOT NULL
);
"""
MAX_NOTIFY_EVENTS = 100

# Defaults for user-visible settings; anything not listed here must be set explicitly.
DEFAULTS = {
    'language': 'en',
}


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def get_db():
    if 'db' not in g:
        g.db = connect(current_app.config['DATABASE'])
    return g.db


def close_db(_exc=None):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()


# Connection-level helpers, usable outside Flask (the cry detector uses them).
# `table` is always one of the constants above, never user input.

def read_value(conn, table, key, default=None):
    row = conn.execute(f'SELECT value FROM {table} WHERE key = ?', (key,)).fetchone()
    return default if row is None else json.loads(row[0])


def write_value(conn, table, key, value):
    conn.execute(f'INSERT INTO {table} (key, value) VALUES (?, ?) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                 (key, json.dumps(value)))
    conn.commit()


def add_notify_event(conn, time, kind):
    conn.execute('INSERT INTO notify_events (time, kind) VALUES (?, ?)', (time, kind))
    conn.execute('DELETE FROM notify_events WHERE id <= '
                 '(SELECT MAX(id) FROM notify_events) - ?', (MAX_NOTIFY_EVENTS,))
    conn.commit()


def notify_events_after(conn, after_id):
    rows = conn.execute('SELECT id, time, kind FROM notify_events WHERE id > ? ORDER BY id',
                        (after_id,)).fetchall()
    return [{'id': i, 'time': t, 'kind': k} for i, t, k in rows]


def last_notify_event_id(conn):
    return conn.execute('SELECT COALESCE(MAX(id), 0) FROM notify_events').fetchone()[0]


def get_setting(key, default=None):
    return read_value(get_db(), 'settings', key, DEFAULTS.get(key, default))


def set_setting(key, value):
    write_value(get_db(), 'settings', key, value)


def load_secret_key():
    """Return the session signing key, creating and storing one on first use."""
    key = get_setting('secret_key')
    if key is None:
        key = secrets.token_hex(32)
        set_setting('secret_key', key)
    return key


@click.command('init-db')
def init_db_command():
    """Create the database tables."""
    init_db()
    click.echo(f'Initialised {current_app.config["DATABASE"]}')


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    with app.app_context():
        init_db()
        app.secret_key = load_secret_key()
