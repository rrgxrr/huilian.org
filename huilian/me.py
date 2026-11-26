"""Signed-in area: Profile, enquiries, supplier profile, promotions, billing, credentials."""
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import login_required
from .core import (CREDENTIALS_FOR, DOC_EXT, HOLD_DAYS, IMG_EXT, NEEDS_AML, audit, current_lang, latest_credential,
                   next_invoice_number, next_months, notify, notify_company, save_upload, slot_taken, today,
                   valid_credential)
from .db import execute, get_db, query
from .i18n import translate
from .public import VOLUMES, service_label

bp = Blueprint("me", __name__, url_prefix="/me")


def _t(key, **kw):
    return translate(key, current_lang(), **kw)


def supplier_only():
    if g.company is None or g.company["kind"] != "supplier":
        abort(403)


def account_state(company) -> str:
    """verified | pending | info | rejected | paused | removed"""
    if company["kind"] == "platform":
        return "verified"
    if company["status"] == "active" and valid_credential(company["id"]):
        return "verified"
    if company["status"] in ("paused", "removed"):
        return company["status"]
    cred = latest_credential(company["id"])
    if cred and cred["status"] == "info_requested":
        return "info"
    if company["status"] == "rejected" or (cred and cred["status"] == "rejected"):
        return "rejected"
    return "pending"


def _enquiry_rows(company):
    if company["kind"] == "operator":
        rows = query(
            """SELECT e.*, c.name AS other_name, c.id AS other_id, c.category AS other_category,
                      (SELECT body FROM messages m WHERE m.enquiry_id = e.id ORDER BY m.id DESC LIMIT 1) AS preview
               FROM enquiries e JOIN companies c ON c.id = e.supplier_id
               WHERE e.operator_id = ? ORDER BY e.updated_at DESC, e.id DESC""", (company["id"],))
    else:
        rows = query(
            """SELECT e.*, c.name AS other_name, c.id AS other_id, NULL AS other_category,
                      (SELECT body FROM messages m WHERE m.enquiry_id = e.id ORDER BY m.id DESC LIMIT 1) AS preview
               FROM enquiries e JOIN companies c ON c.id = e.operator_id
               WHERE e.supplier_id = ? ORDER BY e.updated_at DESC, e.id DESC""", (company["id"],))
    return rows


def status_label(enq, company) -> tuple[str, str]:
    """(translation key, css class)"""
    if enq["status"] == "closed":
        return "status_closed", "st-closed"
    if company["kind"] == "operator":
        return ("status_replied", "st-ok") if enq["status"] == "replied" else ("status_awaiting", "st-warn")
    return ("status_new", "st-new") if enq["status"] == "new" else ("status_replied", "st-ok")


@bp.route("/")
@login_required
def profile():
    co = g.company
    if co["kind"] == "platform":
        return redirect(url_for("admin.review"))
    state = account_state(co)
    cred = valid_credential(co["id"]) or latest_credential(co["id"])
    rows = _enquiry_rows(co)
    enquiries = [dict(r, label=status_label(r, co)) for r in rows]
    stats = None
    if co["kind"] == "supplier":
        since = (today() - timedelta(days=30)).isoformat()
        total = query("SELECT COUNT(*) n FROM enquiries WHERE supplier_id = ? AND created_at >= ?",
                      (co["id"], since), one=True)["n"]
        answered = query(
            """SELECT COUNT(*) n FROM enquiries e WHERE e.supplier_id = ? AND e.created_at >= ?
               AND EXISTS (SELECT 1 FROM messages m WHERE m.enquiry_id = e.id AND m.sender_company_id = e.supplier_id)""",
            (co["id"], since), one=True)["n"]
        views = query("SELECT COUNT(*) n FROM events WHERE company_id = ? AND kind = 'view' AND created_at >= ?",
                      (co["id"], since), one=True)["n"]
        impr = query("SELECT COUNT(*) n FROM events WHERE company_id = ? AND kind = 'impression' AND created_at >= ?",
                     (co["id"], since), one=True)["n"]
        stats = {"enquiries": total, "reply": f"{round(100 * answered / total)}%" if total else "—",
                 "views": views, "impr": impr}
    new_count = sum(1 for e in enquiries if e["status"] == "new")
    return render_template("me.html", state=state, cred=cred, enquiries=enquiries, stats=stats, new_count=new_count)


