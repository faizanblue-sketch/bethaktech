import hmac
import html as _html
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
import streamlit as st
from sqlalchemy import bindparam, text

# =============================================================================
# 1. APP SETUP & CONSTANTS
# =============================================================================
st.set_page_config(page_title="Sports & Food Tracker", layout="wide")

conn = st.connection("postgresql", type="sql")

CURRENCY = "QAR"
CREDIT_CATEGORY = "Credit"
EXPENSE_COLS = ["id", "entry_date", "user_name", "category", "activity", "amount", "paid_status"]

# Groups are settled / displayed together. Anyone not listed is an individual.
GROUPS = {
    "Ahmed's Group": ["Ahmed", "Arsalan", "Kamran"],
    "Kashif's Group": ["Kashif", "Imran"],
}
GROUPED_USERS = {u for members in GROUPS.values() for u in members}

DEFAULT_SPORTS = ["Marvel Court Fee", "Shuttles"]
DEFAULT_FOOD = [
    "Orange Bethak", "Food Bethak", "Tea/Snacks", "Mandi",
    "Other", "Lala Dabar", "Cake Castle", "Family Gathering",
]

CENT = Decimal("0.01")


# =============================================================================
# 2. MONEY & SETTLEMENT LOGIC
# =============================================================================
# Ledger model (one table, signed amounts):
#   * expense row  -> amount > 0   (what the member owes)
#   * credit row   -> amount < 0   (advance / payment received from the member)
#   * paid_status  -> 0 = still open, 1 = settled
#
# Net balance of a member = SUM(amount) over OPEN rows.
#   positive = member owes money, negative = member holds credit.
#
# When a credit is received, open credit is matched against open dues, oldest
# first. Fully covered rows are flipped to paid; if money runs out mid-row the
# row is split into a paid part and an open remainder. Whatever credit is left
# over stays open as a negative balance, so it shows up as "remaining credit".
# <<CORE>>
def money(value) -> Decimal:
    """Convert anything numeric to a 2-decimal Decimal (avoids float drift)."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def split_amount(total, n_people):
    """Split a total into n shares that add up to exactly `total`."""
    total = money(total)
    base = (total / n_people).quantize(CENT, rounding=ROUND_HALF_UP)
    shares = [base] * n_people
    shares[0] += total - sum(shares)  # push the rounding difference onto the first share
    return shares


def plan_consumption(rows, budget):
    """
    rows   : [(id, signed_amount)] oldest first, all the same sign
    budget : positive Decimal, how much (absolute value) may be consumed
    returns: [("pay", id)] or [("split", id, paid_part, open_part)] (signed amounts)
    """
    actions, remaining = [], budget
    for rid, amt in rows:
        if remaining <= 0:
            break
        size = abs(amt)
        if size <= remaining:
            actions.append(("pay", rid))
            remaining -= size
        else:
            sign = 1 if amt > 0 else -1
            actions.append(("split", rid, remaining * sign, (size - remaining) * sign))
            remaining = Decimal("0")
    return actions


def plan_settlement(open_rows):
    """
    open_rows: [(id, amount)] of one member's OPEN rows, oldest first.
    Matches open credit against open dues. Returns (actions, amount_settled).
    """
    debts = [(i, money(a)) for i, a in open_rows if money(a) > 0]
    credits = [(i, money(a)) for i, a in open_rows if money(a) < 0]
    used = min(sum((a for _, a in debts), Decimal("0")),
               sum((-a for _, a in credits), Decimal("0")))
    if used <= 0:
        return [], Decimal("0")
    return plan_consumption(debts, used) + plan_consumption(credits, used), used


def _apply_actions(session, actions):
    for act in actions:
        if act[0] == "pay":
            session.execute(text("UPDATE expenses SET paid_status = 1 WHERE id = :id"), {"id": act[1]})
        else:
            _, rid, paid_part, open_part = act
            # open remainder keeps the same date/user/category/activity
            session.execute(text("""
                INSERT INTO expenses (entry_date, user_name, category, activity, amount, paid_status)
                SELECT entry_date, user_name, category, activity, :rem, 0 FROM expenses WHERE id = :id
            """), {"id": rid, "rem": float(open_part)})
            session.execute(
                text("UPDATE expenses SET amount = :paid, paid_status = 1 WHERE id = :id"),
                {"id": rid, "paid": float(paid_part)},
            )


def settle_user(session, user):
    """Match a member's open credit against their open dues. Returns amount settled."""
    lock = " FOR UPDATE" if session.get_bind().dialect.name == "postgresql" else ""
    rows = session.execute(
        text("SELECT id, amount FROM expenses WHERE user_name = :u AND paid_status = 0 "
             "ORDER BY entry_date, id" + lock),
        {"u": user},
    ).fetchall()
    actions, used = plan_settlement([(r[0], r[1]) for r in rows])
    _apply_actions(session, actions)
    return used


def open_balance(session, user):
    val = session.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE user_name = :u AND paid_status = 0"),
        {"u": user},
    ).scalar()
    return money(val)


def apply_payment(user, amount, entry_date, activity="Advance Payment"):
    """
    Record money received from `user` and settle their oldest dues with it.
    Everything happens in one transaction. Returns a summary dict.
    """
    amount = money(amount)
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    with conn.session as s:
        before = open_balance(s, user)
        s.execute(
            text("INSERT INTO expenses (entry_date, user_name, category, activity, amount, paid_status) "
                 "VALUES (:d, :u, :c, :a, :amt, 0)"),
            {"d": str(entry_date), "u": user, "c": CREDIT_CATEGORY, "a": activity, "amt": float(-amount)},
        )
        settled = settle_user(s, user)
        after = open_balance(s, user)
        s.commit()
    return {"before": before, "settled": settled, "after": after}


