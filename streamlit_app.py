import streamlit as st
import pandas as pd
from sqlalchemy import text, bindparam
import plotly.express as px
from datetime import date

# --- 1. APP SETUP ---
st.set_page_config(page_title="Sports & Food Tracker", layout="wide")

# --- 2. DATABASE CONNECTION ---
conn = st.connection("postgresql", type="sql")

# Default activity lists for fallback
default_sports = ["Marvel Court Fee", "Shuttles"]
default_food = ["Orange Bethak", "Food Bethak", "Tea/Snacks", "Mandi", "Other", "Lala Dabar", "Cake Castle", "Family Gathering"]

# --- 3. DATABASE SCHEMA INITIALIZATION ---
def init_activities_db():
    try:
        with conn.session as session:
            # Create table if not exists
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS category_activities (
                    id SERIAL PRIMARY KEY,
                    category VARCHAR(50) NOT NULL,
                    activity_name VARCHAR(100) NOT NULL
                );
            """))
            
            # Add Unique Constraint safely
            session.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'unique_category_activity'
                    ) THEN
                        ALTER TABLE category_activities ADD CONSTRAINT unique_category_activity UNIQUE (category, activity_name);
                    END IF;
                END $$;
            """))
            session.commit()
    except Exception as e:
        st.error(f"Error initializing DB schema: {e}")

init_activities_db()

# --- 4. DATA LOADING & INITIALIZATION ---
@st.cache_data(ttl=0)
def load_expense_data():
    try:
        query = "SELECT * FROM expenses;"
        df = conn.query(query, ttl=0)
        if not df.empty:
            df['entry_date'] = pd.to_datetime(df['entry_date']).dt.date
            df['amount'] = pd.to_numeric(df['amount'], errors='coerce').fillna(0.0)
            df['paid_status'] = pd.to_numeric(df['paid_status'], errors='coerce').fillna(0).astype(int)
        return df
    except Exception:
        return pd.DataFrame()

def load_participants():
    try:
        df_p = conn.query("SELECT name FROM participants ORDER BY name ASC;", ttl=0)
        return df_p['name'].tolist() if not df_p.empty else []
    except:
        return []

def load_activities(category_name, fallback_list):
    try:
        # Handles spacing, manual formatting, and casing issues using TRIM and LOWER
        query = text("""
            SELECT DISTINCT TRIM(activity_name) as activity_name 
            FROM category_activities 
            WHERE LOWER(TRIM(category)) = LOWER(TRIM(:cat)) 
            ORDER BY activity_name ASC;
        """)
        
        df_act = conn.query(query.text, params={"cat": category_name}, ttl=0)
        
        if not df_act.empty and 'activity_name' in df_act.columns:
            activities = [act for act in df_act['activity_name'].dropna().tolist() if str(act).strip()]
            if activities:
                return activities
        return fallback_list
    except Exception as e:
        st.warning(f"Failed to fetch activities for {category_name}: {e}")
        return fallback_list

df_expenses = load_expense_data()
saved_participants = load_participants()
all_users = sorted(list(set(saved_participants + (df_expenses['user_name'].unique().tolist() if not df_expenses.empty else []))))

# Load dynamically from PostgreSQL DB with explicit matching
sports_activities = load_activities("Sports", default_sports)
food_activities = load_activities("Food", default_food)

# --- 5. TABS ---
tabs = st.tabs([
    "📊 Group Summary", 
    "📋 Daily Ledger", 
    "📥 New Entry", 
    "🛠️ Admin Control",
    "🔌 System Health"
])

