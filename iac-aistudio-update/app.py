"""
IaC Build — Flask app for Azure Container Apps deployment.

Entra-only PostgreSQL authentication via the Container App's system-assigned
managed identity. Flask secret key is pulled from Azure Key Vault at startup.
PG-backed sessions, in-app rate limiting, security headers.

Environment variables (all required in cloud mode):
  KEY_VAULT_URI                       https://kv-iacb-lean.vault.azure.net/
  PG_HOST                             pg-iacb-lean.postgres.database.azure.com
  PG_DB                               appdb
  PG_USER                             ca-iacb-web    (the MI's name, case-sensitive)
  APPLICATIONINSIGHTS_CONNECTION_STRING  (optional, enables AI export)

Local-dev mode:
  Set LOCAL_DEV=1 to use SQLite + an env-var-based secret key. Useful
  for running on your laptop without Azure dependencies.
"""
import os
import requests
import json
import tempfile
import subprocess
import re
import html
import random
import string
import time
import hmac
import hashlib
import logging
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from flask import (Flask, render_template, request, session, redirect,
                   url_for, Response, make_response)
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session
from flask_compress import Compress
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import event
from sqlalchemy.engine import Engine

# ----- Environment ----------------------------------------------------------

LOCAL_DEV = os.environ.get("LOCAL_DEV", "").lower() in ("1", "true", "yes")
KV_URI    = os.environ.get("KEY_VAULT_URI")
PG_HOST   = os.environ.get("PG_HOST")
PG_DB     = os.environ.get("PG_DB", "appdb")
PG_USER   = os.environ.get("PG_USER")

OSSRDBMS_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("iacb")

# ----- Azure Identity -------------------------------------------------------

_cred = None
if not LOCAL_DEV:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    _cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)

# ----- Application Insights (auto-instrumentation) -------------------------
# Configures the OTLP exporter to Azure Monitor and auto-instruments Flask,
# requests, urllib, logging, and SQLAlchemy. No code changes elsewhere needed.
# Must run BEFORE Flask app is created so Flask routes get instrumented.
if not LOCAL_DEV and os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor(
            logger_name="iacb",
            disable_offline_storage=True,   # ACA filesystem is ephemeral
        )
        log.info("Application Insights instrumentation configured")
    except Exception as e:
        # Never let telemetry init crash the app on cold-start.
        log.warning("App Insights setup failed (continuing without): %s", e)

# Quiet Azure SDK HTTP logging: at root INFO it dumps every telemetry POST
# (URL, headers, status) to stdout, which then floods ContainerAppConsoleLogs_CL
# and App Insights traces. App's own "iacb" logger stays at INFO.
for _n in ("azure", "azure.core.pipeline.policies.http_logging_policy",
           "azure.monitor.opentelemetry.exporter", "azure.identity"):
    logging.getLogger(_n).setLevel(logging.WARNING)

# ----- Flask app ------------------------------------------------------------

app = Flask(__name__)
Compress(app)   # gzip/br responses (HTML/CSS/JS/JSON) to cut transfer size
csrf = CSRFProtect(app)   # protects all HTML form POSTs; JSON proxy + logout exempted below

# Trust ACA's TLS termination — sets request.is_secure correctly behind the
# Container Apps edge load balancer so secure cookies and url_for(_scheme)
# work as expected.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ----- Secret key: Key Vault in cloud, env var local -----------------------

if LOCAL_DEV:
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-do-not-ship")
    log.warning("LOCAL_DEV=1 — using insecure dev secret key")
else:
    if not KV_URI:
        raise RuntimeError("KEY_VAULT_URI must be set in cloud mode")
    _sc = SecretClient(vault_url=KV_URI, credential=_cred)
    # Stable signing key from Key Vault. Keeps sessions and CSRF tokens valid
    # across redeploys/restarts instead of logging every user out on each deploy.
    app.secret_key = _sc.get_secret("flask-secret-key").value
    log.info("Loaded stable session secret from Key Vault")

# ----- LLM API key for the server-side /api/generate proxy ------------------
# Fetched once at startup (KV in cloud, env var locally). The route returns 503
# if the key is absent, so the app still boots before the secret is created.
_LLM_KEY = os.environ.get("LLM_API_KEY")
if not LOCAL_DEV:
    try:
        _LLM_KEY = _sc.get_secret("llm-api-key").value
    except Exception as _e:
        log.warning("LLM key not in Key Vault yet: %s", _e)

