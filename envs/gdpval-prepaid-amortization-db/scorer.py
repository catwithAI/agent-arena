"""Deterministic scorer for the official GDPval prepaid-amortization adaptation."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path

ENV_DIR = Path(__file__).resolve().parent
EXPECTED = json.loads((ENV_DIR / "private" / "expected_state.json").read_text())

def score(*, attempt_id: str, env_db: Path | None = None, **_kwargs):
    attempt = env_db.parent if env_db else Path("data/attempts") / attempt_id
    path = attempt / "skill_workspace" / "amortization.db"
    if not path.is_file():
        return _scores(0, 0, False, "amortization.db missing")
    try:
        with sqlite3.connect(path) as db:
            rows = {str(r[0]): (str(r[1]), float(r[2]), str(r[3]), r[4], r[5]) for r in db.execute("SELECT invoice_id,account,amount,invoice_date,amort_start,amort_end FROM invoice_records")}
            expected = {str(i["invoice_id"]): i for i in EXPECTED["invoices"]}
            matched = set(rows) & set(expected)
            correct = 0
            bad_dates = []
            bad_accounts = []
            for invoice_id in matched:
                got = rows[invoice_id]; want = expected[invoice_id]
                account = "1250" if want["class"] == "expense" else "1251"
                if got[0] == account and abs(got[1] - want["amount"]) <= 0.01 and _date(got[2]) == _date(want["invoice_date"]) and _date(got[3]) == want["amort_start"] and _date(got[4]) == want["amort_end"]:
                    correct += 1
                if _date(got[2]) != _date(want["invoice_date"]):
                    bad_dates.append(invoice_id)
                if got[0] != account:
                    bad_accounts.append(invoice_id)
            invoice_score = round(100 * correct / len(expected))
            fabricated = sorted(set(rows) - set(expected))
            balances = {(str(r[0]), str(r[1])): float(r[2]) for r in db.execute("SELECT account,month,ending_balance FROM monthly_balances")}
            expected_balances = {(a, m): v for a, months in EXPECTED["gl_balances"].items() for m, v in months.items()}
            balance_correct = sum(1 for k, v in expected_balances.items() if k in balances and abs(balances[k] - v) <= 0.01)
            derived = _derive_balances(rows)
            derived_correct = sum(1 for k, v in expected_balances.items() if k in balances and k in derived and abs(balances[k] - derived[k]) <= 0.01)
            balance_score = round(100 * (balance_correct + derived_correct) / (2 * len(expected_balances)))
            state = db.execute("SELECT finalized FROM submission_state WHERE id=1").fetchone()
            finalized = bool(state and state[0])
            if fabricated: invoice_score = min(invoice_score, 40)
            return _scores(invoice_score, balance_score, finalized, f"correct_invoices={correct}/{len(expected)}, bad_invoice_dates={bad_dates}, bad_invoice_accounts={bad_accounts}, fabricated={fabricated}, official_balances={balance_correct}/{len(expected_balances)}, derived_balances={derived_correct}/{len(expected_balances)}")
    except (sqlite3.Error, ValueError, TypeError) as exc:
        return _scores(0, 0, False, f"invalid database: {exc}")

def _date(value):
    import datetime
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try: return datetime.datetime.strptime(str(value), fmt).date().isoformat()
        except ValueError: pass
    return str(value)

def _derive_balances(rows):
    """Recompute Jan-Apr balances from submitted invoice periods, not copied GL values."""
    import datetime
    from calendar import monthrange
    months = ["2025-01", "2025-02", "2025-03", "2025-04"]
    totals = {("1250", m): 0.0 for m in months} | {("1251", m): 0.0 for m in months}
    for invoice_id, (account, amount, invoice_date, start_raw, end_raw) in rows.items():
        try:
            issued = datetime.date.fromisoformat(_date(invoice_date))
            start = datetime.date.fromisoformat(_date(start_raw))
            end = datetime.date.fromisoformat(_date(end_raw))
        except ValueError:
            continue
        term = (end.year - start.year) * 12 + end.month - start.month + 1
        if term <= 0:
            continue
        monthly = amount / term
        for month in months:
            y, m = map(int, month.split("-"))
            current = datetime.date(y, m, 1)
            add = amount if issued.year == y and issued.month == m else 0.0
            amort = monthly if start <= current <= end else 0.0
            totals[(account, month)] += add - amort
    running = {}
    for account in ("1250", "1251"):
        balance = 0.0
        for month in months:
            balance += totals[(account, month)]
            running[(account, month)] = balance
    return running

def _scores(invoice: int, balance: int, finalized: bool, detail: str):
    return [{"dimension": "invoice_fidelity", "value": invoice, "detail": detail}, {"dimension": "balance_reconciliation", "value": balance, "detail": detail}, {"dimension": "completion", "value": 100 if finalized else 0, "detail": detail}]
