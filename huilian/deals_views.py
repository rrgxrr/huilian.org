"""Web pages for guarantee deals, Telegram linking, and admin arbitration."""
import hashlib
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import deals as D
from .auth import login_required, staff_required
from .core import DOC_EXT, IMG_EXT, audit, current_lang, save_upload
from .db import execute, query
from .i18n import translate
from .telegram import deep_link, link_for, make_link_code

bp = Blueprint("deals", __name__)


def _t(key, **kw):
    return translate(key, current_lang(), **kw)


def _staff() -> bool:
    return g.user is not None and g.user["role"] in ("admin", "reviewer")


@bp.route("/deals")
@login_required
def index():
    if g.company["kind"] == "platform":
        return redirect(url_for("deals.disputes"))
    return render_template("deals.html", rows=D.deals_for(g.company["id"]))


@bp.route("/deals/new", methods=["GET", "POST"])
@login_required
def new():
    enq = query("SELECT * FROM enquiries WHERE id = ?", (request.values.get("enquiry", type=int),), one=True)
    if enq is None or g.company["id"] != enq["supplier_id"]:
        abort(404)
    operator = query("SELECT * FROM companies WHERE id = ?", (enq["operator_id"],), one=True)
    form = request.form
    if request.method == "POST":
        rows = list(zip(form.getlist("ms_title"), form.getlist("ms_amount"), form.getlist("ms_due")))
        try:
            deal_id = D.propose(enq["id"], g.company, g.user, form.get("title", ""), form.get("scope", ""),
                                form.get("currency", ""), form.get("pay_terms", ""), rows)
        except D.DealError as e:
            flash(_t(e.key), "error")
            return render_template("deal_new.html", enq=enq, operator=operator, form=form,
                                   currencies=D.CURRENCIES, max_ms=D.MAX_MILESTONES), 400
        flash(_t("deal_proposed_ok"), "ok")
        return redirect(url_for("deals.view", deal_id=deal_id))
    return render_template("deal_new.html", enq=enq, operator=operator, form=form,
                           currencies=D.CURRENCIES, max_ms=D.MAX_MILESTONES)


@bp.route("/deals/<int:deal_id>")
@login_required
def view(deal_id: int):
    deal = D.get_deal(deal_id)
    my_side = D.side(deal, g.company)
    if deal is None or (my_side is None and not _staff()):
        abort(404)
    operator = query("SELECT * FROM companies WHERE id = ?", (deal["operator_id"],), one=True)
    supplier = query("SELECT * FROM companies WHERE id = ?", (deal["supplier_id"],), one=True)
    files = query("""SELECT f.*, c.name FROM deal_files f JOIN companies c ON c.id = f.company_id
                     WHERE f.deal_id = ? ORDER BY f.id""", (deal_id,))
    disputes = query("SELECT * FROM disputes WHERE deal_id = ? ORDER BY id", (deal_id,))
    return render_template("deal.html", deal=deal, ms=D.milestones(deal_id), events=D.events(deal_id),
                           side=my_side, operator=operator, supplier=supplier, files=files, disputes=disputes,
                           chain_ok=D.verify_chain(deal_id), staff=_staff())


@bp.route("/deals/<int:deal_id>/act", methods=["POST"])
@login_required
def act(deal_id: int):
    f = request.form
    action = f.get("action", "")
    ms_id = f.get("milestone_id", type=int)
    c, u = g.company, g.user
    try:
        if action == "accept":
            D.accept(deal_id, c, u)
        elif action == "decline":
            D.decline(deal_id, c, u, f.get("note", ""))
        elif action == "withdraw":
            D.withdraw(deal_id, c, u)
        elif action == "deliver":
            D.mark_delivered(deal_id, ms_id, c, u, f.get("note", ""))
        elif action == "accept_delivery":
            D.accept_delivery(deal_id, ms_id, c, u)
        elif action == "paid":
            D.mark_paid(deal_id, ms_id, c, u, f.get("reference", ""))
        elif action == "confirm_paid":
            D.confirm_paid(deal_id, ms_id, c, u)
        elif action == "dispute":
            D.dispute(deal_id, c, u, f.get("reason", ""), ms_id or None)
        elif action == "evidence":
            up = request.files.get("file")
            data = up.read() if up and up.filename else b""
            if up and up.filename:
                up.stream.seek(0)
            path = save_upload(up, "evidence", DOC_EXT | IMG_EXT)
            if not path:
                raise D.DealError("bad_file")
            D.add_evidence(deal_id, c, u, path, hashlib.sha256(data).hexdigest(), f.get("note", ""))
        else:
            abort(400)
    except D.DealError as e:
        if e.key == "not_found":
            abort(404)
        flash(_t(e.key), "error")
        return redirect(url_for("deals.view", deal_id=deal_id))
    flash(_t("done"), "ok")
    return redirect(url_for("deals.view", deal_id=deal_id))


@bp.route("/deals/<int:deal_id>/file/<int:file_id>")
@login_required
def evidence_file(deal_id: int, file_id: int):
    deal = D.get_deal(deal_id)
    if deal is None or (D.side(deal, g.company) is None and not _staff()):
        abort(404)
    row = query("SELECT * FROM deal_files WHERE id = ? AND deal_id = ?", (file_id, deal_id), one=True)
    if row is None:
        abort(404)
    from .public import _send_upload
    return _send_upload(row["path"])


# ---------- Telegram linking ----------

@bp.route("/me/telegram", methods=["GET", "POST"])
@login_required
def telegram():
    code = link = None
    if request.method == "POST":
        if request.form.get("action") == "unlink":
            execute("DELETE FROM telegram_links WHERE user_id = ?", (g.user["id"],))
            flash(_t("tg_unlinked"), "ok")
            return redirect(url_for("deals.telegram"))
        code = make_link_code(g.user["id"])
        link = deep_link(code)
    return render_template("telegram.html", linked=link_for(g.user["id"]), code=code, link=link,
                           bot=current_app.config.get("TELEGRAM_BOT_USERNAME"))


# ---------- admin arbitration ----------

@bp.route("/admin/disputes", methods=["GET", "POST"])
@staff_required
def disputes():
    from .admin import kpis
    if request.method == "POST":
        try:
            D.resolve(request.form.get("dispute_id", type=int), g.user, request.form.get("outcome", ""),
                      request.form.get("decision", ""))
        except D.DealError as e:
            flash(_t(e.key), "error")
            return redirect(url_for("deals.disputes"))
        audit("dispute_resolve", f"dispute:{request.form.get('dispute_id')}", request.form.get("outcome", ""))
        flash(_t("done"), "ok")
        return redirect(url_for("deals.disputes"))
    rows = query("""SELECT x.*, d.title, d.currency, o.name AS operator_name, s.name AS supplier_name,
                           c.name AS opened_name FROM disputes x JOIN deals d ON d.id = x.deal_id
                    JOIN companies o ON o.id = d.operator_id JOIN companies s ON s.id = d.supplier_id
                    JOIN companies c ON c.id = x.opened_by
                    ORDER BY x.status = 'resolved', x.id DESC LIMIT 200""")
    return render_template("admin/disputes.html", rows=rows, k=kpis(), active="disputes")