# SECURITY (P1-5): pin the generation model and output cap server-side.
# Previously /api/generate took "model" straight from the client payload
# (only max_tokens was clamped), so a caller could request an arbitrary, more
# expensive model. These are now fixed here and overridable ONLY via env vars
# on the Container App — never by the request body.
LLM_MODEL          = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
LLM_MAX_TOKENS     = int(os.environ.get("LLM_MAX_TOKENS", "8000"))
LLM_DEFAULT_TOKENS = int(os.environ.get("LLM_DEFAULT_TOKENS", "4000"))

# ----- OAuth social login (Google + GitHub) --------------------------------
# Client IDs are public; secrets come from Key Vault (same vault as llm-api-key).
# TWO GitHub apps (one per domain) since a classic OAuth App locks to one host.
GOOGLE_CLIENT_ID        = os.environ.get("GOOGLE_CLIENT_ID",        "810180534624-mm6n7c8a50k2r98pmgrnksn983gcp05m.apps.googleusercontent.com")
GITHUB_CLIENT_ID_IAC    = os.environ.get("GITHUB_CLIENT_ID_IAC",    "Ov23litRKnOeOcC5YIBS")
GITHUB_CLIENT_ID_ONLINE = os.environ.get("GITHUB_CLIENT_ID_ONLINE", "Ov23ctEWg1GUBxzQtpdU")

_OAUTH_SECRETS = {}
if not LOCAL_DEV:
    for _sn in ("google-oauth-secret", "github-oauth-secret-iac", "github-oauth-secret-online"):
        try:
            _OAUTH_SECRETS[_sn] = _sc.get_secret(_sn).value
        except Exception as _e:
            log.warning("OAuth secret %s not loaded: %s", _sn, _e)

oauth = OAuth(app)
if _OAUTH_SECRETS.get("google-oauth-secret"):
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=_OAUTH_SECRETS["google-oauth-secret"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
for _gk, _gid, _gname in (("github-oauth-secret-iac",    GITHUB_CLIENT_ID_IAC,    "github_iac"),
                          ("github-oauth-secret-online", GITHUB_CLIENT_ID_ONLINE, "github_online")):
    if _OAUTH_SECRETS.get(_gk):
        oauth.register(
            name=_gname,
            client_id=_gid,
            client_secret=_OAUTH_SECRETS[_gk],
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )

OAUTH_ENABLED = bool(_OAUTH_SECRETS)

def _github_client_name():
    host = (request.host or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return "github_online" if "online-shield.com" in host else "github_iac"

# ----- Session policy -------------------------------------------------------

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["IDLE_SECONDS"]               = 1800
app.config["SESSION_COOKIE_HTTPONLY"]    = True
app.config["SESSION_COOKIE_SAMESITE"]    = "Lax"
# Secure cookies only in cloud (HTTPS); LOCAL_DEV runs HTTP.
app.config["SESSION_COOKIE_SECURE"]      = not LOCAL_DEV

# Bump SESSION_VERSION to invalidate ALL existing sessions on next deploy.
# Bumped 3 -> 4 with the auth hardening (OTP/session-rotation) so no pre-deploy
# session lingers in a half-migrated state. Users are logged out once.
SESSION_VERSION = 4

# ----- Database -------------------------------------------------------------

if LOCAL_DEV:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///users.db"
else:
    # Password placeholder in URL is overwritten per-connection by the
    # do_connect event listener below.
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"postgresql+psycopg://{quote_plus(PG_USER)}@{PG_HOST}/{PG_DB}?sslmode=require"
    )
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle":  1800,   # < ~1h token TTL
    }
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# Per-connection Entra token injection. Fires when SQLAlchemy opens a new
# physical connection (not per-query). Only affects PostgreSQL dialect, so
# local SQLite dev mode is unaffected.
@event.listens_for(Engine, "do_connect")
def _provide_token(dialect, conn_rec, cargs, cparams):
    if not LOCAL_DEV and dialect.name == "postgresql":
        cparams["password"] = _cred.get_token(OSSRDBMS_SCOPE).token

# ----- PG-backed sessions (cloud only) -------------------------------------

if not LOCAL_DEV:
    app.config["SESSION_TYPE"]       = "sqlalchemy"
    app.config["SESSION_SQLALCHEMY"] = db
    Session(app)   # auto-creates a 'sessions' table on first request

