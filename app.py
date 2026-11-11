import calendar
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

def month_minutes(month_key):
    """Full 24/7 operating minutes in the selected calendar month."""
    year, month = map(int, month_key.split("-"))
    return calendar.monthrange(year, month)[1] * 24 * 60


def calculate_availability(downtime_minutes, available_minutes):
    """Calculate availability, capped between 0% and 100%."""
    downtime = max(0, int(downtime_minutes or 0))
    if available_minutes <= 0:
        return 0.0
    return max(0.0, (available_minutes - min(downtime, available_minutes)) / available_minutes * 100)


def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def prepare_data(file_bytes):
    df = pd.read_csv(BytesIO(file_bytes))

    # Clean column names: remove BOM and unwanted spaces
    df.columns = [
        str(col).replace("\ufeff", "").strip()
        for col in df.columns
    ]

    # Find Reference and/or Alias, ignoring numeric suffix,
    # spaces and capitalisation.
    alias_column = None

    for col in df.columns:
        normalized = " ".join(str(col).lower().split())

        if normalized.startswith("reference and/or alias"):
            alias_column = col
            break

    if alias_column is None:
        raise ValueError(
            "Reference and/or Alias column not found. "
            f"Columns detected: {df.columns.tolist()}"
        )

    # Rename detected column to a consistent internal name
    df = df.rename(columns={
        alias_column: "Reference and/or Alias"
    })

    # Validate other required columns
    missing = [
        col for col in REQUIRED_COLUMNS
        if col not in df.columns
    ]

    if missing:
        raise ValueError(
            f"Missing required column(s): {missing}. "
            f"Detected columns: {df.columns.tolist()}"
        )

    df = df.copy()

    # Calculate downtime
    df["down_minutes"] = (
        df["Down (h.m)"].apply(parse_hm)
    )

    df["is_down"] = df["down_minutes"] > 0

    # Device identification
    df["device_key"] = (
        df["Reference"].apply(clean_text)
    )

    fallback = (
        df["Reference and/or Alias"].apply(clean_text)
    )

    df.loc[
        df["device_key"] == "",
        "device_key"
    ] = fallback[df["device_key"] == ""]

    df["device_name"] = (
        df["Reference and/or Alias"].apply(clean_text)
    )

    df.loc[
        df["device_name"] == "",
        "device_name"
    ] = df["device_key"]

    # Remove records without an identifier
    df = df[df["device_key"] != ""].copy()

    # Remove duplicate device references
    df = (
        df.sort_values("down_minutes")
        .drop_duplicates("device_key", keep="last")
    )

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

