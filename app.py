import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

import pandas as pd
import streamlit as st

APP_TITLE = "Network Device Monthly Snapshot"
DB_PATH = "network_uptime_history.db"

REQUIRED_COLUMNS = [
    "Last Up",
    "Down (h.m)",
    "Reference and/or Alias",
    "Type",
    "Description",
    "Class",
    "Reference",
]

st.set_page_config(page_title=APP_TITLE, page_icon="📡", layout="wide")

def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_month TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            device_key TEXT NOT NULL,
            device_name TEXT,
            reference TEXT,
            device_type TEXT,
            description TEXT,
            device_class TEXT,
            last_up TEXT,
            down_hm TEXT,
            down_minutes INTEGER,
            is_down INTEGER NOT NULL,
            raw_json TEXT,
            imported_at TEXT NOT NULL,
            UNIQUE(snapshot_month, device_key)
        )
    """)
    conn.commit()
    return conn

def parse_hm(value):
    """Interpret source values as hours.minutes (e.g. 3.15 = 3h 15m)."""
    if pd.isna(value):
        return 0
    raw = str(value).strip()
    if not raw:
        return 0
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return 0
    sign = -1 if number < 0 else 1
    number = abs(number)
    whole_hours = int(number)
    fractional = number - Decimal(whole_hours)
    # Source export uses HH.MM; preserve two-digit minute convention.
    minute_text = str(fractional).split(".")[-1] if "." in str(fractional) else ""
    minute_text = (minute_text + "00")[:2]
    minutes = int(minute_text or "0")
    # Guard against malformed minute values.
    minutes = min(minutes, 59)
    return sign * (whole_hours * 60 + minutes)

def format_minutes(total):
    total = max(0, int(total or 0))
    return f"{total // 60:,}h {total % 60:02d}m"

def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()

def prepare_data(file_bytes):
    df = pd.read_csv(BytesIO(file_bytes))
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError("Missing required column(s): " + ", ".join(missing))

    df = df.copy()
    df["down_minutes"] = df["Down (h.m)"].apply(parse_hm)
    df["is_down"] = df["down_minutes"] > 0

    # Prefer Reference as stable key; fall back to the export's alias/name column.
    df["device_key"] = df["Reference"].apply(clean_text)
    fallback = df["Reference and/or Alias"].apply(clean_text)
    df.loc[df["device_key"] == "", "device_key"] = fallback[df["device_key"] == ""]

    df["device_name"] = df["Reference and/or Alias"].apply(clean_text)
    df.loc[df["device_name"] == "", "device_name"] = df["device_key"]

    df = df[df["device_key"] != ""].copy()
    # If the export has duplicate references, retain the row with the greatest reported downtime.
    df = df.sort_values("down_minutes").drop_duplicates("device_key", keep="last")
    return df

def save_snapshot(conn, df, snapshot_date):
    month_key = snapshot_date.strftime("%Y-%m")
    date_key = snapshot_date.isoformat()
    imported_at = datetime.now().isoformat(timespec="seconds")

    existing = conn.execute(
        "SELECT COUNT(*) FROM monthly_snapshots WHERE snapshot_month = ?",
        (month_key,)
    ).fetchone()[0]
    if existing:
        return False, f"A snapshot for {month_key} is already stored ({existing:,} device records). No records were overwritten."

    rows = []
    for _, r in df.iterrows():
        raw = {str(k): (None if pd.isna(v) else str(v)) for k, v in r.items() if k not in ("down_minutes", "is_down", "device_key", "device_name")}
        rows.append((
            month_key, date_key, r["device_key"], r["device_name"],
            clean_text(r["Reference"]), clean_text(r["Type"]),
            clean_text(r["Description"]), clean_text(r["Class"]),
            clean_text(r["Last Up"]), clean_text(r["Down (h.m)"]),
            int(r["down_minutes"]), int(bool(r["is_down"])),
            json.dumps(raw, ensure_ascii=False), imported_at
        ))
    conn.executemany("""
        INSERT INTO monthly_snapshots (
            snapshot_month, snapshot_date, device_key, device_name, reference,
            device_type, description, device_class, last_up, down_hm,
            down_minutes, is_down, raw_json, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    return True, f"Saved {len(rows):,} device records for {month_key}."

def load_months(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_month FROM monthly_snapshots ORDER BY snapshot_month DESC"
    ).fetchall()]

def load_month(conn, month_key):
    return pd.read_sql_query(
        "SELECT * FROM monthly_snapshots WHERE snapshot_month = ? ORDER BY device_name",
        conn, params=(month_key,)
    )

st.title("📡 Network Device Monthly Snapshot")
st.caption("Individual device reporting • 24/7 operating period • One CSV snapshot on the 1st of each month")

st.warning(
    "Important reporting limitation: one snapshot per month does not contain all outages that occurred during that month. "
    "This version reports device status and ongoing downtime at the snapshot date. It does not claim this is monthly uptime."
)

conn = get_connection()

with st.sidebar:
    st.header("Monthly CSV import")
    uploaded = st.file_uploader("Upload the monitoring-system CSV", type=["csv", "txt"])
    snapshot_date = st.date_input("Snapshot date (must be the 1st)", value=date.today().replace(day=1))
    st.caption("Import one snapshot for each reporting month. A month can only be imported once in this prototype.")

