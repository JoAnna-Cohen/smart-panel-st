"""Leviton Smart Panel – SmartThings Schema Connector (web app).

  /oauth/authorize  Leviton login page (email, password, optional 2FA code)
  /oauth/token      OAuth 2.0 token endpoint SmartThings calls
  /webhook          st-schema webhook (discovery, state, commands, …)
  /                 Landing page
  /health           Health check

Live state pushes run separately in worker.py. Configuration: .env.example.
"""

import hmac
import logging
import os
from urllib.parse import urlencode, urlparse

from flask import Flask, jsonify, redirect, render_template, request, session

from smartpanel_smartthings import leviton, runtime

runtime.setup_logging()
_LOGGER = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True)

auth_manager, connector = runtime.build()

# SmartThings OAuth redirect URIs live on *.smartthings.com; refuse others so
# the login page can't be used to send auth codes somewhere else.
ALLOWED_REDIRECT_SUFFIXES = tuple(
    s.strip()
    for s in os.environ.get("ALLOWED_REDIRECT_HOSTS", ".smartthings.com").split(",")
    if s.strip()
)


def _redirect_allowed(uri: str) -> bool:
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and any(
        host == s.lstrip(".") or host.endswith(s) for s in ALLOWED_REDIRECT_SUFFIXES
    )


# ---------------------------------------------------------------------------
# OAuth – Authorization endpoint
# ---------------------------------------------------------------------------


@app.route("/oauth/authorize", methods=["GET"])
def oauth_authorize_get():
    """SmartThings sends the user here with ?redirect_uri=...&state=..."""
    redirect_uri = request.args.get("redirect_uri", "")
    if not _redirect_allowed(redirect_uri):
        _LOGGER.warning("OAUTH GET: rejected redirect_uri host %s", urlparse(redirect_uri).hostname)
        return render_template("login.html", step="error", error="Invalid redirect URI."), 400

    session.clear()
    session["oauth_redirect_uri"] = redirect_uri
    session["oauth_state"] = request.args.get("state", "")
    return render_template("login.html", step="credentials", error=None)


@app.route("/oauth/authorize", methods=["POST"])
def oauth_authorize_post():
    redirect_uri = session.get("oauth_redirect_uri", "")
    if not _redirect_allowed(redirect_uri):
        return render_template(
            "login.html", step="error",
            error="Your session expired. Start again from the SmartThings app.",
        ), 400

    if request.form.get("step") == "code":
        return _finish_2fa(redirect_uri)

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    allow_control = request.form.get("allow_control") == "on"
    if not email or not password:
        return render_template("login.html", step="credentials", error="Email and password are required.")

    try:
        result = leviton.login(email, password)
    except leviton.TwoFactorRequired:
        session["pending_login"] = auth_manager.start_pending_login(email, password)
        session["allow_control"] = allow_control
        return render_template("login.html", step="code", error=None)
    except leviton.LDATAAuthError:
        return render_template("login.html", step="credentials", error="Invalid email or password.")
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Leviton login failed")
        return render_template(
            "login.html", step="credentials",
            error="Could not reach Leviton. Try again in a minute.",
        )

    return _complete(redirect_uri, {"email": email, "password": password, **result}, allow_control)


def _finish_2fa(redirect_uri: str):
    pending_id = session.get("pending_login", "")
    pending = auth_manager.get_pending_login(pending_id)
    if not pending:
        return render_template(
            "login.html", step="credentials",
            error="That took too long. Please sign in again.",
        )

    code = request.form.get("code", "").strip()
    if not code:
        return render_template("login.html", step="code", error="Enter the code Leviton sent you.")

    try:
        result = leviton.login(pending["email"], pending["password"], code)
    except leviton.LDATAAuthError:
        return render_template("login.html", step="code", error="That code didn't work. Try again.")
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Leviton 2FA login failed")
        return render_template(
            "login.html", step="code", error="Could not reach Leviton. Try again in a minute."
        )

    auth_manager.drop_pending_login(pending_id)
    return _complete(redirect_uri, {**pending, **result}, bool(session.get("allow_control")))


def _complete(redirect_uri: str, creds: dict, allow_control: bool):
    link_id = auth_manager.create_link(creds, allow_control)
    code = auth_manager.create_auth_code(link_id)
    state = session.get("oauth_state", "")
    session.clear()
    separator = "&" if "?" in redirect_uri else "?"
    return redirect(f"{redirect_uri}{separator}{urlencode({'code': code, 'state': state})}")


# ---------------------------------------------------------------------------
# OAuth – Token endpoint
# ---------------------------------------------------------------------------


@app.route("/oauth/token", methods=["POST"])
def oauth_token():
    if request.authorization:
        client_id = request.authorization.username or ""
        client_secret = request.authorization.password or ""
    else:
        client_id = request.form.get("client_id", "")
        client_secret = request.form.get("client_secret", "")

    if not (runtime.ST_CLIENT_ID and runtime.ST_CLIENT_SECRET):
        _LOGGER.error("ST_CLIENT_ID / ST_CLIENT_SECRET are not configured")
        return jsonify({"error": "invalid_client"}), 401
    if not (
        hmac.compare_digest(client_id, runtime.ST_CLIENT_ID)
        and hmac.compare_digest(client_secret, runtime.ST_CLIENT_SECRET)
    ):
        _LOGGER.warning("Token endpoint: invalid client credentials")
        return jsonify({"error": "invalid_client"}), 401

    grant_type = request.form.get("grant_type", "")
    if grant_type == "authorization_code":
        token_resp = auth_manager.exchange_code(request.form.get("code", ""))
    elif grant_type == "refresh_token":
        token_resp = auth_manager.refresh_access_token(request.form.get("refresh_token", ""))
    else:
        return jsonify({"error": "unsupported_grant_type"}), 400

    if not token_resp:
        _LOGGER.warning("Token request failed for grant_type=%s", grant_type)
        return jsonify({"error": "invalid_grant"}), 400
    return jsonify(token_resp)


# ---------------------------------------------------------------------------
# SmartThings Schema Connector webhook
# ---------------------------------------------------------------------------


@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "invalid_json"}), 400
    return jsonify(connector.handle(body))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    _LOGGER.info("Starting Leviton Smart Panel SmartThings Connector on port %d", port)
    app.run(host="127.0.0.1", port=port, debug=runtime.DEBUG)
