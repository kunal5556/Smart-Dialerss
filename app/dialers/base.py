import logging
from dataclasses import dataclass

from app.config import Settings
from app.logging_config import log_event
from app.models.campaign import Campaign
from app.pacing.metrics_snapshot import MetricsSnapshotBuilder
from app.pacing.pacing_engine import PacingEngineConfig, compute_request
from app.repositories.decision_repo import DecisionRepository
from app.safety.models import SafetyDecision
from app.safety.safety_controller import SafetyController
from app.services.call_allocator import AllocationResult, CallAllocator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TickResult:
    requested: int
    decision: SafetyDecision
    allocation: AllocationResult


class DialerBase:
    def __init__(
        self,
        snapshot_builder: MetricsSnapshotBuilder,
        safety_controller: SafetyController,
        call_allocator: CallAllocator,
        decision_repository: DecisionRepository,
        settings: Settings,
    ) -> None:
        self._snapshots = snapshot_builder
        self._safety = safety_controller
        self._allocator = call_allocator
        self._decisions = decision_repository
        self._settings = settings

    def engine_config(self) -> PacingEngineConfig:
        return PacingEngineConfig(
            soon_free_weight=self._settings.SOON_FREE_WEIGHT,
            safety_margin=self._settings.SAFETY_MARGIN,
            min_answer_rate=self._settings.MIN_ANSWER_RATE,
            max_answer_rate=self._settings.MAX_ANSWER_RATE,
            volatility_threshold=self._settings.VOLATILITY_THRESHOLD,
            volatility_factor=self._settings.VOLATILITY_FACTOR,
            max_request_per_tick=self._settings.MAX_REQUEST_PER_TICK,
        )

    async def tick(self, campaign: Campaign, worker_id: str) -> TickResult:
        snapshot = await self._snapshots.build_snapshot(campaign)
        request = compute_request(snapshot, self.engine_config())
        idle = request.requested == 0

        pacing_decision_id = None
        if not idle:
            pacing_decision = await self._decisions.record_pacing_request(campaign.id, request)
            pacing_decision_id = pacing_decision.id

        decision = await self._safety.evaluate(
            campaign, request, pacing_decision_id, persist=not idle
        )
        allocation = await self._allocator.allocate(campaign, decision, worker_id)

        log_event(
            logger,
            logging.DEBUG if idle else logging.INFO,
            "dialer_tick",
            f"{campaign.dialing_mode.value} tick requested {request.requested}, "
            f"approved {decision.approved}, allocated {allocation.allocated}",
            campaign_id=campaign.id,
            worker_id=worker_id,
        )
        return TickResult(
            requested=request.requested,
            decision=decision,
            allocation=allocation,
        )
