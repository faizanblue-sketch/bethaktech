import hmac
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import text

# =============================================================================
# 1. APP SETUP & EXACT ARMOURY-STYLE PREVIEW STYLING (CSS)
# =============================================================================
st.set_page_config(
    page_title="Bethak Tracker — Armoury-style layout",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    /* Dark Theme Core */
    .stApp {
        background-color: #0d1117;
        color: #c9d1d9;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    
    header, footer {visibility: hidden;}
    
    /* Top Armoury Header Bar */
    .armoury-header-bar {
        background: #161b22;
        border: 1px solid #30363d;
        border-left: 4px solid #00f0ff;
        padding: 12px 20px;
        border-radius: 6px;
        margin-bottom: 20px;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .armoury-header-title {
        font-size: 18px;
        font-weight: 800;
        letter-spacing: 1.5px;
        color: #ffffff;
        text-transform: uppercase;
    }
    .armoury-header-sub {
        font-size: 11px;
        color: #8b949e;
        margin-top: 2px;
    }
    .armoury-db-tag {
        background: #23863622;
        border: 1px solid #238636;
        color: #3fb950;
        padding: 3px 10px;
        font-size: 11px;
        border-radius: 12px;
        font-weight: 600;
    }
    
    /* Cards Layout */
    .armoury-card {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 14px;
        margin-bottom: 12px;
    }
    .armoury-card-title {
        font-size: 11px;
        font-weight: 700;
        color: #8b949e;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        margin-bottom: 6px;
    }
    .armoury-card-value {
        font-size: 24px;
        font-weight: 800;
        color: #ffffff;
    }
    .armoury-card-unit {
        font-size: 11px;
        color: #8b949e;
        margin-top: 2px;
    }

    /* Badges */
    .badge-due {
        background: #f8514922;
        border: 1px solid #f85149;
        color: #ff7b72;
        padding: 2px 6px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: bold;
    }
    .badge-credit {
        background: #388bfd22;
        border: 1px solid #388bfd;
        color: #58a6ff;
        padding: 2px 6px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: bold;
    }
    .badge-settled {
        background: #23863622;
        border: 1px solid #238636;
        color: #3fb950;
        padding: 2px 6px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: bold;
    }
    
    /* Custom Sidebar Navigation Styling */
    .stRadio > div {
        background-color: transparent;
    }
    
    /* Global Dataframe Tweaks */
    div[data-testid="stDataFrame"] {
        border: 1px solid #30363d;
        border-radius: 6px;
    }
</style>
""", unsafe_allow_html=True)

# Database Connection & Setup
conn = st.connection("postgresql", type="sql")
CURRENCY = "QAR"
CREDIT_CATEGORY = "Credit"
EXPENSE_COLS = ["id", "entry_date", "user_name", "category", "activity", "amount", "paid_status"]

GROUPS = {
    "Ahmed's group": ["Ahmed", "Arsalan", "Kamran"],
    "Kashif's group": ["Kashif", "Imran"],
}
GROUPED_USERS = {u for members in GROUPS.values() for u in members}

DEFAULT_SPORTS = ["Marvel Court Fee", "Shuttles"]
DEFAULT_FOOD = [
    "Orange Bethak", "Food Bethak", "Tea/Snacks", "Mandi",
    "Other", "Lala Dabar", "Cake Castle", "Family Gathering",
]

CENT = Decimal("0.01")


# =============================================================================
# 2. MONEY & SETTLEMENT ENGINE
# =============================================================================
def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)

def split_amount(total, n_people):
    total = money(total)
    base = (total / n_people).quantize(CENT, rounding=ROUND_HALF_UP)
    shares = [base] * n_people
    shares[0] += total - sum(shares)
    return shares

def plan_consumption(rows, budget):
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


# =============================================================================
# 3. SCHEMA INITIALIZATION & DATA RETRIEVAL
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

def load_expense_data():
    try:
        df = conn.query("SELECT * FROM expenses ORDER BY entry_date, id;", ttl=0)
    except Exception as e:
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
    except Exception:
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
    except Exception:
        return fallback

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


# =============================================================================
# 4. SIDEBAR NAVIGATION
# =============================================================================
with st.sidebar:
    st.markdown("### **SPORTS AND FOOD TRACKER**")
    nav_option = st.radio(
        "Navigation",
        ["Summary", "Ledger", "New", "Admin", "Health"],
        label_visibility="collapsed"
    )

# Header Banner matching Preview Layout
st.markdown(f"""
<div class="armoury-header-bar">
    <div>
        <div class="armoury-header-title">SPORTS AND FOOD TRACKER</div>
        <div class="armoury-header-sub">{nav_option.upper()} VIEW — LIVE SYSTEM TELEMETRY</div>
    </div>
    <div class="armoury-db-tag">● Database connected</div>
</div>
""", unsafe_allow_html=True)


# =============================================================================
# 5. VIEW PAGE ROUTING
# =============================================================================

# --- PAGE 1: SUMMARY ---------------------------------------------------------
if nav_option == "Summary":
    st.markdown("## GROUP SUMMARY")
    st.caption("Dues, credits and settlements across members, live from your database.")

    total_dues = sum(v for v in balances.values() if v > 0)
    total_credit = -sum(v for v in balances.values() if v < 0)
    total_records = len(df_expenses)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(f"""
        <div class="armoury-card">
            <div class="armoury-card-title">OUTSTANDING DUES</div>
            <div class="armoury-card-value">{total_dues:.2f}</div>
            <div class="armoury-card-unit">QAR</div>
        </div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown(f"""
        <div class="armoury-card">
            <div class="armoury-card-title">CREDIT HELD</div>
            <div class="armoury-card-value">{total_credit:.2f}</div>
            <div class="armoury-card-unit">QAR</div>
        </div>
        """, unsafe_allow_html=True)

    with col3:
        st.markdown(f"""
        <div class="armoury-card">
            <div class="armoury-card-title">RECORDS</div>
            <div class="armoury-card-value">{total_records}</div>
            <div class="armoury-card-unit">All time</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    g1, g2 = st.columns([1, 1])

    with g1:
        st.markdown("##### Overall spend split")
        food_sum = df_expenses[df_expenses['category'] == 'Food']['amount'].sum()
        sports_sum = df_expenses[df_expenses['category'] == 'Sports']['amount'].sum()
        total_sum = food_sum + sports_sum

        food_pct = (food_sum / total_sum * 100) if total_sum > 0 else 0
        sports_pct = (sports_sum / total_sum * 100) if total_sum > 0 else 0

        pie_df = pd.DataFrame({
            "Category": ["Food", "Sports"],
            "Amount": [food_sum, sports_sum]
        })
        fig_pie = px.pie(
            pie_df, values="Amount", names="Category", hole=0.6,
            color_discrete_sequence=["#e65c00", "#0088cc"]
        )
        fig_pie.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#c9d1d9"), margin=dict(l=10, r=10, t=10, b=10),
            showlegend=False
        )
        st.plotly_chart(fig_pie, use_container_width=True)
        st.markdown(f"**{food_pct:.1f}% Food ({food_sum:,.0f} QAR)** &nbsp;&nbsp;&nbsp; **{sports_pct:.1f}% Sports ({sports_sum:,.0f} QAR)**")

    with g2:
        st.markdown("##### NET BALANCE")
        ents = entity_balances()
        net_df = pd.DataFrame([{"Member / Entity": e["name"], "Balance (QAR)": f"{e['balance']:.2f}"} for e in ents])
        st.dataframe(net_df, hide_index=True, use_container_width=True)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("##### QUICK STATUS")
    
    q_cols = st.columns(4)
    for idx, e in enumerate(ents):
        with q_cols[idx % 4]:
            if e['balance'] > 0:
                badge = f"<span class='badge-due'>{e['balance']:.2f} due</span>"
            elif e['balance'] < 0:
                badge = f"<span class='badge-credit'>{abs(e['balance']):.2f} credit</span>"
            else:
                badge = "<span class='badge-settled'>Settled</span>"

            st.markdown(f"""
            <div class="armoury-card">
                <div style="font-weight: bold; color: #fff; font-size: 13px;">{e['name']}</div>
                <div style="font-size: 11px; color: #8b949e; margin-bottom: 8px;">{e['caption']}</div>
                <div>{badge}</div>
            </div>
            """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown(f"##### INDIVIDUAL BREAKUP TABLE — MEMBERS ({len(all_users)})")
    
    if not df_expenses.empty:
        per_user_day = (df_expenses.groupby(["entry_date", "user_name"])
                        .agg(amount=("amount", "sum"), paid=("paid_status", "min")).reset_index())
        per_user_day["display"] = per_user_day.apply(
            lambda r: f"{r['amount']:.1f}{'✓' if r['paid'] == 1 else 'X'}", axis=1)
        pivot = per_user_day.pivot(index="entry_date", columns="user_name", values="display").fillna("-")
        cat_totals = df_expenses.groupby(["entry_date", "category"])["amount"].sum().unstack(fill_value=0)
        for cat in ["Sports", "Food", CREDIT_CATEGORY]:
            pivot[f"Σ {cat}"] = cat_totals[cat].map("{:.1f}".format) if cat in cat_totals.columns else "0.0"
        pivot["TOTAL DAY"] = df_expenses.groupby("entry_date")["amount"].sum().map("{:.1f}".format)
        st.dataframe(pivot.sort_index(ascending=False), use_container_width=True)


# --- PAGE 2: LEDGER ----------------------------------------------------------
elif nav_option == "Ledger":
    st.markdown("## DAILY LEDGER")
    
    f1, f2 = st.columns([2, 2])
    with f1:
        sel_user = st.selectbox("FILTER BY MEMBER", ["All members"] + all_users)
    with f2:
        status_filter = st.radio("Status Filter", ["All", "Open only", "Paid only"], horizontal=True)

    ledger_df = df_expenses.copy()
    if sel_user != "All members":
        ledger_df = ledger_df[ledger_df["user_name"] == sel_user]
    
    if status_filter == "Open only":
        ledger_df = ledger_df[ledger_df["paid_status"] == 0]
    elif status_filter == "Paid only":
        ledger_df = ledger_df[ledger_df["paid_status"] == 1]

    if ledger_df.empty:
        st.info("No ledger entries found.")
    else:
        for d in sorted(ledger_df["entry_date"].unique(), reverse=True):
            day_records = ledger_df[ledger_df["entry_date"] == d].copy()
            open_count = (day_records["paid_status"] == 0).sum()
            day_total = day_records["amount"].sum()
            
            status_summary = f"{open_count} OPEN" if open_count > 0 else "ALL PAID"
            
            with st.expander(f"🗓️ {d.strftime('%d %b %Y')}  —  {len(day_records)} entries  |  {day_total:.2f} QAR ({status_summary})"):
                day_records["STATUS"] = day_records["paid_status"].map({0: "X Open", 1: "✔ Paid"})
                st.dataframe(
                    day_records[["user_name", "category", "activity", "amount", "STATUS"]].rename(columns={
                        "user_name": "MEMBER", "category": "CATEGORY", "activity": "ACTIVITY", "amount": "AMOUNT"
                    }),
                    hide_index=True,
                    use_container_width=True
                )


# --- PAGE 3: NEW -------------------------------------------------------------
elif nav_option == "New":
    st.markdown("## RECORD NEW ENTRY")
    with st.form("new_entry_form", clear_on_submit=True):
        event_date = st.date_input("Event Date", value=date.today())
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("##### ⚽ SPORTS MODULE")
            sports_act = st.selectbox("Activity", options=sports_activities, key="sa_form")
            sports_total = st.number_input("Total Amount (QAR)", min_value=0.0, step=5.0, key="st_form")
            sports_sel = st.multiselect("Select Players", options=all_users, key="sp_form")
        with c2:
            st.markdown("##### 🍲 FOOD MODULE")
            food_act = st.selectbox("Activity", options=food_activities, key="fa_form")
            food_total = st.number_input("Total Amount (QAR)", min_value=0.0, step=5.0, key="ft_form")
            food_sel = st.multiselect("Select Consumers", options=all_users, key="fp_form")

        if st.form_submit_button("SUBMIT TRANSACTION"):
            entries = []
            for cat, act, total, people in (("Sports", sports_act, sports_total, sports_sel),
                                            ("Food", food_act, food_total, food_sel)):
                if people and total > 0:
                    for person, share in zip(people, split_amount(total, len(people))):
                        entries.append({"d": str(event_date), "u": person, "c": cat, "a": act,
                                        "am": float(share), "p": 0})
            if entries:
                with conn.session as s:
                    for e in entries:
                        s.execute(text(
                            "INSERT INTO expenses (entry_date, user_name, category, activity, amount, paid_status) "
                            "VALUES (:d, :u, :c, :a, :am, :p)"), e)
                    for person in {e["u"] for e in entries}:
                        settle_user(s, person)
                    s.commit()
                st.success("Entry successfully recorded!")
                st.rerun()


# --- PAGE 4: ADMIN -----------------------------------------------------------
elif nav_option == "Admin":
    st.markdown("## ADMIN CONTROL CENTER")
    if not st.session_state.get("authenticated"):
        with st.form("admin_login"):
            pin = st.text_input("Enter Admin PIN", type="password")
            if st.form_submit_button("Authenticate"):
                try:
                    expected = str(st.secrets["admin"]["pin"])
                except Exception:
                    expected = "1234"
                if hmac.compare_digest(pin.encode(), expected.encode()):
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("Incorrect PIN")
    else:
        st.success("Authenticated as Administrator")
        if st.button("Logout"):
            st.session_state.authenticated = False
            st.rerun()


# --- PAGE 5: HEALTH ----------------------------------------------------------
elif nav_option == "Health":
    st.markdown("## SYSTEM HEALTH & DIAGNOSTICS")
    try:
        conn.query("SELECT 1", ttl=0)
        st.success("✅ PostgreSQL Database Engine Connected")
    except Exception as e:
        st.error(f"❌ Database Disconnected: {e}")
