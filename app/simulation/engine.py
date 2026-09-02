import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import Settings
from app.db_indexes import ensure_indexes
from app.dialers.mode_router import ModeRouter
from app.dialers.predictive_dialer import PredictiveDialer
from app.dialers.progressive_dialer import ProgressiveDialer
from app.logging_config import log_event
from app.metrics.campaign_metrics import CampaignMetrics, CampaignMetricsCollector
from app.metrics.registry import MetricsRegistry
from app.models.agent import Agent
from app.models.base import utc_now
from app.models.borrower import Borrower
from app.models.campaign import Campaign, PacingConfig
from app.models.enums import AgentState, CampaignStatus
from app.pacing.metrics_snapshot import MetricsSnapshotBuilder
from app.providers.base import ProviderEvent
from app.providers.registry import build_registry
from app.repositories.agent_repo import AgentRepository
from app.repositories.borrower_repo import BorrowerRepository
from app.repositories.call_repo import CallRepository
from app.repositories.campaign_repo import CampaignRepository
from app.repositories.decision_repo import DecisionRepository
from app.repositories.event_repo import EventRepository
from app.safety.safety_controller import SafetyController
from app.services.agent_availability import AgentAvailabilityTracker
from app.services.call_allocator import CallAllocator
from app.services.event_processor import EventProcessor
from app.services.provider_health import ProviderHealthManager
from app.services.reservation_service import ReservationService
from app.services.retry_service import RetryService
from app.services.wrap_up_service import WrapUpService
from app.simulation.agent_simulator import AgentSimulator
from app.simulation.borrower_simulator import apply_answer_rate, apply_talk_time, configure_provider
from app.simulation.config import SimulationConfig
from app.simulation.fault_injector import FaultInjector, FaultResult
from app.simulation.invariants import InvariantChecker, InvariantViolation
from app.workers.dialer_worker import DialerWorker
from app.workers.recovery_worker import RecoveryWorker

logger = logging.getLogger(__name__)

INVARIANT_CHECK_INTERVAL_SECONDS = 0.1
DRAIN_POLL_SECONDS = 0.05
DRAIN_BUDGET_MULTIPLIER = 2.0
MIN_DRAIN_SECONDS = 1.0

SIMULATION_COLLECTIONS = (
    "campaigns",
    "agents",
    "borrowers",
    "calls",
    "provider_events",
    "pacing_decisions",
    "safety_decisions",
)


@dataclass
class SimulationReport:
    config: SimulationConfig
    metrics: CampaignMetrics | None
    violations: list[InvariantViolation] = field(default_factory=list)
    faults: list[FaultResult] = field(default_factory=list)
    ticks: int = 0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.violations and self.error is None


