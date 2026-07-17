from datetime import timedelta

from flask import Flask, abort, redirect, request, session, url_for

from .. import config
from . import auth
from .routes import bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    # With debug=False, Jinja compiles each template once and caches it for the
    # life of the process -- editing index.html then had no effect until the
    # whole recognition daemon was restarted. Costs one stat() per render.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    app.secret_key = auth.secret_key()
    if config.WEB_SESSION_HOURS > 0:
        app.permanent_session_lifetime = timedelta(hours=config.WEB_SESSION_HOURS)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    if auth.enabled():
        # Endpoints reachable without a session (the login flow and static files).
        open_endpoints = {"static", "routes.auth_login", "routes.auth_logout"}

        @app.before_request
        def _gate():
            if request.endpoint in open_endpoints:
                return
            if session.get("auth"):
                return
            supplied = request.headers.get("X-Auth-Token") or request.args.get("token", "")
            if auth.check_token(supplied):
                return
            # Humans get the login page; API/machine callers get a clean 401.
            wants_html = "text/html" in request.headers.get("Accept", "") \
                and request.method == "GET"
            if auth.login_enabled() and wants_html:
                return redirect(url_for("routes.auth_login", next=request.full_path))
            abort(401)

    app.register_blueprint(bp)
    return app
