"""Session login shared with Main_Website (same cookie name + secret)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sys
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

from flask import jsonify, redirect, render_template, request, session, url_for

if TYPE_CHECKING:
    from flask import Flask, Response

_log = logging.getLogger("AI_Trade_Program.auth")


def _env_strip(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def website_username() -> str:
    return _env_strip("WEBSITE-USERNAME") or _env_strip("WEBSITE_USERNAME")


def website_password() -> str:
    return _env_strip("WEBSITE-PASSWORD") or _env_strip("WEBSITE_PASSWORD")


def session_secret_key() -> str:
    raw = _env_strip("WEBSITE_SESSION_SECRET") or _env_strip("FLASK_SECRET_KEY")
    if raw:
        return raw
    _log.warning(
        "WEBSITE_SESSION_SECRET not set; using a dev-only default (set a random secret in .env for production).",
    )
    m = hashlib.sha256()
    m.update(b"trading-website-dev-key-v1|")
    m.update(website_password().encode("utf-8"))
    m.update(b"|")
    m.update(website_username().encode("utf-8"))
    return m.hexdigest()


def session_cookie_secure() -> bool:
    return _env_strip("WEBSITE_SESSION_SECURE").lower() in ("1", "true", "yes", "on")


def public_path_prefix() -> str:
    raw = (_env_strip("AI_TRADE_PUBLIC_PATH") or "/ai-trade/").strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    if not raw.endswith("/"):
        raw += "/"
    return raw


def validate_website_auth_config() -> None:
    user = website_username()
    pw = website_password()
    if not user or not pw:
        _log.critical(
            "Set WEBSITE-USERNAME and WEBSITE-PASSWORD in Trading Platform/.env "
            "(or WEBSITE_USERNAME / WEBSITE_PASSWORD).",
        )
        sys.exit(1)


def configure_app(app: Flask) -> None:
    app.config.update(
        SECRET_KEY=session_secret_key(),
        SESSION_COOKIE_NAME="pulse_trading_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=session_cookie_secure(),
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    )


def session_logged_in() -> bool:
    return session.get("_auth") is True


def _login_csrf_issue() -> str:
    tok = secrets.token_urlsafe(32)
    session["_login_csrf"] = tok
    return tok


def _safe_redirect_path(raw: str | None) -> str:
    s = (raw or "").strip()
    if s.startswith("/") and not s.startswith("//"):
        return s
    return ""


def _is_public_site_request() -> bool:
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()
    return host.endswith("opticalmedia.co.uk")


def _login_next_path() -> str:
    if _is_public_site_request():
        prefix = public_path_prefix()
        path = request.path
        if path in ("/", "") or path.startswith(prefix.rstrip("/")):
            return prefix
        return prefix.rstrip("/") + path
    if request.query_string:
        q = request.query_string.decode()
        return f"{request.path}?{q}" if q else request.path
    return request.path or "/"


def register_auth_routes(app: Flask) -> None:
    @app.before_request
    def _require_login() -> Response | tuple[Response, int] | None:
        if request.endpoint in ("health", "login", "logout"):
            return None
        if session_logged_in():
            return None
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="Unauthorized"), 401
        if _is_public_site_request():
            nxt = quote(_login_next_path(), safe="/%?=&")
            return redirect(f"/login?next={nxt}")
        return redirect(url_for("login", next=_login_next_path()))

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response | str | tuple[str, int]:
        if request.method == "GET" and session_logged_in():
            nxt = _safe_redirect_path(request.args.get("next")) or "/"
            return redirect(nxt)

        want_user = website_username().encode("utf-8")
        want_pw = website_password().encode("utf-8")

        if request.method == "POST":
            got_csrf = (request.form.get("csrf_token") or "").strip()
            exp_csrf = session.get("_login_csrf") or ""
            if len(got_csrf) != len(exp_csrf) or not hmac.compare_digest(
                got_csrf.encode("utf-8"),
                exp_csrf.encode("utf-8"),
            ):
                return redirect(url_for("login", next=_safe_redirect_path(request.form.get("next")) or None))
            got_user = (request.form.get("username") or "").encode("utf-8")
            got_pw = (request.form.get("password") or "").encode("utf-8")
            ok_user = len(want_user) == len(got_user) and hmac.compare_digest(want_user, got_user)
            ok_pw = len(want_pw) == len(got_pw) and hmac.compare_digest(want_pw, got_pw)
            if ok_user and ok_pw:
                session.clear()
                session["_auth"] = True
                session.permanent = True
                nxt = _safe_redirect_path(request.form.get("next") or request.args.get("next"))
                if nxt:
                    return redirect(nxt)
                return redirect("/")
            err = "Invalid username or password."
            csrf_token = _login_csrf_issue()
            next_url = _safe_redirect_path(request.form.get("next") or request.args.get("next"))
            return (
                render_template(
                    "login.html",
                    error=err,
                    csrf_token=csrf_token,
                    next_url=next_url,
                ),
                401,
            )

        csrf_token = _login_csrf_issue()
        next_url = _safe_redirect_path(request.args.get("next"))
        return render_template(
            "login.html",
            error=None,
            csrf_token=csrf_token,
            next_url=next_url,
        )

    @app.route("/logout", methods=["GET", "POST"])
    def logout() -> Response:
        session.clear()
        if _is_public_site_request():
            return redirect("/login")
        return redirect(url_for("login"))
