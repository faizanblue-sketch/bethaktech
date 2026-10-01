import streamlit as st
import pandas as pd
import sqlite3
import plotly.graph_objects as go

# --- PAGE CONFIG ---
st.set_page_config(page_title="Badminton & Food Tracker", page_icon="🏸", layout="wide")

# --- DATABASE CONNECTION & SETUP ---
def get_connection():
    conn = sqlite3.connect("tracker.db", check_same_thread=False)
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            category TEXT,
            activity TEXT,
            amount REAL,
            user_name TEXT,
            status TEXT DEFAULT 'Unpaid'
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- HELPER FUNCTIONS ---
def load_data():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM expenses", conn)
    conn.close()
    return df

def add_expense(date, category, activity, amount, user_name):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO expenses (date, category, activity, amount, user_name, status)
        VALUES (?, ?, ?, ?, ?, 'Unpaid')
    ''', (str(date), category, activity, amount, user_name))
    conn.commit()
    conn.close()

def mark_as_paid(expense_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE expenses SET status = 'Paid' WHERE id = ?", (expense_id,))
    conn.commit()
    conn.close()

# --- GROUP DEFINITIONS ---
ahmed_g = ["Ahmed", "Arsalan", "Kamran"]
kashif_g = ["Kashif", "Imran"]

# --- APP LAYOUT ---
st.title("🏸 Badminton & Food Expense Tracker")

df = load_data()

# --- SIDEBAR: ENTRY FORM ---
st.sidebar.header("➕ Add New Expense")
date_input = st.sidebar.date_input("Date")
category_input = st.sidebar.selectbox("Category", ["Sports", "Food", "Other"])
activity_input = st.sidebar.text_input("Activity / Item Name")
amount_input = st.sidebar.number_input("Amount (QAR)", min_value=0.0, step=5.0)
user_input = st.sidebar.selectbox("Member", ["Ahmed", "Arsalan", "Kamran", "Kashif", "Imran", "Other"])

if st.sidebar.button("Submit Expense"):
    if activity_input and amount_input > 0:
        add_expense(date_input, category_input, activity_input, amount_input, user_input)
        st.sidebar.success("Expense added successfully!")
        st.rerun()
    else:
        st.sidebar.error("Please fill in all required fields.")

# --- TABS ---
tabs = st.tabs(["📊 Summary & Split", "📋 Expense Records", "⚙️ Manage & Settle"])

# ==========================================
# TAB 1: SUMMARY & SPLIT
# ==========================================
with tabs[0]:
    st.header("📊 Outstanding Dues & Summary")
    
    unpaid_df = df[df['status'] == 'Unpaid'] if not df.empty else pd.DataFrame()
    
    # 1. Summary Metrics
    m1, m2, m3 = st.columns(3)
    total_unpaid = unpaid_df['amount'].sum() if not unpaid_df.empty else 0.0
    total_paid = df[df['status'] == 'Paid']['amount'].sum() if not df.empty else 0.0
    total_records = len(df) if not df.empty else 0
    
    m1.metric("Total Outstanding (QAR)", f"{total_unpaid:.1f}")
    m2.metric("Total Settled (QAR)", f"{total_paid:.1f}")
    m3.metric("Total Records", total_records)
    
    st.divider()
    
    # 2. Build Dues Dataframe for Groups and Users
    bar_data = []
    if not unpaid_df.empty:
        # Calculate group totals
        ahmed_sum = unpaid_df[unpaid_df['user_name'].isin(ahmed_g)]['amount'].sum()
        kashif_sum = unpaid_df[unpaid_df['user_name'].isin(kashif_g)]['amount'].sum()
        
        if ahmed_sum > 0:
            bar_data.append({"Entity": "Ahmed's Group", "Amount": ahmed_sum})
        if kashif_sum > 0:
            bar_data.append({"Entity": "Kashif's Group", "Amount": kashif_sum})
            
        # Others/Individual members not in defined groups
        other_users = unpaid_df[~unpaid_df['user_name'].isin(ahmed_g + kashif_g)]
        if not other_users.empty:
            other_grouped = other_users.groupby('user_name')['amount'].sum().reset_index()
            for _, row in other_grouped.iterrows():
                bar_data.append({"Entity": row['user_name'], "Amount": row['amount']})

    df_bar = pd.DataFrame(bar_data)
    
    # --- DUAL DONUT CHART ---
    if not df_bar.empty and df_bar['Amount'].sum() > 0:
        total_dues = df_bar['Amount'].sum()
        
        # Inner Ring Data (Values in QAR)
        inner_labels = df_bar['Entity'].tolist()
        inner_values = df_bar['Amount'].tolist()
        
        # Outer Ring Data (Percentages per Entity)
        outer_labels = [f"{entity} (%)" for entity in inner_labels]
        outer_values = [(val / total_dues) * 100 for val in inner_values]
        
        # Color palette matching inner and outer rings
        colors_inner = ['#2B5C8F', '#D9534F', '#409093', '#F0AD4E', '#5CB85C']
        colors_outer = ['#5B86E5', '#FF7675', '#64C5B1', '#FFC048', '#88D8B0']

        fig_dual = go.Figure()

        # 1. Inner Ring: Entity Amounts (QAR)
        fig_dual.add_trace(go.Pie(
            labels=inner_labels,
            values=inner_values,
            hole=0.45,
            domain=dict(x=[0.15, 0.85], y=[0.15, 0.85]),
            textinfo='label+value',
            texttemplate='<b>%{label}</b><br>%{value:.1f} QAR',
            hoverinfo='label+value',
            marker=dict(colors=colors_inner, line=dict(color='#FFFFFF', width=2)),
            name="Amount (QAR)",
            sort=False
        ))

        # 2. Outer Ring: Percentage Breakdown (%)
        fig_dual.add_trace(go.Pie(
            labels=outer_labels,
            values=outer_values,
            hole=0.72,
            domain=dict(x=[0, 1], y=[0, 1]),
            textinfo='label+percent',
            texttemplate='%{percent:.1%}',
            textposition='outside',
            hoverinfo='label+percent',
            marker=dict(colors=colors_outer, line=dict(color='#FFFFFF', width=2)),
            name="Share (%)",
            sort=False
        ))

        # Layout styling with total amount in center hole
        fig_dual.update_layout(
            title=dict(
                text="<b>Dues Breakdown: Inner (QAR) vs Outer (%)</b>",
                x=0.5,
                xanchor='center'
            ),
            showlegend=True,
            annotations=[dict(
                text=f"<b>Total</b><br>{total_dues:.1f} QAR",
                x=0.5, y=0.5,
                font_size=15,
                showarrow=False
            )],
            margin=dict(l=20, r=20, t=60, b=20),
            height=500
        )

        st.plotly_chart(fig_dual, use_container_width=True)
    else:
        st.info("No unpaid dues available to display.")

    st.divider()

    # 3. Individual Breakup Table
    st.write("### 🗓️️ Individual Breakup Table")
    if not unpaid_df.empty:
        breakup_df = unpaid_df.groupby(['user_name', 'category'])['amount'].sum().unstack(fill_value=0)
        breakup_df['Total Outstanding'] = breakup_df.sum(axis=1)
        st.dataframe(breakup_df.style.format("{:.1f} QAR"), use_container_width=True)
    else:
        st.write("No active unpaid items found.")

# ==========================================
# TAB 2: EXPENSE RECORDS
# ==========================================
with tabs[1]:
    st.header("📋 All Expense Records")
    if not df.empty:
        st.dataframe(df.sort_values(by="id", ascending=False), use_container_width=True)
    else:
        st.info("No expenses recorded yet.")

# ==========================================
# TAB 3: MANAGE & SETTLE
# ==========================================
with tabs[2]:
    st.header("⚙️ Settle Dues")
    if not unpaid_df.empty:
        st.write("Select an unpaid expense to mark as paid:")
        for idx, row in unpaid_df.iterrows():
            c1, c2, c3, c4 = st.columns([1, 3, 2, 2])
            c1.write(f"**#{row['id']}**")
            c2.write(f"{row['date']} — {row['user_name']} ({row['activity']})")
            c3.write(f"**{row['amount']:.1f} QAR**")
            if c4.button(f"Mark Paid", key=f"pay_{row['id']}"):
                mark_as_paid(row['id'])
                st.success(f"Marked expense #{row['id']} as paid!")
                st.rerun()
    else:
        st.success("All expenses are fully settled! 🎉")
