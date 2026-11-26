"""Platform admin console."""
from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import admin_required, staff_required
from .core import (REVIEW_DEFAULT_DAYS, audit, current_lang, enforce_rules, month_of, next_months, notify_company,
                   today, valid_credential)
from .db import execute, get_db, query
from .i18n import translate

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _t(key, **kw):
    return translate(key, current_lang(), **kw)


def kpis() -> dict:
    t = today().isoformat()
    month = month_of(today())
    pending = query("SELECT COUNT(*) n FROM credentials WHERE status IN ('pending', 'info_requested')", one=True)["n"]
    pending += query("SELECT COUNT(*) n FROM bookings WHERE creative_status = 'pending' AND status != 'cancelled'",
                     one=True)["n"]
    live = """SELECT COUNT(*) n FROM companies c WHERE c.kind = ? AND c.status = 'active' AND EXISTS
              (SELECT 1 FROM credentials k WHERE k.company_id = c.id AND k.status = 'verified' AND k.next_review >= ?)"""
    paid = query("SELECT COALESCE(SUM(amount_php), 0) s FROM invoices WHERE status = 'paid' AND substr(paid_at, 1, 7) = ?",
                 (month,), one=True)["s"]
    disputes = query("SELECT COUNT(*) n FROM disputes WHERE status = 'open'", one=True)["n"]
    return {"pending": pending, "disputes": disputes, "suppliers": query(live, ("supplier", t), one=True)["n"],
            "operators": query(live, ("operator", t), one=True)["n"], "paid": paid}


@bp.route("/")
@staff_required
def review():
    enforce_rules()
    creds = query(
        """SELECT k.*, c.name, c.kind AS company_kind, c.category, c.country, c.status AS company_status
           FROM credentials k JOIN companies c ON c.id = k.company_id
           WHERE k.status IN ('pending', 'info_requested') ORDER BY k.id""")
    creatives = query(
        """SELECT b.*, c.name, s.kind AS slot_kind FROM bookings b JOIN companies c ON c.id = b.company_id
           JOIN ad_slots s ON s.id = b.slot_id
           WHERE b.creative_status = 'pending' AND b.status != 'cancelled' ORDER BY b.id""")
    default_due = {k: (today() + timedelta(days=d)).isoformat() for k, d in REVIEW_DEFAULT_DAYS.items()}
    return render_template("admin/review.html", creds=creds, creatives=creatives, k=kpis(), default_due=default_due,
                           active="review")


@bp.route("/credential/<int:cred_id>", methods=["POST"])
@staff_required
def decide_credential(cred_id: int):
    cred = query("SELECT * FROM credentials WHERE id = ?", (cred_id,), one=True)
    if cred is None:
        abort(404)
    co = query("SELECT * FROM companies WHERE id = ?", (cred["company_id"],), one=True)
    action = request.form.get("action")
    note = request.form.get("note", "").strip()[:1000]
    db = get_db()
    if action == "approve":
        try:
            due = date.fromisoformat(request.form.get("next_review", ""))
        except ValueError:
            due = today() + timedelta(days=REVIEW_DEFAULT_DAYS[cred["kind"]])
        if due <= today():
            flash(_t("fill_required"), "error")
            return redirect(url_for("admin.review"))
        db.execute("UPDATE credentials SET status='verified', verified_at=?, next_review=?, note=? WHERE id=?",
                   (today().isoformat(), due.isoformat(), note, cred_id))
        if co["status"] in ("pending", "paused", "rejected"):
            db.execute("UPDATE companies SET status = 'active' WHERE id = ?", (co["id"],))
        db.commit()
        notify_company(co["id"], "[HUILIAN] Verified", f"Your {cred['kind']} {cred['number']} is verified. "
                       f"Next review: {due.isoformat()}.")
    elif action in ("reject", "ask"):
        if not note:
            flash(_t("note_required"), "error")
            return redirect(url_for("admin.review"))
        status = "rejected" if action == "reject" else "info_requested"
        db.execute("UPDATE credentials SET status = ?, note = ? WHERE id = ?", (status, note, cred_id))
        if action == "reject" and co["status"] == "pending" and not valid_credential(co["id"]):
            db.execute("UPDATE companies SET status = 'rejected' WHERE id = ?", (co["id"],))
        db.commit()
        subject = "[HUILIAN] Verification rejected" if action == "reject" else "[HUILIAN] More information needed"
        notify_company(co["id"], subject, f"Note from our reviewer: {note}\n\nUpload a new document under Profile > Credentials.")
    else:
        abort(400)
    audit(f"credential_{action}", f"credential:{cred_id}", f"company:{co['id']} {note}")
    flash(_t("done"), "ok")
    return redirect(url_for("admin.review"))