# ----- User model -----------------------------------------------------------

class User(db.Model):
    id            = db.Column(db.Integer,     primary_key=True)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    first_name    = db.Column(db.String(50),  nullable=False)
    last_name     = db.Column(db.String(50),  nullable=False)
    phone         = db.Column(db.String(20),  nullable=False)


class GenLog(db.Model):
    __tablename__ = "gen_log"
    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, index=True, nullable=False)
    ts      = db.Column(db.DateTime, default=datetime.utcnow, index=True, nullable=False)


# ----- Access control -------------------------------------------------------
# Admins (comma-separated emails in ADMIN_EMAILS) may run real AI generation.
# Everyone else is locked to DEMO mode so test users cannot spend API credits.
# Fail-closed: if ADMIN_EMAILS is unset, NO account can run live generation.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

def _is_admin(user):
    return bool(user) and bool(user.email) and user.email.lower() in ADMIN_EMAILS


with app.app_context():
    # No-op if tables already exist. Creates User table + (in cloud) sessions
    # table on first run after deploy.
    db.create_all()

# ----- Rate limiter ---------------------------------------------------------

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",   # single-replica prototype; OK per docx
)

# ----- Security & cache headers --------------------------------------------

# Content-Security-Policy.
# HONEST CAVEAT: this is a PERMISSIVE CSP, not a strict one. The existing
# HTML has 110+ inline event handlers (onclick=..., onload=..., etc.) and 11
# inline <script> blocks, which require 'unsafe-inline' on script-src. This
# CSP therefore does NOT prevent inline-script XSS. What it DOES do:
#   - Restricts external script/style/font/img sources to a known allow-list
#   - Blocks <object>/<embed> (object-src 'none')
#   - Blocks framing of this site (frame-ancestors 'none' — clickjacking)
#   - Locks <base> and <form action> to same-origin
# Phase 2 fix: extract inline JS to /static/js/*.js, use script-src nonces.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "style-src-elem 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)


@app.after_request
def _security_headers(resp):
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = _CSP
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["X-Content-Type-Options"]    = "nosniff"
    resp.headers["X-Frame-Options"]           = "DENY"
    resp.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"]        = "camera=(), microphone=(), geolocation=()"
    return resp

# ----- Session guard --------------------------------------------------------

@app.before_request
def _session_guard():
    if "user_id" in session:
        now = time.time()
        if session.get("sv") != SESSION_VERSION:
            session.clear()
            return
        if now - session.get("la", 0) > app.config["IDLE_SECONDS"]:
            session.clear()
            return
        session["la"] = now

# ----- Login helper (session rotation to prevent fixation) ------------------

def _login_user(user):
    """Start an authenticated session. Clears any pre-auth session first so a
    fixated/anonymous session id cannot be reused after login (P1-3)."""
    session.clear()
    session["user_id"] = user.id
    session.permanent  = True
    session["sv"]      = SESSION_VERSION
    session["la"]      = time.time()

# ----- Domain layer ---------------------------------------------------------

def generate_svg_captcha(text):
    width, height = 250, 60
    svg = f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
    svg += '<rect width="100%" height="100%" fill="#0D1017"/>'
    x_position = 25
    for char in text:
        y_position = random.randint(35, 45)
        angle = random.randint(-15, 15)
        font_size = random.randint(32, 40)
        svg += (f'<text x="{x_position}" y="{y_position}" '
                f'transform="rotate({angle} {x_position} {y_position})" '
                f'font-family="Georgia, serif" font-size="{font_size}" fill="#3B82F6">{char}</text>')
        x_position += 35
    svg += "</svg>"
    return svg


def check_password_strength(password):
    if not password or len(password) < 8:
        return False, "Password must be at least 8 characters."
    if not any(c.isupper() for c in password):
        return False, "Password needs a capital letter."
    if not any(c.isdigit() for c in password):
        return False, "Password needs a number."
    return True, "Password is secure."

# ----- OTP (one-time codes) -------------------------------------------------
# Hardened per security review (P0-1): 6-digit cryptographically-random codes,
# stored in the session as an HMAC (never plaintext) with an issue timestamp
# and an attempt counter. Enforces server-side expiry and lockout after too
# many wrong tries, and consumes the code on success (single-use).
OTP_TTL_SECONDS  = int(os.environ.get("OTP_TTL_SECONDS", "600"))   # 10 minutes
OTP_MAX_ATTEMPTS = int(os.environ.get("OTP_MAX_ATTEMPTS", "5"))


