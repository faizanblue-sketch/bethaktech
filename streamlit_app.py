import hmac
import html as _html
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
import streamlit as st
from sqlalchemy import text

import ledger

# =============================================================================
# 1. APP SETUP & CONSTANTS
# =============================================================================
st.set_page_config(page_title="Sports & Food Tracker", layout="wide")

conn = st.connection("postgresql", type="sql")

CURRENCY = "QAR"

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
# 2. MONEY & LEDGER LOGIC  (all of it lives in ledger.py)
# =============================================================================
# Model: every charge and every payment is its own positive row, rows are never changed or deleted,
# balance = total charges - total payments (positive = owes, negative = credit).
money = ledger.money
split_amount = ledger.split_amount


def pin_ok(pin):
    try:
        expected = str(st.secrets["admin"]["pin"])
    except Exception:
        st.error("Admin PIN is not configured in secrets.")
        return False
    return hmac.compare_digest(pin.encode(), expected.encode())


# =============================================================================
# 3. DATABASE SCHEMA
# =============================================================================
def init_aux_tables():
    with conn.session as s:
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
        s.commit()


# 'ok' = ledger ready, 'legacy' = old paid_status table still in place (migration screen is shown), 'error'
try:
    init_aux_tables()
    SCHEMA_STATE = ledger.init_schema(conn)
except Exception as e:
    SCHEMA_STATE = "error"
    st.error(f"Error initializing DB schema: {e}")


# =============================================================================
# 4. DATA LOADING
# =============================================================================
def load_expense_data():
    if SCHEMA_STATE != "ok":
        return pd.DataFrame(columns=ledger.ACTIVE_COLS)
    try:
        return ledger.load_active(conn)
    except Exception as e:
        st.error(f"Could not load expenses: {e}")
        return pd.DataFrame(columns=ledger.ACTIVE_COLS)


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

balances = ledger.balances_dict(df_expenses)      # {member: charges - payments}, + owes / - credit
df_view = ledger.allocate_payments(df_expenses)   # read-only: paid / partial / open for each charge


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
.bt td.pt{color:var(--cr)}
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
    """df = ledger.allocate_payments(...) output (active rows with status columns)."""
    ch = df[df["entry_type"] == "charge"]
    pay = df[df["entry_type"] == "payment"]
    per = ch.groupby(["entry_date", "user_name"]).agg(amount=("amount", "sum"), left=("open_amount", "sum")).reset_index()
    users = sorted(df["user_name"].unique())
    cell = {(r.entry_date, r.user_name): (r.amount, r.left) for r in per.itertuples()}
    cat = ch.groupby(["entry_date", "category"])["amount"].sum().unstack(fill_value=0)
    received = pay.groupby("entry_date")["amount"].sum()
    day_total = ch.groupby("entry_date")["amount"].sum()
    head = ("<tr><th>Date</th>" + "".join(f"<th>{esc(u)}</th>" for u in users) +
            '<th class="sg">Σ Sports</th><th class="sg">Σ Food</th><th class="sg">Σ Payments</th><th class="sg">Total day</th></tr>')
    rows = []
    for d in sorted(set(df["entry_date"]), reverse=True):
        tds = []
        for u in users:
            if (d, u) in cell:
                a, left = cell[(d, u)]
                paid = left < 0.005
                tds.append(f'<td class="{"" if paid else "op"}">{a:.1f}{"✅" if paid else "❌"}</td>')
            else:
                tds.append('<td class="na">-</td>')
        sums = "".join(f'<td class="sg">{(cat[c][d] if (c in cat.columns and d in cat.index) else 0):.1f}</td>'
                       for c in ("Sports", "Food"))
        sums += f'<td class="sg">{float(received.get(d, 0)):.1f}</td>'
        rows.append(f'<tr><th scope="row">{d.strftime("%d %b %Y")}</th>{"".join(tds)}{sums}'
                    f'<td class="sg tt">{float(day_total.get(d, 0)):.1f}</td></tr>')
    return (f'<div class="panel"><h3>🗓️ Individual Breakup Table</h3><div class="tw"><table class="bt">'
            f'<thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div></div>')


