"""Initial data: ad slots, admin account, optional demo companies."""
import struct
import uuid
import zlib
from datetime import timedelta
from pathlib import Path

from flask import current_app
from werkzeug.security import generate_password_hash

from .core import month_of, today
from .db import get_db, init_db

SLOTS = [
    # code, kind, category, capacity, price PHP
    ("spotlight", "spotlight", None, 1, 40000),
    ("top_wl", "cat_top", "wl", 2, 15000),
    ("top_pay", "cat_top", "pay", 2, 15000),
    ("top_fx", "cat_top", "fx", 2, 15000),
    ("top_mkt", "cat_top", "mkt", 2, 15000),
    ("newsletter", "newsletter", None, 2, 10000),
]


def _png(path: Path, w: int, h: int, rgb: tuple[int, int, int]) -> None:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * w
    raw = zlib.compress(row * h, 9)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def seed(demo: bool = True, admin_email: str = "admin@huilian.local", admin_password: str = "ChangeMe12345") -> None:
    init_db()
    db = get_db()
    for code, kind, cat, cap, price in SLOTS:
        db.execute("INSERT OR IGNORE INTO ad_slots (code, kind, category, capacity, price_php) VALUES (?, ?, ?, ?, ?)",
                   (code, kind, cat, cap, price))
    if not db.execute("SELECT 1 FROM users WHERE email = ?", (admin_email,)).fetchone():
        platform = db.execute("SELECT id FROM companies WHERE kind = 'platform'").fetchone()
        pid = platform[0] if platform else db.execute(
            "INSERT INTO companies (kind, name, country, status) VALUES ('platform', 'HUILIAN', 'PH', 'active')").lastrowid
        db.execute("INSERT INTO users (company_id, email, password_hash, name, role, lang) VALUES (?, ?, ?, 'Admin', 'admin', 'zh')",
                   (pid, admin_email, generate_password_hash(admin_password)))
    db.commit()
    if demo and not db.execute("SELECT 1 FROM companies WHERE kind = 'supplier'").fetchone():
        _demo(db)


