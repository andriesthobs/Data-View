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

CUSTOM_CSS = """
<style>
    :root {
        --ink: #172b4d;
        --muted: #64748b;
        --line: #e5eaf2;
        --surface: #ffffff;
        --canvas: #f5f7fb;
        --brand: #1769e0;
        --brand-dark: #104ca8;
        --mint: #13a887;
    }

    .stApp {
        background:
            radial-gradient(ellipse at 8% 0%, rgba(23, 105, 224, .075), transparent 32rem),
            var(--canvas);
        color: var(--ink);
    }

    .block-container {
        max-width: 1440px;
        padding-top: 2.25rem;
        padding-bottom: 3rem;
    }

    h1, h2, h3, h4 {
        color: var(--ink);
        letter-spacing: -0.035em;
    }

    h2 {
        margin-top: .25rem;
    }

    [data-testid="stSidebar"] {
        background: #fff;
        border-right: 1px solid var(--line);
    }

    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
        color: var(--muted);
    }

    .app-hero {
        position: relative;
        overflow: hidden;
        padding: 1.8rem 2rem;
        margin: 0 0 1.5rem;
        border: 1px solid rgba(255, 255, 255, .16);
        border-radius: 22px;
        background: linear-gradient(118deg, #102b55 0%, #1769e0 67%, #3988ef 100%);
        box-shadow: 0 14px 35px rgba(31, 75, 132, .17);
        color: white;
    }

    .app-hero::after {
        position: absolute;
        content: "";
        width: 250px;
        height: 250px;
        right: -55px;
        top: -135px;
        border: 1px solid rgba(255,255,255,.15);
        border-radius: 50%;
        box-shadow: 0 0 0 28px rgba(255,255,255,.04), 0 0 0 58px rgba(255,255,255,.035);
    }

    .app-hero .eyebrow {
        margin: 0 0 .4rem;
        color: #cfe1ff;
        font-size: .75rem;
        font-weight: 700;
        letter-spacing: .14em;
        text-transform: uppercase;
    }

    .app-hero h1 {
        margin: 0;
        color: #fff;
        font-size: clamp(1.8rem, 3vw, 2.45rem);
        line-height: 1.15;
        letter-spacing: -.045em;
    }

    .app-hero p {
        position: relative;
        z-index: 1;
        max-width: 720px;
        margin: .7rem 0 0;
        color: #e2edff;
        font-size: .98rem;
    }

    [data-testid="stMetric"] {
        min-height: 118px;
        padding: 1rem 1.1rem;
        border: 1px solid var(--line);
        border-radius: 16px;
        background: var(--surface);
        box-shadow: 0 3px 12px rgba(20, 43, 77, .035);
    }

    [data-testid="stMetricLabel"] {
        color: var(--muted);
        font-size: .82rem;
        font-weight: 600;
    }

    [data-testid="stMetricValue"] {
        color: var(--ink);
        font-size: 1.8rem;
        font-weight: 700;
        letter-spacing: -.035em;
    }

    [data-testid="stDataFrame"] {
        overflow: hidden;
        border: 1px solid var(--line);
        border-radius: 14px;
        background: white;
    }

    .stButton > button, .stDownloadButton > button {
        min-height: 2.7rem;
        border-radius: 10px;
        font-weight: 650;
        transition: transform .15s ease, box-shadow .15s ease;
    }

    .stButton > button[kind="primary"] {
        border: 0;
        background: linear-gradient(110deg, var(--brand), #3988ef);
        box-shadow: 0 5px 13px rgba(23, 105, 224, .2);
    }

    .stButton > button:hover, .stDownloadButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 6px 16px rgba(23, 65, 120, .12);
    }

    [data-testid="stAlert"] {
        border-radius: 12px;
    }

    [data-testid="stExpander"] {
        overflow: hidden;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: white;
    }

    hr {
        border-color: var(--line);
    }

    @media (max-width: 700px) {
        .block-container {
            padding-top: 1.1rem;
            padding-left: 1rem;
            padding-right: 1rem;
        }

        .app-hero {
            padding: 1.35rem 1.25rem;
            border-radius: 16px;
        }
    }
</style>
"""


def configure_page():
    """Set Streamlit page metadata and apply the shared visual theme."""
    st.set_page_config(page_title=APP_TITLE, page_icon="📡", layout="wide")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

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

