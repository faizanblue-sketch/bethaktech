import hmac
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import text, bindparam

# =============================================================================
# 1. APP SETUP & ARMOURY CRATE STYLING (CSS)
# =============================================================================
st.set_page_config(
    page_title="ARMOURY CRATE // Sports & Food Tracker",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom Cyberpunk / ASUS ROG Armoury Crate CSS Styling
st.markdown("""
<style>
    /* Dark Cyberpunk Background */
    .stApp {
        background-color: #0b0e14;
        color: #c5c8c6;
        font-family: 'Consolas', 'Segoe UI', monospace;
    }
    
    /* Hide Default Headers & Footers */
    header, footer {visibility: hidden;}
    
    /* Top Banner / Telemetry Bar */
    .armoury-header {
        background: linear-gradient(90deg, #121824 0%, #1a2332 50%, #121824 100%);
        border: 1px solid #00f0ff33;
        border-left: 4px solid #ff003c;
        padding: 15px 25px;
        border-radius: 4px;
        margin-bottom: 25px;
        box-shadow: 0 0 15px rgba(0, 240, 255, 0.1);
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .armoury-title {
        font-size: 22px;
        font-weight: 800;
        letter-spacing: 2px;
        color: #ffffff;
        text-transform: uppercase;
        text-shadow: 0 0 8px rgba(255, 0, 60, 0.6);
    }
    .telemetry-badge {
        background: #00f0ff1a;
        border: 1px solid #00f0ff;
        color: #00f0ff;
        padding: 4px 10px;
        font-size: 11px;
        border-radius: 2px;
        letter-spacing: 1px;
        font-weight: bold;
    }
    
    /* Container & Card Styling */
    div[data-testid="stVerticalBlock"] > div[data-testid="stBlock"] {
        background: #121824;
        border: 1px solid #1f293d;
        border-radius: 4px;
        padding: 12px;
        box-shadow: inset 0 0 10px rgba(0, 0, 0, 0.5);
    }
    
    /* Metric Cards */
    div[data-testid="stMetricValue"] {
        font-size: 26px !important;
        font-weight: 700 !important;
        color: #00f0ff !important;
        text-shadow: 0 0 10px rgba(0, 240, 255, 0.4);
    }
    div[data-testid="stMetricLabel"] {
        color: #8899a6 !important;
        font-size: 11px !important;
        letter-spacing: 1px;
        text-transform: uppercase;
    }
    
    /* Tabs Styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background-color: #0b0e14;
    }
    .stTabs [data-baseweb="tab"] {
        height: 45px;
        background-color: #121824;
        border: 1px solid #1f293d;
        border-radius: 4px 4px 0px 0px;
        color: #8899a6;
        font-weight: 600;
        letter-spacing: 1px;
        padding: 0px 20px;
    }
    .stTabs [aria-selected="true"] {
        background-color: #1a2332 !important;
        color: #ff003c !important;
        border-bottom: 3px solid #ff003c !important;
        border-color: #00f0ff #1f293d #ff003c #1f293d !important;
        text-shadow: 0 0 8px rgba(255, 0, 60, 0.5);
    }
    
    /* Status Badges */
    .status-due {
        color: #ff003c;
        font-weight: bold;
        background: #ff003c1a;
        padding: 2px 8px;
        border: 1px solid #ff003c;
        border-radius: 2px;
    }
    .status-credit {
        color: #00f0ff;
        font-weight: bold;
        background: #00f0ff1a;
        padding: 2px 8px;
        border: 1px solid #00f0ff;
        border-radius: 2px;
    }
    .status-settled {
        color: #00ff66;
        font-weight: bold;
        background: #00ff661a;
        padding: 2px 8px;
        border: 1px solid #00ff66;
        border-radius: 2px;
    }

    /* Buttons Styling */
    .stButton > button, .stDownloadButton > button {
        background: #1a2332 !important;
        color: #00f0ff !important;
        border: 1px solid #00f0ff !important;
        font-family: 'Consolas', monospace !important;
        font-weight: bold !important;
        letter-spacing: 1px !important;
        transition: all 0.2s ease-in-out;
    }
    .stButton > button:hover {
        background: #00f0ff !important;
        color: #0b0e14 !important;
        box-shadow: 0 0 10px rgba(0, 240, 255, 0.5);
    }

    /* Dataframe Styling */
    div[data-testid="stDataFrame"] {
        border: 1px solid #1f293d;
        background-color: #121824;
    }
</style>
""", unsafe_allow_html=True)

# Database Connection & Constants
conn = st.connection("postgresql", type="sql")
CURRENCY = "QAR"
CREDIT_CATEGORY = "Credit"
EXPENSE_COLS = ["id", "entry_date", "user_name", "category", "activity", "amount", "paid_status"]

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
def money(value) -> Decimal:
    """Convert numeric values to 2-decimal Decimal."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)

def split_amount(total, n_people):
    """Split total into n exact shares."""
    total = money(total)
    base = (total / n_people).quantize(CENT, rounding=ROUND_HALF_UP)
    shares = [base] * n_people
    shares[0] += total - sum(shares)
    return shares

def plan_consumption(rows, budget):
    """Calculate payment actions for open rows."""
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
    """Match open credit against open dues, oldest first."""
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
            session.execute(text("""
                INSERT INTO expenses (entry_date, user_name, category, activity, amount, paid_status)
                SELECT entry_date, user_name, category, activity, :rem, 0 FROM expenses WHERE id = :id
            """), {"id": rid, "rem": float(open_part)})
            session.execute(
                text("UPDATE expenses SET amount = :paid, paid_status = 1 WHERE id = :id"),
                {"id": rid, "paid": float(paid_part)},
            )

def settle_user(session, user):
    """Match a member's open credit against open dues."""
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
    """Record money received and automatically settle previous dues."""
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
    """Match open credit against open dues for users."""
    result = {}
    with conn.session as s:
        for u in users:
            result[u] = settle_user(s, u)
        s.commit()
    return result


# =============================================================================
# 3. DATABASE SCHEMA INITIALIZATION
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
    with conn.session as s:
        s.execute(text(sql), params or {})
        s.commit()

df_expenses = load_expense_data()
saved_participants = load_participants()
all_users = sorted(set(saved_participants) | set(df_expenses["user_name"].dropna().unique().tolist()))
sports_activities = load_activities("Sports", DEFAULT_SPORTS)
food_activities = load_activities("Food", DEFAULT_FOOD)

open_df = df_expenses[df_expenses["paid_status"] == 0]
balances = open_df.groupby("user_name")["amount"].sum().round(2).to_dict()

def entity_balances():
    ents = [{"name": g, "members": m, "balance": round(sum(balances.get(x, 0) for x in m), 2),
             "caption": ", ".join(m)} for g, m in GROUPS.items()]
    ents += [{"name": u, "members": [u], "balance": round(balances.get(u, 0), 2), "caption": "Individual"}
             for u in all_users if u not in GROUPED_USERS]
    return ents

def balance_label(bal):
    if bal > 0.005:
        return f"<span class='status-due'>🔴 {bal:.2f} {CURRENCY} DUE</span>"
    if bal < -0.005:
        return f"<span class='status-credit'>💳 {abs(bal):.2f} {CURRENCY} CREDIT</span>"
    return "<span class='status-settled'>🟢 SETTLED</span>"


# =============================================================================
# 5. ARMOURY CRATE TOP TELEMETRY BAR
# =============================================================================
st.markdown(f"""
<div class="armoury-header">
    <div>
        <span class="armoury-title">⚡ ROG ARMOURY // SPORTS & FOOD TRACKER</span>
        <div style="font-size: 11px; color: #8899a6; margin-top: 4px;">
            SYSTEM CORE: ONLINE | DATABASE STATUS: CONNECTED
        </div>
    </div>
    <div style="display: flex; gap: 12px; align-items: center;">
        <span class="telemetry-badge">CPU: 12%</span>
        <span class="telemetry-badge">GPU: 42°C</span>
        <span class="telemetry-badge">DB LATENCY: 1.2ms</span>
    </div>
</div>
""", unsafe_allow_html=True)


# =============================================================================
# 6. TABS NAVIGATION
# =============================================================================
tabs = st.tabs([
    "📊 SYSTEM SUMMARY", 
    "📋 LEDGER LOGS", 
    "📥 DATA INPUT", 
    "🛠️ CONTROL CENTER", 
    "🔌 DIAGNOSTICS"
])

# --- TAB 1: SYSTEM SUMMARY ---------------------------------------------------
with tabs[0]:
    st.markdown("##### ⚡ TELEMETRY OVERVIEW")
    if df_expenses.empty:
        st.info("No records yet. Add records from the DATA INPUT tab.")
    else:
        total_dues = sum(v for v in balances.values() if v > 0)
        total_credit = -sum(v for v in balances.values() if v < 0)
        settled_total = df_expenses[(df_expenses["paid_status"] == 1) &
                                    (df_expenses["category"] != CREDIT_CATEGORY)]["amount"].sum()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("OUTSTANDING DUES", f"{total_dues:.2f} {CURRENCY}")
        m2.metric("CREDIT HELD", f"{total_credit:.2f} {CURRENCY}")
        m3.metric("TOTAL SETTLED", f"{settled_total:.2f} {CURRENCY}")
        m4.metric("TOTAL LOGS", f"{len(df_expenses)} RECORDS")

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("##### 👤 MEMBER STATUS HUD")
        
        ents = entity_balances()
        cols = st.columns(min(len(ents), 4) if ents else 1)
        for idx, e in enumerate(ents):
            with cols[idx % 4]:
                st.markdown(f"""
                <div style="background: #121824; border: 1px solid #1f293d; padding: 12px; border-radius: 4px; margin-bottom: 10px;">
                    <div style="font-weight: bold; color: #fff;">{e['name']}</div>
                    <div style="font-size: 11px; color: #8899a6;">{e['caption']}</div>
                    <div style="margin-top: 8px;">{balance_label(e['balance'])}</div>
                </div>
                """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        g1, g2 = st.columns([1, 2])
        with g1:
            dues_only = open_df[(open_df["amount"] > 0)]
            if dues_only.empty:
                st.info("No open dues 🎉")
            else:
                fig_pie = px.pie(
                    dues_only, values="amount", names="category", hole=0.6,
                    color_discrete_sequence=["#ff003c", "#00f0ff"],
                    title="OPEN DUES SPLIT"
                )
                fig_pie.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color="#c5c8c6", family="Consolas"),
                    margin=dict(l=10, r=10, t=40, b=10)
                )
                st.plotly_chart(fig_pie, use_container_width=True)
                
        with g2:
            df_bar = pd.DataFrame({"Entity": [e["name"] for e in ents], "Balance": [e["balance"] for e in ents]})
            fig_bar = px.bar(
                df_bar, x="Entity", y="Balance", color="Entity", text="Balance",
                color_discrete_sequence=["#ff003c", "#00f0ff", "#00ff66"],
                title="NET BALANCES BY ENTITY (QAR)"
            )
            fig_bar.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#c5c8c6", family="Consolas"),
                xaxis=dict(showgrid=False), yaxis=dict(gridcolor="#1f293d"),
                margin=dict(l=10, r=10, t=40, b=10)
            )
            fig_bar.update_traces(texttemplate="<b>%{text:.1f}</b>", textposition="outside")
            st.plotly_chart(fig_bar, use_container_width=True)

        st.markdown("##### 🗓️ INDIVIDUAL BREAKUP MATRIX")
        per_user_day = (df_expenses.groupby(["entry_date", "user_name"])
                        .agg(amount=("amount", "sum"), paid=("paid_status", "min")).reset_index())
        per_user_day["display"] = per_user_day.apply(
            lambda r: f"{r['amount']:.1f}{'✅' if r['paid'] == 1 else '❌'}", axis=1)
        pivot = per_user_day.pivot(index="entry_date", columns="user_name", values="display").fillna("-")
        cat_totals = df_expenses.groupby(["entry_date", "category"])["amount"].sum().unstack(fill_value=0)
        for cat in ["Sports", "Food", CREDIT_CATEGORY]:
            pivot[f"Σ {cat}"] = cat_totals[cat].map("{:.1f}".format) if cat in cat_totals.columns else "0.0"
        pivot["TOTAL DAY"] = df_expenses.groupby("entry_date")["amount"].sum().map("{:.1f}".format)
        st.dataframe(pivot.sort_index(ascending=False), use_container_width=True)

