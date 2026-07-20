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

    # Auth is mandatory. Before credentials exist, everything funnels to the
    # one-time /setup page; afterward, a session or token is required.
    open_endpoints = {"static", "routes.auth_login", "routes.auth_logout",
                      "routes.setup"}

    @app.before_request
    def _gate():
        if request.endpoint in open_endpoints:
            return
        wants_html = "text/html" in request.headers.get("Accept", "") \
            and request.method == "GET"
        if auth.needs_setup():
            if wants_html:
                return redirect(url_for("routes.setup"))
            abort(401)
        if session.get("auth"):
            return
        supplied = request.headers.get("X-Auth-Token") or request.args.get("token", "")
        if auth.check_token(supplied):
            return
        # Humans get the login page; API/machine callers get a clean 401.
        if wants_html:
            return redirect(url_for("routes.auth_login", next=request.full_path))
        abort(401)

    app.register_blueprint(bp)
    return app
