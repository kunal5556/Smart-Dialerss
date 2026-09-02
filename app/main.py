import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pymongo.errors import OperationFailure, PyMongoError

from app import __version__
from app.api import register_api
from app.config import Settings, get_settings
from app.db import connect, disconnect, get_simulation_db, ping
from app.db_indexes import ensure_indexes
from app.dialers.mode_router import ModeRouter
from app.dialers.predictive_dialer import PredictiveDialer
from app.dialers.progressive_dialer import ProgressiveDialer
from app.logging_config import configure_logging, log_event
from app.metrics.campaign_metrics import CampaignMetricsCollector
from app.metrics.collector import MetricsSampler
from app.metrics.registry import MetricsRegistry
from app.pacing.metrics_snapshot import MetricsSnapshotBuilder
from app.providers.base import ProviderEvent
from app.providers.registry import build_registry
from app.repositories.agent_repo import AgentRepository
from app.repositories.borrower_repo import BorrowerRepository
from app.repositories.call_repo import CallRepository
from app.repositories.campaign_repo import CampaignRepository
from app.repositories.decision_repo import DecisionRepository
from app.repositories.event_repo import EventRepository
from app.repositories.metrics_repo import MetricsRepository
from app.safety.safety_controller import SafetyController
from app.services.call_allocator import CallAllocator
from app.services.agent_availability import AgentAvailabilityTracker
from app.services.event_processor import EventProcessor
from app.services.provider_health import ProviderHealthManager
from app.services.reservation_service import ReservationService
from app.services.retry_service import RetryService
from app.services.wrap_up_service import WrapUpService
from app.workers.dialer_worker import DialerWorker
from app.simulation.fault_injector import FaultInjector
from app.repositories.simulation_run_repo import SimulationRunRepository
from app.simulation.runner import SimulationRunner, stop_orphaned_simulation_campaigns
from app.workers.recovery_worker import RecoveryWorker

logger = logging.getLogger(__name__)


def build_runtime(app: FastAPI, settings: Settings) -> None:
    agents = AgentRepository()
    borrowers = BorrowerRepository()
    calls = CallRepository()
    campaigns = CampaignRepository()
    events = EventRepository()
    decisions = DecisionRepository()

    registry_counters = MetricsRegistry()
    health_manager = ProviderHealthManager(settings)
    retry_service = RetryService(health_manager, settings)
    availability_tracker = AgentAvailabilityTracker(settings)
    event_processor = EventProcessor(
        call_repository=calls,
        event_repository=events,
        agent_repository=agents,
        borrower_repository=borrowers,
        retry_service=retry_service,
        settings=settings,
    )

    async def on_event(event: ProviderEvent) -> None:
        health_manager.record_event_received(event.provider_name)
        await event_processor.process_event(event)

    registry = build_registry(on_event=on_event, seed=settings.PROVIDER_RANDOM_SEED)
    reservations = ReservationService(agents, borrowers, settings, registry_counters)
    allocator = CallAllocator(
        reservation_service=reservations,
        call_repository=calls,
        agent_repository=agents,
        borrower_repository=borrowers,
        provider_registry=registry,
        health_manager=health_manager,
        retry_service=retry_service,
        settings=settings,
        registry=registry_counters,
    )
    safety_controller = SafetyController(
        agent_repository=agents,
        call_repository=calls,
        decision_repository=decisions,
        health_manager=health_manager,
        availability_tracker=availability_tracker,
        settings=settings,
    )
    snapshot_builder = MetricsSnapshotBuilder(agents, calls, health_manager, settings)

    dialer_arguments = {
        "snapshot_builder": snapshot_builder,
        "safety_controller": safety_controller,
        "call_allocator": allocator,
        "decision_repository": decisions,
        "settings": settings,
    }
    router = ModeRouter(
        progressive_dialer=ProgressiveDialer(**dialer_arguments),
        predictive_dialer=PredictiveDialer(**dialer_arguments),
    )

    metrics_collector = CampaignMetricsCollector(
        agent_repository=agents,
        call_repository=calls,
        decision_repository=decisions,
        registry=registry_counters,
        settings=settings,
    )

    app.state.campaign_repository = campaigns
    app.state.agent_repository = agents
    app.state.borrower_repository = borrowers
    app.state.call_repository = calls
    app.state.event_repository = events
    app.state.decision_repository = decisions
    app.state.metrics_repository = MetricsRepository()
    app.state.fault_injector = FaultInjector(registry, events, event_processor)
    app.state.simulation_runner = SimulationRunner(
        get_simulation_db(), settings, SimulationRunRepository()
    )
    app.state.metrics_registry = registry_counters
    app.state.metrics_collector = metrics_collector
    app.state.metrics_sampler = MetricsSampler(
        campaign_repository=campaigns,
        metrics_collector=metrics_collector,
        metrics_repository=app.state.metrics_repository,
        settings=settings,
    )
    app.state.health_manager = health_manager
    app.state.event_processor = event_processor
    app.state.provider_registry = registry
    app.state.dialer_worker = DialerWorker(
        campaign_repository=campaigns,
        agent_repository=agents,
        mode_router=router,
        wrap_up_service=WrapUpService(agents, settings),
        settings=settings,
    )
    app.state.recovery_worker = RecoveryWorker(
        agent_repository=agents,
        borrower_repository=borrowers,
        call_repository=calls,
        provider_registry=registry,
        retry_service=retry_service,
        settings=settings,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    await connect()
    try:
        await ensure_indexes()
        log_event(logger, logging.INFO, "startup_indexes_ready", "Database indexes ensured")
    except OperationFailure as error:
        log_event(
            logger,
            logging.ERROR,
            "startup_index_conflict",
            f"Index definition conflicts with an existing index: {error}",
        )
        raise
    except PyMongoError as error:
        log_event(
            logger,
            logging.ERROR,
            "startup_database_unreachable",
            f"Could not ensure indexes because the database is unreachable: {error}",
        )

    settings = get_settings()
    build_runtime(app, settings)
    log_event(
        logger,
        logging.INFO,
        "startup_providers_ready",
        f"Registered providers: {', '.join(app.state.provider_registry.names())}",
    )

    await app.state.simulation_runner.reconcile()
    await stop_orphaned_simulation_campaigns(get_simulation_db())

    if settings.DIALER_ENABLED:
        app.state.dialer_worker.start()
        log_event(logger, logging.INFO, "startup_dialer_started", "Dialer worker started")

    if settings.RECOVERY_ENABLED:
        app.state.recovery_worker.start()
        log_event(logger, logging.INFO, "startup_recovery_started", "Recovery worker started")

    app.state.metrics_sampler.start()

    try:
        yield
    finally:
        await app.state.dialer_worker.stop()
        await app.state.recovery_worker.stop()
        await app.state.metrics_sampler.stop()
        await app.state.simulation_runner.shutdown()
        await app.state.provider_registry.shutdown()
        await disconnect()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="SmartDialer API", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_api(app)

    @app.get("/health")
    async def health(response: Response) -> dict[str, str]:
        database_connected = await ping()
        if not database_connected:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if database_connected else "degraded",
            "database": "connected" if database_connected else "error",
            "version": __version__,
        }

    return app


app = create_app()
