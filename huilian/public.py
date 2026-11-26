"""Public pages: directory, supplier profile, enquiry, language, public images."""
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_from_directory,
                   session, url_for)

from .auth import safe_next
from .core import (CATEGORIES, audit, current_lang, is_verified, log_event, month_of, notify_company, sponsored_ids,
                   spotlight, today, valid_credential, visible_suppliers)
from .db import get_db, query
from .i18n import LANGS, translate

bp = Blueprint("public", __name__)

SERVICES = {
    "wl": ["svc_wl_platform", "svc_wl_sportsbook", "svc_wl_casino", "svc_other"],
    "pay": ["svc_payin", "svc_payout", "svc_payin_payout", "svc_other"],
    "fx": ["svc_fx_conversion", "svc_fx_settlement", "svc_other"],
    "mkt": ["svc_mkt_creative", "svc_mkt_media", "svc_mkt_crm", "svc_other"],
}
SERVICE_LABELS = {
    "svc_wl_platform": ("Full platform (casino + sportsbook)", "一体化平台（娱乐场 + 体育）"),
    "svc_wl_sportsbook": ("Sportsbook", "体育平台"),
    "svc_wl_casino": ("Casino / game aggregation", "娱乐场 / 游戏聚合"),
    "svc_payin": ("Pay-in", "代收（Pay-in）"),
    "svc_payout": ("Pay-out", "代付（Pay-out）"),
    "svc_payin_payout": ("Pay-in and pay-out", "代收 + 代付"),
    "svc_fx_conversion": ("FX conversion", "外币兑换"),
    "svc_fx_settlement": ("Cross-border settlement", "跨境结算"),
    "svc_mkt_creative": ("Compliant creative", "合规素材制作"),
    "svc_mkt_media": ("Media buying", "媒介投放"),
    "svc_mkt_crm": ("CRM and retention", "CRM 与留存"),
    "svc_other": ("Other", "其他"),
}
VOLUMES = ["vol_1", "vol_2", "vol_3", "vol_4"]


def service_label(key: str, lang: str) -> str:
    en, zh = SERVICE_LABELS.get(key, (key, key))
    return zh if lang == "zh" else en


