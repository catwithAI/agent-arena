CREATE TABLE source_documents(document_id TEXT PRIMARY KEY, filename TEXT NOT NULL, source_task_id TEXT NOT NULL);
CREATE TABLE required_invoices(invoice_id TEXT PRIMARY KEY, account TEXT NOT NULL, source_document TEXT NOT NULL, amount REAL NOT NULL, invoice_date TEXT NOT NULL);
-- All invoice dates use ISO YYYY-MM-DD (year-month-day).
CREATE TABLE invoice_records(invoice_id TEXT PRIMARY KEY, account TEXT NOT NULL, vendor TEXT, amount REAL NOT NULL, invoice_date TEXT NOT NULL, amort_start TEXT, amort_end TEXT);
CREATE TABLE monthly_balances(account TEXT NOT NULL, month TEXT NOT NULL, ending_balance REAL NOT NULL, PRIMARY KEY(account, month));
CREATE TABLE submission_state(id INTEGER PRIMARY KEY CHECK(id=1), finalized INTEGER NOT NULL DEFAULT 0);
INSERT INTO submission_state(id, finalized) VALUES(1, 0);