st.info(
    "**Availability assumption:** `Down (h.m)` is treated as downtime attributable to the selected reporting month. "
    "Availability is calculated against every minute in that calendar month (28, 29, 30 or 31 days). "
    "If this field is only the duration of an outage still in progress at snapshot time, the percentage is an estimate, "
    "not true monthly availability. Accurate monthly uptime requires complete outage history or monthly downtime totals."
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
    minutes_in_month = month_minutes(selected_month)
    hours_in_month = minutes_in_month // 60
    report["availability_pct"] = report["down_minutes"].apply(
        lambda value: calculate_availability(value, minutes_in_month)
    )
    report["downtime_exceeds_month"] = report["down_minutes"] > minutes_in_month

    total = len(report)
    down_count = int(report["is_down"].sum())
    average_availability = report["availability_pct"].mean() if total else 0.0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Devices in snapshot", f"{total:,}")
    c2.metric("Reported down", f"{down_count:,}")
    c3.metric("Average device availability", f"{average_availability:.2f}%")
    c4.metric("Total reported downtime", format_minutes(report["down_minutes"].sum()))

    st.caption(
        f"{selected_month} has {hours_in_month:,} operating hours "
        f"({minutes_in_month:,} minutes), assuming continuous 24/7 availability."
    )
    if report["downtime_exceeds_month"].any():
        st.warning(
            "One or more devices have downtime greater than the selected month's duration. "
            "Their availability is capped at 0.00%; check the source values."
        )

    st.markdown("#### Device availability")
    filter_col, sort_col = st.columns([2, 2])
    with filter_col:
        status_filter = st.radio(
            "Show devices",
            ["All devices", "Reported down only", "Down = 0 only"],
            horizontal=True
        )
    with sort_col:
        sort_order = st.selectbox(
            "Sort availability",
            ["Lowest availability first", "Highest availability first", "Device name"]
        )

    search = st.text_input("Search by device, reference, type or description")
    view = report.copy()
    if status_filter == "Reported down only":
        view = view[view["is_down"] == 1]
    elif status_filter == "Down = 0 only":
        view = view[view["down_minutes"] == 0]

    if search.strip():
        q = search.strip().lower()
        mask = (
            view["device_name"].fillna("").str.lower().str.contains(q, regex=False)
            | view["reference"].fillna("").str.lower().str.contains(q, regex=False)
            | view["device_type"].fillna("").str.lower().str.contains(q, regex=False)
            | view["description"].fillna("").str.lower().str.contains(q, regex=False)
        )
        view = view[mask]

    if sort_order == "Lowest availability first":
        view = view.sort_values("availability_pct", ascending=True)
    elif sort_order == "Highest availability first":
        view = view.sort_values("availability_pct", ascending=False)
    else:
        view = view.sort_values("device_name", ascending=True)

    display = view[[
        "device_name", "reference", "device_type", "device_class",
        "last_up", "down_hm", "down_minutes", "availability_pct", "is_down"
    ]].copy()
    display["Status"] = display["is_down"].map({1: "DOWN (reported)", 0: "No downtime reported"})
    display["Downtime"] = display["down_minutes"].apply(format_minutes)
    display["Availability"] = display["availability_pct"].map(lambda value: f"{value:.2f}%")
    display = display.rename(columns={
        "device_name": "Device",
        "reference": "Reference",
        "device_type": "Type",
        "device_class": "Class",
        "last_up": "Last Up",
        "down_hm": "Source Down (h.m)"
    }).drop(columns=["down_minutes", "availability_pct", "is_down"])

    st.dataframe(
        display[["Device", "Reference", "Type", "Source Down (h.m)",
                 "Downtime", "Availability", "Status", "Last Up", "Class"]],
        use_container_width=True,
        hide_index=True,
        height=520
    )
    st.caption(
        f"Showing {len(view):,} of {total:,} devices. "
        "Devices with Down (h.m) = 0 receive 100.00% availability."
    )

    show_details = st.checkbox("Show / hide expanded device information", value=False)
    if show_details:
        st.markdown("#### Expanded device information")
        if view.empty:
            st.info("No devices match the current filters.")
        else:
            for _, device in view.iterrows():
                label = f"{device['device_name']} | {device['availability_pct']:.2f}% availability"
                with st.expander(label, expanded=False):
                    d1, d2, d3 = st.columns(3)
                    d1.metric("Availability", f"{device['availability_pct']:.2f}%")
                    d2.metric("Downtime", format_minutes(device["down_minutes"]))
                    d3.metric("Monthly operating time", format_minutes(minutes_in_month))
                    left, right = st.columns(2)
                    with left:
                        st.write("**Device reference:**", device["reference"] or "—")
                        st.write("**Device type:**", device["device_type"] or "—")
                        st.write("**Device class:**", device["device_class"] or "—")
                    with right:
                        st.write("**Last Up:**", device["last_up"] or "—")
                        st.write("**Source Down (h.m):**", device["down_hm"] or "0.00")
                        st.write("**Description:**", device["description"] or "—")
                    st.write(
                        "**Snapshot status:**",
                        "DOWN (reported)" if int(device["is_down"]) else "No downtime reported"
                    )

    export_view = view.copy()
    export_view["Monthly availability (%)"] = export_view["availability_pct"].round(2)
    export_view["Monthly downtime (formatted)"] = export_view["down_minutes"].apply(format_minutes)
    csv_out = export_view.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Download filtered device report (CSV)",
        data=csv_out,
        file_name=f"device_availability_{selected_month}.csv",
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
        st.caption("Historical averages use the same downtime assumption described above; complete outage-event history is needed for verified monthly uptime.")

st.divider()
with st.expander("Calculation and data notes"):
    st.markdown("""
    - The source field `Down (h.m)` is interpreted as **hours.minutes**, not decimal hours. For example, `3.15` becomes 3 hours 15 minutes.
    - A device is provisionally marked as down when the parsed downtime is greater than zero.
    - The sample export supports this interpretation because several reported durations align with the elapsed time since `Last Up` and the export capture time.
    - The CSV does not provide a complete event history. A device can go down and recover between monthly imports without that outage appearing in either snapshot.
    - Monthly availability = `(minutes in month - downtime minutes) / minutes in month × 100`.
    - A device with `Down (h.m) = 0` receives **100.00%** availability.
    - Availability is capped between 0.00% and 100.00%.
    - This is a true monthly figure only if `Down (h.m)` is the total downtime for that reporting month. If it is only the current ongoing outage duration, the result is an estimate.
    - A single monthly snapshot cannot capture outages that started and recovered between imports. Accurate monthly availability requires complete outage events or monthly downtime totals.
    - Confirm that `Reference` is a unique, stable device identifier and that the source downtime field has the meaning assumed above.
    """)
