"""Single-password login, as upstream: one shared password for the whole household."""
import functools
import time

import click
from flask import (Blueprint, current_app, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_setting, set_setting

bp = Blueprint('auth', __name__, cli_group=None)  # CLI: `flask set-password`

FAILED_LOGIN_DELAY_S = 1.0


def is_logged_in():
    return session.get('logged_in') is True


def login_required(view):
    """Redirect pages to the login form; answer API calls with 401."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not is_logged_in():
            if request.path.startswith('/api/'):
                return jsonify(error='login required'), 401
            return redirect(url_for('auth.login', next=request.path))
        return view(*args, **kwargs)

    return wrapped


def set_password(password):
    set_setting('password_hash', generate_password_hash(password))


@bp.route('/login', methods=['GET', 'POST'])
def login():
    password_hash = get_setting('password_hash')
    if request.method == 'POST':
        if password_hash and check_password_hash(password_hash,
                                                 request.form.get('password', '')):
            session.clear()
            session.permanent = True
            session['logged_in'] = True
            target = request.args.get('next', '')
            # Only follow local paths, never another site.
            if not target.startswith('/') or target.startswith('//'):
                target = url_for('index')
            return redirect(target)
        time.sleep(FAILED_LOGIN_DELAY_S)
        flash('Wrong password.')
    return render_template('login.html', password_set=bool(password_hash))


@bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@bp.route('/api/password', methods=['POST'])
@login_required
def change_password():
    data = request.get_json(silent=True) or {}
    if not check_password_hash(get_setting('password_hash'), data.get('current', '')):
        time.sleep(FAILED_LOGIN_DELAY_S)
        return jsonify(error='current password is wrong'), 403
    new = data.get('new', '')
    if len(new) < 8:
        return jsonify(error='new password must be at least 8 characters'), 400
    set_password(new)
    return jsonify(ok=True)


@bp.cli.command('set-password')
@click.password_option()
def set_password_command(password):
    """Set the web UI password (needed once after installation)."""
    set_password(password)
    click.echo('Password set.')


@bp.cli.command('password-is-set')
def password_is_set_command():
    """Exit with 0 if a web password has been set, 1 if not (for the install script)."""
    is_set = bool(get_setting('password_hash'))
    click.echo('yes' if is_set else 'no')
    raise SystemExit(0 if is_set else 1)


@bp.app_context_processor
def inject_login_state():
    return {'logged_in': is_logged_in(), 'whep_base': current_app.config['WHEP_BASE']}
