"""End-to-end flows through the Flask test client."""
import io
import re
from datetime import date, timedelta

import pytest

from huilian import create_app
from huilian.db import query
from huilian.seed import seed

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
       b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.sqlite"), "UPLOAD_DIR": str(tmp_path / "up"),
                      "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"})
    with app.app_context():
        seed(demo=True)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, email, password="DemoPass12345"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=True)


def logout(client):
    client.post("/logout")


def admin(client):
    logout(client)
    return login(client, "admin@huilian.local", "ChangeMe12345")


def db(app, sql, args=(), one=False):
    with app.app_context():
        return query(sql, args, one=one)


def register_supplier(client, email="new@sup.test", category="mkt", cred="business_reg", aml=False):
    data = {"role": "supplier", "company_name": "New Supplier Co", "country": "Philippines", "category": category,
            "credential_type": cred, "credential_number": "REG-1", "name": "Nina", "email": email,
            "password": "LongPassword1", "pledge": "1", "credential_file": (io.BytesIO(PDF), "cert.pdf")}
    if aml:
        data["aml_file"] = (io.BytesIO(PDF), "aml.pdf")
    return client.post("/register", data=data, content_type="multipart/form-data", follow_redirects=True)


def register_operator(client, email="ops@op.test", next_url=None):
    data = {"role": "operator", "company_name": "Brand New Operator", "country": "Philippines",
            "credential_number": "PAGCOR-9", "name": "Oscar", "email": email, "password": "LongPassword1",
            "credential_file": (io.BytesIO(PDF), "licence.pdf")}
    if next_url:
        data["next"] = next_url
    return client.post("/register", data=data, content_type="multipart/form-data", follow_redirects=True)


def approve_latest_credential(client, app, company_name):
    cred = db(app, "SELECT k.id FROM credentials k JOIN companies c ON c.id = k.company_id WHERE c.name = ? "
                   "ORDER BY k.id DESC LIMIT 1", (company_name,), one=True)
    admin(client)
    due = (date.today() + timedelta(days=200)).isoformat()
    r = client.post(f"/admin/credential/{cred['id']}", data={"action": "approve", "next_review": due},
                    follow_redirects=True)
    assert r.status_code == 200
    logout(client)


# ---------- public ----------

def test_public_pages_both_languages(client):
    for url in ["/", "/?tab=feat", "/?cat=pay", "/s/2", "/login", "/register?role=operator"]:
        assert client.get(url).status_code == 200
    client.get("/lang/zh?next=/")
    body = client.get("/").get_data(as_text=True)
    assert "灰链 HUILIAN" in body and "服务商" in body
    client.get("/lang?code=en&next=/")
    assert "Suppliers" in client.get("/").get_data(as_text=True)


def test_home_lists_sponsored_first_and_featured_tab(client):
    body = client.get("/").get_data(as_text=True)
    names = re.findall(r'<span class="name">([^<]+)</span>', body)
    assert names[:2] == ["Supplier A Gaming Tech", "Supplier B Payments"]
    feat = client.get("/?tab=feat").get_data(as_text=True)
    assert "Launch a full casino and sportsbook in 8 weeks" in feat  # spotlight
    assert "Supplier C Exchange" not in feat
    assert "Supplier C Exchange" in client.get("/?cat=fx").get_data(as_text=True)


def test_csrf_required(app):
    app.config["WTF_CSRF_DISABLED"] = False
    c = app.test_client()
    r = c.post("/login", data={"email": "x@y.z", "password": "nope"})
    assert r.status_code == 400


# ---------- supplier onboarding ----------

def test_supplier_hidden_until_approved(client, app):
    r = register_supplier(client)
    assert "Verification pending" in r.get_data(as_text=True)
    assert "New Supplier Co" not in client.get("/").get_data(as_text=True)
    approve_latest_credential(client, app, "New Supplier Co")
    assert "New Supplier Co" in client.get("/").get_data(as_text=True)


def test_payment_supplier_needs_aml(client):
    r = register_supplier(client, category="pay", cred="bsp", aml=False)
    assert r.status_code == 400
    r = register_supplier(client, email="pay2@sup.test", category="pay", cred="bsp", aml=True)
    assert r.status_code == 200


def test_bad_upload_rejected(client):
    data = {"role": "operator", "company_name": "X", "country": "PH", "credential_number": "1", "name": "N",
            "email": "fake@op.test", "password": "LongPassword1",
            "credential_file": (io.BytesIO(b"not really a pdf"), "licence.pdf")}
    r = client.post("/register", data=data, content_type="multipart/form-data")
    assert r.status_code == 400