def _hash_otp(code):
    key = app.secret_key
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, (code or "").encode(), hashlib.sha256).hexdigest()


def _clear_otp():
    for _k in ("otp_hash", "otp_ts", "otp_attempts"):
        session.pop(_k, None)


def issue_otp():
    """Generate a 6-digit code, store its HMAC + metadata in the session, and
    return the plaintext code (to be emailed)."""
    code = f"{secrets.randbelow(10**6):06d}"
    session["otp_hash"]     = _hash_otp(code)
    session["otp_ts"]       = time.time()
    session["otp_attempts"] = 0
    return code


def verify_otp(submitted):
    """Validate a submitted code against the session. Returns (ok, message).
    Enforces expiry and attempt lockout; consumes the code on success."""
    submitted = (submitted or "").strip()
    stored = session.get("otp_hash")
    if not stored:
        return False, "No code outstanding — request a new one."
    if time.time() - session.get("otp_ts", 0) > OTP_TTL_SECONDS:
        _clear_otp()
        return False, "Code expired — request a new one."
    session["otp_attempts"] = session.get("otp_attempts", 0) + 1
    if session["otp_attempts"] > OTP_MAX_ATTEMPTS:
        _clear_otp()
        return False, "Too many attempts — request a new code."
    if hmac.compare_digest(stored, _hash_otp(submitted)):
        _clear_otp()
        return True, ""
    return False, "Incorrect verification code."

# ----- Email OTP sender (Resend with log fallback) -------------------------

RESEND_FROM = os.environ.get("RESEND_FROM", "noreply@iac-aistudio.com").strip()
RESEND_FROM_NAME = os.environ.get("RESEND_FROM_NAME", "IaC-AIStudio IaC Build").strip()
RESEND_API_KEY_KV_SECRET = os.environ.get("RESEND_API_KEY_KV_SECRET", "resend-api-key")


def _resend_from():
    """Pick the email sender by the domain the request is on, so each domain
    sends from itself (online-shield.com -> its address, everything else -> the
    RESEND_FROM default = iac-aistudio.com). Mirrors the per-host OAuth routing."""
    host = ""
    try:
        host = (request.host or "").lower().split(":")[0]
    except Exception:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    if "online-shield.com" in host:
        return ("Online-Shield IaC Build", "noreply@online-shield.com")
    return (RESEND_FROM_NAME, RESEND_FROM)


def send_otp_email(email, code, subject="Your verification code"):
    """Send OTP via Resend. Falls back to log if Resend not configured."""
    if not email or LOCAL_DEV or _sc is None:
        log.info("[MOCK-EMAIL] to=%s subject=%s code=%s", email, subject, code)
        return
    try:
        api_key = _sc.get_secret(RESEND_API_KEY_KV_SECRET).value
        import resend
        resend.api_key = api_key
        html = (
            '<div style="font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;'
            'max-width:480px;margin:auto;padding:32px;background:#0D1017;color:#F2F4F6;border-radius:14px">'
            f'<h2 style="color:#3B82F6;margin:0 0 16px;font-size:20px">{subject}</h2>'
            '<p style="margin:0 0 20px;color:#C3CCD7">Your verification code is:</p>'
            f'<div style="font-size:34px;letter-spacing:10px;font-weight:700;color:#3B82F6;'
            'text-align:center;background:#080A0F;padding:24px;border-radius:10px;margin:20px 0;'
            f'border:1px solid #1F2735">{code}</div>'
            '<p style="color:#A7AEB8;font-size:13px;margin:24px 0 8px">'
            "This code expires in 10 minutes. If you didn't request this, ignore this email.</p>"
            '<p style="color:#6E757F;font-size:12px;margin:24px 0 0;border-top:1px solid #1F2735;padding-top:16px">'
            "— IaC-AIStudio.com IaC Build</p></div>"
        )
        _fn, _fa = _resend_from()
        resend.Emails.send({
            "from": f"{_fn} <{_fa}>",
            "to": [email],
            "subject": subject,
            "html": html,
        })
        log.info("[EMAIL-SENT] to=%s subject=%s", email, subject)
    except Exception as exc:
        log.error("[EMAIL-FAIL] to=%s err=%s", email, exc)
        log.info("[MOCK-EMAIL] to=%s subject=%s code=%s", email, subject, code)