if uploaded is not None:
    try:
        preview_df = prepare_data(uploaded.getvalue())
        st.subheader("Import preview")
        c1, c2, c3 = st.columns(3)
        c1.metric("Device records", f"{len(preview_df):,}")
        c2.metric("Reported down", f"{int(preview_df['is_down'].sum()):,}")
        c3.metric("Reported up / no downtime", f"{int((~preview_df['is_down']).sum()):,}")
        st.dataframe(
            preview_df[["device_name", "Reference", "Type", "Down (h.m)", "down_minutes", "Last Up"]]
            .rename(columns={"down_minutes": "Downtime (minutes)"}),
            use_container_width=True, hide_index=True
        )
        if snapshot_date.day != 1:
            st.error("Choose the 1st day of the month before importing.")
        else:
            if st.button(f"Save {snapshot_date.strftime('%B %Y')} snapshot", type="primary"):
                ok, message = save_snapshot(conn, preview_df, snapshot_date)
                (st.success if ok else st.error)(message)
    except Exception as exc:
        st.error(f"Could not read this file: {exc}")

months = load_months(conn)
st.divider()
st.subheader("Monthly management report")

if not months:
    st.info("No monthly snapshots have been saved yet. Upload your first CSV using the sidebar.")
else:
    selected_month = st.selectbox("Reporting month", months)
    report = load_month(conn, selected_month)
    total = len(report)
    down_count = int(report["is_down"].sum())
    up_count = total - down_count
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Devices in snapshot", f"{total:,}")
    c2.metric("Reported down", f"{down_count:,}")
    c3.metric("No downtime reported", f"{up_count:,}")
    c4.metric("Total ongoing downtime", format_minutes(report["down_minutes"].sum()))

    st.caption(
        f"Snapshot captured for reporting month {selected_month}. "
        "“No downtime reported” means Down (h.m) was zero in this export; confirm the source system's status convention."
    )

    status_filter = st.radio("Show devices", ["All devices", "Reported down only", "No downtime reported"], horizontal=True)
    search = st.text_input("Search by device, reference, type or description")
    view = report.copy()
    if status_filter == "Reported down only":
        view = view[view["is_down"] == 1]
    elif status_filter == "No downtime reported":
        view = view[view["is_down"] == 0]
    if search.strip():
        q = search.strip().lower()
        mask = (
            view["device_name"].fillna("").str.lower().str.contains(q, regex=False)
            | view["reference"].fillna("").str.lower().str.contains(q, regex=False)
            | view["device_type"].fillna("").str.lower().str.contains(q, regex=False)
            | view["description"].fillna("").str.lower().str.contains(q, regex=False)
        )
        view = view[mask]

    display = view[[
        "device_name", "reference", "device_type", "device_class",
        "last_up", "down_hm", "down_minutes", "is_down"
    ]].copy()
    display["Snapshot status"] = display["is_down"].map({1: "DOWN (reported)", 0: "No downtime reported"})
    display["Ongoing downtime"] = display["down_minutes"].apply(format_minutes)
    display = display.rename(columns={
        "device_name": "Device",
        "reference": "Reference",
        "device_type": "Type",
        "device_class": "Class",
        "last_up": "Last Up",
        "down_hm": "Source Down (h.m)"
    }).drop(columns=["down_minutes", "is_down"])
    st.dataframe(display, use_container_width=True, hide_index=True)

    csv_out = view.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Download filtered device report (CSV)",
        data=csv_out,
        file_name=f"device_snapshot_{selected_month}.csv",
        mime="text/csv"
    )

    st.subheader("Snapshot history")
    history = pd.read_sql_query("""
        SELECT snapshot_month,
               COUNT(*) AS devices,
               SUM(is_down) AS reported_down,
               SUM(CASE WHEN is_down = 0 THEN 1 ELSE 0 END) AS no_downtime_reported,
               SUM(down_minutes) AS ongoing_downtime_minutes
        FROM monthly_snapshots
        GROUP BY snapshot_month
        ORDER BY snapshot_month
    """, conn)
    if not history.empty:
        history["ongoing_downtime"] = history["ongoing_downtime_minutes"].apply(format_minutes)
        st.dataframe(
            history[["snapshot_month", "devices", "reported_down", "no_downtime_reported", "ongoing_downtime"]]
            .rename(columns={
                "snapshot_month": "Month",
                "devices": "Devices",
                "reported_down": "Reported down",
                "no_downtime_reported": "No downtime reported",
                "ongoing_downtime": "Ongoing downtime total"
            }),
            use_container_width=True, hide_index=True
        )
        st.caption("Month-to-month comparison is a comparison of snapshots, not a full monthly uptime calculation.")

st.divider()
with st.expander("Calculation and data notes"):
    st.markdown("""
    - The source field `Down (h.m)` is interpreted as **hours.minutes**, not decimal hours. For example, `3.15` becomes 3 hours 15 minutes.
    - A device is provisionally marked as down when the parsed downtime is greater than zero.
    - The sample export supports this interpretation because several reported durations align with the elapsed time since `Last Up` and the export capture time.
    - The CSV does not provide a complete event history. A device can go down and recover between monthly imports without that outage appearing in either snapshot.
    - Therefore, monthly uptime percentages are deliberately not calculated in this version.
    - Before production use, confirm with the monitoring-system owner that `Down (h.m) > 0` reliably means the device is down and that `Reference` is a unique, stable device identifier.
    """)
