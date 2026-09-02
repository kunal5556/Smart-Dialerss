import pandas as pd
import streamlit as st

from dashboard.api_client import SmartDialerClient
from dashboard.formatting import duration, milliseconds, percentage

CALL_STATE_COLUMNS = [
    ("calls_ringing", "Ringing"),
    ("calls_connected", "Connected"),
    ("calls_completed", "Completed"),
    ("calls_failed", "Failed"),
    ("calls_cancelled", "Cancelled"),
]


def render(client: SmartDialerClient, campaign_id: str) -> None:
    st.subheader("Metrics")
    with st.spinner("Loading metrics"):
        metrics = client.get_metrics(campaign_id)
        history = client.get_metrics_history(campaign_id)

    columns = st.columns(4)
    columns[0].metric("Talk utilization", percentage(metrics["talk_utilization"]))
    columns[1].metric("Productive utilization", percentage(metrics["productive_utilization"]))
    columns[2].metric("Answer rate", percentage(metrics["answer_rate"]))
    columns[3].metric("Active calls", metrics["active_calls"])

    second_row = st.columns(4)
    second_row[0].metric("Calls initiated", metrics["calls_initiated"])
    second_row[1].metric("Peak concurrent", metrics["peak_concurrent_calls"])
    second_row[2].metric("Avg talk time", duration(metrics["average_talk_time_seconds"]))
    second_row[3].metric("Avg setup", milliseconds(metrics["average_setup_time_ms"]))

    st.caption("Calls by state")
    st.bar_chart(
        pd.DataFrame(
            [{label: metrics[key] for key, label in CALL_STATE_COLUMNS}]
        ).T.rename(columns={0: "calls"})
    )

    if not history:
        st.info("No history samples yet — they appear once the campaign is running.")
        return

    frame = pd.DataFrame(history)
    frame["collected_at"] = pd.to_datetime(frame["collected_at"], format="ISO8601")
    frame = frame.set_index("collected_at")

    st.caption("Utilization over time")
    st.line_chart(frame[["talk_utilization"]].dropna())

    st.caption("Active calls over time")
    st.line_chart(frame[["active_calls", "calls_ringing"]])