# Backwards-compat alias
def send_mock_email(email, code, subject="Your verification code"):
    send_otp_email(email, code, subject)


def log_audit_trail(email, action):
    log.info("[AUDIT] %s: %s", action, email)

# ----- App HTML serving (single dark-blue variant) -------------------------

APP_DIR      = os.path.dirname(os.path.abspath(__file__))
LANDING_FILE = os.path.join(APP_DIR, "landing.html")
APP_FILE     = os.path.join(APP_DIR, "app.html")


def serve_iac_app(user):
    with open(APP_FILE, encoding="utf-8") as f:
        page = f.read()
    name = html.escape(f"{user.first_name} {user.last_name}")
    page = page.replace('class="nav-item user-menu hidden"',
                        'class="nav-item user-menu"')
    page = page.replace("__USER_NAME__", name)
    page = page.replace("/*__IS_ADMIN__*/false/*__/IS_ADMIN__*/",
                        "true" if _is_admin(user) else "false")
    page = page.replace('id="authBtns"', 'id="authBtns" style="display:none"')

    _form = ('<form method="post" action="/logout" style="display:inline;margin:0">'
             '<button type="submit" style="all:unset;cursor:pointer;color:#F06A5C;'
             'display:block;padding:9px 14px;border-radius:8px;font-size:12.5px;'
             'width:100%;box-sizing:border-box;text-align:left">Logout</button></form>')
    page = re.sub(r'<a href="(?:/logout|#)"[^>]*>Logout</a>', _form, page)
    return Response(page, mimetype="text/html")

# ----- Routes ---------------------------------------------------------------

@app.route("/")
def home():
    with open(LANDING_FILE, encoding="utf-8") as f:
        page = f.read()
    if "user_id" in session:
        user = db.session.get(User, session["user_id"])
        if user is not None:
            start = page.find("<!--AUTH-->")
            end = page.find("<!--/AUTH-->") + len("<!--/AUTH-->")
            if start != -1 and end > start:
                name = (user.first_name or user.email.split("@")[0])
                replacement = (
                    f'<span style="font-size:12px;color:var(--text2);'
                    f'white-space:nowrap;align-self:center">Signed in as '
                    f'<b style="color:var(--text)">{html.escape(name)}</b></span>\n'
                    f'      <a class="btn primary" href="/app">⌂ Open Workspace</a>\n'
                    f'      <form method="post" action="/logout" '
                    f'style="display:inline;margin:0">'
                    f'<button type="submit" class="btn" style="cursor:pointer">'
                    f'Logout</button></form>'
                )
                page = page[:start] + replacement + page[end:]
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def login():
    if request.method == "GET":
        if "user_id" in session:
            return redirect(url_for("iac_app"))
        return render_template("auth.html", view="login", oauth_enabled=OAUTH_ENABLED)

    email = request.form.get("email")
    password = request.form.get("password")
    user = User.query.filter_by(email=email).first()
    if user and check_password_hash(user.password_hash, password):
        _login_user(user)
        log_audit_trail(user.email, "LOGGED IN")
        return redirect(url_for("iac_app"))
    return render_template("auth.html", view="login",
                           error_message="Invalid email or password.")


@app.route("/upgrade")
def upgrade():
    return render_template("auth.html", view="upgrade")


@app.route("/auth/<provider>/login")
def oauth_login(provider):
    if provider == "google":
        client = oauth.create_client("google")
    elif provider == "github":
        client = oauth.create_client(_github_client_name())
    else:
        client = None
    if client is None:
        return redirect(url_for("login"))
    redirect_uri = url_for("oauth_callback", provider=provider, _external=True,
                           _scheme=("http" if LOCAL_DEV else "https"))
    return client.authorize_redirect(redirect_uri)


