"""OpenBabyMonitor web service: login, mode switching, settings and stream access control.

Media never passes through this process. Streams are served by MediaMTX (WebRTC/WHEP)
behind the HTTPS proxy, which asks /auth/stream whether the browser is logged in.
"""
import os
from datetime import timedelta

from flask import Flask

from . import agent_client, auth, db, modes, streams


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.environ.get('BM_DATABASE',
                                os.path.join(app.instance_path, 'babymonitor.sqlite3')),
        # Unix socket of the root host agent, or 'fake' for development without one.
        AGENT_SOCKET=os.environ.get('BM_AGENT_SOCKET', '/run/babymonitor/agent.sock'),
        # Where the browser sends WHEP requests: '/stream' behind Caddy, or e.g.
        # 'http://rock-4se:8889' when developing against MediaMTX directly.
        WHEP_BASE=os.environ.get('BM_WHEP_BASE', '/stream'),
        SESSION_COOKIE_SECURE=os.environ.get('BM_INSECURE_COOKIES') != '1',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict',
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if test_config is not None:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)
    db.init_app(app)
    agent_client.init_app(app)

    app.register_blueprint(auth.bp)
    app.register_blueprint(modes.bp)
    app.register_blueprint(streams.bp)
    app.add_url_rule('/', endpoint='index', view_func=modes.index)
    return app
