"""Guarantee (担保) deals: agreement, milestones, confirmations, disputes.

HUILIAN only records and arbitrates. Money moves directly between the two
companies; the platform never receives, holds or releases funds.

Every state change goes through one of the action functions below, so the
web pages and the Telegram bot share the same rules and the same log.
"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import current_app

from .core import is_verified, notify_company
from .db import get_db, query

CURRENCIES = ("PHP", "USD", "USDT")
MAX_MILESTONES = 6
GENESIS = "0" * 64


class DealError(Exception):
    """An action that is not allowed. `key` is an i18n key."""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


# ---------- reads ----------

def get_deal(deal_id: int):
    return query("SELECT * FROM deals WHERE id = ?", (deal_id,), one=True)


def milestones(deal_id: int):
    return query("SELECT * FROM deal_milestones WHERE deal_id = ? ORDER BY seq", (deal_id,))


def events(deal_id: int):
    return query("""SELECT e.*, c.name AS company_name FROM deal_events e
                    LEFT JOIN companies c ON c.id = e.company_id WHERE e.deal_id = ? ORDER BY e.id""", (deal_id,))


def open_dispute(deal_id: int):
    return query("SELECT * FROM disputes WHERE deal_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                 (deal_id,), one=True)


def side(deal, company) -> str | None:
    if company is None or deal is None:
        return None
    if company["id"] == deal["operator_id"]:
        return "operator"
    if company["id"] == deal["supplier_id"]:
        return "supplier"
    return None


def deals_for(company_id: int):
    return query("""SELECT d.*, o.name AS operator_name, s.name AS supplier_name FROM deals d
                    JOIN companies o ON o.id = d.operator_id JOIN companies s ON s.id = d.supplier_id
                    WHERE d.operator_id = ? OR d.supplier_id = ? ORDER BY d.updated_at DESC, d.id DESC""",
                 (company_id, company_id))


def track_record(supplier_id: int) -> dict:
    row = query("""SELECT
        SUM(status = 'completed') AS completed,
        SUM(status IN ('active', 'disputed')) AS active,
        (SELECT COUNT(*) FROM disputes x JOIN deals y ON y.id = x.deal_id WHERE y.supplier_id = ?) AS disputes,
        (SELECT COUNT(*) FROM disputes x JOIN deals y ON y.id = x.deal_id
           WHERE y.supplier_id = ? AND x.outcome = 'operator') AS lost
        FROM deals WHERE supplier_id = ?""", (supplier_id, supplier_id, supplier_id), one=True)
    return {k: (row[k] or 0) for k in ("completed", "active", "disputes", "lost")}


def terms_of(deal, ms) -> dict:
    return {"deal": deal["id"], "operator": deal["operator_id"], "supplier": deal["supplier_id"],
            "title": deal["title"], "scope": deal["scope"], "currency": deal["currency"], "pay_terms": deal["pay_terms"],
            "milestones": [{"seq": m["seq"], "title": m["title"], "amount": m["amount"], "due": m["due_date"] or ""}
                           for m in ms]}


def hash_terms(terms: dict) -> str:
    blob = json.dumps(terms, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------- tamper-evident log ----------

def _event_hash(prev: str, deal_id: int, kind: str, company_id, user_id, detail: str, created_at: str) -> str:
    raw = f"{prev}|{deal_id}|{kind}|{company_id or ''}|{user_id or ''}|{detail}|{created_at}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def append_event(deal_id: int, kind: str, detail: str = "", company_id=None, user_id=None, via: str = "web") -> None:
    db = get_db()
    last = db.execute("SELECT hash FROM deal_events WHERE deal_id = ? ORDER BY id DESC LIMIT 1", (deal_id,)).fetchone()
    prev = last[0] if last else GENESIS
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    h = _event_hash(prev, deal_id, kind, company_id, user_id, detail, created_at)
    db.execute("""INSERT INTO deal_events (deal_id, company_id, user_id, via, kind, detail, created_at, prev_hash, hash)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (deal_id, company_id, user_id, via, kind, detail, created_at, prev, h))
    db.execute("UPDATE deals SET updated_at = datetime('now') WHERE id = ?", (deal_id,))