def _demo(db) -> None:
    t = today()
    year = (t + timedelta(days=365)).isoformat()
    month = (t + timedelta(days=30)).isoformat()
    pw = generate_password_hash("DemoPass12345")
    uploads = Path(current_app.config["UPLOAD_DIR"])

    suppliers = [
        ("Supplier A Gaming Tech", "wl", "pagcor_accreditation", "Malta",
         "Turnkey casino and sportsbook platform with game aggregation, back office and KYC.",
         "交钥匙娱乐场与体育平台，含游戏聚合、后台与 KYC 模块。",
         "Platform: casino + sportsbook, ready in 8 weeks\nAggregation: 3,000+ games\nBack office: reports, risk, KYC",
         "一体化平台: 娱乐场 + 体育，8 周上线\n游戏聚合: 3,000+ 款游戏\n后台: 报表、风控、KYC", (233, 185, 73)),
        ("Supplier B Payments", "pay", "bsp", "Philippines",
         "Local pay-in and pay-out over e-wallets and bank rails, with AML screening.",
         "本地电子钱包与银行通道代收代付，附 AML 筛查。",
         "Pay-in: e-wallets, InstaPay, QR Ph\nPay-out: same-day bank and e-wallet\nAML: transaction monitoring",
         "代收: 电子钱包、InstaPay、QR Ph\n代付: 当日到账\nAML: 交易监控", (95, 203, 138)),
        ("Supplier C Exchange", "fx", "bsp", "Philippines",
         "PHP, USD, SGD and MYR conversion and settlement for licensed operators.",
         "为持牌运营商提供 PHP、USD、SGD、MYR 兑换与结算。",
         "FX conversion: PHP / USD / SGD / MYR\nSettlement: T+0 to T+1", "外币兑换: PHP / USD / SGD / MYR\n结算: T+0 至 T+1",
         (157, 194, 242)),
        ("Supplier D Creative", "mkt", "business_reg", "Philippines",
         "ASC-ready creative, media buying and CRM for responsible campaigns.",
         "符合 ASC 规范的素材制作、媒介投放与 CRM。",
         "Creative: ASC-ready banners and video\nMedia: programmatic and social\nCRM: retention journeys",
         "素材: 符合 ASC 的横幅与视频\n媒介: 程序化与社媒投放\nCRM: 留存流程", (242, 179, 107)),
        ("Supplier E Sportsbook", "wl", "pagcor_accreditation", "Cyprus",
         "White label sportsbook with live odds feed and risk management.", "白标体育平台，含实时赔率与风控。",
         "Sportsbook: pre-match and live\nRisk: trading team 24/7", "体育: 赛前与滚球\n风控: 24 小时操盘团队", (200, 160, 230)),
        ("Supplier F Acquiring", "pay", "bsp", "Philippines",
         "Card and QR acquiring with chargeback management.", "银行卡与二维码收单，含拒付管理。",
         "Cards: Visa, Mastercard\nQR: QR Ph\nChargebacks: managed", "银行卡: Visa、Mastercard\n二维码: QR Ph\n拒付: 托管处理",
         (120, 200, 200)),
    ]
    ids = {}
    for i, (name, cat, cred, country, aen, azh, sen, szh, color) in enumerate(suppliers):
        logo = f"logos/{uuid.uuid4().hex}.png"
        _png(uploads / logo, 96, 96, color)
        cid = db.execute("""INSERT INTO companies (kind, name, country, category, status, logo_path, pledge_signed_at)
                            VALUES ('supplier', ?, ?, ?, 'active', ?, ?)""",
                         (name, country, cat, logo, t.isoformat())).lastrowid
        ids[name[9]] = cid
        db.execute("INSERT INTO users (company_id, email, password_hash, name) VALUES (?, ?, ?, ?)",
                   (cid, f"supplier{chr(97 + i)}@demo.local", pw, f"Demo {name[9]}"))
        db.execute("""INSERT INTO credentials (company_id, kind, number, status, verified_at, next_review)
                      VALUES (?, ?, ?, 'verified', ?, ?)""",
                   (cid, cred, f"DEMO-{cred.upper()}-{100 + i}", t.isoformat(), year if i != 2 else (t + timedelta(days=12)).isoformat()))
        db.execute("""INSERT INTO supplier_profiles (company_id, about_en, about_zh, services_en, services_zh, markets, currencies)
                      VALUES (?, ?, ?, ?, ?, 'Philippines', 'PHP, USD')""", (cid, aen, azh, sen, szh))

    op1 = db.execute("INSERT INTO companies (kind, name, country, status) VALUES ('operator', 'Operator K Gaming', 'Philippines', 'active')").lastrowid
    db.execute("INSERT INTO users (company_id, email, password_hash, name) VALUES (?, 'operator@demo.local', ?, 'Demo Operator')", (op1, pw))
    db.execute("""INSERT INTO credentials (company_id, kind, number, status, verified_at, next_review)
                  VALUES (?, 'pagcor_licence', 'DEMO-PAGCOR-777', 'verified', ?, ?)""", (op1, t.isoformat(), month))
    op2 = db.execute("INSERT INTO companies (kind, name, country, status) VALUES ('operator', 'Operator L Play', 'Philippines', 'pending')").lastrowid
    db.execute("INSERT INTO users (company_id, email, password_hash, name) VALUES (?, 'pending@demo.local', ?, 'Pending Operator')", (op2, pw))
    db.execute("INSERT INTO credentials (company_id, kind, number) VALUES (?, 'pagcor_licence', 'DEMO-PAGCOR-778')", (op2,))

    m = month_of(t)
    slot = {r["code"]: r["id"] for r in db.execute("SELECT id, code FROM ad_slots")}
    banner = f"creatives/{uuid.uuid4().hex}.png"
    _png(uploads / banner, 1200, 720, (40, 44, 54))
    bookings = [(slot["spotlight"], ids["A"], "Launch a full casino and sportsbook in 8 weeks", banner, "approved"),
                (slot["top_pay"], ids["B"], "", None, "none"),
                (slot["top_wl"], ids["A"], "", None, "none")]
    for n, (sid, cid, headline, path, cs) in enumerate(bookings, start=1):
        bid = db.execute("""INSERT INTO bookings (slot_id, company_id, month, headline, creative_path, creative_status, status)
                            VALUES (?, ?, ?, ?, ?, ?, 'paid')""", (sid, cid, m, headline, path, cs)).lastrowid
        price = db.execute("SELECT price_php FROM ad_slots WHERE id = ?", (sid,)).fetchone()[0]
        db.execute("""INSERT INTO invoices (number, company_id, booking_id, amount_php, method, status, due_date, paid_at, reference)
                      VALUES (?, ?, ?, ?, 'bank', 'paid', ?, ?, 'DEMO')""",
                   (f"HL-DEMO-{n:04d}", cid, bid, price, t.isoformat(), t.isoformat()))

    enq = db.execute("""INSERT INTO enquiries (operator_id, supplier_id, service, volume, consent, status)
                        VALUES (?, ?, 'svc_payin', 'vol_2', 1, 'replied')""", (op1, ids["B"])).lastrowid
    db.execute("INSERT INTO messages (enquiry_id, sender_company_id, body) VALUES (?, ?, ?)",
               (enq, op1, "We need GCash and Maya pay-in, live within six weeks."))
    db.execute("INSERT INTO messages (enquiry_id, sender_company_id, body) VALUES (?, ?, ?)",
               (enq, ids["B"], "We support both and can start onboarding next week. Please send your KYB documents."))
    db.commit()