@bp.route("/enquiries/<int:enquiry_id>", methods=["GET", "POST"])
@login_required
def thread(enquiry_id: int):
    enq = query("SELECT * FROM enquiries WHERE id = ?", (enquiry_id,), one=True)
    if enq is None or g.company["id"] not in (enq["operator_id"], enq["supplier_id"]):
        abort(404)
    me_is_operator = g.company["id"] == enq["operator_id"]
    other_id = enq["supplier_id"] if me_is_operator else enq["operator_id"]
    other = query("SELECT * FROM companies WHERE id = ?", (other_id,), one=True)

    if request.method == "POST":
        if request.form.get("action") == "close":
            execute("UPDATE enquiries SET status = 'closed', updated_at = datetime('now') WHERE id = ?", (enquiry_id,))
            audit("enquiry_close", f"enquiry:{enquiry_id}")
            return redirect(url_for("me.thread", enquiry_id=enquiry_id))
        body = request.form.get("body", "").strip()
        if enq["status"] == "closed":
            flash(_t("enquiry_closed"), "error")
        elif body:
            db = get_db()
            db.execute("INSERT INTO messages (enquiry_id, sender_company_id, body) VALUES (?, ?, ?)",
                       (enquiry_id, g.company["id"], body[:5000]))
            new_status = enq["status"] if me_is_operator else "replied"
            db.execute("UPDATE enquiries SET status = ?, updated_at = datetime('now') WHERE id = ?",
                       (new_status, enquiry_id))
            db.commit()
            notify_company(other_id, f"[HUILIAN] New message from {g.company['name']}",
                           f"Read and reply: {current_app.config['BASE_URL']}/me/enquiries/{enquiry_id}")
        return redirect(url_for("me.thread", enquiry_id=enquiry_id))

    messages = query("SELECT * FROM messages WHERE enquiry_id = ? ORDER BY id", (enquiry_id,))
    contact = None
    if not me_is_operator and enq["consent"]:
        op_cred = valid_credential(other["id"]) or latest_credential(other["id"])
        contact = {"licence": op_cred["number"] if op_cred else "",
                   "emails": [u["email"] for u in query("SELECT email FROM users WHERE company_id = ?", (other["id"],))]}
    lang = current_lang()
    vol_key = enq["volume"] if enq["volume"] in VOLUMES else None
    linked_deals = query("SELECT * FROM deals WHERE enquiry_id = ? ORDER BY id DESC", (enquiry_id,))
    return render_template("thread.html", enq=enq, linked_deals=linked_deals, other=other, messages=messages, me_is_operator=me_is_operator,
                           contact=contact, label=status_label(enq, g.company),
                           service=service_label(enq["service"], lang), volume_key=vol_key)