# --- TAB 2: LEDGER LOGS ------------------------------------------------------
with tabs[1]:
    st.markdown("##### 📋 SYSTEM LEDGER HISTORY")
    if df_expenses.empty:
        st.info("Nothing to show yet.")
    else:
        sel_user = st.selectbox("Filter by member", ["All"] + all_users, key="ledger_user")
        ledger = df_expenses if sel_user == "All" else df_expenses[df_expenses["user_name"] == sel_user]
        for d in sorted(ledger["entry_date"].unique(), reverse=True):
            with st.expander(f"🗓️ {d}"):
                day_df = ledger[ledger["entry_date"] == d].copy()
                day_df["Status"] = day_df["paid_status"].map({0: "❌ Open", 1: "✅ Paid"})
                st.dataframe(day_df[["user_name", "category", "activity", "amount", "Status"]],
                             hide_index=True, use_container_width=True)

# --- TAB 3: DATA INPUT -------------------------------------------------------
with tabs[2]:
    st.markdown("##### 📥 NEW TRANSACTION RECORDING")
    with st.form("expense_form", clear_on_submit=True):
        event_date = st.date_input("Event Date", value=date.today())
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("###### ⚽ SPORTS MODULE")
            sports_act = st.selectbox("Activity", options=sports_activities, key="sports_activity")
            sports_total = st.number_input(f"Total Amount ({CURRENCY})", min_value=0.0, step=5.0, key="sports_total")
            sports_sel = st.multiselect("Select Players", options=all_users, key="sports_players")
        with c2:
            st.markdown("###### 🍲 FOOD MODULE")
            food_act = st.selectbox("Activity", options=food_activities, key="food_activity")
            food_total = st.number_input(f"Total Amount ({CURRENCY})", min_value=0.0, step=5.0, key="food_total")
            food_sel = st.multiselect("Select Consumers", options=all_users, key="food_players")

        if st.form_submit_button("SUBMIT RECORD"):
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
                        for person in {e["u"] for e in entries}:
                            settle_user(s, person)
                        s.commit()
                    st.success("✅ Recorded & Settled!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save: {e}")

