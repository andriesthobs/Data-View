# Network Device Monthly Snapshot

A first prototype for individual network-device reporting using one CSV snapshot on the first day of each month.

## Run locally

1. Install Python 3.10 or newer.
2. Extract this folder.
3. Open a terminal in this folder.
4. Run:

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

On macOS/Linux, activate with `source .venv/bin/activate`.

## Import workflow

- Upload the CSV export in the sidebar.
- Select the first day of the reporting month.
- Review the import preview.
- Save the snapshot.
- Select a saved month to view the device-level report.
- Download a filtered report as CSV.

The SQLite database `network_uptime_history.db` is created automatically in the working directory.

## Important limitation

One snapshot per month cannot calculate actual monthly uptime. It only captures device status and any ongoing downtime at the snapshot time. Outages that begin and end between monthly snapshots are invisible. This prototype intentionally does not display a false monthly uptime percentage.

For accurate monthly uptime, obtain outage start/end event history from the monitoring platform, or collect status snapshots more frequently. A future API connector can replace the CSV uploader without changing the reporting interface.