@app.route("/auth/<provider>/callback")
def oauth_callback(provider):
    try:
        if provider == "google":
            client = oauth.create_client("google")
            if client is None:
                return redirect(url_for("login"))
            token = client.authorize_access_token()
            info  = token.get("userinfo") or {}
            email    = (info.get("email") or "").strip().lower()
            verified = bool(info.get("email_verified"))
            fn = info.get("given_name") or ""
            ln = info.get("family_name") or ""
        elif provider == "github":
            client = oauth.create_client(_github_client_name())
            if client is None:
                return redirect(url_for("login"))
            client.authorize_access_token()
            emails  = client.get("user/emails").json()
            primary = next((e for e in emails
                            if e.get("primary") and e.get("verified")), None)
            email    = ((primary or {}).get("email") or "").strip().lower()
            verified = primary is not None
            prof = client.get("user").json()
            full = (prof.get("name") or prof.get("login") or "").strip()
            bits = full.split(" ", 1)
            fn = bits[0] if bits else ""
            ln = bits[1] if len(bits) > 1 else ""
        else:
            return redirect(url_for("login"))
    except Exception as _e:
        log.warning("OAuth callback failed (%s): %s", provider, _e)
        return redirect(url_for("login"))

    if not email or not verified:
        log.warning("OAuth: no verified email from %s", provider)
        return redirect(url_for("login"))

    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(
            email=email,
            password_hash=generate_password_hash(secrets.token_urlsafe(32)),
            first_name=(fn or email.split("@")[0])[:50],
            last_name=(ln or "")[:50],
            phone="",
        )
        db.session.add(user)
        db.session.commit()
        log_audit_trail(email, f"REGISTERED via {provider}")

    _login_user(user)
    log_audit_trail(email, f"LOGGED IN via {provider}")
    return redirect(url_for("iac_app"))


@app.route("/captcha_image")
def captcha_image():
    text = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    session["captcha_answer"] = text.lower()
    return Response(generate_svg_captcha(text), mimetype="image/svg+xml")


@app.route("/start_register")
def start_register():
    return render_template("auth.html", view="register", step=1)


@app.route("/register", methods=["POST"])
@limiter.limit("5/minute")
def register():
    user_captcha = request.form.get("captcha", "").lower().strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password")

    if user_captcha != session.get("captcha_answer"):
        return render_template("auth.html", view="register", step=1,
                               error_message="CAPTCHA Failed.")
    if User.query.filter_by(email=email).first():
        return render_template("auth.html", view="register", step=1,
                               error_message="Email already registered.")
    ok, msg = check_password_strength(password)
    if not ok:
        return render_template("auth.html", view="register", step=1,
                               error_message=msg)

    session["reg_email"]    = email
    session["reg_hash"]     = generate_password_hash(password)
    session["reg_verified"] = False
    send_otp_email(email, issue_otp())
    return render_template("auth.html", view="register", step=2,
                           success_message="Code sent — check your email inbox.")


@app.route("/verify", methods=["POST"])
@limiter.limit("10/minute")
def verify():
    ok, msg = verify_otp(request.form.get("otp"))
    if ok:
        session["reg_verified"] = True
        return render_template("auth.html", view="register", step=3,
                               success_message="Code confirmed! Complete profile.")
    return render_template("auth.html", view="register", step=2,
                           error_message=msg)


@app.route("/profile", methods=["POST"])
def profile():
    first = request.form.get("first_name")
    last  = request.form.get("last_name")
    phone = request.form.get("phone", "").strip()  # optional now
    if not all([first, last]):
        return render_template("auth.html", view="register", step=3,
                               error_message="First and last name are required.")
    if not session.get("reg_email") or not session.get("reg_hash"):
        return render_template("auth.html", view="register", step=1,
                               error_message="Session expired — restart registration.")
    # Require that the emailed OTP was actually verified in this session, so the
    # account-creation step can't be reached by skipping verification.
    if not session.get("reg_verified"):
        return render_template("auth.html", view="register", step=1,
                               error_message="Please verify your email code first.")
    new_user = User(
        email=session.get("reg_email"),
        password_hash=session.get("reg_hash"),
        first_name=first, last_name=last, phone=phone,
    )
    db.session.add(new_user)
    db.session.commit()
    log_audit_trail(new_user.email, "ACCOUNT CREATED")
    session.clear()
    return render_template("auth.html", view="register", step=4)


def _gen_day_window():
    """Calendar-day cap window (UTC): returns (midnight_today, resets_in_seconds)."""
    now = datetime.utcnow()
    since = datetime(now.year, now.month, now.day)
    secs = int((since + timedelta(days=1) - now).total_seconds())
    return since, max(0, secs)