# --- TAB 4: CONTROL CENTER ---------------------------------------------------
with tabs[3]:
    st.markdown("##### 🛠️ ADMIN CONTROL CENTER")

    if not st.session_state.get("authenticated"):
        with st.form("admin_login"):
            pin = st.text_input("Enter Admin PIN", type="password")
            if st.form_submit_button("AUTHENTICATE"):
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
            if st.button("LOGOUT", use_container_width=True):
                st.session_state.authenticated = False
                st.rerun()
        st.divider()

        # ---- Credit / Payments ----
        if admin_task == "Credit / Payments":
            st.markdown("###### 💳 CREDIT & ADVANCE PAYMENTS")
            st.caption("Clears oldest open dues automatically. Remaining credit stays on balance.")
            
            if st.session_state.get("flash"):
                st.success(st.session_state.pop("flash"))

            credit_open = open_df[open_df["amount"] < 0].groupby("user_name")["amount"].sum().abs()
            dues_open = open_df[open_df["amount"] > 0].groupby("user_name")["amount"].sum()
            pending = sorted(set(credit_open.index) & set(dues_open.index))
            if pending:
                st.warning("Members with open credit and pending dues detected.")
                st.dataframe(pd.DataFrame({
                    "Member": pending,
                    "Open Dues": [round(dues_open[u], 2) for u in pending],
                    "Open Credit": [round(credit_open[u], 2) for u in pending],
                    "Will Be Cleared": [round(min(dues_open[u], credit_open[u]), 2) for u in pending],
                    "Credit Left": [round(max(credit_open[u] - dues_open[u], 0), 2) for u in pending],
                }), hide_index=True, use_container_width=True)
                if st.button("🔄 RECONCILE CREDITS", key="cr_reconcile"):
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
                cc1, cc2, cc3 = st.columns(3)
                c_user = cc1.selectbox("Member", all_users, key="cr_user")
                c_amt = cc2.number_input(f"Amount ({CURRENCY})", min_value=0.0, step=5.0, key="cr_amt")
                c_date = cc3.date_input("Payment Date", value=date.today(), key="cr_date")

                bal = balances.get(c_user, 0.0)
                p1, p2, p3 = st.columns(3)
                p1.metric("Current Balance", f"{abs(bal):.2f} {CURRENCY}")
                if c_amt > 0:
                    pay = money(c_amt)
                    dues_now = max(money(bal), Decimal("0"))
                    credit_now = max(-money(bal), Decimal("0"))
                    pool = pay + credit_now
                    p2.metric("Dues Cleared", f"{min(pool, dues_now):.2f} {CURRENCY}")
                    p3.metric("Credit Remaining", f"{max(pool - dues_now, Decimal('0')):.2f} {CURRENCY}")

                if st.button("APPLY CREDIT", disabled=c_amt <= 0, key="cr_apply"):
                    try:
                        r = apply_payment(c_user, c_amt, c_date)
                        msg = f"Cleared {r['settled']:.2f} {CURRENCY} for {c_user}. "
                        msg += (f"Remaining credit: {abs(r['after']):.2f} {CURRENCY}." if r["after"] < 0
                                else f"Still due: {r['after']:.2f} {CURRENCY}." if r["after"] > 0
                                else "Fully settled.")
                        st.session_state["flash"] = msg
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not apply credit: {e}")

        # ---- Member Settlement ----
        elif admin_task == "Member Settlement":
            st.markdown("###### 💰 MEMBER SETTLEMENT")
            active = [(u, balances[u]) for u in all_users if abs(balances.get(u, 0)) > 0.005]
            if not active:
                st.success("Everyone is settled 🎉")
            else:
                scols = st.columns(2)
                for idx, (user, net) in enumerate(active):
                    with scols[idx % 2]:
                        with st.container():
                            st.markdown(f"**👤 {user}**")
                            if net > 0:
                                st.metric("Dues", f"{net:.2f} {CURRENCY}")
                                if st.button(f"Settle Dues for {user}", key=f"adm_set_{user}"):
                                    apply_payment(user, net, date.today(), "Settlement Payment")
                                    st.rerun()
                            else:
                                st.metric("Credit Held", f"{abs(net):.2f} {CURRENCY}")
                                if st.button(f"Refund Credit to {user}", key=f"adm_ref_{user}"):
                                    run_write("UPDATE expenses SET paid_status = 1 "
                                              "WHERE user_name = :u AND paid_status = 0 AND amount < 0", {"u": user})
                                    st.rerun()

        # ---- Group Payments ----
        elif admin_task == "Group Payments":
            st.markdown("###### 🤝 CONSOLIDATED GROUP PAYMENTS")
            ents = [e for e in entity_balances() if abs(e["balance"]) > 0.005]
            if not ents:
                st.success("Everyone is settled 🎉")
            else:
                gcols = st.columns(3)
                for idx, e in enumerate(ents):
                    with gcols[idx % 3]:
                        with st.container():
                            st.markdown(f"**{e['name']}**")
                            st.metric("Balance", f"{e['balance']:.2f} {CURRENCY}")
                            payers = [m for m in e["members"] if balances.get(m, 0) > 0.005]
                            if payers and st.button(f"Settle Group", key=f"adm_grp_{e['name']}", use_container_width=True):
                                for m in payers:
                                    apply_payment(m, balances[m], date.today(), "Group Settlement")
                                st.rerun()

        # ---- Modify Records ----
        elif admin_task == "Modify Records":
            if df_expenses.empty:
                st.info("No records.")
            for d in sorted(df_expenses["entry_date"].unique(), reverse=True):
                with st.container():
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
                        run_write("UPDATE expenses SET paid_status = 1 WHERE entry_date = :d AND amount > 0", {"d": str(d)})
                        st.rerun()
                    if b2.button("💾 Save Changes", key=f"sv_{d}"):
                        try:
                            orig = date_df.set_index("id")
                            kept_ids = set(edited["id"].dropna().astype(int))
                            touched = set()
                            with conn.session as s:
                                for rid in set(orig.index) - kept_ids:
                                    touched.add(orig.loc[rid, "user_name"])
                                    s.execute(text("DELETE FROM expenses WHERE id = :id"), {"id": int(rid)})
                                for _, row in edited.iterrows():
                                    if pd.isna(row["user_name"]) or pd.isna(row["amount"]):
                                        continue
                                    vals = {"u": row["user_name"], "c": "Food" if pd.isna(row["category"]) else row["category"],
                                            "a": None if pd.isna(row["activity"]) else row["activity"], "am": float(row["amount"]),
                                            "p": int(bool(row["paid_status"]))}
                                    touched.add(row["user_name"])
                                    if pd.isna(row["id"]):
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
                    with b3.popover("🗑️ Delete Day"):
                        st.warning(f"Delete all entries for {d}?")
                        if st.button("Confirm Delete", key=f"dd_{d}"):
                            run_write("DELETE FROM expenses WHERE entry_date = :d", {"d": str(d)})
                            st.rerun()

        # ---- Categories Management ----
        elif admin_task == "Categories Management":
            st.markdown("###### 📁 CATEGORIES & ACTIVITIES")
            col_a, col_b = st.columns(2)
            for col, cat, icon, acts in ((col_a, "Sports", "⚽", sports_activities),
                                         (col_b, "Food", "🍲", food_activities)):
                with col, st.container():
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
                                st.error(f"Error: {e}")
                    st.write("---")
                    for idx, act in enumerate(acts):
                        r1, r2 = st.columns([3, 1])
                        r1.write(f"• {act}")
                        if r2.button("🗑️", key=f"del_{cat}_{idx}"):
                            run_write("DELETE FROM category_activities WHERE LOWER(TRIM(category)) = LOWER(:c) "
                                      "AND LOWER(TRIM(activity_name)) = LOWER(TRIM(:a));", {"c": cat, "a": act})
                            st.rerun()

        # ---- User Management ----
        elif admin_task == "User Management":
            u1, u2 = st.columns([1, 2])
            with u1:
                nu = st.text_input("New Member Name")
                if st.button("Add Member") and nu.strip():
                    try:
                        run_write("INSERT INTO participants (name) VALUES (:n) ON CONFLICT DO NOTHING", {"n": nu.strip()})
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error adding member: {e}")
            with u2:
                for p in saved_participants:
                    r1, r2 = st.columns([3, 1])
                    bal = balances.get(p, 0)
                    r1.write(f"👤 {p}")
                    if r2.button("🗑️", key=f"adm_du_{p}", disabled=abs(bal) > 0.005):
                        run_write("DELETE FROM participants WHERE name = :n", {"n": p})
                        st.rerun()

        # ---- Bulk Operations ----
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
                    up_df["paid_status"] = pd.to_numeric(up_df.get("paid_status", 0), errors="coerce").fillna(0).astype(int)
                    st.dataframe(up_df.head(20), use_container_width=True)
                    if st.button("Confirm Upload"):
                        up_df.to_sql("expenses", conn.engine, if_exists="append", index=False)
                        st.success(f"Imported {len(up_df)} rows.")
                        st.rerun()
            if not df_expenses.empty:
                st.download_button("📥 Download Backup (CSV)", df_expenses.to_csv(index=False),
                                   "expense_backup.csv", use_container_width=True)

# --- TAB 5: DIAGNOSTICS ------------------------------------------------------
with tabs[4]:
    st.markdown("##### 🔌 SYSTEM DIAGNOSTICS")
    try:
        conn.query("SELECT 1", ttl=0)
        st.success("✅ Database: Connected and Operational")
    except Exception as e:
        st.error(f"❌ Disconnected: {e}")
