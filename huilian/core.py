"""Business rules shared by the views."""
import smtplib
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import current_app, g, session
from werkzeug.utils import secure_filename

from .db import execute, get_db, query

TZ = ZoneInfo("Asia/Manila")

CATEGORIES = ("wl", "pay", "fx", "mkt")
# Which credential each account type may submit.
CREDENTIALS_FOR = {
    "operator": ("pagcor_licence",),
    "wl": ("pagcor_accreditation", "business_reg"),
    "pay": ("bsp",),
    "fx": ("bsp",),
    "mkt": ("business_reg",),
}
NEEDS_AML = ("pay", "fx")
DOC_EXT = {".pdf", ".png", ".jpg", ".jpeg"}
IMG_EXT = {".png", ".jpg", ".jpeg"}
MAX_UPLOAD = 10 * 1024 * 1024
HOLD_DAYS = 7
REVIEW_DEFAULT_DAYS = {"pagcor_licence": 30, "pagcor_accreditation": 365, "bsp": 365, "business_reg": 365}


# ---------- time ----------

def today() -> date:
    override = current_app.config.get("TODAY")
    if override:
        return date.fromisoformat(override)
    return datetime.now(TZ).date()


def month_of(d: date) -> str:
    return d.strftime("%Y-%m")


def next_months(n: int = 6) -> list[str]:
    d = today().replace(day=1)
    out = []
    for _ in range(n):
        out.append(month_of(d))
        d = (d + timedelta(days=32)).replace(day=1)
    return out


# ---------- audit + mail ----------

def audit(action: str, target: str = "", detail: str = "") -> None:
    user = getattr(g, "user", None)
    execute(
        "INSERT INTO audit_log (user_id, action, target, detail) VALUES (?, ?, ?, ?)",
        (user["id"] if user else None, action, target, detail),
    )


def notify(to_email: str, subject: str, body: str) -> None:
    """Queue an email; send it right away when SMTP is configured."""
    outbox_id = execute("INSERT INTO outbox (to_email, subject, body) VALUES (?, ?, ?)", (to_email, subject, body))
    cfg = current_app.config
    if not cfg.get("SMTP_HOST"):
        current_app.logger.info("MAIL (not sent, no SMTP) to=%s subject=%s", to_email, subject)
        return
    msg = EmailMessage()
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], int(cfg.get("SMTP_PORT", 587)), timeout=10) as smtp:
            smtp.starttls()
            if cfg.get("SMTP_USER"):
                smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            smtp.send_message(msg)
        execute("UPDATE outbox SET sent = 1 WHERE id = ?", (outbox_id,))
    except Exception:  # noqa: BLE001 - mail must never break a request
        current_app.logger.exception("Mail send failed to %s", to_email)


def company_emails(company_id: int) -> list[str]:
    return [r["email"] for r in query("SELECT email FROM users WHERE company_id = ?", (company_id,))]


def notify_company(company_id: int, subject: str, body: str) -> None:
    for email in company_emails(company_id):
        notify(email, subject, body)


# ---------- uploads ----------

def _looks_like(head: bytes, ext: str) -> bool:
    if ext == ".pdf":
        return head.startswith(b"%PDF")
    if ext == ".png":
        return head.startswith(b"\x89PNG")
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8")
    return False


def save_upload(file, subdir: str, allowed: set[str]) -> str | None:
    """Save an uploaded file under UPLOAD_DIR/subdir. Returns the relative path, or None if invalid."""
    if not file or not file.filename:
        return None
    ext = Path(secure_filename(file.filename)).suffix.lower()
    if ext not in allowed:
        return None
    data = file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD or not _looks_like(data[:8], ext):
        return None
    rel = f"{subdir}/{uuid.uuid4().hex}{ext}"
    dest = Path(current_app.config["UPLOAD_DIR"]) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return rel


# ---------- credentials + visibility ----------

def valid_credential(company_id: int):
    return query(
        """SELECT * FROM credentials WHERE company_id = ? AND status = 'verified' AND next_review >= ?
           ORDER BY next_review DESC LIMIT 1""",
        (company_id, today().isoformat()), one=True)


