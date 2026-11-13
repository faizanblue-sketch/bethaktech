"""
ledger.py - append-only credit/debit ledger for the Sports & Food Tracker.

Model
-----
* Every charge and every payment is its own row in `expenses`. amount is ALWAYS positive.
      entry_type = 'charge'   -> member owes this (Sports, Food, Refund ...)
      entry_type = 'payment'  -> member paid this (advance, settlement, carried credit ...)
* Rows are never updated, split or deleted (a database trigger enforces it).
  A mistaken row is cancelled by adding a row to `expense_voids`, which leaves the original untouched.
* balance = total charges - total payments
      > 0  member owes money        < 0  member holds credit (advance / overpayment)
* "Paid / partial / open" for each charge is DERIVED when reading: a member's total payments cover
  their charges oldest-first. Nothing about it is stored, so there is nothing to fix up later.

Usage from the Streamlit app:  `import ledger` and pass the `st.connection("postgresql", type="sql")` object.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import numpy as np
import pandas as pd
from sqlalchemy import text

CHARGE = "charge"
PAYMENT = "payment"
PAYMENT_CATEGORY = "Payment"
REFUND_CATEGORY = "Refund"
CENT = Decimal("0.01")


# =============================================================================
# 1. MONEY HELPERS
# =============================================================================
def money(value) -> Decimal:
    """Any number -> Decimal with 2 places (no float drift)."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _cents(value) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def split_amount(total, n_people):
    """Split a total into n shares that add up to exactly `total`."""
    total = money(total)
    base = (total / n_people).quantize(CENT, rounding=ROUND_HALF_UP)
    shares = [base] * n_people
    shares[0] += total - sum(shares)
    return shares


