-- order-desk env DB (one independent sqlite file per attempt, see
-- `<data_path>/attempts/{attempt_id}/env.db`).

CREATE TABLE IF NOT EXISTS env_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    book_id TEXT,
    title TEXT,
    quantity INTEGER,
    unit_price REAL,
    total_price REAL,
    currency TEXT DEFAULT 'USD',
    raw_data TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_env_orders_session ON env_orders(session_id);

CREATE TABLE IF NOT EXISTS env_api_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    api_name TEXT NOT NULL,
    request_data TEXT,
    response_data TEXT,
    is_error INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