@bp.route("/creative/<int:booking_id>", methods=["POST"])
@staff_required
def decide_creative(booking_id: int):
    b = query("SELECT * FROM bookings WHERE id = ?", (booking_id,), one=True)
    if b is None:
        abort(404)
    action = request.form.get("action")
    note = request.form.get("note", "").strip()[:1000]
    if action == "approve":
        execute("UPDATE bookings SET creative_status = 'approved' WHERE id = ?", (booking_id,))
        notify_company(b["company_id"], "[HUILIAN] Creative approved", f"Your banner for {b['month']} is approved.")
    elif action == "reject":
        if not note:
            flash(_t("note_required"), "error")
            return redirect(url_for("admin.review"))
        execute("UPDATE bookings SET creative_status = 'rejected' WHERE id = ?", (booking_id,))
        notify_company(b["company_id"], "[HUILIAN] Creative rejected", f"Reason: {note}")
    else:
        abort(400)
    audit(f"creative_{action}", f"booking:{booking_id}", note)
    flash(_t("done"), "ok")
    return redirect(url_for("admin.review"))


@bp.route("/calendar")
@staff_required
def calendar():
    months = next_months(6)
    slots = query("SELECT * FROM ad_slots WHERE active = 1 ORDER BY CASE kind WHEN 'spotlight' THEN 0 "
                  "WHEN 'cat_top' THEN 1 ELSE 2 END, category")
    grid = []
    for s in slots:
        cells = []
        for m in months:
            rows = query("""SELECT b.status, c.name FROM bookings b JOIN companies c ON c.id = b.company_id
                            WHERE b.slot_id = ? AND b.month = ? AND b.status != 'cancelled' ORDER BY b.id""",
                         (s["id"], m))
            cells.append({"rows": rows, "free": s["capacity"] - len(rows)})
        grid.append({"slot": s, "cells": cells})
    return render_template("admin/calendar.html", grid=grid, months=months, k=kpis(), active="calendar")


@bp.route("/billing", methods=["GET", "POST"])
@admin_required
def billing():
    if request.method == "POST":
        inv = query("SELECT * FROM invoices WHERE id = ?", (request.form.get("invoice_id", type=int),), one=True)
        if inv is None:
            abort(404)
        action = request.form.get("action")
        db = get_db()
        if action == "paid" and inv["status"] in ("unpaid", "overdue"):
            ref = request.form.get("reference", "").strip()[:200]
            if inv["booking_id"]:
                b = query("SELECT * FROM bookings WHERE id = ?", (inv["booking_id"],), one=True)
                slot = query("SELECT * FROM ad_slots WHERE id = ?", (b["slot_id"],), one=True)
                taken = query("SELECT COUNT(*) n FROM bookings WHERE slot_id=? AND month=? AND status='paid'",
                              (b["slot_id"], b["month"]), one=True)["n"]
                if b["status"] == "cancelled" and taken >= slot["capacity"]:
                    flash(_t("slot_full"), "error")
                    return redirect(url_for("admin.billing"))
                db.execute("UPDATE bookings SET status = 'paid' WHERE id = ?", (inv["booking_id"],))
            db.execute("UPDATE invoices SET status='paid', paid_at=?, reference=? WHERE id=?",
                       (today().isoformat(), ref, inv["id"]))
            db.commit()
            notify_company(inv["company_id"], f"[HUILIAN] Payment received {inv['number']}",
                           "Thank you. Your promotion is confirmed.")
        elif action == "void" and inv["status"] != "paid":
            db.execute("UPDATE invoices SET status = 'void' WHERE id = ?", (inv["id"],))
            if inv["booking_id"]:
                db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (inv["booking_id"],))
            db.commit()
        else:
            abort(400)
        audit(f"invoice_{action}", f"invoice:{inv['id']}", inv["number"])
        flash(_t("done"), "ok")
        return redirect(url_for("admin.billing"))
    enforce_rules()
    invoices = query(
        """SELECT i.*, c.name, b.month, s.kind AS slot_kind, s.category AS slot_category FROM invoices i
           JOIN companies c ON c.id = i.company_id LEFT JOIN bookings b ON b.id = i.booking_id
           LEFT JOIN ad_slots s ON s.id = b.slot_id
           ORDER BY CASE i.status WHEN 'overdue' THEN 0 WHEN 'unpaid' THEN 1 ELSE 2 END, i.id DESC LIMIT 300""")
    return render_template("admin/billing.html", invoices=invoices, k=kpis(), active="billing")


