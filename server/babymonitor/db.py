"""SQLite storage: a single key/value settings table with JSON-encoded values."""
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
"""

# Defaults for user-visible settings; anything not listed here must be set explicitly.
DEFAULTS = {
    'language': 'en',
}


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'])
        g.db.execute('PRAGMA journal_mode=WAL')
    return g.db


def close_db(_exc=None):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()


def get_setting(key, default=None):
    row = get_db().execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key, default)
    return json.loads(row[0])


def set_setting(key, value):
    conn = get_db()
    conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                 (key, json.dumps(value)))
    conn.commit()


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