class SimulationEngine:
    def __init__(self, database: AsyncIOMotorDatabase, settings: Settings) -> None:
        self._database = database
        self._settings = settings

    async def run(self, config: SimulationConfig) -> SimulationReport:
        components = self._build_components(config)
        campaign = await self._seed(config)
        configure_provider(components["registry"].get(config.provider_name), config)

        simulator: AgentSimulator = components["agent_simulator_factory"](campaign.id)
        report = SimulationReport(config=config, metrics=None)
        workers: list[DialerWorker] = []

        try:
            await simulator.log_everyone_in()
            simulator.start_heartbeats()
            workers = components["worker_factory"](config.worker_count)
            for worker in workers:
                worker.start()
            components["recovery"].start()

            report.violations.extend(
                await self._run_timeline(config, campaign, components, simulator)
            )
            report.faults.extend(components["applied_faults"])
        except Exception as error:
            report.error = f"{type(error).__name__}: {error}"
        finally:
            for worker in workers:
                await worker.stop()
            await self._stop_campaign(campaign.id)
            await self._drain(config, components, campaign.id)
            await components["recovery"].stop()
            await simulator.stop()
            await components["registry"].shutdown()
            await self._settle(components, campaign.id)

        report.violations.extend(
            await components["invariants"].check(campaign.id, final=True)
        )
        report.metrics = await components["metrics"].collect(await self._reload(campaign.id))
        self._log_outcome(report)
        return report

    async def _run_timeline(
        self,
        config: SimulationConfig,
        campaign: Campaign,
        components: dict,
        simulator: AgentSimulator,
    ) -> list[InvariantViolation]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.wall_clock_seconds
        pending_changes = sorted(config.availability_schedule, key=lambda item: item.at_second)
        pending_conditions = sorted(config.condition_schedule, key=lambda item: item.at_second)
        provider = components["registry"].get(config.provider_name)
        started_at = loop.time()
        violations: list[InvariantViolation] = []

        while loop.time() < deadline:
            elapsed_simulated = (loop.time() - started_at) * config.time_scale
            while pending_changes and pending_changes[0].at_second <= elapsed_simulated:
                change = pending_changes.pop(0)
                result = await components["faults"].agent_availability_drop(
                    simulator, change.agents_offline
                )
                components["applied_faults"].append(result)

            while pending_conditions and pending_conditions[0].at_second <= elapsed_simulated:
                change = pending_conditions.pop(0)
                if change.answer_rate is not None:
                    apply_answer_rate(provider, change.answer_rate)
                if change.avg_talk_time_seconds is not None:
                    apply_talk_time(provider, config, change.avg_talk_time_seconds)

            found = await components["invariants"].check(campaign.id)
            if found:
                confirmed = await components["invariants"].check(campaign.id)
                if confirmed:
                    violations.extend(confirmed)
                    break

            await asyncio.sleep(INVARIANT_CHECK_INTERVAL_SECONDS)
        return violations

    def _build_components(self, config: SimulationConfig) -> dict:
        agents = AgentRepository(self._database)
        borrowers = BorrowerRepository(self._database)
        calls = CallRepository(self._database)
        events = EventRepository(self._database)
        decisions = DecisionRepository(self._database)
        campaigns_repo = CampaignRepository(self._database)

        counters = MetricsRegistry()
        health = ProviderHealthManager(self._settings)
        retries = RetryService(health, self._settings)
        availability = AgentAvailabilityTracker(self._settings)
        processor = EventProcessor(
            call_repository=calls,
            event_repository=events,
            agent_repository=agents,
            borrower_repository=borrowers,
            retry_service=retries,
            settings=self._settings,
        )

        async def on_event(event: ProviderEvent) -> None:
            health.record_event_received(event.provider_name)
            await processor.process_event(event)

        registry = build_registry(on_event=on_event, seed=config.seed)
        reservations = ReservationService(agents, borrowers, self._settings, counters)
        allocator = CallAllocator(
            reservation_service=reservations,
            call_repository=calls,
            agent_repository=agents,
            borrower_repository=borrowers,
            provider_registry=registry,
            health_manager=health,
            retry_service=retries,
            settings=self._settings,
            registry=counters,
        )
        safety = SafetyController(
            agent_repository=agents,
            call_repository=calls,
            decision_repository=decisions,
            health_manager=health,
            availability_tracker=availability,
            settings=self._settings,
        )
        snapshots = MetricsSnapshotBuilder(agents, calls, health, self._settings)
        dialer_arguments = {
            "snapshot_builder": snapshots,
            "safety_controller": safety,
            "call_allocator": allocator,
            "decision_repository": decisions,
            "settings": self._settings,
        }
        router = ModeRouter(
            progressive_dialer=ProgressiveDialer(**dialer_arguments),
            predictive_dialer=PredictiveDialer(**dialer_arguments),
        )

        def worker_factory(count: int) -> list[DialerWorker]:
            return [
                DialerWorker(
                    campaign_repository=campaigns_repo,
                    agent_repository=agents,
                    mode_router=router,
                    wrap_up_service=WrapUpService(agents, self._settings),
                    settings=self._settings,
                )
                for _ in range(count)
            ]

        return {
            "registry": registry,
            "agents": agents,
            "borrowers": borrowers,
            "calls": calls,
            "invariants": InvariantChecker(agents, calls, self._settings),
            "metrics": CampaignMetricsCollector(
                agent_repository=agents,
                call_repository=calls,
                decision_repository=decisions,
                registry=counters,
                settings=self._settings,
            ),
            "faults": FaultInjector(registry, events, processor),
            "applied_faults": [],
            "recovery": RecoveryWorker(
                agent_repository=agents,
                borrower_repository=borrowers,
                call_repository=calls,
                provider_registry=registry,
                retry_service=retries,
                settings=self._settings,
            ),
            "agent_simulator_factory": lambda campaign_id: AgentSimulator(
                agents, campaign_id, config
            ),
            "worker_factory": worker_factory,
        }

    async def _seed(self, config: SimulationConfig) -> Campaign:
        await ensure_indexes(self._database)
        for collection in SIMULATION_COLLECTIONS:
            await self._database[collection].delete_many({})

        campaign = Campaign(
            name=f"Simulation {config.name}",
            status=CampaignStatus.RUNNING,
            dialing_mode=config.dialing_mode,
            provider_name=config.provider_name,
            max_concurrent_calls=max(config.agents * 3, 10),
            pacing_config=PacingConfig(baseline_answer_rate=config.baseline_answer_rate),
        )
        await self._database["campaigns"].insert_one(campaign.to_mongo())

        await self._database["agents"].insert_many(
            Agent(
                campaign_id=campaign.id,
                name=f"Agent {number:04d}",
                state=AgentState.OFFLINE,
            ).to_mongo()
            for number in range(1, config.agents + 1)
        )
        await self._database["borrowers"].insert_many(
            Borrower(
                campaign_id=campaign.id,
                name=f"Borrower {number:05d}",
                phone_number=f"+1555{number:07d}",
            ).to_mongo()
            for number in range(1, config.borrowers + 1)
        )
        return campaign

    async def _drain(
        self,
        config: SimulationConfig,
        components: dict,
        campaign_id: str,
    ) -> None:
        calls = components["calls"]
        budget = max(
            MIN_DRAIN_SECONDS,
            config.scaled(config.ring_duration_seconds + config.avg_talk_time_seconds)
            * DRAIN_BUDGET_MULTIPLIER,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget

        while loop.time() < deadline:
            if not await calls.find_active(campaign_id):
                return
            await asyncio.sleep(DRAIN_POLL_SECONDS)

        remaining = len(await calls.find_active(campaign_id))
        if remaining:
            log_event(
                logger,
                logging.INFO,
                "simulation_drain_incomplete",
                f"{remaining} calls were still active after {budget:.2f}s of draining",
                campaign_id=campaign_id,
            )

    async def _settle(self, components: dict, campaign_id: str) -> None:
        expiry = utc_now() - timedelta(seconds=1)
        await self._database["agents"].update_many(
            {"campaign_id": campaign_id, "reserved_by": {"$ne": None}},
            {"$set": {"lease_expires_at": expiry}},
        )
        await self._database["borrowers"].update_many(
            {"campaign_id": campaign_id, "reserved_by": {"$ne": None}},
            {"$set": {"lease_expires_at": expiry}},
        )
        await components["recovery"].run_sweeps()

    async def _stop_campaign(self, campaign_id: str) -> None:
        await self._database["campaigns"].update_one(
            {"_id": campaign_id},
            {"$set": {"status": CampaignStatus.STOPPED.value, "updated_at": utc_now()}},
        )

    async def _reload(self, campaign_id: str) -> Campaign:
        document = await self._database["campaigns"].find_one({"_id": campaign_id})
        return Campaign.from_mongo(document)

    def _log_outcome(self, report: SimulationReport) -> None:
        log_event(
            logger,
            logging.INFO if report.passed else logging.ERROR,
            "simulation_completed",
            f"Simulation {report.config.name} "
            f"({report.config.dialing_mode.value}) "
            f"{'passed' if report.passed else 'FAILED'} "
            f"with {len(report.violations)} invariant violations",
        )
