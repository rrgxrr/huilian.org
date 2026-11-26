"""HUILIAN guarantee bot for Telegram (担保机器人).

Uses the Bot API over HTTPS with the standard library only. Telegram calls
POST /tg/webhook; we answer commands and inline-button taps, and push every
deal event to the linked users of the other party and to bound groups.

One-tap buttons cover confirmations only (accept deal, mark delivered,
accept delivery, confirm payment received). Anything that needs typed
input or a file (payment reference, dispute, evidence) opens the web page.
"""
import json
import secrets
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, request

from . import deals as D
from .db import execute, get_db, query
from .i18n import translate

bp = Blueprint("tg", __name__, url_prefix="/tg")

CODE_MINUTES = 15


# ---------- Bot API ----------

def api(method: str, payload: dict) -> dict | None:
    app = current_app
    token = app.config.get("TELEGRAM_BOT_TOKEN")
    if app.testing or not token:
        app.extensions.setdefault("tg_sent", []).append((method, payload))
        return None
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - Telegram must never break a request
        app.logger.exception("Telegram %s failed", method)
        return None


def send(chat_id: int, text: str, buttons: list | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    api("sendMessage", payload)


def _btn(label: str, data: str | None = None, url: str | None = None) -> dict:
    return {"text": label, "url": url} if url else {"text": label, "callback_data": data}


# ---------- linking ----------

def make_link_code(user_id: int) -> str:
    code = secrets.token_hex(4).upper()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=CODE_MINUTES)).isoformat(timespec="seconds")
    execute("DELETE FROM telegram_codes WHERE user_id = ?", (user_id,))
    execute("INSERT INTO telegram_codes (code, user_id, expires_at) VALUES (?, ?, ?)", (code, user_id, expires))
    return code


def deep_link(code: str) -> str | None:
    name = current_app.config.get("TELEGRAM_BOT_USERNAME")
    return f"https://t.me/{name}?start={code}" if name else None


def link_for(user_id: int):
    return query("SELECT * FROM telegram_links WHERE user_id = ?", (user_id,), one=True)


def _user_by_chat(chat_id: int):
    return query("""SELECT u.* FROM telegram_links l JOIN users u ON u.id = l.user_id WHERE l.chat_id = ?""",
                 (chat_id,), one=True)


def _company(user):
    return query("SELECT * FROM companies WHERE id = ?", (user["company_id"],), one=True) if user else None


# ---------- deal card ----------

def _t(key: str, lang: str, **kw) -> str:
    return translate(key, lang, **kw)


def deal_card(deal, viewer_company, lang: str) -> tuple[str, list]:
    base = current_app.config["BASE_URL"]
    ms = D.milestones(deal["id"])
    other_id = deal["supplier_id"] if viewer_company and viewer_company["id"] == deal["operator_id"] else deal["operator_id"]
    other = query("SELECT name FROM companies WHERE id = ?", (other_id,), one=True)
    lines = [f"#{deal['id']} {deal['title']}",
             f"{_t('deal_with', lang)}: {other['name']}",
             f"{_t('status', lang)}: {_t('ds_' + deal['status'], lang)}", ""]
    for m in ms:
        lines.append(f"{m['seq']}. {m['title']} — {m['amount']} {deal['currency']}")
        lines.append(f"   {_t('md_' + m['delivery'], lang)} · {_t('mp_' + m['payment'], lang)}")
    lines += ["", _t("deal_no_funds", lang)]
    my_side = D.side(deal, viewer_company)
    rows: list = []
    if deal["status"] == "proposed" and my_side == "operator":
        rows.append([_btn("✅ " + _t("deal_accept", lang), f"acc:{deal['id']}:0")])
    if deal["status"] == "active":
        for m in ms:
            if my_side == "supplier" and m["delivery"] == "open":
                rows.append([_btn(f"📦 {_t('deal_mark_delivered', lang)} #{m['seq']}", f"dlv:{deal['id']}:{m['id']}")])
            if my_side == "operator" and m["delivery"] == "delivered":
                rows.append([_btn(f"✅ {_t('deal_accept_delivery', lang)} #{m['seq']}", f"dok:{deal['id']}:{m['id']}")])
            if my_side == "supplier" and m["payment"] == "claimed":
                rows.append([_btn(f"💰 {_t('deal_confirm_paid', lang)} #{m['seq']}", f"pok:{deal['id']}:{m['id']}")])
    rows.append([_btn("🔗 " + _t("deal_open_web", lang), url=f"{base}/deals/{deal['id']}")])
    return "\n".join(lines), rows