def verify_chain(deal_id: int) -> bool:
    prev = GENESIS
    for e in query("SELECT * FROM deal_events WHERE deal_id = ? ORDER BY id", (deal_id,)):
        if e["prev_hash"] != prev:
            return False
        if e["hash"] != _event_hash(prev, deal_id, e["kind"], e["company_id"], e["user_id"], e["detail"], e["created_at"]):
            return False
        prev = e["hash"]
    return True


# ---------- helpers ----------

def _amount(text: str) -> str:
    try:
        value = Decimal(str(text).replace(",", "").strip())
    except InvalidOperation:
        raise DealError("deal_bad_amount")
    if value <= 0 or value > Decimal("1000000000000") or value.as_tuple().exponent < -2:
        raise DealError("deal_bad_amount")
    return f"{value.quantize(Decimal('0.01')):f}"


def _require(deal, company, role: str) -> None:
    if deal is None:
        raise DealError("not_found")
    if side(deal, company) != role:
        raise DealError("forbidden")


def _status(deal, *allowed) -> None:
    if deal["status"] not in allowed:
        raise DealError("deal_wrong_state")


def _milestone(deal, milestone_id: int):
    m = query("SELECT * FROM deal_milestones WHERE id = ? AND deal_id = ?", (milestone_id, deal["id"]), one=True)
    if m is None:
        raise DealError("not_found")
    return m


def _after(deal_id: int, kind: str, actor_company_id: int, detail: str = "") -> None:
    """Commit, tell the other side (email + Telegram), and complete the deal if every milestone is done."""
    db = get_db()
    deal = get_deal(deal_id)
    if deal["status"] == "active":
        ms = milestones(deal_id)
        if ms and all(m["delivery"] == "accepted" and m["payment"] == "confirmed" for m in ms):
            db.execute("UPDATE deals SET status = 'completed', completed_at = datetime('now') WHERE id = ?", (deal_id,))
            append_event(deal_id, "completed")
            kind_done = True
        else:
            kind_done = False
    else:
        kind_done = False
    db.commit()
    other = deal["supplier_id"] if actor_company_id == deal["operator_id"] else deal["operator_id"]
    base = current_app.config["BASE_URL"]
    notify_company(other, f"[HUILIAN] Deal #{deal_id}: {kind.replace('_', ' ')}",
                   f"{deal['title']}\n{detail}\n\n{base}/deals/{deal_id}".strip())
    from .telegram import push_deal_event  # local import: telegram imports this module
    push_deal_event(deal_id, kind, actor_company_id, detail)
    if kind_done:
        notify_company(deal["operator_id"], f"[HUILIAN] Deal #{deal_id} completed", f"{base}/deals/{deal_id}")
        notify_company(deal["supplier_id"], f"[HUILIAN] Deal #{deal_id} completed", f"{base}/deals/{deal_id}")
        push_deal_event(deal_id, "completed", None, "")


# ---------- actions ----------