# --- TAB 1: GROUP SUMMARY ---
with tabs[0]:
    st.header("📊 Group Analytics")
    if not df_expenses.empty:
        unpaid_only = df_expenses[df_expenses['paid_status'] == 0]
        
        m1, m2, m3 = st.columns(3)
        m1.metric("Net Outstanding", f"{unpaid_only['amount'].sum():.2f} QAR")
        m2.metric("Total Settled", f"{df_expenses[df_expenses['paid_status'] == 1]['amount'].sum():.2f} QAR")
        m3.metric("Records", len(df_expenses))

        # Member Status Logic
        u_sums = unpaid_only.groupby('user_name')['amount'].sum().to_dict()
        ahmed_g, kashif_g = ['Ahmed', 'Arsalan', 'Kamran'], ['Kashif', 'Imran']
        
        dynamic_others = [u for u in all_users if u not in ahmed_g and u not in kashif_g]
        
        st.write("### ⚡ Quick Status")
        scols = st.columns(2 + len(dynamic_others))
        
        with scols[0]:
            with st.container(border=True):
                amt_a = sum(u_sums.get(m, 0) for m in ahmed_g)
                st.markdown(f"**Ahmed's G.**")
                st.caption(f"{', '.join(ahmed_g)}")
                st.markdown(f"{'🔴' if amt_a > 0 else '🟢'} **{amt_a:.1f}**")
        
        with scols[1]:
            with st.container(border=True):
                amt_k = sum(u_sums.get(m, 0) for m in kashif_g)
                st.markdown(f"**Kashif's G.**")
                st.caption(f"{', '.join(kashif_g)}")
                st.markdown(f"{'🔴' if amt_k > 0 else '🟢'} **{amt_k:.1f}**")
        
        for i, user in enumerate(dynamic_others):
            with scols[i+2]:
                with st.container(border=True):
                    amt_u = u_sums.get(user, 0)
                    st.markdown(f"**{user}**")
                    st.caption("Individual")
                    st.markdown(f"{'🔴' if amt_u > 0 else '🟢'} **{amt_u:.1f}**")

        st.divider()
        g1, g2 = st.columns([1, 2])
        with g1:
            st.plotly_chart(px.pie(unpaid_only, values='amount', names='category', hole=0.5, title="Expense Split"), use_container_width=True)
        with g2:
            bar_data = []
            temp_sums = unpaid_only.groupby('user_name')['amount'].sum().to_dict()
            bar_data.append({"Entity": "Ahmed's Group", "Amount": sum(temp_sums.get(m, 0) for m in ahmed_g)})
            bar_data.append({"Entity": "Kashif's Group", "Amount": sum(temp_sums.get(m, 0) for m in kashif_g)})
            
            for u in all_users:
                if u not in ahmed_g and u not in kashif_g:
                    bar_data.append({"Entity": u, "Amount": temp_sums.get(u, 0)})
            
            df_bar = pd.DataFrame(bar_data)
            fig_bar = px.bar(df_bar, x='Entity', y='Amount', title="Dues by Group/Member", color='Entity', text='Amount')
            fig_bar.update_traces(texttemplate='<b>%{text:.1f}</b>', textposition='outside')
            st.plotly_chart(fig_bar, use_container_width=True)

        st.write("### 🗓️ Individual Breakup Table")
        df_piv = df_expenses.copy()
        user_daily_totals = df_piv.groupby(['entry_date', 'user_name']).agg({'amount': 'sum', 'paid_status': 'min'}).reset_index()
        user_daily_totals['display'] = user_daily_totals.apply(lambda x: f"{x['amount']:.1f}{'✅' if x['paid_status'] == 1 else '❌'}", axis=1)
        pivot = user_daily_totals.pivot(index='entry_date', columns='user_name', values='display').fillna("-")
        cat_totals = df_piv.groupby(['entry_date', 'category'])['amount'].sum().unstack(fill_value=0)
        for cat in ['Sports', 'Food', 'Credit']:
            pivot[f"Σ {cat}"] = cat_totals[cat].map("{:.1f}".format) if cat in cat_totals.columns else "0.0"
        pivot['TOTAL DAY'] = df_piv.groupby('entry_date')['amount'].sum().map("{:.1f}".format)
        st.dataframe(pivot.sort_index(ascending=False), use_container_width=True)

# --- TAB 2: DAILY LEDGER ---
with tabs[1]:
    if not df_expenses.empty:
        for d in sorted(df_expenses['entry_date'].unique(), reverse=True):
            with st.expander(f"🗓️ {d}"):
                day_df = df_expenses[df_expenses['entry_date'] == d].copy()
                day_df['Status'] = day_df['paid_status'].map({0: "❌ Unpaid", 1: "✅ Paid"})
                st.table(day_df[['user_name', 'category', 'activity', 'amount', 'Status']])