def reconcile_users(users):
    """Match existing open credit against open dues for each user. Returns {user: amount settled}."""
    result = {}
    with conn.session as s:
        for u in users:
            result[u] = settle_user(s, u)
        s.commit()
    return result
# <<END CORE>>


# =============================================================================
# 3. DATABASE SCHEMA
# =============================================================================
def init_db():
    try:
        with conn.session as s:
            s.execute(text("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY,
                    entry_date DATE NOT NULL,
                    user_name VARCHAR(100) NOT NULL,
                    category VARCHAR(50) NOT NULL,
                    activity VARCHAR(100),
                    amount NUMERIC(12,2) NOT NULL,
                    paid_status SMALLINT NOT NULL DEFAULT 0
                );
            """))
            s.execute(text("CREATE TABLE IF NOT EXISTS participants (name VARCHAR(100) PRIMARY KEY);"))
            s.execute(text("""
                CREATE TABLE IF NOT EXISTS category_activities (
                    id SERIAL PRIMARY KEY,
                    category VARCHAR(50) NOT NULL,
                    activity_name VARCHAR(100) NOT NULL
                );
            """))
            s.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'unique_category_activity') THEN
                        ALTER TABLE category_activities
                        ADD CONSTRAINT unique_category_activity UNIQUE (category, activity_name);
                    END IF;
                END $$;
            """))
            s.execute(text("CREATE INDEX IF NOT EXISTS idx_expenses_user_status ON expenses (user_name, paid_status);"))
            s.commit()
    except Exception as e:
        st.error(f"Error initializing DB schema: {e}")


init_db()


# =============================================================================
# 4. DATA LOADING
# =============================================================================
def load_expense_data():
    try:
        df = conn.query("SELECT * FROM expenses ORDER BY entry_date, id;", ttl=0)
    except Exception as e:
        st.error(f"Could not load expenses: {e}")
        return pd.DataFrame(columns=EXPENSE_COLS)
    if df.empty:
        return pd.DataFrame(columns=EXPENSE_COLS)
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0).astype(float)
    df["paid_status"] = pd.to_numeric(df["paid_status"], errors="coerce").fillna(0).astype(int)
    return df


def load_participants():
    try:
        df = conn.query("SELECT name FROM participants ORDER BY name ASC;", ttl=0)
        return df["name"].tolist() if not df.empty else []
    except Exception as e:
        st.warning(f"Could not load participants: {e}")
        return []


def load_activities(category, fallback):
    try:
        df = conn.query(
            "SELECT DISTINCT TRIM(activity_name) AS activity_name FROM category_activities "
            "WHERE LOWER(TRIM(category)) = LOWER(TRIM(:cat)) ORDER BY activity_name ASC;",
            params={"cat": category}, ttl=0,
        )
        acts = [a for a in df["activity_name"].dropna().tolist() if str(a).strip()]
        return acts or fallback
    except Exception as e:
        st.warning(f"Failed to fetch activities for {category}: {e}")
        return fallback


def run_write(sql, params=None):
    """Run one write statement in its own transaction."""
    with conn.session as s:
        s.execute(text(sql), params or {})
        s.commit()


df_expenses = load_expense_data()
saved_participants = load_participants()
all_users = sorted(set(saved_participants) | set(df_expenses["user_name"].dropna().unique().tolist()))
sports_activities = load_activities("Sports", DEFAULT_SPORTS)
food_activities = load_activities("Food", DEFAULT_FOOD)

open_df = df_expenses[df_expenses["paid_status"] == 0]
balances = open_df.groupby("user_name")["amount"].sum().round(2).to_dict()  # +owes / -credit


def entity_balances():
    """Groups first, then individuals. Each: name, members, balance."""
    ents = [{"name": g, "members": m, "balance": round(sum(balances.get(x, 0) for x in m), 2),
             "caption": ", ".join(m)} for g, m in GROUPS.items()]
    ents += [{"name": u, "members": [u], "balance": round(balances.get(u, 0), 2), "caption": "Individual"}
             for u in all_users if u not in GROUPED_USERS]
    return ents


def balance_label(bal):
    if bal > 0.005:
        return f"🔴 **{bal:.2f}** due"
    if bal < -0.005:
        return f"💳 **{abs(bal):.2f}** credit"
    return "🟢 **Settled**"


def render_in_grid(items, per_row, render):
    for start in range(0, len(items), per_row):
        cols = st.columns(per_row)
        for col, item in zip(cols, items[start:start + per_row]):
            with col:
                render(item)