def propose(enquiry_id: int, company, user, title: str, scope: str, currency: str, pay_terms: str,
            rows: list[tuple[str, str, str]], via: str = "web") -> int:
    enq = query("SELECT * FROM enquiries WHERE id = ?", (enquiry_id,), one=True)
    if enq is None or company is None or company["id"] != enq["supplier_id"]:
        raise DealError("forbidden")
    if enq["status"] == "closed":
        raise DealError("enquiry_closed")
    operator = query("SELECT * FROM companies WHERE id = ?", (enq["operator_id"],), one=True)
    if not (is_verified(company) and is_verified(operator)):
        raise DealError("deal_both_verified")
    title, scope, pay_terms = title.strip()[:200], scope.strip()[:4000], pay_terms.strip()[:1000]
    if not title or not scope or currency not in CURRENCIES:
        raise DealError("fill_required")
    clean = []
    for t, a, due in rows:
        if not (t or "").strip() and not (a or "").strip():
            continue
        if not (t or "").strip():
            raise DealError("fill_required")
        if due:
            try:
                datetime.strptime(due, "%Y-%m-%d")
            except ValueError:
                raise DealError("fill_required")
        clean.append((t.strip()[:200], _amount(a), due or None))
    if not 1 <= len(clean) <= MAX_MILESTONES:
        raise DealError("deal_need_milestone")
    db = get_db()
    deal_id = db.execute(
        """INSERT INTO deals (enquiry_id, operator_id, supplier_id, title, scope, currency, pay_terms)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (enquiry_id, enq["operator_id"], company["id"], title, scope, currency, pay_terms)).lastrowid
    for seq, (t, a, due) in enumerate(clean, start=1):
        db.execute("INSERT INTO deal_milestones (deal_id, seq, title, amount, due_date) VALUES (?, ?, ?, ?, ?)",
                   (deal_id, seq, t, a, due))
    deal = get_deal(deal_id)
    h = hash_terms(terms_of(deal, milestones(deal_id)))
    db.execute("UPDATE deals SET terms_hash = ? WHERE id = ?", (h, deal_id))
    append_event(deal_id, "proposed", f"terms {h[:16]}", company["id"], user["id"], via)
    _after(deal_id, "proposed", company["id"], title)
    return deal_id


def accept(deal_id: int, company, user, via: str = "web") -> None:
    deal = get_deal(deal_id)
    _require(deal, company, "operator")
    _status(deal, "proposed")
    if not is_verified(company):
        raise DealError("deal_both_verified")
    h = hash_terms(terms_of(deal, milestones(deal_id)))
    if h != deal["terms_hash"]:
        raise DealError("deal_terms_changed")
    get_db().execute("UPDATE deals SET status = 'active', accepted_at = datetime('now') WHERE id = ?", (deal_id,))
    append_event(deal_id, "accepted", f"terms {h[:16]}", company["id"], user["id"], via)
    _after(deal_id, "accepted", company["id"])


def decline(deal_id: int, company, user, note: str = "", via: str = "web") -> None:
    deal = get_deal(deal_id)
    _require(deal, company, "operator")
    _status(deal, "proposed")
    get_db().execute("UPDATE deals SET status = 'declined' WHERE id = ?", (deal_id,))
    append_event(deal_id, "declined", note.strip()[:1000], company["id"], user["id"], via)
    _after(deal_id, "declined", company["id"], note)


def withdraw(deal_id: int, company, user, via: str = "web") -> None:
    deal = get_deal(deal_id)
    _require(deal, company, "supplier")
    _status(deal, "proposed")
    get_db().execute("UPDATE deals SET status = 'cancelled' WHERE id = ?", (deal_id,))
    append_event(deal_id, "withdrawn", "", company["id"], user["id"], via)
    _after(deal_id, "withdrawn", company["id"])


def mark_delivered(deal_id: int, milestone_id: int, company, user, note: str = "", via: str = "web") -> None:
    deal = get_deal(deal_id)
    _require(deal, company, "supplier")
    _status(deal, "active")
    m = _milestone(deal, milestone_id)
    if m["delivery"] != "open":
        raise DealError("deal_wrong_state")
    get_db().execute("UPDATE deal_milestones SET delivery = 'delivered' WHERE id = ?", (m["id"],))
    detail = f"#{m['seq']} {m['title']}" + (f" — {note.strip()[:1000]}" if note.strip() else "")
    append_event(deal_id, "delivered", detail, company["id"], user["id"], via)
    _after(deal_id, "delivered", company["id"], detail)


def accept_delivery(deal_id: int, milestone_id: int, company, user, via: str = "web") -> None:
    deal = get_deal(deal_id)
    _require(deal, company, "operator")
    _status(deal, "active")
    m = _milestone(deal, milestone_id)
    if m["delivery"] != "delivered":
        raise DealError("deal_wrong_state")
    get_db().execute("UPDATE deal_milestones SET delivery = 'accepted' WHERE id = ?", (m["id"],))
    detail = f"#{m['seq']} {m['title']}"
    append_event(deal_id, "delivery_accepted", detail, company["id"], user["id"], via)
    _after(deal_id, "delivery_accepted", company["id"], detail)


def mark_paid(deal_id: int, milestone_id: int, company, user, reference: str, via: str = "web") -> None:
    deal = get_deal(deal_id)
    _require(deal, company, "operator")
    _status(deal, "active")
    m = _milestone(deal, milestone_id)
    reference = (reference or "").strip()[:300]
    if m["payment"] != "unpaid":
        raise DealError("deal_wrong_state")
    if not reference:
        raise DealError("deal_need_reference")
    get_db().execute("UPDATE deal_milestones SET payment = 'claimed', pay_reference = ? WHERE id = ?",
                     (reference, m["id"]))
    detail = f"#{m['seq']} {m['amount']} {deal['currency']} ref {reference}"
    append_event(deal_id, "paid_claimed", detail, company["id"], user["id"], via)
    _after(deal_id, "paid_claimed", company["id"], detail)


def confirm_paid(deal_id: int, milestone_id: int, company, user, via: str = "web") -> None:
    deal = get_deal(deal_id)
    _require(deal, company, "supplier")
    _status(deal, "active")
    m = _milestone(deal, milestone_id)
    if m["payment"] != "claimed":
        raise DealError("deal_wrong_state")
    get_db().execute("UPDATE deal_milestones SET payment = 'confirmed' WHERE id = ?", (m["id"],))
    detail = f"#{m['seq']} {m['amount']} {deal['currency']}"
    append_event(deal_id, "paid_confirmed", detail, company["id"], user["id"], via)
    _after(deal_id, "paid_confirmed", company["id"], detail)


def dispute(deal_id: int, company, user, reason: str, milestone_id: int | None = None, via: str = "web") -> int:
    deal = get_deal(deal_id)
    if side(deal, company) is None:
        raise DealError("forbidden")
    _status(deal, "active")
    reason = (reason or "").strip()[:4000]
    if not reason:
        raise DealError("deal_need_reason")
    if milestone_id:
        _milestone(deal, milestone_id)
    db = get_db()
    did = db.execute("INSERT INTO disputes (deal_id, milestone_id, opened_by, reason) VALUES (?, ?, ?, ?)",
                     (deal_id, milestone_id, company["id"], reason)).lastrowid
    db.execute("UPDATE deals SET status = 'disputed' WHERE id = ?", (deal_id,))
    append_event(deal_id, "disputed", reason, company["id"], user["id"], via)
    _after(deal_id, "disputed", company["id"], reason)
    from .core import notify
    for staff in query("SELECT email FROM users WHERE role IN ('admin', 'reviewer')"):
        notify(staff["email"], f"[HUILIAN] Dispute on deal #{deal_id}", reason)
    return did


def add_evidence(deal_id: int, company, user, path: str, sha: str, note: str, via: str = "web") -> None:
    deal = get_deal(deal_id)
    if side(deal, company) is None:
        raise DealError("forbidden")
    _status(deal, "active", "disputed")
    get_db().execute("INSERT INTO deal_files (deal_id, company_id, path, sha256, note) VALUES (?, ?, ?, ?, ?)",
                     (deal_id, company["id"], path, sha, note.strip()[:500]))
    append_event(deal_id, "evidence", f"sha256 {sha[:16]} {note.strip()[:200]}", company["id"], user["id"], via)
    _after(deal_id, "evidence", company["id"], note)


def resolve(dispute_id: int, admin_user, outcome: str, decision: str) -> None:
    d = query("SELECT * FROM disputes WHERE id = ? AND status = 'open'", (dispute_id,), one=True)
    if d is None:
        raise DealError("not_found")
    decision = (decision or "").strip()[:4000]
    if outcome not in ("operator", "supplier", "split", "cancel") or not decision:
        raise DealError("fill_required")
    db = get_db()
    db.execute("""UPDATE disputes SET status = 'resolved', outcome = ?, decision = ?, resolved_by = ?,
                  resolved_at = datetime('now') WHERE id = ?""", (outcome, decision, admin_user["id"], dispute_id))
    db.execute("UPDATE deals SET status = 'resolved' WHERE id = ?", (d["deal_id"],))
    append_event(d["deal_id"], "resolved", f"{outcome}: {decision}", None, admin_user["id"], "admin")
    db.commit()
    deal = get_deal(d["deal_id"])
    base = current_app.config["BASE_URL"]
    for cid in (deal["operator_id"], deal["supplier_id"]):
        notify_company(cid, f"[HUILIAN] Dispute decision on deal #{deal['id']}", f"{decision}\n\n{base}/deals/{deal['id']}")
    from .telegram import push_deal_event
    push_deal_event(deal["id"], "resolved", None, decision)