# ---------- enquiry flow ----------

def test_operator_enquiry_end_to_end(client, app):
    supplier_id = db(app, "SELECT id FROM companies WHERE name = 'Supplier D Creative'", one=True)["id"]
    url = f"/s/{supplier_id}/enquire"
    assert "Verify your operator account first" in client.get(url).get_data(as_text=True)

    r = register_operator(client, next_url=url)
    assert "Checking your licence" in r.get_data(as_text=True)
    approve_latest_credential(client, app, "Brand New Operator")

    login(client, "ops@op.test", "LongPassword1")
    form = {"service": "svc_mkt_creative", "volume": "vol_1", "details": "Need ASC banners"}
    r = client.post(url, data=form, follow_redirects=True)
    assert "Tick the box" in r.get_data(as_text=True)
    r = client.post(url, data={**form, "consent": "1"}, follow_redirects=True)
    assert "Need ASC banners" in r.get_data(as_text=True)
    enq = db(app, "SELECT * FROM enquiries ORDER BY id DESC LIMIT 1", one=True)
    assert enq["status"] == "new"

    logout(client)
    login(client, "supplierd@demo.local")
    page = client.get(f"/me/enquiries/{enq['id']}").get_data(as_text=True)
    assert "PAGCOR-9" in page and "ops@op.test" in page  # contact shared with consent
    client.post(f"/me/enquiries/{enq['id']}", data={"body": "Quote attached"})
    assert db(app, "SELECT status FROM enquiries WHERE id = ?", (enq["id"],), one=True)["status"] == "replied"

    logout(client)
    login(client, "ops@op.test", "LongPassword1")
    assert "Replied" in client.get("/me/").get_data(as_text=True)
    assert db(app, "SELECT COUNT(*) n FROM outbox WHERE to_email = 'ops@op.test'", one=True)["n"] >= 2


def test_supplier_cannot_enquire_and_threads_are_private(client, app):
    login(client, "suppliera@demo.local")
    assert "Supplier accounts can" in client.get("/s/3/enquire").get_data(as_text=True)
    enq = db(app, "SELECT id FROM enquiries LIMIT 1", one=True)
    assert client.get(f"/me/enquiries/{enq['id']}").status_code == 404


# ---------- promotions + billing ----------

def test_booking_capacity_and_payment(client, app):
    login(client, "supplierb@demo.local")
    month = db(app, "SELECT strftime('%Y-%m', 'now', '+40 days') m", one=True)["m"]
    top_pay = db(app, "SELECT id FROM ad_slots WHERE code = 'top_pay'", one=True)["id"]
    r = client.post("/me/promotions", data={"slot_id": top_pay, "month": month, "method": "rrpay"},
                    follow_redirects=True)
    assert "Invoice" in r.get_data(as_text=True) or "invoice" in r.get_data(as_text=True).lower()
    logout(client)
    login(client, "supplierf@demo.local")
    client.post("/me/promotions", data={"slot_id": top_pay, "month": month, "method": "bank"})
    assert db(app, "SELECT COUNT(*) n FROM bookings WHERE slot_id = ? AND month = ?", (top_pay, month), one=True)["n"] == 2
    # third booking on a capacity-2 slot is refused
    logout(client)
    register_supplier(client, email="third@sup.test", category="pay", cred="bsp", aml=True)
    approve_latest_credential(client, app, "New Supplier Co")
    login(client, "third@sup.test", "LongPassword1")
    r = client.post("/me/promotions", data={"slot_id": top_pay, "month": month, "method": "bank"}, follow_redirects=True)
    assert "sold out" in r.get_data(as_text=True)

    inv = db(app, "SELECT * FROM invoices WHERE method = 'rrpay' ORDER BY id DESC LIMIT 1", one=True)
    admin(client)
    client.post("/admin/billing", data={"invoice_id": inv["id"], "action": "paid", "reference": "TX123"})
    b = db(app, "SELECT status FROM bookings WHERE id = ?", (inv["booking_id"],), one=True)
    assert b["status"] == "paid"