# --- TAB 3: NEW ENTRY ---
with tabs[2]:
    st.header("Add New Record")
    with st.form("expense_form", clear_on_submit=True):
        event_date = st.date_input("Event Date", value=date.today())
        c1, c2 = st.columns(2)
        with c1:
            st.info("⚽ Sports")
            sports_act = st.selectbox("Activity", options=sports_activities, key="sa")
            sports_total = st.number_input("Total Amount (QAR)", min_value=0.0, step=5.0, key="st")
            sports_sel = st.multiselect("Select Players", options=all_users, key="sps")
        with c2:
            st.success("🍲 Food")
            food_act = st.selectbox("Activity", options=food_activities, key="fa")
            food_total = st.number_input("Total Amount (QAR)", min_value=0.0, step=5.0, key="ft")
            food_sel = st.multiselect("Select Consumers", options=all_users, key="fps")
        
        if st.form_submit_button("Submit"):
            entries = []
            if sports_sel and sports_total > 0:
                share = sports_total / len(sports_sel)
                for p in sports_sel: 
                    entries.append({"d": str(event_date), "u": p, "c": "Sports", "a": sports_act, "am": share, "p": 0})
            if food_sel and food_total > 0:
                share = food_total / len(food_sel)
                for p in food_sel: 
                    entries.append({"d": str(event_date), "u": p, "c": "Food", "a": food_act, "am": share, "p": 0})
            
            if entries:
                try:
                    with conn.session as s:
                        for e in entries:
                            s.execute(text("INSERT INTO expenses (entry_date, user_name, category, activity, amount, paid_status) VALUES (:d, :u, :c, :a, :am, :p)"), e)
                        s.commit()
                    st.success("✅ Recorded!")
                    st.rerun()
                except Exception as e: 
                    st.error(e)

