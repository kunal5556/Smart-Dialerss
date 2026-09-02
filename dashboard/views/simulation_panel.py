import pandas as pd
import streamlit as st

from dashboard.api_client import ApiError, SmartDialerClient

SCENARIOS = ["A", "B", "C", "D", "faults"]
MODES = ["PROGRESSIVE", "PREDICTIVE"]


def render(client: SmartDialerClient) -> None:
    st.subheader("Simulation")

    with st.form("simulation_form"):
        scenario = st.selectbox("Scenario", SCENARIOS, key="sim_scenario")
        mode = st.selectbox("Dialing mode", MODES, key="sim_mode")
        agents = st.number_input(
            "Agents", min_value=1, max_value=200, value=10, key="sim_agents"
        )
        borrowers = st.number_input(
            "Borrowers", min_value=1, max_value=5000, value=300, key="sim_borrowers"
        )
        duration = st.number_input(
            "Simulated seconds",
            min_value=10.0,
            max_value=3600.0,
            value=300.0,
            key="sim_duration",
        )
        time_scale = st.number_input(
            "Time scale",
            min_value=1.0,
            max_value=300.0,
            value=60.0,
            key="sim_time_scale",
            help=(
                "Compresses the simulated timeline but not real database or provider round "
                "trips. Keep it modest: 60 simulated seconds at 60x is one real second, "
                "which is shorter than a single dialer tick."
            ),
        )
        submitted = st.form_submit_button("Start simulation")

    if submitted:
        payload = {
            "scenario": scenario,
            "dialing_mode": mode,
            "agents": int(agents),
            "borrowers": int(borrowers),
            "duration_seconds": float(duration),
            "time_scale": float(time_scale),
        }
        try:
            started = client.start_simulation(payload)
        except ApiError as error:
            st.error(error.message)
        else:
            st.success(f"Simulation {started['id'][:8]} started")

    st.divider()
    runs = client.list_simulations()

    if not runs:
        st.info("No simulations have been run yet.")
        return

    running = [run for run in runs if run["status"] == "RUNNING"]
    if running:
        st.info(f"Simulation {running[0]['id'][:8]} is running.")

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Run": run["id"][:8],
                    "Scenario": run["scenario"],
                    "Mode": run["dialing_mode"],
                    "Status": run["status"],
                    "Passed": "—" if run["passed"] is None else ("yes" if run["passed"] else "no"),
                    "Violations": ", ".join(run["violations"]) or "—",
                    "Error": run["error"] or "—",
                }
                for run in runs
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    finished = [run for run in runs if run["metrics"]]
    if finished:
        st.caption("Result of the most recent finished run")
        st.json(finished[0]["metrics"])