def _status_cell(r):
    if r.entry_type == "payment":
        return '<td class="pt">💳 Payment</td>'
    if r.status == "paid":
        return '<td class="pd">✅ Paid</td>'
    if r.status == "partial":
        return f'<td class="op">🟡 Partial · {r.open_amount:,.2f} left</td>'
    return '<td class="op">❌ Open</td>'


def ledger_html(view):
    groups = []
    for i, d in enumerate(sorted(view["entry_date"].unique(), reverse=True)):
        g = view[view["entry_date"] == d]
        ch = g[g["entry_type"] == "charge"]
        pay = g[g["entry_type"] == "payment"]
        n_open = int((ch["status"] != "paid").sum())
        tag = ""
        if len(ch):
            tag = f'<span class="tag op">{n_open} open</span>' if n_open else '<span class="tag ok">All paid</span>'
        parts = []
        if len(ch):
            parts.append(f'{ch["amount"].sum():,.2f} {CURRENCY} charged')
        if len(pay):
            parts.append(f'{pay["amount"].sum():,.2f} {CURRENCY} received')
        rows = "".join(
            f'<tr><th scope="row">{esc(r.user_name)}</th><td>{esc(r.entry_type.title())}</td><td>{esc(r.category)}</td>'
            f'<td>{esc(r.activity)}</td><td class="n{" pt" if r.entry_type == "payment" else ""}">'
            f'{"+" if r.entry_type == "payment" else ""}{r.amount:,.2f}</td>{_status_cell(r)}</tr>'
            for r in g.itertuples())
        table = ('<div class="tw"><table class="bt lg"><thead><tr><th>Member</th><th>Type</th><th>Category</th>'
                 f'<th>Activity</th><th class="n">Amount</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>')
        groups.append(f'<details class="dg"{" open" if i == 0 else ""}><summary><b>{d.strftime("%d %b %Y")}</b>'
                      f'<span>{len(g)} entries</span><span class="tot">{" · ".join(parts)}</span>{tag}</summary>{table}</details>')
    return "".join(groups)


def render_setup_screen():
    """Shown instead of the app until the one-time ledger migration has been run."""
    if SCHEMA_STATE == "error":
        st.markdown(bar_html("Database setup failed", "See the error above"), unsafe_allow_html=True)
        return
    st.markdown(bar_html("One-time upgrade required",
                         "The database still uses the old paid_status model"), unsafe_allow_html=True)
    st.write("Your data will be converted to the new ledger: every charge and payment becomes its own row, "
             "and nothing is changed afterwards. The old table is kept as `expenses_legacy`. "
             "Each member's balance is checked before and after, and the upgrade is cancelled if any would change.")
    try:
        st.dataframe(ledger.legacy_balances(conn), hide_index=True, use_container_width=True)
    except Exception as e:
        st.error(f"Could not read current balances: {e}")
    with st.form("migrate_form"):
        pin = st.text_input("Admin PIN", type="password")
        if st.form_submit_button("Run migration", type="primary"):
            if pin_ok(pin):
                try:
                    ledger.migrate_legacy(conn)
                    st.rerun()
                except Exception as e:
                    st.error(f"Migration failed and was rolled back: {e}")
            else:
                st.error("Incorrect PIN")
# <<END THEME>>

# =============================================================================
# 5. TABS
# =============================================================================
st.markdown("<style>" + THEME_CSS + "</style>", unsafe_allow_html=True)
st.markdown('<div class="top"><h2>Sports and Food Tracker</h2><div class="pill"><span class="dot"></span>Live data</div></div>',
            unsafe_allow_html=True)
if SCHEMA_STATE != "ok":
    render_setup_screen()
    st.stop()

tabs = st.tabs(["📊 Group Summary", "📋 Daily Ledger", "📥 New Entry", "🛠️ Admin Control", "🔌 System Health"])