def latest_credential(company_id: int):
    return query("SELECT * FROM credentials WHERE company_id = ? ORDER BY id DESC LIMIT 1", (company_id,), one=True)


def is_verified(company) -> bool:
    return company is not None and company["status"] == "active" and valid_credential(company["id"]) is not None


def visible_suppliers(category: str | None = None):
    sql = """SELECT c.* FROM companies c
             WHERE c.kind = 'supplier' AND c.status = 'active'
               AND EXISTS (SELECT 1 FROM credentials k WHERE k.company_id = c.id
                           AND k.status = 'verified' AND k.next_review >= ?)"""
    args: list = [today().isoformat()]
    if category:
        sql += " AND c.category = ?"
        args.append(category)
    return query(sql + " ORDER BY c.name COLLATE NOCASE", args)


def paid_bookings(month: str):
    return query(
        """SELECT b.*, s.kind AS slot_kind, s.category AS slot_category FROM bookings b
           JOIN ad_slots s ON s.id = b.slot_id
           WHERE b.month = ? AND b.status = 'paid'""", (month,))


def sponsored_ids(month: str) -> set[int]:
    return {b["company_id"] for b in paid_bookings(month) if b["slot_kind"] in ("cat_top", "spotlight")}


def spotlight(month: str):
    return query(
        """SELECT b.*, c.name, c.logo_path, c.category FROM bookings b
           JOIN ad_slots s ON s.id = b.slot_id JOIN companies c ON c.id = b.company_id
           WHERE b.month = ? AND b.status = 'paid' AND s.kind = 'spotlight'
             AND b.creative_status = 'approved' AND c.status = 'active'
           ORDER BY b.id LIMIT 1""", (month,), one=True)


def slot_taken(slot_id: int, month: str) -> int:
    row = query("SELECT COUNT(*) AS n FROM bookings WHERE slot_id = ? AND month = ? AND status != 'cancelled'",
                (slot_id, month), one=True)
    return row["n"]


def next_invoice_number() -> str:
    prefix = "HL-" + today().strftime("%Y%m") + "-"
    row = query("SELECT COUNT(*) AS n FROM invoices WHERE number LIKE ?", (prefix + "%",), one=True)
    return f"{prefix}{row['n'] + 1:04d}"


def log_event(company_id: int, kind: str) -> None:
    get_db().execute("INSERT INTO events (company_id, kind) VALUES (?, ?)", (company_id, kind))


# ---------- scheduled rules (run on admin load and via `flask enforce`) ----------

def enforce_rules() -> dict:
    """Expire lapsed credentials, pause companies without one, release unpaid holds."""
    t = today().isoformat()
    db = get_db()
    expired = db.execute(
        "UPDATE credentials SET status = 'expired' WHERE status = 'verified' AND next_review < ?", (t,)).rowcount
    to_pause = query(
        """SELECT id, name FROM companies c WHERE c.status = 'active' AND c.kind != 'platform'
           AND NOT EXISTS (SELECT 1 FROM credentials k WHERE k.company_id = c.id
                           AND k.status = 'verified' AND k.next_review >= ?)""", (t,))
    for c in to_pause:
        db.execute("UPDATE companies SET status = 'paused' WHERE id = ?", (c["id"],))
        db.execute("INSERT INTO audit_log (action, target, detail) VALUES ('auto_pause', ?, 'credential expired')",
                   (f"company:{c['id']}",))
    overdue = query("SELECT id, booking_id FROM invoices WHERE status = 'unpaid' AND due_date < ?", (t,))
    for inv in overdue:
        db.execute("UPDATE invoices SET status = 'overdue' WHERE id = ?", (inv["id"],))
        if inv["booking_id"]:
            db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'unpaid'",
                       (inv["booking_id"],))
    db.commit()
    return {"expired": expired, "paused": len(to_pause), "overdue": len(overdue)}


def current_lang() -> str:
    return session.get("lang") or "en"
