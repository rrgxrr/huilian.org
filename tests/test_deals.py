"""Guarantee deals and the Telegram bot."""
import io

from huilian.db import get_db
from test_flows import PDF, admin, app, client, db, login, logout  # noqa: F401  (fixtures)

OP_CHAT = 111
SUP_CHAT = 222


def tg(client, app, update):
    app.extensions["tg_sent"] = []
    r = client.post("/tg/webhook", json=update)
    assert r.status_code == 200
    return app.extensions["tg_sent"]


def msg(chat, text, chat_type="private", user=None):
    return {"message": {"chat": {"id": chat, "type": chat_type}, "from": {"id": user or chat, "username": "u"}, "text": text}}


def tap(user_chat, data):
    return {"callback_query": {"id": "cb1", "from": {"id": user_chat}, "data": data,
                               "message": {"chat": {"id": user_chat}}}}


def link(client, app, email, chat):
    logout(client)
    login(client, email)
    client.post("/me/telegram")
    code = db(app, "SELECT code FROM telegram_codes ORDER BY rowid DESC LIMIT 1", one=True)["code"]
    sent = tg(client, app, msg(chat, f"/start {code}"))
    assert "Connected" in sent[-1][1]["text"] or "已连接" in sent[-1][1]["text"]
    logout(client)


def propose(client, rows=(("Setup", "1000"), ("Go live", "2500.50"))):
    login(client, "supplierb@demo.local")
    data = {"enquiry": 1, "title": "GCash pay-in setup", "scope": "Integrate pay-in", "currency": "USDT",
            "pay_terms": "USDT TRC20 to our wallet", "ms_title": [r[0] for r in rows],
            "ms_amount": [r[1] for r in rows], "ms_due": ["" for _ in rows]}
    return client.post("/deals/new", data=data, follow_redirects=True)


def deal_id(app):
    return db(app, "SELECT id FROM deals ORDER BY id DESC LIMIT 1", one=True)["id"]


def ms_ids(app, did):
    return [r["id"] for r in db(app, "SELECT id FROM deal_milestones WHERE deal_id = ? ORDER BY seq", (did,))]


def test_full_deal_with_bot(client, app):
    link(client, app, "operator@demo.local", OP_CHAT)
    link(client, app, "supplierb@demo.local", SUP_CHAT)

    app.extensions["tg_sent"] = []
    r = propose(client)
    assert "Deal sent" in r.get_data(as_text=True)
    did = deal_id(app)
    d = db(app, "SELECT * FROM deals WHERE id = ?", (did,), one=True)
    assert d["status"] == "proposed" and len(d["terms_hash"]) == 64
    pushed = [p for m, p in app.extensions["tg_sent"] if m == "sendMessage"]
    assert pushed and pushed[0]["chat_id"] == OP_CHAT
    assert any(b[0]["callback_data"] == f"acc:{did}:0" for b in pushed[0]["reply_markup"]["inline_keyboard"]
               if "callback_data" in b[0])

    # operator accepts from Telegram
    tg(client, app, tap(OP_CHAT, f"acc:{did}:0"))
    assert db(app, "SELECT status FROM deals WHERE id = ?", (did,), one=True)["status"] == "active"
    m1, m2 = ms_ids(app, did)

    # milestone 1: supplier delivers on Telegram, operator accepts on Telegram, pays on web, supplier confirms on Telegram
    tg(client, app, tap(SUP_CHAT, f"dlv:{did}:{m1}"))
    tg(client, app, tap(OP_CHAT, f"dok:{did}:{m1}"))
    logout(client)
    login(client, "operator@demo.local")
    r = client.post(f"/deals/{did}/act", data={"action": "paid", "milestone_id": m1, "reference": ""}, follow_redirects=True)
    assert "payment reference" in r.get_data(as_text=True)
    client.post(f"/deals/{did}/act", data={"action": "paid", "milestone_id": m1, "reference": "TX-abc123"})
    tg(client, app, tap(SUP_CHAT, f"pok:{did}:{m1}"))

    # milestone 2 entirely on the web
    logout(client)
    login(client, "supplierb@demo.local")
    client.post(f"/deals/{did}/act", data={"action": "deliver", "milestone_id": m2, "note": "Live in prod"})
    logout(client)
    login(client, "operator@demo.local")
    client.post(f"/deals/{did}/act", data={"action": "accept_delivery", "milestone_id": m2})
    client.post(f"/deals/{did}/act", data={"action": "paid", "milestone_id": m2, "reference": "TX-def456"})
    logout(client)
    login(client, "supplierb@demo.local")
    app.extensions["tg_sent"] = []
    client.post(f"/deals/{did}/act", data={"action": "confirm_paid", "milestone_id": m2})

    assert db(app, "SELECT status FROM deals WHERE id = ?", (did,), one=True)["status"] == "completed"
    vias = {r["via"] for r in db(app, "SELECT via FROM deal_events WHERE deal_id = ?", (did,))}
    assert vias == {"web", "telegram"}
    page = client.get(f"/deals/{did}").get_data(as_text=True)
    assert "Record intact" in page
    assert any("completed" in p["text"].lower() or "已完成" in p["text"] for m, p in app.extensions["tg_sent"])
    assert "1 completed" in client.get("/s/3").get_data(as_text=True)