@bp.route("/renewals", methods=["GET", "POST"])
@staff_required
def renewals():
    if request.method == "POST":
        cred = query("SELECT * FROM credentials WHERE id = ?", (request.form.get("credential_id", type=int),), one=True)
        if cred is None:
            abort(404)
        notify_company(cred["company_id"], "[HUILIAN] Please renew your credential",
                       f"Your {cred['kind']} {cred['number']} is due for review on {cred['next_review']}. "
                       "Upload the renewed document under Profile > Credentials to stay listed.")
        execute("UPDATE credentials SET reminded_at = ? WHERE id = ?", (today().isoformat(), cred["id"]))
        audit("renewal_reminder", f"credential:{cred['id']}")
        flash(_t("done"), "ok")
        return redirect(url_for("admin.renewals"))
    enforce_rules()
    horizon = (today() + timedelta(days=45)).isoformat()
    rows = query(
        """SELECT k.*, c.name, c.kind AS company_kind, c.category FROM credentials k
           JOIN companies c ON c.id = k.company_id
           WHERE k.id IN (SELECT MAX(id) FROM credentials WHERE status IN ('verified', 'expired') GROUP BY company_id)
             AND k.next_review <= ? AND c.status != 'removed'
           ORDER BY k.next_review""", (horizon,))
    items = []
    for r in rows:
        delta = (date.fromisoformat(r["next_review"]) - today()).days
        items.append(dict(r, delta=delta))
    return render_template("admin/renewals.html", items=items, k=kpis(), active="renewals")


@bp.route("/companies", methods=["GET", "POST"])
@staff_required
def companies():
    if request.method == "POST":
        if g.user["role"] != "admin":
            abort(403)
        co = query("SELECT * FROM companies WHERE id = ?", (request.form.get("company_id", type=int),), one=True)
        action = request.form.get("action")
        if co is None or co["kind"] == "platform":
            abort(404)
        if action == "pause":
            execute("UPDATE companies SET status = 'paused' WHERE id = ?", (co["id"],))
        elif action == "remove":
            execute("UPDATE companies SET status = 'removed' WHERE id = ?", (co["id"],))
        elif action == "reactivate":
            if not valid_credential(co["id"]):
                flash(_t("account_paused"), "error")
                return redirect(url_for("admin.companies"))
            execute("UPDATE companies SET status = 'active' WHERE id = ?", (co["id"],))
        else:
            abort(400)
        audit(f"company_{action}", f"company:{co['id']}", request.form.get("note", "")[:500])
        flash(_t("done"), "ok")
        return redirect(url_for("admin.companies"))
    rows = query("""SELECT c.*, (SELECT MAX(next_review) FROM credentials k WHERE k.company_id = c.id
                    AND k.status = 'verified') AS next_review,
                    (SELECT COUNT(*) FROM users u WHERE u.company_id = c.id) AS users
                    FROM companies c WHERE c.kind != 'platform' ORDER BY c.created_at DESC""")
    return render_template("admin/companies.html", rows=rows, k=kpis(), active="companies")


@bp.route("/telegram-webhook", methods=["POST"])
@admin_required
def telegram_webhook():
    from flask import current_app
    from .telegram import set_webhook
    cfg = current_app.config
    if not (cfg.get("TELEGRAM_BOT_TOKEN") and cfg.get("TELEGRAM_WEBHOOK_SECRET")):
        flash(_t("tg_not_configured"), "error")
        return redirect(url_for("admin.review"))
    result = set_webhook() or {}
    audit("telegram_webhook", "", str(result)[:300])
    flash(_t("done") if result.get("ok") else f"Telegram: {result.get('description', 'error')}",
          "ok" if result.get("ok") else "error")
    return redirect(url_for("admin.review"))


@bp.route("/audit")
@admin_required
def audit_log():
    rows = query("""SELECT a.*, u.email FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
                    ORDER BY a.id DESC LIMIT 300""")
    return render_template("admin/audit.html", rows=rows, k=kpis(), active="audit")