# --- TAB 1: GROUP SUMMARY ----------------------------------------------------
with tabs[0]:
    if df_expenses.empty:
        st.markdown(hero_html("Group Summary", "No records yet. Add one from the New Entry tab."), unsafe_allow_html=True)
    else:
        spend = df_expenses[df_expenses["entry_type"] == "charge"].groupby("category")["amount"].sum()
        totals = ledger.summarize(df_expenses)
        st.markdown(hero_html("Group Summary",
                              f"Dues, credits and payments across {len(all_users)} members, live from your database.",
                              split_bar_html(float(spend.get("Food", 0)), float(spend.get("Sports", 0)))),
                    unsafe_allow_html=True)
        st.markdown(tiles_html([
            ("Outstanding dues", f"{totals['due'].sum():,.2f}", CURRENCY, "due"),
            ("Credit held", f"{totals['credit'].sum():,.2f}", CURRENCY, "cr"),
            ("Total received", f"{totals['total_payments'].sum():,.2f}", CURRENCY, ""),
            ("Records", f"{len(df_expenses):,}", "All time", ""),
        ]), unsafe_allow_html=True)
        ents = entity_balances()
        st.markdown('<div class="cols">' + member_cards_html(ents) + net_bars_html(ents) + "</div>",
                    unsafe_allow_html=True)
        st.markdown(breakup_table_html(df_view), unsafe_allow_html=True)