def test_tampering_is_detected(client, app):
    propose(client)
    did = deal_id(app)
    with app.app_context():
        get_db().execute("UPDATE deal_events SET detail = 'edited' WHERE deal_id = ?", (did,))
        get_db().commit()
    assert "does not verify" in client.get(f"/deals/{did}").get_data(as_text=True)


def test_rules_and_permissions(client, app):
    r = propose(client, rows=(("Setup", "-5"),))
    assert "positive numbers" in r.get_data(as_text=True)
    r = propose(client, rows=(("", ""),))
    assert "1 and 6 milestones" in r.get_data(as_text=True)
    propose(client)
    did = deal_id(app)
    m1 = ms_ids(app, did)[0]
    # supplier cannot accept its own proposal or deliver before acceptance
    r = client.post(f"/deals/{did}/act", data={"action": "deliver", "milestone_id": m1}, follow_redirects=True)
    assert "isn" in r.get_data(as_text=True)
    assert client.post(f"/deals/{did}/act", data={"action": "accept"}).status_code == 302
    assert db(app, "SELECT status FROM deals WHERE id = ?", (did,), one=True)["status"] == "proposed"
    # an outsider cannot see it
    logout(client)
    login(client, "suppliera@demo.local")
    assert client.get(f"/deals/{did}").status_code == 404
    # the operator cannot propose deals
    logout(client)
    login(client, "operator@demo.local")
    assert client.get("/deals/new?enquiry=1").status_code == 404


def test_dispute_and_arbitration(client, app):
    propose(client)
    did = deal_id(app)
    logout(client)
    login(client, "operator@demo.local")
    client.post(f"/deals/{did}/act", data={"action": "accept"})
    r = client.post(f"/deals/{did}/act", data={"action": "dispute", "reason": ""}, follow_redirects=True)
    assert "Explain what went wrong" in r.get_data(as_text=True)
    client.post(f"/deals/{did}/act", data={"action": "dispute", "reason": "Integration never went live"})
    client.post(f"/deals/{did}/act", data={"action": "evidence", "note": "Email thread",
                                           "file": (io.BytesIO(PDF), "mail.pdf")}, content_type="multipart/form-data")
    assert db(app, "SELECT status FROM deals WHERE id = ?", (did,), one=True)["status"] == "disputed"
    f = db(app, "SELECT * FROM deal_files WHERE deal_id = ?", (did,), one=True)
    assert len(f["sha256"]) == 64
    assert client.get(f"/deals/{did}/file/{f['id']}").status_code == 200

    admin(client)
    assert "Integration never went live" in client.get("/admin/disputes").get_data(as_text=True)
    x = db(app, "SELECT id FROM disputes WHERE deal_id = ?", (did,), one=True)["id"]
    r = client.post("/admin/disputes", data={"dispute_id": x, "outcome": "operator", "decision": ""}, follow_redirects=True)
    assert db(app, "SELECT status FROM disputes WHERE id = ?", (x,), one=True)["status"] == "open"
    client.post("/admin/disputes", data={"dispute_id": x, "outcome": "operator",
                                         "decision": "Supplier did not deliver milestone 1."})
    assert db(app, "SELECT status FROM deals WHERE id = ?", (did,), one=True)["status"] == "resolved"
    logout(client)
    login(client, "supplierb@demo.local")
    page = client.get(f"/deals/{did}").get_data(as_text=True)
    assert "Supplier did not deliver milestone 1." in page and "Record intact" in page


def test_bot_guards_and_groups(client, app):
    sent = tg(client, app, msg(999, "/start WRONG"))
    assert "expired" in sent[0][1]["text"]
    sent = tg(client, app, msg(999, "/deals"))
    assert "isn't connected" in sent[0][1]["text"]
    propose(client)
    did = deal_id(app)
    sent = tg(client, app, tap(999, f"acc:{did}:0"))
    assert sent[0][0] == "answerCallbackQuery" and sent[0][1]["show_alert"]

    link(client, app, "operator@demo.local", OP_CHAT)
    sent = tg(client, app, msg(OP_CHAT, "/deals"))
    assert f"view:{did}:0" in str(sent[0][1]["reply_markup"])
    # wrong side tapping a supplier-only button
    sent = tg(client, app, tap(OP_CHAT, f"dlv:{did}:{ms_ids(app, did)[0]}"))
    assert sent[0][1].get("show_alert")
    # group binding: party can bind, stranger cannot
    sent = tg(client, app, msg(-500, f"/bind@HuilianBot {did}", chat_type="group", user=OP_CHAT))
    assert f"#{did}" in sent[0][1]["text"]
    sent = tg(client, app, msg(-500, f"/bind {did}", chat_type="group", user=777))
    assert "Only a connected member" in sent[0][1]["text"] or "只有" in sent[0][1]["text"]
    # updates now reach the group
    link(client, app, "supplierb@demo.local", SUP_CHAT)
    app.extensions["tg_sent"] = []
    tg(client, app, tap(OP_CHAT, f"acc:{did}:0"))
    chats = [p["chat_id"] for m, p in app.extensions["tg_sent"] if m == "sendMessage"]
    assert -500 in chats and SUP_CHAT in chats


def test_webhook_needs_secret_outside_tests(app):
    app.config["TELEGRAM_WEBHOOK_SECRET"] = "s3cret"
    app.testing = False
    c = app.test_client()
    assert c.post("/tg/webhook", json={}).status_code == 403
    assert c.post("/tg/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"}).status_code == 200
