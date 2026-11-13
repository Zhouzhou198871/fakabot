CREATE TABLE IF NOT EXISTS faka_orders (
 order_id TEXT PRIMARY KEY,
 idempotency_key TEXT NOT NULL UNIQUE,
 telegram_user_id TEXT NOT NULL,
 supplier_product_id TEXT NOT NULL,
 product_title TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 10),
 catalog_version TEXT NOT NULL,
 unit_price_usdt TEXT NOT NULL,
 amount_usdt TEXT NOT NULL,
 payment_status TEXT NOT NULL DEFAULT 'pending',
 payment_provider TEXT,
 payment_ref TEXT UNIQUE,
 procurement_status TEXT NOT NULL DEFAULT 'not_started',
 delivery_status TEXT NOT NULL DEFAULT 'not_ready',
 supplier_order_ref TEXT,
 delivery_ciphertext TEXT,
 created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 paid_at TEXT,
 purchased_at TEXT,
 delivered_at TEXT,
 updated_at TEXT NOT NULL,
 error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_faka_orders_user_created ON faka_orders(telegram_user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_faka_orders_status ON faka_orders(payment_status,procurement_status);
CREATE TABLE IF NOT EXISTS faka_payment_events (
 event_id TEXT PRIMARY KEY,
 order_id TEXT NOT NULL,
 provider TEXT NOT NULL,
 payment_ref TEXT NOT NULL UNIQUE,
 amount_usdt TEXT NOT NULL,
 currency TEXT NOT NULL,
 payload_hash TEXT NOT NULL DEFAULT '',
 received_at TEXT NOT NULL,
 FOREIGN KEY(order_id) REFERENCES faka_orders(order_id)
);
CREATE TABLE IF NOT EXISTS faka_procurement_tasks (
 task_id TEXT PRIMARY KEY,
 order_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 FOREIGN KEY(order_id) REFERENCES faka_orders(order_id)
);
CREATE INDEX IF NOT EXISTS idx_faka_tasks_status ON faka_procurement_tasks(status,created_at);
CREATE TABLE IF NOT EXISTS faka_order_audit (
 id TEXT PRIMARY KEY,
 event TEXT NOT NULL,
 order_id TEXT,
 details_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_faka_audit_order ON faka_order_audit(order_id,created_at);