# --- TAB 2: DAILY LEDGER -----------------------------------------------------
with tabs[1]:
    st.markdown(bar_html("Daily Ledger", "Charges and payments, newest date first"), unsafe_allow_html=True)
    if df_expenses.empty:
        st.info("Nothing to show yet.")
    else:
        lf1, lf2 = st.columns([2, 2])
        sel_user = lf1.selectbox("Filter by member", ["All"] + all_users, key="ledger_user")
        sel_status = lf2.radio("Show", ["All", "Open only", "Paid only"], horizontal=True, key="ledger_status")
        ledger_df = df_view if sel_user == "All" else df_view[df_view["user_name"] == sel_user]
        if sel_status == "Open only":
            ledger_df = ledger_df[(ledger_df["entry_type"] == "charge") & (ledger_df["status"] != "paid")]
        elif sel_status == "Paid only":
            ledger_df = ledger_df[(ledger_df["entry_type"] == "charge") & (ledger_df["status"] == "paid")]
        if ledger_df.empty:
            st.info("No entries match these filters.")
        else:
            st.markdown(ledger_html(ledger_df), unsafe_allow_html=True)

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
                        entries.append({"entry_date": event_date, "user": person, "category": cat,
                                        "activity": act, "amount": share})
            if problems:
                for msg in problems:
                    st.error(msg)
            elif not entries:
                st.warning("Nothing to record.")
            else:
                try:
                    ledger.add_charges(conn, entries)
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
                "Credit / Payments", "Member Settlement", "Group Payments", "Void Entries",
                "Categories Management", "User Management", "Bulk Operations"])
        with h2:
            st.write(" ")
            if st.button("Logout", use_container_width=True):
                st.session_state.authenticated = False
                st.rerun()
        st.divider()

        # ---- Credit / Payments ----
        if admin_task == "Credit / Payments":
            st.subheader("💳 Payment / Advance Credit")
            st.caption("Each payment is saved as its own new row and clears the member's oldest dues first. "
                       "Anything above their dues stays as credit. Nothing already saved is changed.")
            if st.session_state.get("flash"):
                st.success(st.session_state.pop("flash"))
            if not all_users:
                st.info("Add members first.")
            else:
                cc1, cc2, cc3 = st.columns([2, 2, 2])
                c_user = cc1.selectbox("Member", all_users, key="cr_user")
                c_amt = cc2.number_input(f"Amount ({CURRENCY})", min_value=0.0, step=5.0, key="cr_amt")
                c_date = cc3.date_input("Payment Date", value=date.today(), key="cr_date")

                bal = money(balances.get(c_user, 0.0))
                p1, p2, p3 = st.columns(3)
                p1.metric("Current Dues" if bal >= 0 else "Current Credit", f"{abs(bal):.2f} {CURRENCY}")
                if c_amt > 0:
                    after = bal - money(c_amt)
                    p2.metric("Dues Cleared", f"{min(max(bal, Decimal('0')), money(c_amt)):.2f} {CURRENCY}")
                    p3.metric("Credit After" if after <= 0 else "Still Due After", f"{abs(after):.2f} {CURRENCY}")

                if st.button("Apply Payment", type="primary", disabled=c_amt <= 0, key="cr_apply"):
                    try:
                        r = ledger.record_payment(conn, c_user, c_amt, c_date)
                        msg = f"Recorded {r['paid']:.2f} {CURRENCY} from {c_user}; cleared {r['cleared']:.2f} of dues. "
                        msg += (f"Credit remaining: {r['credit']:.2f} {CURRENCY}." if r["credit"] > 0
                                else f"Still due: {r['due']:.2f} {CURRENCY}." if r["due"] > 0
                                else "Fully settled.")
                        st.session_state["flash"] = msg
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not record payment: {e}")

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
                            ledger.settle_balance(conn, user, date.today())
                            st.rerun()
                    else:
                        st.metric("Credit Held", f"{abs(net):.2f} {CURRENCY}")
                        if st.button(f"Refund credit to {user}", key=f"adm_ref_{user}"):
                            ledger.refund_credit(conn, user, date.today())
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
                            ledger.settle_balance(conn, m, date.today(), "Group Settlement")
                        st.rerun()
                    elif not payers:
                        st.caption("Only credit held, nothing to collect.")
            render_in_grid(ents, 3, _group_card)

        # ---- Void Entries ----
        elif admin_task == "Void Entries":
            st.subheader("🧾 Void a mistaken entry")
            st.caption("Entries are never edited or deleted. Voiding cancels a row: it stays in the database "
                       "but no longer counts towards any balance. Re-enter the correct entry from New Entry or Credit / Payments.")
            if df_expenses.empty:
                st.info("No records.")
            else:
                vf1, vf2 = st.columns(2)
                v_user = vf1.selectbox("Member", all_users, key="void_user")
                v_df = df_view[df_view["user_name"] == v_user].sort_values(["entry_date", "id"], ascending=False)
                v_date = vf2.selectbox("Date", ["All"] + sorted(v_df["entry_date"].unique(), reverse=True), key="void_date")
                if v_date != "All":
                    v_df = v_df[v_df["entry_date"] == v_date]
                st.dataframe(v_df[["id", "entry_date", "entry_type", "category", "activity", "amount", "status"]],
                             hide_index=True, use_container_width=True)
                labels = {int(r.id): f"#{int(r.id)} · {r.entry_date} · {r.entry_type} · {r.category} · {r.amount:.2f}"
                          for r in v_df.itertuples()}
                if labels:
                    v_id = st.selectbox("Entry to void", list(labels), format_func=labels.get, key="void_id")
                    v_reason = st.text_input("Reason (required)", key="void_reason")
                    if st.button("Void entry", type="primary", disabled=not v_reason.strip(), key="void_go"):
                        try:
                            ledger.void_entry(conn, v_id, v_reason)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not void entry: {e}")

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
            st.caption("CSV columns: entry_date, user_name, category, amount (always positive). "
                       "Optional: activity, entry_type ('charge' or 'payment', default charge).")
            csv_file = st.file_uploader("Upload entries CSV", type="csv")
            if csv_file:
                up_df = pd.read_csv(csv_file)
                missing = {"entry_date", "user_name", "category", "amount"} - set(up_df.columns)
                if missing:
                    st.error(f"Missing columns: {', '.join(sorted(missing))}")
                else:
                    up_df["entry_date"] = pd.to_datetime(up_df["entry_date"]).dt.date
                    up_df["amount"] = pd.to_numeric(up_df["amount"], errors="coerce")
                    if "entry_type" not in up_df.columns:
                        up_df["entry_type"] = "charge"
                    up_df["entry_type"] = up_df["entry_type"].astype(str).str.strip().str.lower()
                    if "activity" not in up_df.columns:
                        up_df["activity"] = None
                    problems = []
                    if up_df["amount"].isna().any() or (up_df["amount"] <= 0).any():
                        problems.append("Every amount must be a number greater than zero.")
                    if (~up_df["entry_type"].isin(["charge", "payment"])).any():
                        problems.append("entry_type must be 'charge' or 'payment'.")
                    for msg in problems:
                        st.error(msg)
                    if not problems:
                        st.dataframe(up_df.head(20), use_container_width=True)
                        if st.button("Confirm Bulk Upload"):
                            try:
                                ledger.add_entries(conn, [
                                    {"entry_date": r.entry_date, "user": r.user_name, "entry_type": r.entry_type,
                                     "category": r.category, "activity": None if pd.isna(r.activity) else r.activity,
                                     "amount": r.amount} for r in up_df.itertuples()])
                                st.success(f"Imported {len(up_df)} rows.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Import failed, nothing was saved: {e}")
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