# --- TAB 4: ADMIN CONTROL CENTER ---
with tabs[3]:
    st.header("🛠️ Admin Control Center")
    if 'authenticated' not in st.session_state or not st.session_state.authenticated:
        with st.form("admin_login"):
            pin = st.text_input("Enter Admin PIN", type="password")
            if st.form_submit_button("Login"):
                if pin == st.secrets["admin"]["pin"]:
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("Incorrect PIN")
    else:
        col_hdr1, col_hdr2 = st.columns([3, 1])
        with col_hdr1:
            admin_task = st.selectbox(
                "Select Administrative Task", 
                ["Modify Records", "Categories Management", "Member Settlement", "Group Payments", "User Management", "Bulk Operations"]
            )
        with col_hdr2:
            st.write(" ") 
            if st.button("Logout", use_container_width=True):
                st.session_state.authenticated = False
                st.rerun()
        
        st.divider()

        if admin_task == "Modify Records":
            if not df_expenses.empty:
                for d in sorted(df_expenses['entry_date'].unique(), reverse=True):
                    with st.container(border=True):
                        db_date_str = d.strftime('%Y-%m-%d')
                        st.markdown(f"### 📅 Entries for {d}")
                        with st.expander(f"➕ Add Credit/Advance Payment"):
                            c_col1, c_col2, c_col3 = st.columns([2, 2, 1])
                            c_user = c_col1.selectbox("User", options=all_users, key=f"c_u_{d}")
                            c_amt = c_col2.number_input("Advance Amount", min_value=0.0, step=1.0, key=f"c_a_{d}")
                            if c_col3.button("Apply Credit", key=f"c_b_{d}"):
                                with conn.session as session:
                                    session.execute(text("INSERT INTO expenses (entry_date, user_name, category, activity, amount, paid_status) VALUES (:d, :u, 'Credit', 'Advance Payment', :a, 0)"), {"d": db_date_str, "u": c_user, "a": -c_amt})
                                    session.commit()
                                st.rerun()
                        
                        date_df = df_expenses[df_expenses['entry_date'] == d]
                        edited_df = st.data_editor(date_df, column_config={"id": None, "entry_date": None, "paid_status": st.column_config.CheckboxColumn("Paid?")}, hide_index=True, use_container_width=True, key=f"ed_{d}")
                        
                        b1, b2, b3, _ = st.columns([1, 1, 1, 2])
                        if b1.button("✅ Paid All", key=f"pa_{d}"):
                            with conn.session as session:
                                session.execute(text("UPDATE expenses SET paid_status = 1 WHERE entry_date = :d"), {"d": db_date_str})
                                session.commit()
                            st.rerun()
                        if b2.button("💾 Save", key=f"sv_{d}"):
                            with conn.session as session:
                                session.execute(text("DELETE FROM expenses WHERE entry_date = :d"), {"d": db_date_str})
                                if 'id' in edited_df.columns: edited_df = edited_df.drop(columns=['id'])
                                edited_df.to_sql('expenses', conn.engine, if_exists='append', index=False)
                                session.commit()
                            st.rerun()
                        if b3.button("🗑️ Delete", key=f"dd_{d}"):
                            with conn.session as session:
                                session.execute(text("DELETE FROM expenses WHERE entry_date = :d"), {"d": db_date_str})
                                session.commit()
                            st.rerun()

        elif admin_task == "Categories Management":
            st.subheader("📁 Categories & Activities Management")

            col_cat1, col_cat2 = st.columns(2)

            # --- Sports Activities Management ---
            with col_cat1:
                with st.container(border=True):
                    st.markdown("### ⚽ Sports Activities")
                    
                    with st.form("add_sports_act", clear_on_submit=True):
                        new_sports_act = st.text_input("New Sports Activity")
                        if st.form_submit_button("Add Activity"):
                            act_clean = new_sports_act.strip()
                            if act_clean:
                                try:
                                    with conn.session as session:
                                        session.execute(
                                            text("INSERT INTO category_activities (category, activity_name) VALUES ('Sports', :act) ON CONFLICT (category, activity_name) DO NOTHING;"),
                                            {"act": act_clean}
                                        )
                                        session.commit()
                                    st.success(f"Added '{act_clean}'")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error adding activity: {e}")
                    
                    st.write("---")
                    st.markdown("**Existing DB Activities:**")
                    for idx, act in enumerate(sports_activities):
                        a_col1, a_col2 = st.columns([3, 1])
                        a_col1.write(f"• {act}")
                        if a_col2.button("🗑️", key=f"del_sports_{idx}_{act}"):
                            try:
                                with conn.session as session:
                                    session.execute(
                                        text("DELETE FROM category_activities WHERE LOWER(TRIM(category)) = 'sports' AND LOWER(TRIM(activity_name)) = LOWER(TRIM(:act));"),
                                        {"act": act}
                                    )
                                    session.commit()
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error: {e}")

            # --- Food Activities Management ---
            with col_cat2:
                with st.container(border=True):
                    st.markdown("### 🍲 Food Activities")
                    
                    with st.form("add_food_act", clear_on_submit=True):
                        new_food_act = st.text_input("New Food Activity")
                        if st.form_submit_button("Add Activity"):
                            act_clean = new_food_act.strip()
                            if act_clean:
                                try:
                                    with conn.session as session:
                                        session.execute(
                                            text("INSERT INTO category_activities (category, activity_name) VALUES ('Food', :act) ON CONFLICT (category, activity_name) DO NOTHING;"),
                                            {"act": act_clean}
                                        )
                                        session.commit()
                                    st.success(f"Added '{act_clean}'")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error adding activity: {e}")

                    st.write("---")
                    st.markdown("**Existing DB Activities:**")
                    for idx, act in enumerate(food_activities):
                        f_col1, f_col2 = st.columns([3, 1])
                        f_col1.write(f"• {act}")
                        if f_col2.button("🗑️", key=f"del_food_{idx}_{act}"):
                            try:
                                with conn.session as session:
                                    session.execute(
                                        text("DELETE FROM category_activities WHERE LOWER(TRIM(category)) = 'food' AND LOWER(TRIM(activity_name)) = LOWER(TRIM(:act));"),
                                        {"act": act}
                                    )
                                    session.commit()
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error: {e}")

        elif admin_task == "Member Settlement":
            st.subheader("💰 Member Settlement")
            unpaid_df = df_expenses[df_expenses['paid_status'] == 0]
            if not unpaid_df.empty:
                scols = st.columns(2)
                for idx, user in enumerate(all_users):
                    u_data = unpaid_df[unpaid_df['user_name'] == user]
                    if not u_data.empty:
                        net = u_data['amount'].sum()
                        if abs(net) > 0.001: 
                            with scols[idx % 2]:
                                with st.container(border=True):
                                    st.subheader(f"👤 {user}")
                                    st.metric("Net Balance", f"{net:.2f} QAR")
                                    if st.button(f"Settle {user}", key=f"adm_set_{user}"):
                                        with conn.session as session:
                                            session.execute(text("UPDATE expenses SET paid_status = 1 WHERE user_name = :u AND paid_status = 0"), {"u": user})
                                            session.commit()
                                        st.rerun()

        elif admin_task == "Group Payments":
            st.subheader("🤝 Consolidated Group Liabilities")
            unpaid_df_summary = df_expenses[df_expenses['paid_status'] == 0]
            if not unpaid_df_summary.empty:
                u_sums_summary = unpaid_df_summary.groupby('user_name')['amount'].sum().to_dict()
                ahmed_members, kashif_members = ['Ahmed', 'Arsalan', 'Kamran'], ['Kashif', 'Imran']
                t_ahmed = sum(u_sums_summary.get(m, 0) for m in ahmed_members)
                t_kashif = sum(u_sums_summary.get(m, 0) for m in kashif_members)
                others_list = [u for u in all_users if u not in (ahmed_members + kashif_members) and abs(u_sums_summary.get(u,0)) > 0.001]
                
                entities = []
                if abs(t_ahmed) > 0.001: entities.append({"name": "Ahmed's Group", "amt": t_ahmed, "ids": ahmed_members})
                if abs(t_kashif) > 0.001: entities.append({"name": "Kashif's Group", "amt": t_kashif, "ids": kashif_members})
                for u in others_list: entities.append({"name": f"👤 {u}", "amt": u_sums_summary[u], "ids": [u]})
                cols = st.columns(3)
                for i, ent in enumerate(entities):
                    with cols[i % 3]:
                        with st.container(border=True):
                            st.markdown(f"**{ent['name']}**")
                            st.metric("Balance", f"{ent['amt']:.2f} QAR")
                            if st.button(f"Settle", key=f"adm_grp_set_{ent['name']}", use_container_width=True):
                                with conn.session as session:
                                    stmt = text("UPDATE expenses SET paid_status = 1 WHERE user_name IN :ids AND paid_status = 0").bindparams(bindparam('ids', expanding=True))
                                    session.execute(stmt, {"ids": ent['ids']})
                                    session.commit()
                                st.rerun()

        elif admin_task == "User Management":
            col_u1, col_u2 = st.columns([1, 2])
            with col_u1:
                nu = st.text_input("New Member Name")
                if st.button("Add Member"):
                    if nu:
                        with conn.session as session:
                            session.execute(text("INSERT INTO participants (name) VALUES (:n)"), {"n": nu.strip()})
                            session.commit()
                        st.rerun()
            with col_u2:
                for p in saved_participants:
                    c_p1, c_p2 = st.columns([3, 1])
                    c_p1.write(f"👤 {p}")
                    if c_p2.button("🗑️", key=f"adm_du_{p}"):
                        with conn.session as session:
                            session.execute(text("DELETE FROM participants WHERE name = :n"), {"n": p})
                            session.commit()
                        st.rerun()

        elif admin_task == "Bulk Operations":
            csv_file = st.file_uploader("Upload Expense CSV", type="csv")
            if csv_file:
                up_df = pd.read_csv(csv_file)
                if st.button("Confirm Bulk Upload"):
                    up_df.to_sql('expenses', conn.engine, if_exists='append', index=False)
                    st.success("Data imported!")
                    st.rerun()
            if not df_expenses.empty:
                st.download_button("📥 Download Backup (CSV)", df_expenses.to_csv(index=False), "expense_backup.csv", use_container_width=True)

# --- TAB 5: HEALTH ---
with tabs[4]:
    st.header("🔌 System Health")
    try:
        conn.query("SELECT 1", ttl=0)
        st.success("✅ Database: Connected")
    except Exception as e: st.error(f"❌ Disconnected: {e}")