# =============================================================================
# 2. SCHEMA
# =============================================================================
SCHEMA_SQL = [
    # one row per charge or payment, amount always positive
    """
    CREATE TABLE IF NOT EXISTS expenses (
        id          BIGSERIAL PRIMARY KEY,
        entry_date  DATE          NOT NULL,
        user_name   VARCHAR(100)  NOT NULL,
        entry_type  VARCHAR(10)   NOT NULL CHECK (entry_type IN ('charge', 'payment')),
        category    VARCHAR(50)   NOT NULL,
        activity    VARCHAR(100),
        amount      NUMERIC(12,2) NOT NULL CHECK (amount > 0),
        created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses (user_name, entry_date, id)",
    # corrections: cancel a row without touching it
    """
    CREATE TABLE IF NOT EXISTS expense_voids (
        expense_id BIGINT PRIMARY KEY REFERENCES expenses(id),
        reason     TEXT        NOT NULL,
        voided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # history can never be changed or removed
    """
    CREATE OR REPLACE FUNCTION expenses_append_only() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'expenses is append-only: add a new row or void the entry instead';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_expenses_append_only ON expenses",
    """
    CREATE TRIGGER trg_expenses_append_only
    BEFORE UPDATE OR DELETE ON expenses
    FOR EACH ROW EXECUTE PROCEDURE expenses_append_only()
    """,
    # rows that still count
    """
    CREATE OR REPLACE VIEW expenses_active AS
    SELECT e.* FROM expenses e
    WHERE NOT EXISTS (SELECT 1 FROM expense_voids v WHERE v.expense_id = e.id)
    """,
    # balance = charges - payments, per member
    """
    CREATE OR REPLACE VIEW user_balances AS
    SELECT user_name,
           COALESCE(SUM(amount) FILTER (WHERE entry_type = 'charge'), 0)  AS total_charges,
           COALESCE(SUM(amount) FILTER (WHERE entry_type = 'payment'), 0) AS total_payments,
           COALESCE(SUM(CASE WHEN entry_type = 'charge' THEN amount ELSE -amount END), 0) AS balance
    FROM expenses_active
    GROUP BY user_name
    """,
]


def is_legacy_schema(session) -> bool:
    """True if `expenses` still has the old paid_status column (needs migrate_legacy)."""
    return session.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'expenses' AND column_name = 'paid_status'"
    )).first() is not None


def init_schema(conn) -> str:
    """
    Create the ledger tables if missing. Returns 'ok', or 'legacy' when the old paid_status
    table is still in place. In that case nothing is touched: run migrate_legacy() first.
    """
    with conn.session as s:
        if is_legacy_schema(s):
            return "legacy"
        for stmt in SCHEMA_SQL:
            s.execute(text(stmt))
        s.commit()
    return "ok"


# =============================================================================
# 3. WRITING (only ever INSERTs)
# =============================================================================
def _insert(session, entry_date, user, entry_type, category, activity, amount):
    amount = money(amount)
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    session.execute(
        text("INSERT INTO expenses (entry_date, user_name, entry_type, category, activity, amount) "
             "VALUES (:d, :u, :t, :c, :a, :amt)"),
        {"d": str(entry_date or date.today()), "u": user, "t": entry_type,
         "c": category, "a": activity, "amt": amount},
    )


def get_balance(session, user) -> Decimal:
    """charges - payments for one member (positive = owes, negative = credit)."""
    val = session.execute(
        text("SELECT COALESCE(SUM(CASE WHEN entry_type = 'charge' THEN amount ELSE -amount END), 0) "
             "FROM expenses_active WHERE user_name = :u"),
        {"u": user},
    ).scalar()
    return money(val)


def add_entries(conn, entries):
    """
    Insert several charge and/or payment rows in one transaction (all or nothing).
    entries: [{"entry_date", "user", "entry_type", "category", "activity", "amount"}, ...]
    """
    with conn.session as s:
        for e in entries:
            if e["entry_type"] not in (CHARGE, PAYMENT):
                raise ValueError("entry_type must be 'charge' or 'payment'.")
            _insert(s, e["entry_date"], e["user"], e["entry_type"], e["category"], e.get("activity"), e["amount"])
        s.commit()


def add_charges(conn, entries):
    """Insert several charges. entries: [{"entry_date", "user", "category", "activity", "amount"}, ...]"""
    add_entries(conn, [{**e, "entry_type": CHARGE} for e in entries])


def record_payment(conn, user, amount, entry_date=None, activity="Advance Payment") -> dict:
    """
    Record money received from `user` as ONE new payment row. That is all that happens.
    Paying more than the current dues simply leaves a negative balance (= credit).
    """
    amount = money(amount)
    with conn.session as s:
        before = get_balance(s, user)
        _insert(s, entry_date, user, PAYMENT, PAYMENT_CATEGORY, activity, amount)
        s.commit()
    after = before - amount
    return {
        "before": before,                                   # balance before (+ owed / - credit)
        "paid": amount,
        "cleared": min(max(before, Decimal("0")), amount),  # dues this payment covered
        "after": after,                                     # balance after
        "credit": max(-after, Decimal("0")),                # credit left over
        "due": max(after, Decimal("0")),                    # still owed
    }


def settle_balance(conn, user, entry_date=None, activity="Settlement Payment"):
    """Pay off a member's full outstanding dues. Returns None if they owe nothing."""
    with conn.session as s:
        bal = get_balance(s, user)
    if bal <= 0:
        return None
    return record_payment(conn, user, bal, entry_date, activity)


def refund_credit(conn, user, entry_date=None):
    """
    Hand a member's credit back. Recorded as a new 'Refund' charge that brings the balance to zero.
    Returns the refunded amount (0 if the member holds no credit).
    """
    with conn.session as s:
        bal = get_balance(s, user)
        if bal >= 0:
            return Decimal("0")
        _insert(s, entry_date, user, CHARGE, REFUND_CATEGORY, "Credit refund", -bal)
        s.commit()
    return -bal


def void_entry(conn, entry_id, reason):
    """Cancel a mistaken row. The original row stays in the table; the balance ignores it."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to void an entry.")
    with conn.session as s:
        s.execute(text("INSERT INTO expense_voids (expense_id, reason) VALUES (:i, :r)"),
                  {"i": int(entry_id), "r": reason})
        s.commit()


# =============================================================================
# 4. READING, BALANCES & SUMMARY (pure pandas, no writes)
# =============================================================================
ACTIVE_COLS = ["id", "entry_date", "user_name", "entry_type", "category", "activity", "amount"]


def load_active(conn) -> pd.DataFrame:
    """All rows that count (voided rows excluded), oldest first."""
    df = conn.query("SELECT id, entry_date, user_name, entry_type, category, activity, amount "
                    "FROM expenses_active ORDER BY entry_date, id", ttl=0)
    if df.empty:
        return pd.DataFrame(columns=ACTIVE_COLS)
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df["amount"] = pd.to_numeric(df["amount"]).astype(float)
    return df


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """
    One row per member: total_charges, total_payments, balance, due, credit.
    balance = total_charges - total_payments  (computed in whole cents, so no rounding drift).
    """
    cols = ["user_name", "total_charges", "total_payments", "balance", "due", "credit"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    d = df[["user_name", "entry_type", "amount"]].copy()
    d["cents"] = d["amount"].map(_cents)
    out = pd.DataFrame({
        "total_charges": d[d["entry_type"] == CHARGE].groupby("user_name")["cents"].sum(),
        "total_payments": d[d["entry_type"] == PAYMENT].groupby("user_name")["cents"].sum(),
    }).fillna(0).astype("int64")
    out["balance"] = out["total_charges"] - out["total_payments"]
    out["due"] = out["balance"].clip(lower=0)
    out["credit"] = (-out["balance"]).clip(lower=0)
    return (out / 100).round(2).rename_axis("user_name").reset_index()[cols]


def balances_dict(df: pd.DataFrame) -> dict:
    """{member: balance}, positive = owes, negative = credit."""
    s = summarize(df)
    return dict(zip(s["user_name"], s["balance"]))


def allocate_payments(df: pd.DataFrame) -> pd.DataFrame:
    """
    READ-ONLY view of which charges are covered. Each member's total payments are applied to their
    charges oldest-first. Adds: paid_amount, open_amount, status ('paid' / 'partial' / 'open' for
    charges, 'payment' for payment rows). Nothing is written anywhere.
    """
    d = df.sort_values(["entry_date", "id"]).copy()
    if d.empty:
        return d.assign(paid_amount=0.0, open_amount=0.0, status="")
    d["cents"] = d["amount"].map(_cents)
    paid = pd.Series(0, index=d.index, dtype="int64")
    for _, g in d.groupby("user_name"):
        pool = int(g.loc[g["entry_type"] == PAYMENT, "cents"].sum())
        for idx, cents in g.loc[g["entry_type"] == CHARGE, "cents"].items():
            applied = min(int(cents), pool)
            paid[idx] = applied
            pool -= applied
    is_charge = d["entry_type"] == CHARGE
    d["paid_amount"] = (paid / 100).where(is_charge, 0.0)
    d["open_amount"] = ((d["cents"] - paid) / 100).where(is_charge, 0.0)
    d["status"] = np.select(
        [~is_charge, is_charge & (paid == 0), is_charge & (paid < d["cents"])],
        ["payment", "open", "partial"], default="paid")
    return d.drop(columns="cents")


# =============================================================================
# 5. ONE-TIME MIGRATION from the old paid_status model
# =============================================================================
# Old model: positive rows = charges (paid_status 0/1), negative rows = credits (consumed ones are
# marked paid). New model: charges stay charges; money the member actually paid becomes payment rows.
#   payments = charges that were marked paid + credit that was still unused
# so the new balance equals the old open balance for every member (checked before committing).
_LEGACY_ROUND = "ROUND(CAST(amount AS NUMERIC), 2)"


def legacy_balances(conn) -> pd.DataFrame:
    """Per-member open balance under the OLD model, for comparing before migrating."""
    return conn.query(
        f"SELECT user_name, SUM({_LEGACY_ROUND}) AS legacy_balance FROM expenses "
        "WHERE paid_status = 0 GROUP BY user_name ORDER BY user_name", ttl=0)


def migrate_legacy(conn) -> dict:
    """
    Rename the old table to `expenses_legacy` (kept as a backup), build the new ledger and convert.
    Everything runs in one transaction and is rolled back if any member's balance would change.
    """
    with conn.session as s:
        if not is_legacy_schema(s):
            raise RuntimeError("Nothing to migrate: the ledger schema is already in place.")
        old = {r[0]: money(r[1]) for r in s.execute(text(
            f"SELECT user_name, COALESCE(SUM({_LEGACY_ROUND}), 0) FROM expenses "
            "WHERE paid_status = 0 GROUP BY user_name"))}

        s.execute(text("ALTER TABLE expenses RENAME TO expenses_legacy"))
        for stmt in SCHEMA_SQL:
            s.execute(text(stmt))

        s.execute(text(f"""
            INSERT INTO expenses (entry_date, user_name, entry_type, category, activity, amount)
            SELECT entry_date, user_name, 'charge', category, activity, {_LEGACY_ROUND}
            FROM expenses_legacy WHERE {_LEGACY_ROUND} > 0 ORDER BY entry_date, id"""))
        s.execute(text(f"""
            INSERT INTO expenses (entry_date, user_name, entry_type, category, activity, amount)
            SELECT entry_date, user_name, 'payment', 'Payment', 'Migrated: settled charges', SUM({_LEGACY_ROUND})
            FROM expenses_legacy WHERE paid_status = 1 AND {_LEGACY_ROUND} > 0
            GROUP BY entry_date, user_name"""))
        s.execute(text(f"""
            INSERT INTO expenses (entry_date, user_name, entry_type, category, activity, amount)
            SELECT entry_date, user_name, 'payment', 'Payment', 'Migrated: credit balance', -{_LEGACY_ROUND}
            FROM expenses_legacy WHERE paid_status = 0 AND {_LEGACY_ROUND} < 0"""))

        new = {r[0]: money(r[1]) for r in s.execute(text("SELECT user_name, balance FROM user_balances"))}
        zero = Decimal("0")
        bad = {u: (str(old.get(u, zero)), str(new.get(u, zero)))
               for u in set(old) | set(new)
               if abs(old.get(u, zero) - new.get(u, zero)) > Decimal("0.005")}
        if bad:
            s.rollback()
            raise RuntimeError(f"Migration cancelled, balances would change (old, new): {bad}")
        s.commit()
    return {"members": len(new), "balances": {u: float(v) for u, v in new.items()}}
