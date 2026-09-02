from dataclasses import replace

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import enforce_fault_cooldown, require_api_key
from app.api.errors import ConflictError, NotFoundError
from app.api.routes_metrics import to_response
from app.api.schemas import FaultRequest, FaultResponse, SimulationRequest, SimulationStatus
from app.metrics.campaign_metrics import CampaignMetrics
from app.simulation.agent_simulator import AgentSimulator
from app.simulation.config import SimulationConfig
from app.simulation.fault_injector import AVAILABLE_FAULTS
from app.simulation.scenarios import SCENARIO_NAMES, build_scenario

router = APIRouter(prefix="/api/simulations", tags=["simulations"])


def to_status(run: dict) -> SimulationStatus:
    metrics = run.get("metrics")
    return SimulationStatus(
        id=run["_id"],
        scenario=run["scenario"],
        dialing_mode=run["dialing_mode"],
        status=run["status"],
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        passed=run["passed"],
        violations=run["violations"],
        error=run["error"],
        metrics=to_response(CampaignMetrics(**metrics)) if metrics else None,
    )


@router.post("", response_model=SimulationStatus, dependencies=[Depends(require_api_key)])
async def start_simulation(payload: SimulationRequest, request: Request) -> SimulationStatus:
    if payload.scenario not in SCENARIO_NAMES:
        raise NotFoundError("scenario", payload.scenario)

    config = build_scenario(
        scenario=payload.scenario,
        mode=payload.dialing_mode,
        agents=payload.agents,
        borrowers=payload.borrowers,
        duration_seconds=payload.duration_seconds,
        seed=payload.seed,
        time_scale=payload.time_scale,
    )
    config = replace(config, worker_count=payload.workers)

    try:
        run = await request.app.state.simulation_runner.start(config)
    except RuntimeError as error:
        raise ConflictError(str(error)) from error
    return to_status(run)


@router.get("", response_model=list[SimulationStatus])
async def list_simulations(request: Request) -> list[SimulationStatus]:
    runs = await request.app.state.simulation_runner.history()
    return [to_status(run) for run in runs]


@router.get("/{simulation_id}", response_model=SimulationStatus)
async def get_simulation(simulation_id: str, request: Request) -> SimulationStatus:
    run = await request.app.state.simulation_runner.get(simulation_id)
    if run is None:
        raise NotFoundError("simulation", simulation_id)
    return to_status(run)


@router.post(
    "/faults",
    response_model=FaultResponse,
    dependencies=[Depends(require_api_key), Depends(enforce_fault_cooldown)],
)
async def inject_fault(payload: FaultRequest, request: Request) -> FaultResponse:
    if payload.fault not in AVAILABLE_FAULTS:
        raise NotFoundError("fault", payload.fault)

    injector = request.app.state.fault_injector
    if payload.fault == "provider_latency_spike":
        result = injector.provider_latency_spike(payload.provider_name)
    elif payload.fault == "provider_outage":
        result = injector.provider_outage(payload.provider_name, payload.seconds)
    elif payload.fault == "duplicate_event_burst":
        result = await injector.duplicate_event_burst()
    elif payload.fault == "out_of_order_burst":
        result = await injector.out_of_order_burst()
    else:
        if payload.campaign_id is None:
            raise ConflictError("campaign_id is required to drop agent availability")
        simulator = AgentSimulator(
            request.app.state.agent_repository,
            payload.campaign_id,
            SimulationConfig(name="fault-injection"),
        )
        result = await injector.agent_availability_drop(simulator, payload.agents_offline)

    return FaultResponse(fault=result.fault, detail=result.detail, affected=result.affected)