def push_deal_event(deal_id: int, kind: str, actor_company_id, detail: str) -> None:
    deal = D.get_deal(deal_id)
    if deal is None:
        return
    targets = [deal["operator_id"], deal["supplier_id"]]
    if actor_company_id in targets:
        targets.remove(actor_company_id)
    for cid in targets:
        company = query("SELECT * FROM companies WHERE id = ?", (cid,), one=True)
        for row in query("""SELECT l.chat_id, u.lang FROM telegram_links l JOIN users u ON u.id = l.user_id
                            WHERE u.company_id = ?""", (cid,)):
            text, buttons = deal_card(deal, company, row["lang"])
            head = f"🔔 {_t('ev_' + kind, row['lang'])}" + (f"\n{detail}" if detail else "")
            send(row["chat_id"], f"{head}\n\n{text}", buttons)
    for g in query("SELECT chat_id FROM telegram_groups WHERE deal_id = ?", (deal_id,)):
        send(g["chat_id"], f"🔔 #{deal_id} {_t('ev_' + kind, 'zh')} / {_t('ev_' + kind, 'en')}" +
             (f"\n{detail}" if detail else ""))


# ---------- webhook ----------

def _command(text: str) -> tuple[str, list[str]]:
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", []
    return parts[0][1:].split("@")[0].lower(), parts[1:]


def _help(lang: str) -> str:
    return _t("tg_help", lang)


def _handle_message(msg: dict) -> None:
    chat = msg.get("chat", {})
    chat_id, chat_type = chat.get("id"), chat.get("type")
    from_id = msg.get("from", {}).get("id")
    cmd, args = _command(msg.get("text", ""))
    if not cmd or chat_id is None:
        return
    user = _user_by_chat(from_id) if from_id else None
    lang = user["lang"] if user else "zh"

    if chat_type == "private":
        if cmd == "start" and args:
            row = query("SELECT * FROM telegram_codes WHERE code = ?", (args[0].upper(),), one=True)
            if row is None or row["expires_at"] < datetime.now(timezone.utc).isoformat(timespec="seconds"):
                send(chat_id, _t("tg_code_bad", "zh") + "\n" + _t("tg_code_bad", "en"))
                return
            db = get_db()
            db.execute("DELETE FROM telegram_links WHERE chat_id = ? OR user_id = ?", (chat_id, row["user_id"]))
            db.execute("INSERT INTO telegram_links (user_id, chat_id, username) VALUES (?, ?, ?)",
                       (row["user_id"], chat_id, msg.get("from", {}).get("username", "")))
            db.execute("DELETE FROM telegram_codes WHERE code = ?", (row["code"],))
            db.commit()
            u = query("SELECT * FROM users WHERE id = ?", (row["user_id"],), one=True)
            send(chat_id, _t("tg_linked", u["lang"], company=_company(u)["name"]) + "\n\n" + _help(u["lang"]))
            return
        if user is None:
            send(chat_id, _t("tg_not_linked", "zh") + "\n" + _t("tg_not_linked", "en"))
            return
        if cmd in ("start", "help"):
            send(chat_id, _help(lang))
        elif cmd == "deals":
            rows = [d for d in D.deals_for(user["company_id"]) if d["status"] in ("proposed", "active", "disputed")]
            if not rows:
                send(chat_id, _t("tg_no_deals", lang))
                return
            buttons = [[_btn(f"#{d['id']} {d['title'][:30]} · {_t('ds_' + d['status'], lang)}", f"view:{d['id']}:0")]
                       for d in rows[:20]]
            send(chat_id, _t("tg_pick_deal", lang), buttons)
        elif cmd == "deal" and args and args[0].lstrip("#").isdigit():
            _send_card(chat_id, user, int(args[0].lstrip("#")))
        elif cmd == "unlink":
            execute("DELETE FROM telegram_links WHERE chat_id = ?", (chat_id,))
            send(chat_id, _t("tg_unlinked", lang))
        else:
            send(chat_id, _help(lang))
        return

    # groups: bind a deal so both teams see its updates
    if cmd in ("bind", "unbind", "deal") and args and args[0].lstrip("#").isdigit():
        deal_id = int(args[0].lstrip("#"))
        deal = D.get_deal(deal_id)
        if user is None or deal is None or D.side(deal, _company(user)) is None:
            send(chat_id, _t("tg_group_denied", lang))
            return
        if cmd == "bind":
            execute("INSERT OR IGNORE INTO telegram_groups (chat_id, deal_id, bound_by) VALUES (?, ?, ?)",
                    (chat_id, deal_id, user["id"]))
            D.append_event(deal_id, "group_bound", str(chat_id), user["company_id"], user["id"], "telegram")
            get_db().commit()
            send(chat_id, _t("tg_bound", lang, id=deal_id))
        elif cmd == "unbind":
            execute("DELETE FROM telegram_groups WHERE chat_id = ? AND deal_id = ?", (chat_id, deal_id))
            send(chat_id, _t("tg_unbound", lang, id=deal_id))
        else:
            _send_card(chat_id, user, deal_id)