# =============================================================================
# 4b. ARMOURY-STYLE THEME & HTML HELPERS
# =============================================================================
# <<THEME>>
THEME_CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=Inter:wght@400;500&display=swap');
:root{--ac:#ff3b52;--bg:#0a0a0e;--pn:#14141a;--pn2:#1b1b23;--mut:#9a9aa8;--due:#ff5468;--cr:#4fc3ff;--ok:#2ee59d}
.stApp{background:var(--bg)}
header[data-testid="stHeader"]{background:transparent}
.block-container{padding-top:1.4rem;max-width:1500px}
h1,h2,h3{font-family:'Rajdhani',sans-serif!important;letter-spacing:2px;text-transform:uppercase}
.stTabs [data-baseweb="tab-list"]{gap:8px;background:#0e0e13;padding:8px 10px}
.stTabs [data-baseweb="tab"]{height:46px;padding:0 20px;background:transparent;color:var(--mut);font-family:'Rajdhani',sans-serif;font-weight:600;font-size:16px;letter-spacing:1.5px;text-transform:uppercase;transition:all .25s;clip-path:polygon(0 0,calc(100% - 10px) 0,100% 10px,100% 100%,10px 100%,0 calc(100% - 10px))}
.stTabs [data-baseweb="tab"]:hover{background:#1a1a22;color:#fff}
.stTabs [aria-selected="true"]{background:var(--ac)!important;color:#0a0a0e!important}
.stTabs [data-baseweb="tab-highlight"],.stTabs [data-baseweb="tab-border"]{display:none}
div[data-testid="stVerticalBlockBorderWrapper"]{background:var(--pn);border:0!important;border-top:3px solid var(--ac)!important;border-radius:0!important}
div[data-testid="stMetric"]{background:var(--pn);padding:14px 16px;border-top:3px solid var(--ac)}
div[data-testid="stMetricValue"]{font-family:'Rajdhani',sans-serif;font-weight:700}
.stButton>button,.stDownloadButton>button,[data-testid="stFormSubmitButton"]>button{border:0;border-radius:0;background:#262630;color:#fff;font-family:'Rajdhani',sans-serif;font-weight:600;font-size:16px;letter-spacing:2px;text-transform:uppercase;padding:.55rem 1.4rem;transition:all .25s;clip-path:polygon(0 0,calc(100% - 12px) 0,100% 12px,100% 100%,12px 100%,0 calc(100% - 12px))}
.stButton>button:hover,.stDownloadButton>button:hover,[data-testid="stFormSubmitButton"]>button:hover{background:#34343f;color:#fff;transform:translateY(-2px)}
button[kind="primary"],button[kind="primaryFormSubmit"],[data-testid="stBaseButton-primary"],[data-testid="stBaseButton-primaryFormSubmit"]{background:var(--ac)!important;color:#0a0a0e!important}
button[kind="primary"]:hover,[data-testid="stBaseButton-primary"]:hover{filter:brightness(1.15);box-shadow:0 0 24px var(--ac)}
[data-testid="stForm"]{border:0;background:transparent;padding:0}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.top h2{font-size:30px;margin:0;letter-spacing:3px;color:#ececf2}
.pill{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--mut);background:var(--pn);padding:8px 14px;clip-path:polygon(0 0,calc(100% - 8px) 0,100% 8px,100% 100%,8px 100%,0 calc(100% - 8px))}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(46,229,157,.7)}100%{box-shadow:0 0 0 10px rgba(46,229,157,0)}}
@keyframes rise{0%{opacity:0;transform:translateY(14px)}100%{opacity:1;transform:none}}
@keyframes grow{0%{transform:scaleX(0)}100%{transform:scaleX(1)}}
@keyframes sweep{0%{left:-160px}100%{left:110%}}
.hero{position:relative;overflow:hidden;padding:28px 34px;margin-bottom:18px;background:linear-gradient(110deg,#17171f 0%,#1d0f14 60%,#2b0f17 100%);display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap;color:#ececf2;clip-path:polygon(0 0,calc(100% - 30px) 0,100% 30px,100% 100%,30px 100%,0 calc(100% - 30px))}
.hero::after{content:"";position:absolute;top:0;bottom:0;width:140px;left:-160px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.07),transparent);transform:skewX(-20deg);animation:sweep 5s linear infinite}
.hero h1{font:700 44px 'Rajdhani',sans-serif;letter-spacing:2px;margin:0;text-transform:uppercase}
.hero p{margin:6px 0 0;color:#b4b4c2;font-size:15px}
.split{position:relative;z-index:1;width:300px;font-size:12px;color:#b4b4c2}
.split .sh{display:flex;justify-content:space-between;margin-bottom:6px}
.sb{display:flex;height:26px;overflow:hidden}
.sb i{display:flex;align-items:center;justify-content:center;font:700 15px 'Rajdhani',sans-serif;font-style:normal;color:#0a0a0e;transform-origin:left;animation:grow 1.2s cubic-bezier(.2,.8,.2,1) both}
.sb .f,.sl .f{background:var(--ac)}.sb .s,.sl .s{background:var(--cr)}
.sl{display:flex;gap:18px;margin-top:8px}
.sl u{display:inline-block;width:9px;height:9px;margin-right:6px}
.bar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;background:var(--pn);padding:14px 20px;margin-bottom:14px;color:#ececf2;clip-path:polygon(0 0,calc(100% - 20px) 0,100% 20px,100% 100%,20px 100%,0 calc(100% - 20px))}
.bar h1{font:700 26px 'Rajdhani',sans-serif;letter-spacing:2px;margin:0;text-transform:uppercase}
.bar span{color:var(--mut);font-size:13px}
.fh{font:700 20px 'Rajdhani',sans-serif;letter-spacing:3px;text-transform:uppercase;color:#ececf2;border-top:3px solid var(--k);padding-top:10px;margin-bottom:6px}
.fh.sp{--k:var(--cr)}.fh.fd{--k:var(--ac)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:18px}
.tile{position:relative;background:var(--pn);padding:18px 20px;transition:all .3s;animation:rise .6s both;color:#ececf2;clip-path:polygon(0 0,calc(100% - 16px) 0,100% 16px,100% 100%,16px 100%,0 calc(100% - 16px))}
.tile:hover{background:var(--pn2);transform:translateY(-4px)}
.tile::before{content:"";position:absolute;top:0;left:0;height:3px;width:46%;background:var(--ac);transition:width .4s}
.tile:hover::before{width:100%}
.tile small{color:var(--mut);font-size:12px;letter-spacing:1.5px;text-transform:uppercase}
.tile b{display:block;font:700 38px 'Rajdhani',sans-serif;margin-top:4px}
.tile i{font-style:normal;font-size:13px;color:var(--mut)}
.cols{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:18px;margin-bottom:18px}
.panel{background:var(--pn);padding:20px 22px;margin-bottom:18px;color:#ececf2;clip-path:polygon(0 0,calc(100% - 20px) 0,100% 20px,100% 100%,20px 100%,0 calc(100% - 20px))}
.cols .panel{margin-bottom:0}
.panel h3{font:600 16px 'Rajdhani',sans-serif;letter-spacing:2.5px;text-transform:uppercase;margin:0 0 14px;color:#b4b4c2}
.members{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.m{background:var(--pn2);padding:14px;transition:all .25s}
.m:hover{background:#242430;transform:translateY(-3px);box-shadow:0 6px 22px rgba(0,0,0,.5)}
.m b{display:block;font:600 17px 'Rajdhani',sans-serif;letter-spacing:1px}
.m span{font-size:12px;color:var(--mut)}
.m em{display:block;font:700 24px 'Rajdhani',sans-serif;font-style:normal;margin-top:8px}
.due{color:var(--due)}.cr{color:var(--cr)}.ok{color:var(--ok)}
.row{display:grid;grid-template-columns:90px 1fr 80px;align-items:center;gap:10px;font-size:13px;margin-bottom:12px}
.track{height:10px;background:#0e0e13}
.nb{height:100%;transform-origin:left;animation:grow 1.4s cubic-bezier(.2,.8,.2,1) both}
.nb.due{background:var(--due)}.nb.cr{background:var(--cr)}
.note{font-size:13px;color:var(--mut)}
.tag{font:600 11px 'Rajdhani',sans-serif;letter-spacing:1.5px;padding:3px 9px;text-transform:uppercase}
.tag.op{background:#2a141a;color:var(--due)}.tag.ok{background:#12261f;color:var(--ok)}
.tw{overflow:auto;max-height:440px}
.bt{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap;color:#ececf2}
.bt th,.bt td{padding:9px 12px;text-align:right;border-top:1px solid #23232d}
.bt thead th{position:sticky;top:0;background:var(--pn);font:600 12px 'Rajdhani',sans-serif;letter-spacing:1.5px;text-transform:uppercase;color:#b4b4c2;border-top:0;z-index:2}
.bt th:first-child{text-align:left;position:sticky;left:0;background:var(--pn);z-index:1}
.bt thead th:first-child{z-index:3}
.bt tbody th{font:600 14px 'Rajdhani',sans-serif;color:#ececf2}
.bt tbody tr{transition:background .2s}
.bt tbody tr:hover,.bt tbody tr:hover th{background:var(--pn2)}
.bt td.op{color:var(--due)}.bt td.pd{color:var(--ok)}.bt td.na{color:#7c7c8a}
.bt .sg{color:#b4b4c2}.bt .tt{color:#ececf2;font-weight:500}
.bt.lg th,.bt.lg td{text-align:left}.bt.lg .n{text-align:right}
.dg{background:var(--pn);margin-bottom:12px;color:#ececf2;clip-path:polygon(0 0,calc(100% - 16px) 0,100% 16px,100% 100%,16px 100%,0 calc(100% - 16px))}
.dg summary{list-style:none;display:flex;align-items:center;gap:18px;padding:16px 22px;cursor:pointer;font-size:14px;color:#b4b4c2;transition:background .2s}
.dg summary::-webkit-details-marker{display:none}
.dg summary:hover{background:var(--pn2)}
.dg summary::before{content:"\203A";font:700 22px 'Rajdhani',sans-serif;color:var(--ac);transition:transform .25s;width:12px}
.dg[open] summary::before{transform:rotate(90deg)}
.dg summary b{font:700 19px 'Rajdhani',sans-serif;letter-spacing:1.5px;color:#ececf2;min-width:130px}
.dg summary .tot{margin-left:auto;font:600 17px 'Rajdhani',sans-serif;color:#ececf2}
.dg .tw{padding:0 22px 18px;max-height:none}
@media (max-width:900px){.cols{grid-template-columns:1fr}.hero h1{font-size:32px}.split{width:100%}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


def esc(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return _html.escape(str(v))


def hero_html(title, subtitle, extra=""):
    return (f'<div class="hero"><div><h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div>{extra}</div>')


def bar_html(title, note=""):
    n = f"<span>{esc(note)}</span>" if note else ""
    return f'<div class="bar"><h1>{esc(title)}</h1>{n}</div>'


def split_bar_html(food, sports):
    total = food + sports
    if total <= 0:
        return ""
    fp = food / total * 100
    sp = 100 - fp
    return (f'<div class="split"><div class="sh"><span>Overall spend split</span><span>{total:,.0f} {CURRENCY}</span></div>'
            f'<div class="sb"><i class="f" style="width:{fp:.1f}%">{fp:.1f}%</i>'
            f'<i class="s" style="width:{sp:.1f}%;animation-delay:.25s">{sp:.1f}%</i></div>'
            f'<div class="sl"><span><u class="f"></u>Food {food:,.0f} {CURRENCY}</span>'
            f'<span><u class="s"></u>Sports {sports:,.0f} {CURRENCY}</span></div></div>')


def tiles_html(items):
    out = "".join(
        f'<div class="tile" style="animation-delay:{0.05 + i * 0.1:.2f}s"><small>{esc(l)}</small>'
        f'<b class="{c}">{esc(v)}</b><i>{esc(u)}</i></div>' for i, (l, v, u, c) in enumerate(items))
    return f'<div class="tiles">{out}</div>'


def member_cards_html(ents):
    cards = []
    for e in ents:
        b = e["balance"]
        if b > 0.005:
            cls, txt = "due", f"{b:,.2f} due"
        elif b < -0.005:
            cls, txt = "cr", f"{abs(b):,.2f} credit"
        else:
            cls, txt = "ok", "Settled"
        cards.append(f'<div class="m"><b>{esc(e["name"])}</b><span>{esc(e["caption"])}</span><em class="{cls}">{txt}</em></div>')
    return f'<div class="panel"><h3>Quick status</h3><div class="members">{"".join(cards)}</div></div>'


def net_bars_html(ents):
    nz = [e for e in ents if abs(e["balance"]) > 0.005]
    if not nz:
        body = '<div class="note">Everyone is settled.</div>'
    else:
        mx = max(abs(e["balance"]) for e in nz)
        rows = []
        for e in nz:
            b = e["balance"]
            cls = "due" if b > 0 else "cr"
            rows.append(f'<div class="row"><span>{esc(e["name"])}</span><div class="track"><div class="nb {cls}" '
                        f'style="width:{abs(b) / mx * 100:.0f}%"></div></div><span class="{cls}">{b:,.2f}</span></div>')
        body = "".join(rows)
    return f'<div class="panel"><h3>Net balance</h3>{body}</div>'


def breakup_table_html(df):
    per = df.groupby(["entry_date", "user_name"]).agg(amount=("amount", "sum"), paid=("paid_status", "min")).reset_index()
    users = sorted(per["user_name"].unique())
    cat = df.groupby(["entry_date", "category"])["amount"].sum().unstack(fill_value=0)
    tot = df.groupby("entry_date")["amount"].sum()
    cell = {(r.entry_date, r.user_name): (r.amount, r.paid) for r in per.itertuples()}
    head = ("<tr><th>Date</th>" + "".join(f"<th>{esc(u)}</th>" for u in users) +
            '<th class="sg">Σ Sports</th><th class="sg">Σ Food</th><th class="sg">Σ Credit</th><th class="sg">Total day</th></tr>')
    rows = []
    for d in sorted(tot.index, reverse=True):
        tds = []
        for u in users:
            if (d, u) in cell:
                a, paid = cell[(d, u)]
                tds.append(f'<td class="{"" if paid == 1 else "op"}">{a:.1f}{"✅" if paid == 1 else "❌"}</td>')
            else:
                tds.append('<td class="na">-</td>')
        sums = "".join(f'<td class="sg">{(cat[c][d] if c in cat.columns else 0):.1f}</td>'
                       for c in ("Sports", "Food", CREDIT_CATEGORY))
        rows.append(f'<tr><th scope="row">{d.strftime("%d %b %Y")}</th>{"".join(tds)}{sums}<td class="sg tt">{tot[d]:.1f}</td></tr>')
    return (f'<div class="panel"><h3>🗓️ Individual Breakup Table</h3><div class="tw"><table class="bt">'
            f'<thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div></div>')


def ledger_html(ledger):
    groups = []
    for i, d in enumerate(sorted(ledger["entry_date"].unique(), reverse=True)):
        g = ledger[ledger["entry_date"] == d]
        n_open = int((g["paid_status"] == 0).sum())
        tag = f'<span class="tag op">{n_open} open</span>' if n_open else '<span class="tag ok">All paid</span>'
        rows = "".join(
            f'<tr><th scope="row">{esc(r.user_name)}</th><td>{esc(r.category)}</td><td>{esc(r.activity)}</td>'
            f'<td class="n">{r.amount:,.2f}</td>'
            + ('<td class="pd">✅ Paid</td>' if r.paid_status == 1 else '<td class="op">❌ Open</td>') + "</tr>"
            for r in g.itertuples())
        table = ('<div class="tw"><table class="bt lg"><thead><tr><th>Member</th><th>Category</th><th>Activity</th>'
                 f'<th class="n">Amount</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>')
        groups.append(f'<details class="dg"{" open" if i == 0 else ""}><summary><b>{d.strftime("%d %b %Y")}</b>'
                      f'<span>{len(g)} entries</span><span class="tot">{g["amount"].sum():,.2f} {CURRENCY}</span>{tag}</summary>{table}</details>')
    return "".join(groups)
# <<END THEME>>

# =============================================================================
# 5. TABS
# =============================================================================
st.markdown("<style>" + THEME_CSS + "</style>", unsafe_allow_html=True)
st.markdown('<div class="top"><h2>Sports and Food Tracker</h2><div class="pill"><span class="dot"></span>Live data</div></div>',
            unsafe_allow_html=True)
tabs = st.tabs(["📊 Group Summary", "📋 Daily Ledger", "📥 New Entry", "🛠️ Admin Control", "🔌 System Health"])

# --- TAB 1: GROUP SUMMARY ----------------------------------------------------
with tabs[0]:
    if df_expenses.empty:
        st.markdown(hero_html("Group Summary", "No records yet. Add one from the New Entry tab."), unsafe_allow_html=True)
    else:
        spend = df_expenses[df_expenses["category"].isin(["Food", "Sports"])].groupby("category")["amount"].sum()
        total_dues = sum(v for v in balances.values() if v > 0)
        total_credit = -sum(v for v in balances.values() if v < 0)
        settled_total = df_expenses[(df_expenses["paid_status"] == 1) &
                                    (df_expenses["category"] != CREDIT_CATEGORY)]["amount"].sum()
        st.markdown(hero_html("Group Summary",
                              f"Dues, credits and settlements across {len(all_users)} members, live from your database.",
                              split_bar_html(float(spend.get("Food", 0)), float(spend.get("Sports", 0)))),
                    unsafe_allow_html=True)
        st.markdown(tiles_html([
            ("Outstanding dues", f"{total_dues:,.2f}", CURRENCY, "due"),
            ("Credit held", f"{total_credit:,.2f}", CURRENCY, "cr"),
            ("Total settled", f"{settled_total:,.2f}", CURRENCY, ""),
            ("Records", f"{len(df_expenses):,}", "All time", ""),
        ]), unsafe_allow_html=True)
        ents = entity_balances()
        st.markdown('<div class="cols">' + member_cards_html(ents) + net_bars_html(ents) + "</div>",
                    unsafe_allow_html=True)
        st.markdown(breakup_table_html(df_expenses), unsafe_allow_html=True)

# --- TAB 2: DAILY LEDGER -----------------------------------------------------
with tabs[1]:
    st.markdown(bar_html("Daily Ledger", "Every entry, newest date first"), unsafe_allow_html=True)
    if df_expenses.empty:
        st.info("Nothing to show yet.")
    else:
        lf1, lf2 = st.columns([2, 2])
        sel_user = lf1.selectbox("Filter by member", ["All"] + all_users, key="ledger_user")
        sel_status = lf2.radio("Status", ["All", "Open only", "Paid only"], horizontal=True, key="ledger_status")
        ledger = df_expenses if sel_user == "All" else df_expenses[df_expenses["user_name"] == sel_user]
        if sel_status == "Open only":
            ledger = ledger[ledger["paid_status"] == 0]
        elif sel_status == "Paid only":
            ledger = ledger[ledger["paid_status"] == 1]
        if ledger.empty:
            st.info("No entries match these filters.")
        else:
            st.markdown(ledger_html(ledger), unsafe_allow_html=True)

# --- TAB 3: NEW ENTRY --------------------------------------------------------
with tabs[2]:
    st.markdown(bar_html("Add New Record", "Shares are rounded to cents and always add up to the total"), unsafe_allow_html=True)
    with st.form("expense_form", clear_on_submit=True):
        event_date = st.date_input("Event Date", value=date.today())
        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="fh sp">Sports</div>', unsafe_allow_html=True)
            sports_act = st.selectbox("Activity", options=sports_activities, key="sports_activity")
            sports_total = st.number_input(f"Total Amount ({CURRENCY})", min_value=0.0, step=5.0, key="sports_total")
            sports_sel = st.multiselect("Select Players", options=all_users, key="sports_players")
        with c2:
            st.markdown('<div class="fh fd">Food</div>', unsafe_allow_html=True)
            food_act = st.selectbox("Activity", options=food_activities, key="food_activity")
            food_total = st.number_input(f"Total Amount ({CURRENCY})", min_value=0.0, step=5.0, key="food_total")
            food_sel = st.multiselect("Select Consumers", options=all_users, key="food_players")

        if st.form_submit_button("Submit"):
            problems = []
            if sports_total > 0 and not sports_sel:
                problems.append("Sports amount entered but no players selected.")
            if food_total > 0 and not food_sel:
                problems.append("Food amount entered but no consumers selected.")

            entries = []
            for cat, act, total, people in (("Sports", sports_act, sports_total, sports_sel),
                                            ("Food", food_act, food_total, food_sel)):
                if people and total > 0:
                    for person, share in zip(people, split_amount(total, len(people))):
                        entries.append({"d": str(event_date), "u": person, "c": cat, "a": act,
                                        "am": float(share), "p": 0})
            if problems:
                for msg in problems:
                    st.error(msg)
            elif not entries:
                st.warning("Nothing to record.")
            else:
                try:
                    with conn.session as s:
                        for e in entries:
                            s.execute(text(
                                "INSERT INTO expenses (entry_date, user_name, category, activity, amount, paid_status) "
                                "VALUES (:d, :u, :c, :a, :am, :p)"), e)
                        # any credit a member already holds is used up by the new dues straight away
                        for person in {e["u"] for e in entries}:
                            settle_user(s, person)
                        s.commit()
                    st.success("✅ Recorded!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save: {e}")

# --- TAB 4: ADMIN ------------------------------------------------------------
with tabs[3]:
    st.markdown(bar_html("Admin Control Center", "Credits, settlements, records and members"), unsafe_allow_html=True)

    if not st.session_state.get("authenticated"):
        with st.form("admin_login"):
            pin = st.text_input("Enter Admin PIN", type="password")
            if st.form_submit_button("Login"):
                try:
                    expected = str(st.secrets["admin"]["pin"])
                except Exception:
                    st.error("Admin PIN is not configured in secrets.")
                    expected = None
                if expected is not None:
                    if hmac.compare_digest(pin.encode(), expected.encode()):
                        st.session_state.authenticated = True
                        st.rerun()
                    else:
                        st.error("Incorrect PIN")
    else:
        h1, h2 = st.columns([3, 1])
        with h1:
            admin_task = st.selectbox("Select Administrative Task", [
                "Credit / Payments", "Member Settlement", "Group Payments", "Modify Records",
                "Categories Management", "User Management", "Bulk Operations"])
        with h2:
            st.write(" ")
            if st.button("Logout", use_container_width=True):
                st.session_state.authenticated = False
                st.rerun()
        st.divider()

        # ---- Credit / Payments (Problem 1) ----
        if admin_task == "Credit / Payments":
            st.subheader("💳 Credit / Advance Payment")
            st.caption("The amount first clears the member's oldest open dues (marked ✅ Paid). "
                       "Anything left over stays as credit balance.")
            if st.session_state.get("flash"):
                st.success(st.session_state.pop("flash"))

            # Credits that were entered earlier but never matched against open dues
            credit_open = open_df[open_df["amount"] < 0].groupby("user_name")["amount"].sum().abs()
            dues_open = open_df[open_df["amount"] > 0].groupby("user_name")["amount"].sum()
            pending = sorted(set(credit_open.index) & set(dues_open.index))
            if pending:
                st.warning("These members hold open credit while dues are still open. "
                           "Reconcile to mark the dues as paid.")
                st.dataframe(pd.DataFrame({
                    "Member": pending,
                    "Open Dues": [round(dues_open[u], 2) for u in pending],
                    "Open Credit": [round(credit_open[u], 2) for u in pending],
                    "Will Be Cleared": [round(min(dues_open[u], credit_open[u]), 2) for u in pending],
                    "Credit Left": [round(max(credit_open[u] - dues_open[u], 0), 2) for u in pending],
                }), hide_index=True, use_container_width=True)
                if st.button("🔄 Reconcile existing credits", type="primary", key="cr_reconcile"):
                    try:
                        done = reconcile_users(pending)
                        st.session_state["flash"] = "Reconciled: " + ", ".join(
                            f"{u} ({v:.2f} {CURRENCY} cleared)" for u, v in done.items())
                        st.rerun()
                    except Exception as e:
                        st.error(f"Reconcile failed: {e}")
                st.divider()

            if not all_users:
                st.info("Add members first.")
            else:
                cc1, cc2, cc3 = st.columns([2, 2, 2])
                c_user = cc1.selectbox("Member", all_users, key="cr_user")
                c_amt = cc2.number_input(f"Amount ({CURRENCY})", min_value=0.0, step=5.0, key="cr_amt")
                c_date = cc3.date_input("Payment Date", value=date.today(), key="cr_date")

                bal = balances.get(c_user, 0.0)
                p1, p2, p3 = st.columns(3)
                p1.metric("Current Dues" if bal >= 0 else "Current Credit", f"{abs(bal):.2f} {CURRENCY}")
                if c_amt > 0:
                    pay = money(c_amt)
                    dues_now = max(money(bal), Decimal("0"))
                    credit_now = max(-money(bal), Decimal("0"))
                    pool = pay + credit_now
                    p2.metric("Dues Cleared", f"{min(pool, dues_now):.2f} {CURRENCY}")
                    p3.metric("Credit Remaining After", f"{max(pool - dues_now, Decimal('0')):.2f} {CURRENCY}")

                if st.button("Apply Credit", type="primary", disabled=c_amt <= 0, key="cr_apply"):
                    try:
                        r = apply_payment(c_user, c_amt, c_date)
                        msg = f"Cleared {r['settled']:.2f} {CURRENCY} of dues for {c_user}. "
                        msg += (f"Remaining credit: {abs(r['after']):.2f} {CURRENCY}." if r["after"] < 0
                                else f"Still due: {r['after']:.2f} {CURRENCY}." if r["after"] > 0
                                else "Fully settled.")
                        st.session_state["flash"] = msg
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not apply credit: {e}")

        # ---- Member Settlement ----
        elif admin_task == "Member Settlement":
            st.subheader("💰 Member Settlement")
            active = [(u, balances[u]) for u in all_users if abs(balances.get(u, 0)) > 0.005]
            if not active:
                st.success("Everyone is settled 🎉")

            def _member_card(item):
                user, net = item
                with st.container(border=True):
                    st.subheader(f"👤 {user}")
                    if net > 0:
                        st.metric("Dues", f"{net:.2f} {CURRENCY}")
                        if st.button(f"Record full payment from {user}", key=f"adm_set_{user}"):
                            apply_payment(user, net, date.today(), "Settlement Payment")
                            st.rerun()
                    else:
                        st.metric("Credit Held", f"{abs(net):.2f} {CURRENCY}")
                        if st.button(f"Refund credit to {user}", key=f"adm_ref_{user}"):
                            run_write("UPDATE expenses SET paid_status = 1 "
                                      "WHERE user_name = :u AND paid_status = 0 AND amount < 0", {"u": user})
                            st.rerun()
            render_in_grid(active, 2, _member_card)

        # ---- Group Payments ----
        elif admin_task == "Group Payments":
            st.subheader("🤝 Consolidated Group Liabilities")
            ents = [e for e in entity_balances() if abs(e["balance"]) > 0.005]
            if not ents:
                st.success("Everyone is settled 🎉")

            def _group_card(e):
                with st.container(border=True):
                    st.markdown(f"**{e['name']}**")
                    st.metric("Balance", f"{e['balance']:.2f} {CURRENCY}")
                    payers = [m for m in e["members"] if balances.get(m, 0) > 0.005]
                    if payers and st.button("Settle members' dues", key=f"adm_grp_{e['name']}",
                                            use_container_width=True):
                        for m in payers:
                            apply_payment(m, balances[m], date.today(), "Group Settlement")
                        st.rerun()
                    elif not payers:
                        st.caption("Only credit held, nothing to collect.")
            render_in_grid(ents, 3, _group_card)

        # ---- Modify Records ----
        elif admin_task == "Modify Records":
            if df_expenses.empty:
                st.info("No records.")
            for d in sorted(df_expenses["entry_date"].unique(), reverse=True):
                with st.container(border=True):
                    st.markdown(f"### 📅 Entries for {d}")
                    date_df = df_expenses[df_expenses["entry_date"] == d][EXPENSE_COLS].copy()
                    date_df["paid_status"] = date_df["paid_status"].astype(bool)
                    edited = st.data_editor(
                        date_df, hide_index=True, use_container_width=True, num_rows="dynamic", key=f"ed_{d}",
                        column_config={"id": None, "entry_date": None,
                                       "paid_status": st.column_config.CheckboxColumn("Paid?"),
                                       "user_name": st.column_config.SelectboxColumn("User", options=all_users),
                                       "category": st.column_config.SelectboxColumn(
                                           "Category", options=["Sports", "Food", CREDIT_CATEGORY])})

                    b1, b2, b3, _ = st.columns([1, 1, 1, 2])
                    if b1.button("✅ Mark Dues Paid", key=f"pa_{d}"):
                        run_write("UPDATE expenses SET paid_status = 1 "
                                  "WHERE entry_date = :d AND amount > 0", {"d": str(d)})
                        st.rerun()
                    if b2.button("💾 Save", key=f"sv_{d}"):
                        try:
                            orig = date_df.set_index("id")
                            kept_ids = set(edited["id"].dropna().astype(int))
                            touched = set()
                            with conn.session as s:
                                for rid in set(orig.index) - kept_ids:  # removed rows
                                    touched.add(orig.loc[rid, "user_name"])
                                    s.execute(text("DELETE FROM expenses WHERE id = :id"), {"id": int(rid)})
                                for _, row in edited.iterrows():
                                    if pd.isna(row["user_name"]) or pd.isna(row["amount"]):
                                        continue
                                    vals = {"u": row["user_name"], "c": "Food" if pd.isna(row["category"]) else row["category"],
                                            "a": None if pd.isna(row["activity"]) else row["activity"], "am": float(row["amount"]),
                                            "p": int(bool(row["paid_status"]))}
                                    touched.add(row["user_name"])
                                    if pd.isna(row["id"]):  # new row
                                        s.execute(text(
                                            "INSERT INTO expenses (entry_date, user_name, category, activity, amount, paid_status) "
                                            "VALUES (:d, :u, :c, :a, :am, :p)"), {**vals, "d": str(d)})
                                    else:
                                        s.execute(text(
                                            "UPDATE expenses SET user_name=:u, category=:c, activity=:a, "
                                            "amount=:am, paid_status=:p WHERE id=:id"), {**vals, "id": int(row["id"])})
                                for u in touched:
                                    settle_user(s, u)
                                s.commit()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not save: {e}")
                    with b3.popover("🗑️ Delete day"):
                        st.warning(f"Permanently delete all entries for {d}?")
                        if st.button("Yes, delete", key=f"dd_{d}"):
                            run_write("DELETE FROM expenses WHERE entry_date = :d", {"d": str(d)})
                            st.rerun()

        # ---- Categories ----
        elif admin_task == "Categories Management":
            st.subheader("📁 Categories & Activities Management")
            col_a, col_b = st.columns(2)
            for col, cat, icon, acts in ((col_a, "Sports", "⚽", sports_activities),
                                         (col_b, "Food", "🍲", food_activities)):
                with col, st.container(border=True):
                    st.markdown(f"### {icon} {cat} Activities")
                    with st.form(f"add_{cat.lower()}_act", clear_on_submit=True):
                        new_act = st.text_input(f"New {cat} Activity")
                        if st.form_submit_button("Add Activity") and new_act.strip():
                            try:
                                run_write("INSERT INTO category_activities (category, activity_name) "
                                          "VALUES (:c, :a) ON CONFLICT (category, activity_name) DO NOTHING;",
                                          {"c": cat, "a": new_act.strip()})
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error adding activity: {e}")
                    st.write("---")
                    for idx, act in enumerate(acts):
                        r1, r2 = st.columns([3, 1])
                        r1.write(f"• {act}")
                        if r2.button("🗑️", key=f"del_{cat}_{idx}"):
                            try:
                                run_write("DELETE FROM category_activities WHERE LOWER(TRIM(category)) = LOWER(:c) "
                                          "AND LOWER(TRIM(activity_name)) = LOWER(TRIM(:a));", {"c": cat, "a": act})
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error: {e}")

        # ---- Users ----
        elif admin_task == "User Management":
            u1, u2 = st.columns([1, 2])
            with u1:
                nu = st.text_input("New Member Name")
                if st.button("Add Member") and nu.strip():
                    try:
                        run_write("INSERT INTO participants (name) VALUES (:n) ON CONFLICT DO NOTHING",
                                  {"n": nu.strip()})
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not add member: {e}")
            with u2:
                for p in saved_participants:
                    r1, r2 = st.columns([3, 1])
                    bal = balances.get(p, 0)
                    r1.write(f"👤 {p}  ·  {balance_label(bal).replace('**', '')}")
                    if r2.button("🗑️", key=f"adm_du_{p}", disabled=abs(bal) > 0.005,
                                 help="Members with an open balance can't be removed."):
                        run_write("DELETE FROM participants WHERE name = :n", {"n": p})
                        st.rerun()

        # ---- Bulk ----
        elif admin_task == "Bulk Operations":
            csv_file = st.file_uploader("Upload Expense CSV", type="csv")
            if csv_file:
                up_df = pd.read_csv(csv_file)
                required = {"entry_date", "user_name", "category", "amount"}
                missing = required - set(up_df.columns)
                if missing:
                    st.error(f"Missing columns: {', '.join(sorted(missing))}")
                else:
                    up_df = up_df.drop(columns=["id"], errors="ignore")
                    up_df["entry_date"] = pd.to_datetime(up_df["entry_date"]).dt.date
                    up_df["amount"] = pd.to_numeric(up_df["amount"], errors="coerce")
                    if "paid_status" not in up_df.columns:
                        up_df["paid_status"] = 0
                    up_df["paid_status"] = pd.to_numeric(up_df["paid_status"], errors="coerce").fillna(0).astype(int)
                    bad = up_df["amount"].isna()
                    if bad.any():
                        st.error(f"{int(bad.sum())} row(s) have a non-numeric amount.")
                    else:
                        st.dataframe(up_df.head(20), use_container_width=True)
                        if st.button("Confirm Bulk Upload"):
                            up_df.to_sql("expenses", conn.engine, if_exists="append", index=False)
                            st.success(f"Imported {len(up_df)} rows.")
                            st.rerun()
            if not df_expenses.empty:
                st.download_button("📥 Download Backup (CSV)", df_expenses.to_csv(index=False),
                                   "expense_backup.csv", use_container_width=True)

# --- TAB 5: HEALTH -----------------------------------------------------------
with tabs[4]:
    st.markdown(bar_html("System Health"), unsafe_allow_html=True)
    try:
        conn.query("SELECT 1", ttl=0)
        st.success("✅ Database: Connected")
    except Exception as e:
        st.error(f"❌ Disconnected: {e}")
