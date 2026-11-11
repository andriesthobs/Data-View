from pathlib import Path

code = r'''import calendar
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


st.set_page_config(
    page_title=APP_TITLE,
    page_icon="📡",
    layout="wide",
)


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
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
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------
# Formatting and calculations
# ---------------------------------------------------------------------
def parse_hm(value):
    """
    Interpret source values as hours.minutes, not decimal hours.
    Examples: 3.15 = 3 hours 15 minutes; 2.30 = 2 hours 30 minutes.
    """
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

    # Preserve the HH.MM convention, including values read as floats.
    minute_text = str(fractional).split(".")[-1] if "." in str(fractional) else ""
    minute_text = (minute_text + "00")[:2]

    try:
        minutes = int(minute_text or "0")
    except ValueError:
        minutes = 0

    # Invalid minute portions are bounded to the last valid minute.
    minutes = min(minutes, 59)
    return sign * (whole_hours * 60 + minutes)


def format_minutes(total):
    total = max(0, int(total or 0))
    return f"{total // 60:,}h {total % 60:02d}m"


def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def month_duration_minutes(month_key):
    """Return the exact number of minutes in the selected calendar month."""
    year, month = (int(part) for part in month_key.split("-"))
    days = calendar.monthrange(year, month)[1]
    return days * 24 * 60, days


def calculate_uptime(down_minutes, month_key):
    """
    Calculate uptime from the selected month's total minutes and the
    downtime supplied in the CSV. Values below zero are clamped to 0%.
    """
    total_month_minutes, _ = month_duration_minutes(month_key)
    downtime = max(0, int(down_minutes or 0))
    uptime = ((total_month_minutes - downtime) / total_month_minutes) * 100
    return max(0.0, min(100.0, uptime))


# ---------------------------------------------------------------------
# CSV preparation
# ---------------------------------------------------------------------
def prepare_data(file_bytes):
    df = pd.read_csv(BytesIO(file_bytes))

    # Remove BOM and unwanted spaces from column names.
    df.columns = [
        str(col).replace("\ufeff", "").strip()
        for col in df.columns
    ]

    # Find Reference and/or Alias, ignoring suffixes, spaces and case.
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

    df = df.rename(columns={alias_column: "Reference and/or Alias"})

    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(
            f"Missing required column(s): {missing}. "
            f"Detected columns: {df.columns.tolist()}"
        )

    df = df.copy()

    # Parse downtime into minutes.
    df["down_minutes"] = df["Down (h.m)"].apply(parse_hm)
    df["is_down"] = df["down_minutes"] > 0

    # Device identification.
    df["device_key"] = df["Reference"].apply(clean_text)
    fallback = df["Reference and/or Alias"].apply(clean_text)

    df.loc[df["device_key"] == "", "device_key"] = fallback[df["device_key"] == ""]

    df["device_name"] = df["Reference and/or Alias"].apply(clean_text)
    df.loc[df["device_name"] == "", "device_name"] = df["device_key"]

    # Remove records without an identifier.
    df = df[df["device_key"] != ""].copy()

    # Keep the record with the greatest reported downtime for duplicate keys.
    df = (
        df.sort_values("down_minutes")
        .drop_duplicates("device_key", keep="last")
    )

    return df


# ---------------------------------------------------------------------
# Snapshot storage and retrieval
# ---------------------------------------------------------------------
def save_snapshot(conn, df, snapshot_date):
    month_key = snapshot_date.strftime("%Y-%m")
    date_key = snapshot_date.isoformat()
    imported_at = datetime.now().isoformat(timespec="seconds")

    existing = conn.execute(
        "SELECT COUNT(*) FROM monthly_snapshots WHERE snapshot_month = ?",
        (month_key,),
    ).fetchone()[0]

    if existing:
        return (
            False,
            f"A snapshot for {month_key} is already stored "
            f"({existing:,} device records). No records were overwritten.",
        )

    rows = []
    for _, row in df.iterrows():
        raw = {
            str(key): (None if pd.isna(value) else str(value))
            for key, value in row.items()
            if key not in (
                "down_minutes",
                "is_down",
                "device_key",
                "device_name",
            )
        }

        rows.append(
            (
                month_key,
                date_key,
                row["device_key"],
                row["device_name"],
                clean_text(row["Reference"]),
                clean_text(row["Type"]),
                clean_text(row["Description"]),
                clean_text(row["Class"]),
                clean_text(row["Last Up"]),
                clean_text(row["Down (h.m)"]),
                int(row["down_minutes"]),
                int(bool(row["is_down"])),
                json.dumps(raw, ensure_ascii=False),
                imported_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO monthly_snapshots (
            snapshot_month, snapshot_date, device_key, device_name, reference,
            device_type, description, device_class, last_up, down_hm,
            down_minutes, is_down, raw_json, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    return True, f"Saved {len(rows):,} device records for {month_key}."


def load_months(conn):
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT snapshot_month
            FROM monthly_snapshots
            ORDER BY snapshot_month DESC
            """
        ).fetchall()
    ]


def load_month(conn, month_key):
    return pd.read_sql_query(
        """
        SELECT *
        FROM monthly_snapshots
        WHERE snapshot_month = ?
        ORDER BY device_name
        """,
        conn,
        params=(month_key,),
    )


# ---------------------------------------------------------------------
# Page styling
# ---------------------------------------------------------------------
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.7rem;
        padding-bottom: 3rem;
    }
    [data-testid="stMetric"] {
        background: rgba(128, 128, 128, 0.07);
        border: 1px solid rgba(128, 128, 128, 0.16);
        padding: 14px 16px;
        border-radius: 12px;
    }
    div[data-testid="stExpander"] {
        border: 1px solid rgba(128, 128, 128, 0.20);
        border-radius: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------
# Header and sidebar
# ---------------------------------------------------------------------
st.title("📡 Network Device Monthly Snapshot")
st.caption(
    "Monthly network availability reporting • One calendar month per report • "
    "Device-level uptime and downtime"
)

st.info(
    "Uptime is calculated using the number of days in the selected calendar "
    "month (28, 29, 30 or 31) and the downtime in the imported "
    "`Down (h.m)` field. This is a downtime-based calculation from the "
    "snapshot data, not a reconstruction of every outage event during the month."
)

conn = get_connection()

with st.sidebar:
    st.header("📥 Monthly CSV import")
    uploaded = st.file_uploader(
        "Upload the monitoring-system CSV",
        type=["csv", "txt"],
    )
    snapshot_date = st.date_input(
        "Snapshot date (must be the 1st)",
        value=date.today().replace(day=1),
    )
    st.caption(
        "Import one snapshot per reporting month. Each month can only be "
        "imported once in this version."
    )


# Prepare uploaded data here, but render its preview at the bottom of the page.
preview_df = None
preview_error = None

if uploaded is not None:
    try:
        preview_df = prepare_data(uploaded.getvalue())
    except Exception as exc:
        preview_error = str(exc)


# ---------------------------------------------------------------------
# Monthly management report
# ---------------------------------------------------------------------
months = load_months(conn)
st.divider()
st.subheader("📊 Monthly management report")

if not months:
    st.info(
        "No monthly snapshots have been saved yet. Upload your first CSV "
        "using the sidebar."
    )
else:
    selected_month = st.selectbox("Reporting month", months)
    report = load_month(conn, selected_month)

    total_month_minutes, month_days = month_duration_minutes(selected_month)
    report["uptime_pct"] = report["down_minutes"].apply(
        lambda value: calculate_uptime(value, selected_month)
    )
    report["uptime_exceeds_month"] = report["down_minutes"] > total_month_minutes

    total = len(report)
    down_count = int(report["is_down"].sum())
    up_count = total - down_count
    average_uptime = (
        float(report["uptime_pct"].mean()) if total else 0.0
    )
    total_downtime = int(report["down_minutes"].sum())

    st.caption(
        f"Reporting period: {calendar.month_name[int(selected_month.split('-')[1])]} "
        f"{selected_month.split('-')[0]} • {month_days} days • "
        f"{total_month_minutes:,} total operating minutes per device"
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Devices in snapshot", f"{total:,}")
    c2.metric("Reported down", f"{down_count:,}")
    c3.metric("Average uptime", f"{average_uptime:.2f}%")
    c4.metric("Total reported downtime", format_minutes(total_downtime))

    if report["uptime_exceeds_month"].any():
        st.warning(
            "At least one device has reported downtime greater than the "
            "selected month's total duration. Its uptime is shown as 0%. "
            "Please verify the source record."
        )

    st.caption(
        "A device is marked as reported down when `Down (h.m)` is greater "
        "than zero. The uptime percentage uses that device's reported "
        "downtime against the full duration of the selected month."
    )

    filter_col, search_col = st.columns([1, 2])
    with filter_col:
        status_filter = st.radio(
            "Show devices",
            [
                "All devices",
                "Reported down only",
                "No downtime reported",
            ],
        )
    with search_col:
        search = st.text_input(
            "Search by device, reference, type or description"
        )

    view = report.copy()

    if status_filter == "Reported down only":
        view = view[view["is_down"] == 1]
    elif status_filter == "No downtime reported":
        view = view[view["is_down"] == 0]

    if search.strip():
        query = search.strip().lower()
        mask = (
            view["device_name"].fillna("").str.lower().str.contains(query, regex=False)
            | view["reference"].fillna("").str.lower().str.contains(query, regex=False)
            | view["device_type"].fillna("").str.lower().str.contains(query, regex=False)
            | view["description"].fillna("").str.lower().str.contains(query, regex=False)
        )
        view = view[mask]

    display = view[
        [
            "device_name",
            "uptime_pct",
            "reference",
            "device_type",
            "device_class",
            "last_up",
            "down_hm",
            "down_minutes",
            "is_down",
            "uptime_exceeds_month",
        ]
    ].copy()

    display["Uptime %"] = display["uptime_pct"].map(
        lambda value: f"{value:.2f}%"
    )
    display["Snapshot status"] = display["is_down"].map(
        {
            1: "DOWN (reported)",
            0: "No downtime reported",
        }
    )
    display["Ongoing downtime"] = display["down_minutes"].apply(format_minutes)
    display["Data check"] = display["uptime_exceeds_month"].map(
        {
            True: "Exceeds month",
            False: "OK",
        }
    )

    display = display.rename(
        columns={
            "device_name": "Device Name",
            "reference": "Reference",
            "device_type": "Type",
            "device_class": "Class",
            "last_up": "Last Up",
            "down_hm": "Down (h.m)",
        }
    )

    # Keep Uptime % directly between Device Name and Reference.
    display = display[
        [
            "Device Name",
            "Uptime %",
            "Reference",
            "Type",
            "Class",
            "Last Up",
            "Down (h.m)",
            "Ongoing downtime",
            "Snapshot status",
            "Data check",
        ]
    ]

    with st.expander(
        f"📋 Device uptime table — {len(display):,} records",
        expanded=True,
    ):
        st.dataframe(
            display,
            use_container_width=True,
            hide_index=True,
        )

    csv_out = view.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Download filtered device report (CSV)",
        data=csv_out,
        file_name=f"device_uptime_report_{selected_month}.csv",
        mime="text/csv",
    )

    # -----------------------------------------------------------------
    # Snapshot history
    # -----------------------------------------------------------------
    st.subheader("🗂️ Snapshot history")

    history = pd.read_sql_query(
        """
        SELECT snapshot_month,
               COUNT(*) AS devices,
               SUM(is_down) AS reported_down,
               SUM(CASE WHEN is_down = 0 THEN 1 ELSE 0 END) AS no_downtime_reported,
               SUM(down_minutes) AS ongoing_downtime_minutes
        FROM monthly_snapshots
        GROUP BY snapshot_month
        ORDER BY snapshot_month DESC
        """,
        conn,
    )

    if not history.empty:
        history["ongoing_downtime"] = history[
            "ongoing_downtime_minutes"
        ].apply(format_minutes)

        history_display = history[
            [
                "snapshot_month",
                "devices",
                "reported_down",
                "no_downtime_reported",
                "ongoing_downtime",
            ]
        ].rename(
            columns={
                "snapshot_month": "Month",
                "devices": "Devices",
                "reported_down": "Reported down",
                "no_downtime_reported": "No downtime reported",
                "ongoing_downtime": "Reported downtime total",
            }
        )

        with st.expander("📚 View stored monthly snapshots", expanded=False):
            st.dataframe(
                history_display,
                use_container_width=True,
                hide_index=True,
            )

        st.caption(
            "History lists the snapshots stored in the database. "
            "Each report calculates uptime for its selected month only."
        )


# ---------------------------------------------------------------------
# Calculation notes
# ---------------------------------------------------------------------
with st.expander("ℹ️ Calculation and data notes", expanded=False):
    st.markdown(
        """
        - `Down (h.m)` is interpreted as **hours.minutes**, not decimal hours.
          For example, `3.15` means 3 hours and 15 minutes.
        - The selected calendar month determines the available operating time:
          28, 29, 30 or 31 days, multiplied by 24 hours.
        - Uptime formula:
          `((Total minutes in month - Down minutes) / Total minutes in month) × 100`.
        - Uptime is limited to a range of 0% to 100%. If reported downtime
          exceeds the selected month's duration, the record is flagged for review
          and its displayed uptime is 0%.
        - A device is provisionally marked as down when parsed downtime is
          greater than zero.
        - The CSV does not provide a complete event history. A device can go
          down and recover between snapshots without that outage appearing in
          the export. The calculated percentage should therefore be understood
          as a downtime-based estimate using the supplied `Down (h.m)` value,
          not a verified event-by-event monthly availability figure.
        - `Reference` is used as the preferred stable device identifier;
          `Reference and/or Alias` is used as a fallback.
        """
    )


# ---------------------------------------------------------------------
# Import preview — deliberately placed at the bottom of the page
# ---------------------------------------------------------------------
st.divider()
st.subheader("📥 Import preview")
st.caption(
    "The preview is displayed here at the bottom so you can review the "
    "incoming CSV before saving the monthly snapshot."
)

if preview_error:
    st.error(f"Could not read this file: {preview_error}")

elif uploaded is None:
    st.info("Upload a CSV from the sidebar to see its import preview here.")

else:
    if snapshot_date.day != 1:
        st.error("Choose the 1st day of the month before importing.")

    preview_month = snapshot_date.strftime("%Y-%m")
    preview_total_minutes, preview_days = month_duration_minutes(preview_month)

    preview = preview_df.copy()
    preview["Uptime %"] = preview["down_minutes"].apply(
        lambda value: f"{calculate_uptime(value, preview_month):.2f}%"
    )
    preview["Downtime"] = preview["down_minutes"].apply(format_minutes)
    preview["Data check"] = preview["down_minutes"].apply(
        lambda value: (
            "Exceeds month"
            if int(value or 0) > preview_total_minutes
            else "OK"
        )
    )

    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Device records", f"{len(preview):,}")
    p2.metric("Reported down", f"{int(preview['is_down'].sum()):,}")
    p3.metric(
        "No downtime reported",
        f"{int((~preview['is_down']).sum()):,}",
    )
    p4.metric("Month length", f"{preview_days} days")

    preview_display = preview[
        [
            "device_name",
            "Uptime %",
            "Reference",
            "Type",
            "Down (h.m)",
            "Downtime",
            "Last Up",
            "Data check",
        ]
    ].rename(columns={"device_name": "Device Name"})

    with st.expander(
        f"👀 Preview imported devices — {len(preview_display):,} records",
        expanded=True,
    ):
        st.dataframe(
            preview_display,
            use_container_width=True,
            hide_index=True,
        )

    if preview["down_minutes"].gt(preview_total_minutes).any():
        st.warning(
            "Some records contain downtime greater than the selected month's "
            "total duration. Review these records before importing."
        )

    if snapshot_date.day == 1:
        if st.button(
            f"💾 Save {snapshot_date.strftime('%B %Y')} snapshot",
            type="primary",
        ):
            ok, message = save_snapshot(conn, preview_df, snapshot_date)
            if ok:
                st.success(message)
                st.rerun()
            else:
                st.error(message)
'''

output_path = Path("/mnt/data/network_device_monthly_snapshot_updated.py")
output_path.write_text(code, encoding="utf-8")

# Basic syntax validation before sharing.
compile(code, str(output_path), "exec")

print(f"Created and syntax-checked: {output_path}")
print(f"Lines: {len(code.splitlines())}")
