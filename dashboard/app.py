import streamlit as st

from dashboard.api_client import (
    API_KEY_SECRET,
    BASE_URL_SECRET,
    ApiError,
    ApiUnreachable,
    MissingConfiguration,
    SmartDialerClient,
    resolve_setting,
)
from dashboard.views import (
    agent_panel,
    calls_view,
    campaign_controls,
    fault_panel,
    metrics_charts,
    pacing_panel,
    provider_panel,
    safety_panel,
    simulation_panel,
)

REFRESH_INTERVAL = "2s"


def read_secrets() -> dict | None:
    try:
        return dict(st.secrets)
    except Exception:
        return None


@st.cache_resource
def build_client() -> SmartDialerClient:
    secrets = read_secrets()
    return SmartDialerClient(
        base_url=resolve_setting(BASE_URL_SECRET, secrets) or "",
        api_key=resolve_setting(API_KEY_SECRET, secrets),
    )


def render_error(error: Exception) -> None:
    if isinstance(error, ApiUnreachable):
        st.error(error.message)
        st.info("The backend may be waking up on its free tier — this can take about 30 seconds.")
        return
    if isinstance(error, ApiError):
        if error.status_code == 401:
            st.error("The API key is missing or wrong. Check SD_API_KEY in the dashboard secrets.")
        else:
            st.error(f"{error.message}")
        return
    st.error(f"Unexpected dashboard error: {error}")


def guarded(render, *args) -> None:
    try:
        render(*args)
    except Exception as error:
        render_error(error)


def main() -> None:
    st.set_page_config(page_title="SmartDialer", page_icon="📞", layout="wide")
    st.title("SmartDialer")

    try:
        client = build_client()
    except MissingConfiguration as error:
        st.error(str(error))
        return

    st.sidebar.header("Controls")
    auto_refresh = st.sidebar.toggle("Auto refresh (2s)", value=True)
    st.sidebar.caption(f"API: {client.base_url}")

    try:
        campaigns = client.list_campaigns()
    except Exception as error:
        render_error(error)
        return

    if not campaigns:
        st.info("No campaigns yet. Create one through the API or seed demo data.")
        return

    labels = {f"{item['name']} ({item['status']})": item for item in campaigns}
    chosen_label = st.sidebar.selectbox("Campaign", list(labels))
    campaign = labels[chosen_label]

    tabs = st.tabs(
        [
            "Overview",
            "Agents",
            "Calls",
            "Pacing & Safety",
            "Providers",
            "Simulation",
            "Faults",
        ]
    )

    with tabs[0]:
        guarded(campaign_controls.render, client, campaign)
        _live(auto_refresh, metrics_charts.render, client, campaign["id"])

    with tabs[1]:
        _live(auto_refresh, agent_panel.render, client, campaign["id"])

    with tabs[2]:
        _live(auto_refresh, calls_view.render, client, campaign["id"])

    with tabs[3]:
        _live(auto_refresh, pacing_panel.render, client, campaign["id"])
        _live(auto_refresh, safety_panel.render, client, campaign["id"])

    with tabs[4]:
        _live(auto_refresh, provider_panel.render, client)

    with tabs[5]:
        _live(auto_refresh, simulation_panel.render, client)

    with tabs[6]:
        guarded(fault_panel.render, client, campaign["id"])


def _live(auto_refresh: bool, render, *args) -> None:
    if not auto_refresh:
        guarded(render, *args)
        return

    @st.fragment(run_every=REFRESH_INTERVAL)
    def _fragment() -> None:
        guarded(render, *args)

    _fragment()


main()
