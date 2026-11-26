PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('operator', 'supplier', 'platform')),
    name TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT '',
    category TEXT CHECK (category IN ('wl', 'pay', 'fx', 'mkt')),
    logo_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'paused', 'rejected', 'removed')),
    pledge_signed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'reviewer', 'admin')),
    lang TEXT NOT NULL DEFAULT 'en',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    kind TEXT NOT NULL CHECK (kind IN ('pagcor_licence', 'pagcor_accreditation', 'bsp', 'business_reg')),
    number TEXT NOT NULL,
    file_path TEXT,
    aml_policy_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'verified', 'rejected', 'info_requested', 'expired')),
    verified_at TEXT,
    next_review TEXT,
    note TEXT NOT NULL DEFAULT '',
    reminded_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS supplier_profiles (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id),
    about_en TEXT NOT NULL DEFAULT '',
    about_zh TEXT NOT NULL DEFAULT '',
    services_en TEXT NOT NULL DEFAULT '',
    services_zh TEXT NOT NULL DEFAULT '',
    markets TEXT NOT NULL DEFAULT 'Philippines',
    currencies TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ad_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('spotlight', 'cat_top', 'newsletter')),
    category TEXT CHECK (category IN ('wl', 'pay', 'fx', 'mkt')),
    capacity INTEGER NOT NULL,
    price_php INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL REFERENCES ad_slots(id),
    company_id INTEGER NOT NULL REFERENCES companies(id),
    month TEXT NOT NULL,
    headline TEXT NOT NULL DEFAULT '',
    creative_path TEXT,
    creative_status TEXT NOT NULL DEFAULT 'none'
        CHECK (creative_status IN ('none', 'pending', 'approved', 'rejected')),
    status TEXT NOT NULL DEFAULT 'unpaid' CHECK (status IN ('unpaid', 'paid', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL UNIQUE,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    booking_id INTEGER REFERENCES bookings(id),
    amount_php INTEGER NOT NULL,
    method TEXT NOT NULL DEFAULT 'bank',
    status TEXT NOT NULL DEFAULT 'unpaid' CHECK (status IN ('unpaid', 'paid', 'overdue', 'void')),
    due_date TEXT NOT NULL,
    paid_at TEXT,
    reference TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS enquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id INTEGER NOT NULL REFERENCES companies(id),
    supplier_id INTEGER NOT NULL REFERENCES companies(id),
    service TEXT NOT NULL,
    volume TEXT NOT NULL DEFAULT '',
    consent INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'replied', 'closed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enquiry_id INTEGER NOT NULL REFERENCES enquiries(id),
    sender_company_id INTEGER NOT NULL REFERENCES companies(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    kind TEXT NOT NULL CHECK (kind IN ('impression', 'view')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id);
CREATE INDEX IF NOT EXISTS idx_cred_company ON credentials(company_id);
CREATE INDEX IF NOT EXISTS idx_bookings_month ON bookings(month, slot_id);
CREATE INDEX IF NOT EXISTS idx_enq_operator ON enquiries(operator_id);
CREATE INDEX IF NOT EXISTS idx_enq_supplier ON enquiries(supplier_id);
CREATE INDEX IF NOT EXISTS idx_msg_enq ON messages(enquiry_id);
CREATE INDEX IF NOT EXISTS idx_events_company ON events(company_id, kind, created_at);

-- ===== Guarantee (担保): records and arbitration only. HUILIAN never holds funds. =====

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enquiry_id INTEGER REFERENCES enquiries(id),
    operator_id INTEGER NOT NULL REFERENCES companies(id),
    supplier_id INTEGER NOT NULL REFERENCES companies(id),
    title TEXT NOT NULL,
    scope TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('PHP', 'USD', 'USDT')),
    pay_terms TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'active', 'completed', 'disputed', 'resolved', 'declined', 'cancelled')),
    terms_hash TEXT,
    accepted_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deal_milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL REFERENCES deals(id),
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    amount TEXT NOT NULL,
    due_date TEXT,
    delivery TEXT NOT NULL DEFAULT 'open' CHECK (delivery IN ('open', 'delivered', 'accepted')),
    payment TEXT NOT NULL DEFAULT 'unpaid' CHECK (payment IN ('unpaid', 'claimed', 'confirmed')),
    pay_reference TEXT NOT NULL DEFAULT ''
);

-- Append-only, hash-chained log: each row's hash covers the previous row's hash.
CREATE TABLE IF NOT EXISTS deal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL REFERENCES deals(id),
    company_id INTEGER REFERENCES companies(id),
    user_id INTEGER REFERENCES users(id),
    via TEXT NOT NULL DEFAULT 'web',
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deal_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL REFERENCES deals(id),
    company_id INTEGER NOT NULL REFERENCES companies(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL REFERENCES deals(id),
    milestone_id INTEGER REFERENCES deal_milestones(id),
    opened_by INTEGER NOT NULL REFERENCES companies(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    outcome TEXT CHECK (outcome IN ('operator', 'supplier', 'split', 'cancel')),
    decision TEXT NOT NULL DEFAULT '',
    resolved_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS telegram_links (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    chat_id INTEGER NOT NULL UNIQUE,
    username TEXT NOT NULL DEFAULT '',
    linked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS telegram_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_groups (
    chat_id INTEGER NOT NULL,
    deal_id INTEGER NOT NULL REFERENCES deals(id),
    bound_by INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (chat_id, deal_id)
);

CREATE INDEX IF NOT EXISTS idx_deals_operator ON deals(operator_id);
CREATE INDEX IF NOT EXISTS idx_deals_supplier ON deals(supplier_id);
CREATE INDEX IF NOT EXISTS idx_events_deal ON deal_events(deal_id, id);
CREATE INDEX IF NOT EXISTS idx_ms_deal ON deal_milestones(deal_id, seq);