def render_header():
    """Render the page title and explain the availability assumption."""
    st.markdown("""
    <section class="app-hero">
        <p class="eyebrow">Network operations · Monthly reporting</p>
        <h1>Device availability</h1>
        <p>Import a monthly network snapshot, review device downtime, and export a filtered report.</p>
    </section>
    """, unsafe_allow_html=True)
    st.caption(
        "One CSV snapshot per month · Continuous 24/7 operating period "
        "· Import on the first day of the month"
    )
    st.info(
        "**Availability assumption:** `Down (h.m)` is treated as downtime attributable "
        "to the selected reporting month. Availability is calculated against every "
        "minute in that calendar month (28, 29, 30 or 31 days). If this field is only "
        "the duration of an outage still in progress at snapshot time, the percentage "
        "is an estimate, not true monthly availability. Accurate monthly uptime requires "
        "complete outage history or monthly downtime totals."
    )


def render_import_preview(conn, preview_df, snapshot_date):
    """Show the uploaded data preview and save it after date validation."""
    st.subheader("Import preview")
    c1, c2, c3 = st.columns(3)
    c1.metric("Device records", f"{len(preview_df):,}")
    c2.metric("Reported down", f"{int(preview_df['is_down'].sum()):,}")
    c3.metric("Reported up / no downtime", f"{int((~preview_df['is_down']).sum()):,}")

    preview = preview_df[
        ["device_name", "Reference", "Type", "Down (h.m)", "down_minutes", "Last Up"]
    ].rename(columns={"down_minutes": "Downtime (minutes)"})
    with st.expander("Preview uploaded records", expanded=False):
        st.dataframe(preview, use_container_width=True, hide_index=True)

    if snapshot_date.day != 1:
        st.error("Choose the 1st day of the month before importing.")
        return

    if st.button(f"Save {snapshot_date.strftime('%B %Y')} snapshot", type="primary"):
        saved, message = save_snapshot(conn, preview_df, snapshot_date)
        (st.success if saved else st.error)(message)


def render_import_section(conn):
    """Render the sidebar uploader and handle an uploaded snapshot."""
    with st.sidebar:
        st.header("Import snapshot")
        st.caption("Add one monitoring export for each reporting month.")
        uploaded = st.file_uploader("Monitoring-system CSV", type=["csv", "txt"])
        snapshot_date = st.date_input(
            "Snapshot date",
            value=date.today().replace(day=1),
            help="Choose the first day of the reporting month.",
        )
        st.caption("Each month can be imported once.")

    if uploaded is None:
        return

    try:
        preview_df = prepare_data(uploaded.getvalue())
        render_import_preview(conn, preview_df, snapshot_date)
    except Exception as exc:
        st.error(f"Could not read this file: {exc}")


def build_monthly_report(conn, month_key):
    """Load a month's saved rows and derive availability fields."""
    report = load_month(conn, month_key)
    minutes_in_month = month_minutes(month_key)
    report["availability_pct"] = report["down_minutes"].apply(
        lambda value: calculate_availability(value, minutes_in_month)
    )
    report["downtime_exceeds_month"] = report["down_minutes"] > minutes_in_month
    return report, minutes_in_month


def render_report_metrics(report, month_key, minutes_in_month):
    """Show the four report totals and the selected month's operating window."""
    total = len(report)
    down_count = int(report["is_down"].sum())
    average_availability = report["availability_pct"].mean() if total else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Devices in snapshot", f"{total:,}")
    c2.metric("Reported down", f"{down_count:,}")
    c3.metric("Average device availability", f"{average_availability:.2f}%")
    c4.metric("Total reported downtime", format_minutes(report["down_minutes"].sum()))

    st.caption(
        f"{month_key} has {minutes_in_month // 60:,} operating hours "
        f"({minutes_in_month:,} minutes), assuming continuous 24/7 availability."
    )
    if report["downtime_exceeds_month"].any():
        st.warning(
            "One or more devices have downtime greater than the selected month's duration. "
            "Their availability is capped at 0.00%; check the source values."
        )
    return total


def filter_report(report, status_filter, sort_order, search):
    """Apply the report's status, search, and sort controls."""
    view = report.copy()
    if status_filter == "Reported down only":
        view = view[view["is_down"] == 1]
    elif status_filter == "Down = 0 only":
        view = view[view["down_minutes"] == 0]

    query = search.strip().lower()
    if query:
        matches_query = (
            view["device_name"].fillna("").str.lower().str.contains(query, regex=False)
            | view["reference"].fillna("").str.lower().str.contains(query, regex=False)
            | view["device_type"].fillna("").str.lower().str.contains(query, regex=False)
            | view["description"].fillna("").str.lower().str.contains(query, regex=False)
        )
        view = view[matches_query]

    if sort_order == "Lowest availability first":
        return view.sort_values("availability_pct", ascending=True)
    if sort_order == "Highest availability first":
        return view.sort_values("availability_pct", ascending=False)
    return view.sort_values("device_name", ascending=True)