def test_spotlight_needs_creative_and_review(client, app):
    login(client, "supplierc@demo.local")
    month = db(app, "SELECT strftime('%Y-%m', 'now', '+40 days') m", one=True)["m"]
    spot = db(app, "SELECT id FROM ad_slots WHERE code = 'spotlight'", one=True)["id"]
    r = client.post("/me/promotions", data={"slot_id": spot, "month": month, "method": "bank"}, follow_redirects=True)
    assert "Fill in every required field" in r.get_data(as_text=True)
    client.post("/me/promotions", data={"slot_id": spot, "month": month, "method": "bank", "headline": "Fast FX",
                                         "creative": (io.BytesIO(PNG), "b.png")}, content_type="multipart/form-data")
    b = db(app, "SELECT * FROM bookings WHERE slot_id = ? AND month = ?", (spot, month), one=True)
    assert b["creative_status"] == "pending"
    admin(client)
    assert "Fast FX" in client.get("/admin/").get_data(as_text=True)
    client.post(f"/admin/creative/{b['id']}", data={"action": "approve"})
    assert db(app, "SELECT creative_status FROM bookings WHERE id = ?", (b["id"],), one=True)["creative_status"] == "approved"


# ---------- rules ----------

def test_expiry_pauses_and_hides(app):
    c = app.test_client()
    assert "Supplier C Exchange" in c.get("/").get_data(as_text=True)
    app.config["TODAY"] = (date.today() + timedelta(days=20)).isoformat()  # Supplier C review is in 12 days
    admin(c)
    c.get("/admin/")
    assert db(app, "SELECT status FROM companies WHERE name = 'Supplier C Exchange'", one=True)["status"] == "paused"
    logout(c)
    assert "Supplier C Exchange" not in c.get("/").get_data(as_text=True)


def test_unpaid_hold_released(client, app):
    login(client, "supplierb@demo.local")
    month = db(app, "SELECT strftime('%Y-%m', 'now', '+40 days') m", one=True)["m"]
    news = db(app, "SELECT id FROM ad_slots WHERE code = 'newsletter'", one=True)["id"]
    client.post("/me/promotions", data={"slot_id": news, "month": month, "method": "bank"})
    app.config["TODAY"] = (date.today() + timedelta(days=9)).isoformat()
    admin(client)
    client.get("/admin/billing")
    inv = db(app, "SELECT i.status, b.status AS bstatus FROM invoices i JOIN bookings b ON b.id = i.booking_id "
                  "WHERE b.slot_id = ?", (news,), one=True)
    assert inv["status"] == "overdue" and inv["bstatus"] == "cancelled"


# ---------- access ----------

def test_access_control(client, app):
    assert client.get("/admin/").status_code == 302  # to login
    register_supplier(client, email="docs@sup.test")
    logout(client)
    login(client, "suppliera@demo.local")
    assert client.get("/admin/").status_code == 403
    assert client.get("/admin/billing").status_code == 403
    path = db(app, "SELECT file_path FROM credentials WHERE file_path IS NOT NULL LIMIT 1", one=True)
    assert path is not None
    assert client.get(f"/files/{path['file_path']}").status_code == 404  # not theirs
    admin(client)
    assert client.get(f"/files/{path['file_path']}").status_code == 200
    for url in ["/admin/", "/admin/calendar", "/admin/billing", "/admin/renewals", "/admin/companies", "/admin/audit"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/files/../../etc/passwd").status_code == 404


def test_profile_pages_render(client):
    login(client, "supplierb@demo.local")
    for url in ["/me/", "/me/edit", "/me/promotions", "/me/billing", "/me/credentials"]:
        assert client.get(url).status_code == 200, url
    r = client.post("/me/edit", data={"about_en": "Updated about", "about_zh": "", "services_en": "X: y",
                                      "services_zh": "", "markets": "PH", "currencies": "PHP", "website": "rrpay.example"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "Updated about" in client.get("/s/3").get_data(as_text=True)
    logout(client)
    login(client, "operator@demo.local")
    assert client.get("/me/promotions").status_code == 403
    assert client.get("/me/").status_code == 200


def test_password_reset_single_use(client, app):
    client.post("/forgot", data={"email": "operator@demo.local"})
    body = db(app, "SELECT body FROM outbox WHERE subject LIKE '%Reset%' ORDER BY id DESC LIMIT 1", one=True)["body"]
    path = "/reset/" + body.split("/reset/")[1].strip()
    assert client.post(path, data={"password": "BrandNewPass99"}, follow_redirects=True).status_code == 200
    assert "Signed in" in login(client, "operator@demo.local", "BrandNewPass99").get_data(as_text=True)
    logout(client)
    r = client.get(path, follow_redirects=True)  # link no longer valid
    assert "expired or was already used" in r.get_data(as_text=True)
    # unknown email gets the same answer
    r = client.post("/forgot", data={"email": "nobody@x.test"}, follow_redirects=True)
    assert "If that email has an account" in r.get_data(as_text=True)
