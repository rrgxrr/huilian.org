"""Accounts: register, sign in, sign out, access checks."""
from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from .core import (CATEGORIES, CREDENTIALS_FOR, DOC_EXT, NEEDS_AML, audit, current_lang, notify, save_upload, today)
from .db import execute, get_db, query
from .i18n import translate

bp = Blueprint("auth", __name__)


def load_user() -> None:
    g.user = None
    g.company = None
    uid = session.get("uid")
    if uid:
        g.user = query("SELECT * FROM users WHERE id = ?", (uid,), one=True)
        if g.user:
            g.company = query("SELECT * FROM companies WHERE id = ?", (g.user["company_id"],), one=True)
        else:
            session.pop("uid", None)


def _t(key: str, **kw) -> str:
    return translate(key, current_lang(), **kw)


def safe_next(target: str | None) -> str | None:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash(_t("login_needed"), "info")
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if g.user["role"] not in ("admin", "reviewer"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@bp.route("/register", methods=["GET", "POST"])
def register():
    role = request.values.get("role", "supplier")
    if role not in ("operator", "supplier"):
        role = "supplier"
    nxt = safe_next(request.values.get("next"))
    form = request.form
    if request.method == "POST":
        name = form.get("company_name", "").strip()
        country = form.get("country", "").strip()
        category = form.get("category") if role == "supplier" else None
        cred_kind = "pagcor_licence" if role == "operator" else form.get("credential_type", "")
        number = form.get("credential_number", "").strip()
        person = form.get("name", "").strip()
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")

        error = None
        if not all([name, country, number, person, email, password]) or "@" not in email:
            error = "fill_required"
        elif role == "supplier" and (category not in CATEGORIES or cred_kind not in CREDENTIALS_FOR[category]):
            error = "fill_required"
        elif len(password) < 10:
            error = "password_short"
        elif role == "supplier" and not form.get("pledge"):
            error = "pledge_required"
        elif query("SELECT 1 FROM users WHERE email = ?", (email,), one=True):
            error = "email_taken"

        cert_path = aml_path = None
        if not error:
            cert_path = save_upload(request.files.get("credential_file"), "credentials", DOC_EXT)
            if not cert_path:
                error = "bad_file"
        if not error and role == "supplier" and category in NEEDS_AML:
            aml_path = save_upload(request.files.get("aml_file"), "credentials", DOC_EXT)
            if not aml_path:
                error = "aml_required"

        if error:
            flash(_t(error), "error")
            return render_template("register.html", role=role, form=form, next=nxt,
                                   credentials_for=CREDENTIALS_FOR), 400

        db = get_db()
        cur = db.execute(
            "INSERT INTO companies (kind, name, country, category, status, pledge_signed_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (role, name, country, category, today().isoformat() if role == "supplier" else None))
        company_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO users (company_id, email, password_hash, name, lang) VALUES (?, ?, ?, ?, ?)",
            (company_id, email, generate_password_hash(password), person, current_lang()))
        user_id = cur.lastrowid
        db.execute(
            "INSERT INTO credentials (company_id, kind, number, file_path, aml_policy_path) VALUES (?, ?, ?, ?, ?)",
            (company_id, cred_kind, number, cert_path, aml_path))
        if role == "supplier":
            db.execute("INSERT INTO supplier_profiles (company_id) VALUES (?)", (company_id,))
        db.commit()
        session.clear()
        session["uid"] = user_id
        session["lang"] = current_lang()
        load_user()
        audit("register", f"company:{company_id}", f"{role} {name}")
        for staff in query("SELECT email FROM users WHERE role IN ('admin', 'reviewer')"):
            notify(staff["email"], f"[HUILIAN] New {role} application: {name}",
                   f"{name} ({country}) submitted {cred_kind} {number}. Review it in the admin console.")
        flash(_t("registered_ok"), "ok")
        return redirect(nxt or url_for("me.profile"))

    return render_template("register.html", role=role, form=form, next=nxt, credentials_for=CREDENTIALS_FOR)


@bp.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if not user or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash(_t("bad_login"), "error")
            return render_template("login.html", next=nxt, email=email), 400
        lang = session.get("lang") or user["lang"]
        session.clear()
        session["uid"] = user["id"]
        session["lang"] = lang
        flash(translate("signed_in", lang), "ok")
        if user["role"] in ("admin", "reviewer") and not nxt:
            return redirect(url_for("admin.review"))
        return redirect(nxt or url_for("me.profile"))
    return render_template("login.html", next=nxt, email="")


@bp.route("/logout", methods=["POST"])
def logout():
    lang = current_lang()
    session.clear()
    session["lang"] = lang
    flash(translate("signed_out", lang), "ok")
    return redirect(url_for("public.home"))


def _reset_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="password-reset")


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if user:
            # the hash fragment makes the link single-use: it stops matching once the password changes
            token = _reset_serializer().dumps({"uid": user["id"], "h": user["password_hash"][-12:]})
            link = f"{current_app.config['BASE_URL']}{url_for('auth.reset', token=token)}"
            notify(email, "[HUILIAN] Reset your password", f"Set a new password within 1 hour: {link}")
            audit("password_reset_request", f"user:{user['id']}")
        flash(_t("reset_sent"), "ok")  # same answer whether or not the email exists
        return redirect(url_for("auth.login"))
    return render_template("forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token: str):
    try:
        data = _reset_serializer().loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        flash(_t("reset_invalid"), "error")
        return redirect(url_for("auth.forgot"))
    user = query("SELECT * FROM users WHERE id = ?", (data.get("uid"),), one=True)
    if user is None or user["password_hash"][-12:] != data.get("h"):
        flash(_t("reset_invalid"), "error")
        return redirect(url_for("auth.forgot"))
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 10:
            flash(_t("password_short"), "error")
            return render_template("reset.html", token=token), 400
        execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user["id"]))
        g.user = user
        audit("password_reset", f"user:{user['id']}")
        flash(_t("reset_done"), "ok")
        return redirect(url_for("auth.login"))
    return render_template("reset.html", token=token)
