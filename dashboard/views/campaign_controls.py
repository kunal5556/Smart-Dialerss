import streamlit as st

from dashboard.api_client import ApiError, SmartDialerClient

MODES = ["PROGRESSIVE", "PREDICTIVE"]


def render(client: SmartDialerClient, campaign: dict) -> None:
    st.subheader("Campaign")
    campaign = _refreshed(client, campaign)
    columns = st.columns(4)
    columns[0].metric("Status", campaign["status"])
    columns[1].metric("Mode", campaign["dialing_mode"])
    columns[2].metric("Provider", campaign["provider_name"])
    columns[3].metric("Max concurrent", campaign["max_concurrent_calls"])

    actions = st.columns(3)
    if actions[0].button("Start", width="stretch"):
        _run(lambda: client.start_campaign(campaign["id"]), "Campaign started")
    if actions[1].button("Pause", width="stretch"):
        _run(lambda: client.pause_campaign(campaign["id"]), "Campaign paused")
    if actions[2].button("Stop", width="stretch"):
        _run(lambda: client.stop_campaign(campaign["id"]), "Campaign stopped")

    st.divider()
    st.caption("Dialing mode")
    selected = st.radio(
        "Dialing mode",
        MODES,
        index=MODES.index(campaign["dialing_mode"]),
        horizontal=True,
        label_visibility="collapsed",
        key="mode_selector",
    )
    if selected != campaign["dialing_mode"]:
        _run(lambda: client.set_mode(campaign["id"], selected), f"Mode set to {selected}")

    st.divider()
    with st.expander("Seed demo data"):
        agents = st.number_input("Agents", min_value=0, max_value=500, value=10)
        borrowers = st.number_input("Borrowers", min_value=0, max_value=5000, value=200)
        if st.button("Seed this campaign"):
            _run(
                lambda: client.seed_campaign(campaign["id"], int(agents), int(borrowers)),
                "Demo data created",
            )


def _refreshed(client: SmartDialerClient, campaign: dict) -> dict:
    try:
        return client.get_campaign(campaign["id"])
    except ApiError:
        return campaign


def _run(action, success_message: str) -> None:
    try:
        action()
    except ApiError as error:
        st.error(f"{error.message}")
        return
    st.success(success_message)
    st.rerun()