def _send_card(chat_id: int, user, deal_id: int) -> None:
    deal = D.get_deal(deal_id)
    company = _company(user)
    if deal is None or D.side(deal, company) is None:
        send(chat_id, _t("not_found", user["lang"]))
        return
    text, buttons = deal_card(deal, company, user["lang"])
    send(chat_id, text, buttons)


ACTIONS = {
    "acc": lambda d, m, c, u: D.accept(d, c, u, via="telegram"),
    "dlv": lambda d, m, c, u: D.mark_delivered(d, m, c, u, via="telegram"),
    "dok": lambda d, m, c, u: D.accept_delivery(d, m, c, u, via="telegram"),
    "pok": lambda d, m, c, u: D.confirm_paid(d, m, c, u, via="telegram"),
}


def _handle_callback(cb: dict) -> None:
    cb_id = cb.get("id")
    from_id = cb.get("from", {}).get("id")
    chat_id = cb.get("message", {}).get("chat", {}).get("id") or from_id
    user = _user_by_chat(from_id) if from_id else None
    if user is None:
        api("answerCallbackQuery", {"callback_query_id": cb_id, "text": _t("tg_not_linked", "zh"), "show_alert": True})
        return
    lang = user["lang"]
    try:
        action, deal_id, ms_id = (cb.get("data") or "").split(":")
        deal_id, ms_id = int(deal_id), int(ms_id)
    except ValueError:
        api("answerCallbackQuery", {"callback_query_id": cb_id})
        return
    if action == "view":
        api("answerCallbackQuery", {"callback_query_id": cb_id})
        _send_card(chat_id, user, deal_id)
        return
    fn = ACTIONS.get(action)
    if fn is None:
        api("answerCallbackQuery", {"callback_query_id": cb_id})
        return
    try:
        fn(deal_id, ms_id, _company(user), user)
    except D.DealError as e:
        api("answerCallbackQuery", {"callback_query_id": cb_id, "text": _t(e.key, lang), "show_alert": True})
        return
    api("answerCallbackQuery", {"callback_query_id": cb_id, "text": _t("done", lang)})
    _send_card(chat_id, user, deal_id)


@bp.route("/webhook", methods=["POST"])
def webhook():
    secret = current_app.config.get("TELEGRAM_WEBHOOK_SECRET")
    if not current_app.testing and (not secret or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret):
        abort(403)
    update = request.get_json(silent=True) or {}
    if "message" in update:
        _handle_message(update["message"])
    elif "callback_query" in update:
        _handle_callback(update["callback_query"])
    return {"ok": True}


def set_webhook() -> dict | None:
    cfg = current_app.config
    return api("setWebhook", {"url": f"{cfg['BASE_URL']}/tg/webhook", "secret_token": cfg["TELEGRAM_WEBHOOK_SECRET"],
                              "allowed_updates": ["message", "callback_query"]})