def parse_services(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in (":", "："):
            if sep in line:
                head, body = line.split(sep, 1)
                out.append({"h": head.strip(), "b": body.strip()})
                break
        else:
            out.append({"h": line, "b": ""})
    return out


@bp.route("/")
def home():
    tab = request.args.get("tab", "all")
    tab = tab if tab in ("all", "feat") else "all"
    cat = request.args.get("cat", "all")
    cat = cat if cat in CATEGORIES else "all"
    month = month_of(today())
    sponsored = sponsored_ids(month)

    pool = list(visible_suppliers())
    if tab == "feat":
        pool = [c for c in pool if c["id"] in sponsored]
    else:
        pool.sort(key=lambda c: c["id"] not in sponsored)
    counts = {"all": len(pool)}
    for c in pool:
        counts[c["category"]] = counts.get(c["category"], 0) + 1
    items = [c for c in pool if cat == "all" or c["category"] == cat]

    spot = spotlight(month) if tab == "feat" else None
    shown_ads = {c["id"] for c in items if c["id"] in sponsored}
    if spot:
        shown_ads.add(spot["company_id"])
    for cid in shown_ads:
        log_event(cid, "impression")
    get_db().commit()

    return render_template("home.html", tab=tab, cat=cat, items=items, counts=counts, sponsored=sponsored,
                           spot=spot, categories=CATEGORIES)


@bp.route("/s/<int:company_id>")
def supplier(company_id: int):
    co = query("SELECT * FROM companies WHERE id = ? AND kind = 'supplier'", (company_id,), one=True)
    if co is None:
        abort(404)
    owner = g.company is not None and g.company["id"] == co["id"]
    staff = g.user is not None and g.user["role"] in ("admin", "reviewer")
    cred = valid_credential(co["id"])
    if not (co["status"] == "active" and cred) and not (owner or staff):
        abort(404)
    prof = query("SELECT * FROM supplier_profiles WHERE company_id = ?", (co["id"],), one=True)
    if not owner and not staff:
        log_event(co["id"], "view")
        get_db().commit()
    lang = current_lang()
    services_text = (prof["services_zh"] or prof["services_en"]) if lang == "zh" else (prof["services_en"] or prof["services_zh"])
    sponsored = co["id"] in sponsored_ids(month_of(today()))
    from .deals import track_record
    return render_template("supplier.html", co=co, prof=prof, cred=cred, record=track_record(co["id"]), services=parse_services(services_text),
                           sponsored=sponsored, preview=not (co["status"] == "active" and cred))


@bp.route("/s/<int:company_id>/enquire", methods=["GET", "POST"])
def enquire(company_id: int):
    co = query("SELECT * FROM companies WHERE id = ? AND kind = 'supplier' AND status = 'active'",
               (company_id,), one=True)
    if co is None or not valid_credential(co["id"]):
        abort(404)
    lang = current_lang()
    here = url_for("public.enquire", company_id=company_id)

    if g.user is None:
        return render_template("enquire.html", co=co, state="anon", here=here)
    if g.company["kind"] != "operator":
        return render_template("enquire.html", co=co, state="supplier", here=here)
    if not is_verified(g.company):
        return render_template("enquire.html", co=co, state="pending", here=here)

    services = SERVICES[co["category"]]
    if request.method == "POST":
        service = request.form.get("service", "")
        volume = request.form.get("volume", "")
        details = request.form.get("details", "").strip()
        if service not in services or volume not in VOLUMES or not details:
            flash(translate("fill_required", lang), "error")
        elif not request.form.get("consent"):
            flash(translate("consent_required", lang), "error")
        else:
            db = get_db()
            cur = db.execute(
                "INSERT INTO enquiries (operator_id, supplier_id, service, volume, consent) VALUES (?, ?, ?, ?, 1)",
                (g.company["id"], co["id"], service, volume))
            enq_id = cur.lastrowid
            db.execute("INSERT INTO messages (enquiry_id, sender_company_id, body) VALUES (?, ?, ?)",
                       (enq_id, g.company["id"], details[:5000]))
            db.commit()
            audit("enquiry_create", f"enquiry:{enq_id}", f"to company:{co['id']}")
            notify_company(co["id"], f"[HUILIAN] New enquiry from {g.company['name']}",
                           f"{g.company['name']} sent you an enquiry. Reply in Profile: "
                           f"{current_app.config['BASE_URL']}/me/enquiries/{enq_id}")
            flash(translate("enquiry_sent", lang), "ok")
            return redirect(url_for("me.thread", enquiry_id=enq_id))

    options = [{"key": s, "label": service_label(s, lang)} for s in services]
    return render_template("enquire.html", co=co, state="form", here=here, options=options, volumes=VOLUMES,
                           form=request.form)


@bp.route("/lang")
@bp.route("/lang/<code>")
def set_lang(code: str = ""):
    code = code or request.args.get("code", "")
    if code in LANGS:
        session["lang"] = code
        if g.user:
            get_db().execute("UPDATE users SET lang = ? WHERE id = ?", (code, g.user["id"]))
            get_db().commit()
    return redirect(safe_next(request.args.get("next")) or url_for("public.home"))


def _send_upload(rel: str):
    base = Path(current_app.config["UPLOAD_DIR"]).resolve()
    path = (base / rel).resolve()
    if base not in path.parents or not path.is_file():
        abort(404)
    return send_from_directory(base, str(path.relative_to(base)))


@bp.route("/logo/<int:company_id>")
def logo(company_id: int):
    co = query("SELECT logo_path FROM companies WHERE id = ?", (company_id,), one=True)
    if co is None or not co["logo_path"]:
        abort(404)
    return _send_upload(co["logo_path"])


@bp.route("/banner/<int:booking_id>")
def banner(booking_id: int):
    b = query("SELECT * FROM bookings WHERE id = ?", (booking_id,), one=True)
    if b is None or not b["creative_path"]:
        abort(404)
    public = b["status"] == "paid" and b["creative_status"] == "approved"
    owner = g.company is not None and g.company["id"] == b["company_id"]
    staff = g.user is not None and g.user["role"] in ("admin", "reviewer")
    if not (public or owner or staff):
        abort(404)
    return _send_upload(b["creative_path"])


@bp.route("/files/<path:rel>")
def private_file(rel: str):
    """Credential documents: staff only, or the company that uploaded them."""
    if g.user is None:
        abort(404)
    staff = g.user["role"] in ("admin", "reviewer")
    owns = query("SELECT 1 FROM credentials WHERE company_id = ? AND (file_path = ? OR aml_policy_path = ?)",
                 (g.user["company_id"], rel, rel), one=True)
    if not (staff or owns):
        abort(404)
    return _send_upload(rel)