@app.post("/api/generate")
@limiter.limit("30/minute")
def api_generate():
    # Server-side proxy to the model API. The builder (/app) is auth-gated, so
    # this endpoint requires a session too: it keeps the API key server-side and
    # blocks anonymous use of a paid endpoint. Streams (SSE) when the client sets
    # {"stream": true}; otherwise returns the full JSON response.
    if "user_id" not in session:
        return {"error": "authentication required"}, 401
    if not _is_admin(db.session.get(User, session["user_id"])):
        return {"error": "demo_only",
                "message": "This account is limited to Demo mode; live AI generation is disabled."}, 403
    if not _LLM_KEY:
        return {"error": "LLM key not configured on the server"}, 503
    payload = request.get_json(force=True, silent=True) or {}
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return {"error": "messages array is required"}, 400
    # Per-user daily generation cap (cost guardrail). Counts accepted requests
    # since 00:00 UTC today; configurable via DAILY_GEN_CAP (default 100).
    _cap = int(os.environ.get("DAILY_GEN_CAP", "100"))
    _since, _ = _gen_day_window()
    _used = db.session.query(GenLog).filter(
        GenLog.user_id == session["user_id"], GenLog.ts >= _since).count()
    if _used >= _cap:
        return {"error": "daily generation limit reached", "limit": _cap, "used": _used}, 429
    db.session.add(GenLog(user_id=session["user_id"]))
    db.session.commit()
    log.info("generation user=%s used=%s/%s", session["user_id"], _used + 1, _cap)
    want_stream = bool(payload.get("stream"))
    body = {
        # Model is PINNED server-side (P1-5) — the client "model" field is
        # intentionally ignored. Change it via the LLM_MODEL env var only.
        "model": LLM_MODEL,
        "max_tokens": min(int(payload.get("max_tokens", LLM_DEFAULT_TOKENS)), LLM_MAX_TOKENS),
        "messages": messages,
        "stream": want_stream,
    }
    headers = {
        "x-api-key": _LLM_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if not want_stream:
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers=headers, json=body, timeout=120)
            return Response(r.text, status=r.status_code, mimetype="application/json")
        except requests.RequestException as e:
            log.warning("LLM upstream error: %s", e)
            return {"error": "upstream request failed"}, 502

    def relay():
        try:
            with requests.post("https://api.anthropic.com/v1/messages",
                               headers=headers, json=body, stream=True, timeout=300) as up:
                for chunk in up.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
        except requests.RequestException as e:
            log.warning("LLM stream error: %s", e)
            yield b'event: error\ndata: {"error":"upstream"}\n\n'

    return Response(relay(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/usage")
def api_usage():
    if "user_id" not in session:
        return {"error": "authentication required"}, 401
    cap = int(os.environ.get("DAILY_GEN_CAP", "100"))
    since, resets_in = _gen_day_window()
    used = db.session.query(GenLog).filter(
        GenLog.user_id == session["user_id"], GenLog.ts >= since).count()
    return {"used": used, "limit": cap, "remaining": max(0, cap - used),
            "resets_in_seconds": resets_in}


@app.post("/api/validate")
@limiter.limit("20/minute")
def api_validate():
    # Real static IaC scan (checkov) on generated HCL. checkov parses the HCL
    # only: no terraform init, no provider download, no code execution.
    if "user_id" not in session:
        return {"error": "authentication required"}, 401
    payload = request.get_json(force=True, silent=True) or {}
    hcl = payload.get("hcl", "")
    if not isinstance(hcl, str) or not hcl.strip():
        return {"error": "hcl required"}, 400
    if len(hcl) > 200000:
        return {"error": "hcl too large"}, 413
    with tempfile.TemporaryDirectory() as d:
        fp = os.path.join(d, "main.tf")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(hcl)
        try:
            r = subprocess.run(
                ["checkov", "-f", fp, "-o", "json", "--compact", "--quiet"],
                capture_output=True, text=True, timeout=120, cwd=d)
        except subprocess.TimeoutExpired:
            return {"error": "validation timed out"}, 504
        except FileNotFoundError:
            return {"error": "validator not installed"}, 500
        out = (r.stdout or "").strip()
    try:
        data = json.loads(out) if out else {}
    except ValueError:
        return {"error": "validator output unparseable"}, 500
    if isinstance(data, list):
        data = data[0] if data else {}
    results = data.get("results") or {}
    failed = results.get("failed_checks") or []
    passed = results.get("passed_checks") or []
    return {
        "passed": len(passed),
        "failed": len(failed),
        "failures": [
            {"id": c.get("check_id"), "name": c.get("check_name"),
             "resource": c.get("resource")} for c in failed[:60]
        ],
    }


@app.route("/app")
def iac_app():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user = db.session.get(User, session["user_id"])
    if user is None:
        session.clear()
        return redirect(url_for("login"))
    return serve_iac_app(user)


@app.route("/dashboard")
def dashboard():
    return redirect(url_for("iac_app"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user_id" not in session:
        return redirect(url_for("home"))
    user = db.session.get(User, session["user_id"])
    if user is None:
        session.clear()
        return redirect(url_for("home"))
    if request.method == "POST":
        first = request.form.get("first_name", "").strip()
        last  = request.form.get("last_name", "").strip()
        phone = request.form.get("phone", "").strip()
        if not all([first, last, phone]):
            return render_template("auth.html", view="settings", user=user,
                                   error_message="All fields are required.")
        user.first_name, user.last_name, user.phone = first, last, phone
        db.session.commit()
        log_audit_trail(user.email, "PROFILE UPDATED")
        return render_template("auth.html", view="settings", user=user,
                               success_message="Profile saved.")
    return render_template("auth.html", view="settings", user=user)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    # POST logs out; GET is harmless (browser back replay won't kill sessions).
    if request.method == "POST":
        session.clear()
    return redirect(url_for("home"))


@app.route("/start_recover")
def start_recover():
    return render_template("auth.html", view="recover", step=1)


@app.route("/recover_lookup", methods=["POST"])
@limiter.limit("5/minute")
def recover_lookup():
    email = request.form.get("email", "").strip()
    user = User.query.filter_by(email=email).first()
    # Uniform response whether or not the account exists, to avoid email
    # enumeration. A real code is only issued + sent when the user exists.
    if user:
        session["recover_email"]    = email
        session["recover_verified"] = False
        send_otp_email(email, issue_otp(), subject="Password Reset Code")
    else:
        _clear_otp()
        session.pop("recover_email", None)
        session.pop("recover_verified", None)
    return render_template("auth.html", view="recover", step=2,
                           success_message="If that account exists, a reset code has been sent.")


@app.route("/recover_verify", methods=["POST"])
@limiter.limit("10/minute")
def recover_verify():
    ok, msg = verify_otp(request.form.get("otp"))
    if ok:
        session["recover_verified"] = True
        return render_template("auth.html", view="recover", step=3,
                               success_message="Identity verified. Set new password.")
    return render_template("auth.html", view="recover", step=2,
                           error_message=msg)


@app.route("/recover_reset", methods=["POST"])
def recover_reset():
    # Require a verified OTP in THIS session before allowing a reset, so the
    # reset step cannot be reached by skipping verification. Without this guard,
    # anyone who can set recover_email (a plain POST to /recover_lookup) could
    # POST here directly and change another account's password.
    if not session.get("recover_verified"):
        return render_template("auth.html", view="recover", step=1,
                               error_message="Session expired — restart recovery.")
    new_password = request.form.get("new_password")
    ok, msg = check_password_strength(new_password)
    if not ok:
        return render_template("auth.html", view="recover", step=3,
                               error_message=msg)
    user = User.query.filter_by(email=session.get("recover_email")).first()
    if user is None:
        return render_template("auth.html", view="recover", step=1,
                               error_message="Session expired — restart recovery.")
    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    log_audit_trail(user.email, "PASSWORD RESET")
    session.clear()
    return render_template("auth.html", view="login",
                           success_message="Password reset. Please log in.")


# ----- Health check (for Container Apps probes / external monitors) --------

@app.route("/healthz")
@limiter.exempt
def healthz():
    # Note: this hits the DB by design — a healthy app must be able to query PG.
    try:
        db.session.execute(db.text("SELECT 1"))
        return {"status": "ok"}, 200
    except Exception as e:
        log.error("Healthcheck failed: %s", e)
        return {"status": "degraded", "detail": str(e)}, 503


if __name__ == "__main__":
    # Local dev only — production runs via Gunicorn (see Dockerfile).
    app.run(host="0.0.0.0", port=8000, debug=False)


# --- CSRF exemptions: the session-gated JSON proxy (the SameSite=Lax session
# cookie already blocks cross-site POST) and logout (served from raw HTML that
# carries no token). All HTML <form> POSTs remain CSRF-protected. ---
for _ep in ("api_generate", "api_validate", "logout"):
    _vf = app.view_functions.get(_ep)
    if _vf is not None:
        csrf.exempt(_vf)