def render_device_filters(report):
    """Render the filter controls and return the filtered device rows."""
    filter_col, sort_col = st.columns([2, 2])
    with filter_col:
        status_filter = st.radio(
            "Show devices",
            ["All devices", "Reported down only", "Down = 0 only"],
            horizontal=True,
        )
    with sort_col:
        sort_order = st.selectbox(
            "Sort availability",
            [
                "Lowest availability first",
                "Highest availability first",
                "Device name",
            ],
        )
    search = st.text_input("Search by device, reference, type or description")
    return filter_report(report, status_filter, sort_order, search)


def format_device_table(view):
    """Build the user-facing table while retaining source values for export."""
    display = view[
        [
            "device_name",
            "reference",
            "device_type",
            "device_class",
            "last_up",
            "down_hm",
            "down_minutes",
            "availability_pct",
            "is_down",
        ]
    ].copy()
    display["Status"] = display["is_down"].map(
        {1: "DOWN (reported)", 0: "No downtime reported"}
    )
    display["Downtime"] = display["down_minutes"].apply(format_minutes)
    display["Availability"] = display["availability_pct"].map(
        lambda value: f"{value:.2f}%"
    )
    display = display.rename(
        columns={
            "device_name": "Device",
            "reference": "Reference",
            "device_type": "Type",
            "device_class": "Class",
            "last_up": "Last Up",
            "down_hm": "Source Down (h.m)",
        }
    ).drop(columns=["down_minutes", "availability_pct", "is_down"])
    return display[
        [
            "Device",
            "Reference",
            "Type",
            "Source Down (h.m)",
            "Downtime",
            "Availability",
            "Status",
            "Last Up",
            "Class",
        ]
    ]


def render_device_details(view, minutes_in_month):
    """Show optional expanded information for each filtered device."""
    if not st.checkbox("Show / hide expanded device information", value=False):
        return

    st.markdown("#### Expanded device information")
    if view.empty:
        st.info("No devices match the current filters.")
        return

    for _, device in view.iterrows():
        label = (
            f"{device['device_name']} | "
            f"{device['availability_pct']:.2f}% availability"
        )
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
                "DOWN (reported)" if int(device["is_down"]) else "No downtime reported",
            )


def render_device_export(view, month_key):
    """Download the filtered rows, including the original report fields."""
    export_view = view.copy()
    export_view["Monthly availability (%)"] = export_view["availability_pct"].round(2)
    export_view["Monthly downtime (formatted)"] = export_view["down_minutes"].apply(
        format_minutes
    )
    csv_out = export_view.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Download filtered device report (CSV)",
        data=csv_out,
        file_name=f"device_availability_{month_key}.csv",
        mime="text/csv",
    )


def render_snapshot_history(conn):
    """Display the month-by-month snapshot totals."""
    st.subheader("Snapshot history")
    history = pd.read_sql_query(
        """
        SELECT snapshot_month,
               COUNT(*) AS devices,
               SUM(is_down) AS reported_down,
               SUM(CASE WHEN is_down = 0 THEN 1 ELSE 0 END) AS no_downtime_reported,
               SUM(down_minutes) AS ongoing_downtime_minutes
        FROM monthly_snapshots
        GROUP BY snapshot_month
        ORDER BY snapshot_month
        """,
        conn,
    )
    if history.empty:
        return

    history["ongoing_downtime"] = history["ongoing_downtime_minutes"].apply(
        format_minutes
    )
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
            "ongoing_downtime": "Ongoing downtime total",
        }
    )
    st.dataframe(history_display, use_container_width=True, hide_index=True)
    st.caption(
        "Historical averages use the same downtime assumption described above; "
        "complete outage-event history is needed for verified monthly uptime."
    )


def render_monthly_report(conn):
    """Render the report, filters, expanded device details, and exports."""
    st.divider()
    st.subheader("Monthly report")
    months = load_months(conn)
    if not months:
        st.info(
            "No monthly snapshots have been saved yet. "
            "Upload your first CSV using the sidebar."
        )
        return

    selected_month = st.selectbox("Reporting month", months)
    report, minutes_in_month = build_monthly_report(conn, selected_month)
    total = render_report_metrics(report, selected_month, minutes_in_month)

    st.markdown("#### Device availability")
    view = render_device_filters(report)
    st.dataframe(
        format_device_table(view),
        use_container_width=True,
        hide_index=True,
        height=520,
    )
    st.caption(
        f"Showing {len(view):,} of {total:,} devices. "
        "Devices with Down (h.m) = 0 receive 100.00% availability."
    )

    render_device_details(view, minutes_in_month)
    render_device_export(view, selected_month)
    render_snapshot_history(conn)


def render_calculation_notes():
    """Explain the source-field assumptions and availability formula."""
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


def main():
    """Compose the Streamlit app from its independent UI sections."""
    configure_page()
    render_header()
    conn = get_connection()
    render_import_section(conn)
    render_monthly_report(conn)
    render_calculation_notes()


if __name__ == "__main__":
    main()
