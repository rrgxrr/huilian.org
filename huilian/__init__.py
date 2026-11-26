"""HUILIAN 灰链 — B2B iGaming supplier directory (MVP)."""
import os
import secrets
from pathlib import Path

import click
from dotenv import load_dotenv
from flask import Flask, abort, g, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db as dbmod
from .core import current_lang
from .i18n import LANGS, translate


def create_app(test_config: dict | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    instance = Path(app.instance_path)
    instance.mkdir(parents=True, exist_ok=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or "dev-only-change-me",
        DATABASE=os.environ.get("DATABASE") or str(instance / "huilian.sqlite"),
        UPLOAD_DIR=os.environ.get("UPLOAD_DIR") or str(instance / "uploads"),
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        SMTP_HOST=os.environ.get("SMTP_HOST"),
        SMTP_PORT=os.environ.get("SMTP_PORT", "587"),
        SMTP_USER=os.environ.get("SMTP_USER"),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD"),
        MAIL_FROM=os.environ.get("MAIL_FROM", "HUILIAN <no-reply@huilian.ph>"),
        BASE_URL=os.environ.get("BASE_URL", "http://localhost:5000"),
        TELEGRAM_BOT_TOKEN=os.environ.get("TELEGRAM_BOT_TOKEN"),
        TELEGRAM_BOT_USERNAME=os.environ.get("TELEGRAM_BOT_USERNAME"),
        TELEGRAM_WEBHOOK_SECRET=os.environ.get("TELEGRAM_WEBHOOK_SECRET"),
        TODAY=None,
    )
    if test_config:
        app.config.update(test_config)
    if app.config["SECRET_KEY"] == "dev-only-change-me" and not app.debug and not app.testing:
        app.logger.warning("SECRET_KEY is not set. Set it in .env before going live.")

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.teardown_appcontext(dbmod.close_db)
    state = {"enforced_on": None}

    from .auth import bp as auth_bp, load_user
    from .public import bp as public_bp
    from .me import bp as me_bp
    from .admin import bp as admin_bp
    from .deals_views import bp as deals_bp
    from .telegram import bp as tg_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(me_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(deals_bp)
    app.register_blueprint(tg_bp)

    @app.before_request
    def _before():
        # daily rules without a cron job: the first request of each day runs them
        from .core import enforce_rules, today
        if not app.testing and state["enforced_on"] != today():
            state["enforced_on"] = today()
            try:
                enforce_rules()
            except Exception:  # noqa: BLE001
                app.logger.exception("enforce_rules failed")
        load_user()
        if request.method == "POST" and not app.config.get("WTF_CSRF_DISABLED") and request.path != "/tg/webhook":
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
                abort(400, description="csrf")

    def csrf_token() -> str:
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(32)
        return session["_csrf"]

    @app.context_processor
    def _ctx():
        lang = current_lang()
        return {
            "t": lambda key, **kw: translate(key, lang, **kw),
            "lang": lang,
            "langs": LANGS,
            "csrf_token": csrf_token,
            "user": g.get("user"),
            "company": g.get("company"),
            "loc": lambda en, zh: (zh or en) if lang == "zh" else (en or zh),
        }

    @app.errorhandler(400)
    def _bad(e):
        key = "csrf_failed" if getattr(e, "description", "") == "csrf" else "fill_required"
        return render_template("error.html", message_key=key), 400

    @app.errorhandler(403)
    def _forbidden(_e):
        return render_template("error.html", message_key="forbidden"), 403

    @app.errorhandler(404)
    def _missing(_e):
        return render_template("error.html", message_key="not_found"), 404

    @app.cli.command("init-db")
    def init_db_cmd():
        """Create the tables."""
        dbmod.init_db()
        click.echo("Database ready.")

    @app.cli.command("seed")
    def seed_cmd():
        """Load ad slots, an admin account and demo companies."""
        from .seed import seed
        seed(demo=True)
        click.echo("Seeded. Admin: admin@huilian.local / ChangeMe12345 (change it).")

    @app.cli.command("seed-prod")
    @click.option("--email", required=True)
    @click.option("--password", required=True)
    def seed_prod_cmd(email, password):
        """Load ad slots and one admin account, no demo data."""
        from .seed import seed
        seed(demo=False, admin_email=email, admin_password=password)
        click.echo(f"Ready. Admin: {email}")

    @app.route("/healthz")
    def _health():
        dbmod.query("SELECT 1")
        return {"ok": True}

    @app.cli.command("bootstrap")
    def bootstrap_cmd():
        """Create tables and slots; create the admin from ADMIN_EMAIL / ADMIN_PASSWORD if set. Safe to run on every start."""
        from .seed import seed
        email, password = os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD")
        if email and password and len(password) >= 10:
            seed(demo=False, admin_email=email.strip().lower(), admin_password=password)
            click.echo(f"Ready. Admin: {email}")
        else:
            dbmod.init_db()
            click.echo("Tables ready. Set ADMIN_EMAIL and ADMIN_PASSWORD (10+ chars) to create the admin.")

    @app.cli.command("enforce")
    def enforce_cmd():
        """Expire credentials, pause companies, release unpaid holds. Run daily."""
        from .core import enforce_rules
        click.echo(enforce_rules())

    @app.cli.command("tg-set-webhook")
    def tg_webhook_cmd():
        """Point Telegram at BASE_URL/tg/webhook (needs TELEGRAM_* settings)."""
        from .telegram import set_webhook
        click.echo(set_webhook())

    return app