@bp.route("/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    supplier_only()
    prof = query("SELECT * FROM supplier_profiles WHERE company_id = ?", (g.company["id"],), one=True)
    if request.method == "POST":
        f = request.form
        fields = {k: f.get(k, "").strip()[:4000] for k in
                  ("about_en", "about_zh", "services_en", "services_zh", "markets", "currencies", "website")}
        if fields["website"] and not fields["website"].startswith(("http://", "https://")):
            fields["website"] = "https://" + fields["website"]
        execute("""UPDATE supplier_profiles SET about_en=?, about_zh=?, services_en=?, services_zh=?, markets=?,
                   currencies=?, website=? WHERE company_id=?""", (*fields.values(), g.company["id"]))
        logo = request.files.get("logo")
        if logo and logo.filename:
            path = save_upload(logo, "logos", IMG_EXT)
            if path:
                execute("UPDATE companies SET logo_path = ? WHERE id = ?", (path, g.company["id"]))
            else:
                flash(_t("bad_file"), "error")
        audit("profile_edit", f"company:{g.company['id']}")
        flash(_t("profile_saved"), "ok")
        return redirect(url_for("me.edit_profile"))
    return render_template("edit_profile.html", prof=prof)


@bp.route("/promotions", methods=["GET", "POST"])
@login_required
def promotions():
    supplier_only()
    co = g.company
    slots = query("SELECT * FROM ad_slots WHERE active = 1 ORDER BY CASE kind WHEN 'spotlight' THEN 0 "
                  "WHEN 'cat_top' THEN 1 ELSE 2 END, category")
    # suppliers only see their own category's top slot
    slots = [s for s in slots if s["kind"] != "cat_top" or s["category"] == co["category"]]
    months = next_months(6)

    if request.method == "POST":
        if account_state(co) != "verified":
            flash(_t("must_be_active"), "error")
            return redirect(url_for("me.promotions"))
        slot = query("SELECT * FROM ad_slots WHERE id = ? AND active = 1", (request.form.get("slot_id", type=int),),
                     one=True)
        month = request.form.get("month", "")
        method = request.form.get("method", "bank")
        if (slot is None or slot["id"] not in {s["id"] for s in slots} or month not in months
                or method not in ("bank", "rrpay")):
            flash(_t("fill_required"), "error")
            return redirect(url_for("me.promotions"))
        if slot_taken(slot["id"], month) >= slot["capacity"]:
            flash(_t("slot_full"), "error")
            return redirect(url_for("me.promotions"))
        creative = None
        headline = ""
        if slot["kind"] == "spotlight":
            headline = request.form.get("headline", "").strip()[:120]
            creative = save_upload(request.files.get("creative"), "creatives", IMG_EXT)
            if not headline or not creative:
                flash(_t("fill_required"), "error")
                return redirect(url_for("me.promotions"))
        db = get_db()
        cur = db.execute(
            "INSERT INTO bookings (slot_id, company_id, month, headline, creative_path, creative_status) VALUES (?, ?, ?, ?, ?, ?)",
            (slot["id"], co["id"], month, headline, creative, "pending" if creative else "none"))
        booking_id = cur.lastrowid
        number = next_invoice_number()
        due = (today() + timedelta(days=HOLD_DAYS)).isoformat()
        db.execute("INSERT INTO invoices (number, company_id, booking_id, amount_php, method, due_date) VALUES (?, ?, ?, ?, ?, ?)",
                   (number, co["id"], booking_id, slot["price_php"], method, due))
        db.commit()
        audit("booking_create", f"booking:{booking_id}", f"{slot['code']} {month} {number}")
        notify_company(co["id"], f"[HUILIAN] Invoice {number}",
                       f"Amount: PHP {slot['price_php']:,}. Due {due}. Reference: {number}.")
        for staff in query("SELECT email FROM users WHERE role IN ('admin', 'reviewer')"):
            notify(staff["email"], f"[HUILIAN] New booking {number}", f"{co['name']} booked {slot['code']} for {month}.")
        flash(_t("booked_ok", number=number), "ok")
        return redirect(url_for("me.billing"))

    grid = []
    for s in slots:
        cells = []
        for m in months:
            mine = query("SELECT 1 FROM bookings WHERE slot_id=? AND month=? AND company_id=? AND status!='cancelled'",
                         (s["id"], m, co["id"]), one=True)
            taken = slot_taken(s["id"], m)
            state = "mine" if mine else ("sold" if taken >= s["capacity"] else "open")
            cells.append({"month": m, "state": state})
        grid.append({"slot": s, "cells": cells})
    bookings = query(
        """SELECT b.*, s.kind AS slot_kind, s.category AS slot_category, i.number, i.status AS inv_status
           FROM bookings b JOIN ad_slots s ON s.id = b.slot_id LEFT JOIN invoices i ON i.booking_id = b.id
           WHERE b.company_id = ? ORDER BY b.month DESC, b.id DESC""", (co["id"],))
    return render_template("promotions.html", grid=grid, months=months, slots=slots, bookings=bookings,
                           can_book=account_state(co) == "verified")


@bp.route("/billing")
@login_required
def billing():
    supplier_only()
    invoices = query(
        """SELECT i.*, b.month, s.kind AS slot_kind, s.category AS slot_category FROM invoices i
           LEFT JOIN bookings b ON b.id = i.booking_id LEFT JOIN ad_slots s ON s.id = b.slot_id
           WHERE i.company_id = ? ORDER BY i.id DESC""", (g.company["id"],))
    return render_template("billing.html", invoices=invoices)


@bp.route("/credentials", methods=["GET", "POST"])
@login_required
def credentials():
    co = g.company
    if co["kind"] == "platform":
        abort(403)
    allowed = CREDENTIALS_FOR["operator" if co["kind"] == "operator" else co["category"]]
    if request.method == "POST":
        kind = request.form.get("credential_type", allowed[0])
        number = request.form.get("credential_number", "").strip()
        if kind not in allowed or not number:
            flash(_t("fill_required"), "error")
            return redirect(url_for("me.credentials"))
        cert = save_upload(request.files.get("credential_file"), "credentials", DOC_EXT)
        aml = None
        if co["kind"] == "supplier" and co["category"] in NEEDS_AML:
            aml = save_upload(request.files.get("aml_file"), "credentials", DOC_EXT)
            if not aml:
                flash(_t("aml_required"), "error")
                return redirect(url_for("me.credentials"))
        if not cert:
            flash(_t("bad_file"), "error")
            return redirect(url_for("me.credentials"))
        cid = execute("INSERT INTO credentials (company_id, kind, number, file_path, aml_policy_path) VALUES (?, ?, ?, ?, ?)",
                      (co["id"], kind, number, cert, aml))
        audit("credential_upload", f"credential:{cid}", f"company:{co['id']}")
        for staff in query("SELECT email FROM users WHERE role IN ('admin', 'reviewer')"):
            notify(staff["email"], f"[HUILIAN] Credential to review: {co['name']}", f"{kind} {number}")
        flash(_t("cred_uploaded"), "ok")
        return redirect(url_for("me.credentials"))
    creds = query("SELECT * FROM credentials WHERE company_id = ? ORDER BY id DESC", (co["id"],))
    return render_template("credentials.html", creds=creds, allowed=allowed,
                           needs_aml=co["kind"] == "supplier" and co["category"] in NEEDS_AML)
