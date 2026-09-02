import logging
from dataclasses import dataclass
from datetime import timedelta

from app.config import Settings
from app.logging_config import log_event
from app.models.base import utc_now
from app.models.campaign import Campaign
from app.models.enums import AgentState, CallState, DialingMode, ProviderHealthStatus, SafetyVerdict
from app.repositories.agent_repo import AgentRepository
from app.repositories.call_repo import CallRepository
from app.repositories.decision_repo import DecisionRepository
from app.safety.fallback import FallbackInputs, should_fallback_to_progressive
from app.safety.models import PacingRequest, SafetyConstraintResult, SafetyDecision
from app.services.agent_availability import AgentAvailabilityTracker
from app.services.provider_health import ProviderHealthManager

logger = logging.getLogger(__name__)

CONSTRAINT_AGENT_CAPACITY = "agent_capacity"
CONSTRAINT_CAMPAIGN_CONCURRENCY = "campaign_concurrency"
CONSTRAINT_RINGING_CEILING = "ringing_ceiling"
CONSTRAINT_PROVIDER_HEALTH = "provider_health"
CONSTRAINT_PROGRESSIVE_MODE = "progressive_mode_cap"
CONSTRAINT_STALE_STATE = "stale_state"
CONSTRAINT_AVAILABILITY_DROP = "availability_drop"
CONSTRAINT_FAILURE_RATE = "failure_rate"

ACTIVE_CALL_STATES = (
    CallState.RESERVED,
    CallState.INITIATED,
    CallState.RINGING,
    CallState.ANSWERED,
    CallState.CONNECTED,
)


@dataclass(frozen=True)
class CapacityReading:
    available_agents: int
    reserved_agents: int
    active_calls: int
    ringing_calls: int
    expired_leases: int
    call_failure_rate: float
    provider_status: ProviderHealthStatus


class SafetyController:
    def __init__(
        self,
        agent_repository: AgentRepository,
        call_repository: CallRepository,
        decision_repository: DecisionRepository,
        health_manager: ProviderHealthManager,
        availability_tracker: AgentAvailabilityTracker,
        settings: Settings,
    ) -> None:
        self._agents = agent_repository
        self._calls = call_repository
        self._decisions = decision_repository
        self._health = health_manager
        self._availability = availability_tracker
        self._settings = settings

    async def evaluate(
        self,
        campaign: Campaign,
        request: PacingRequest,
        pacing_decision_id: str | None = None,
        persist: bool = True,
    ) -> SafetyDecision:
        try:
            decision = await self._evaluate(campaign, request, pacing_decision_id)
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "safety_evaluation_failed",
                f"Safety evaluation failed, rejecting all calls: {error}",
                campaign_id=campaign.id,
            )
            decision = SafetyDecision(
                campaign_id=campaign.id,
                requested=request.requested,
                approved=0,
                verdict=SafetyVerdict.REJECTED,
                constraints=[],
                binding_constraint="evaluation_error",
                snapshot_age_ms=0,
                created_at=utc_now(),
                fallback_reason="evaluation_error",
                pacing_decision_id=pacing_decision_id,
            )

        if persist:
            await self._persist(decision)
        return decision

    async def _evaluate(
        self,
        campaign: Campaign,
        request: PacingRequest,
        pacing_decision_id: str | None,
    ) -> SafetyDecision:
        reading = await self._read_capacity(campaign)
        snapshot_age_ms = self._snapshot_age_ms(request)
        snapshot_is_stale = (
            snapshot_age_ms > self._settings.MAX_SNAPSHOT_AGE_SECONDS * 1000
            or reading.expired_leases > 0
        )
        drop = self._availability.record_and_detect(campaign.id, reading.available_agents)
        availability_drop_ratio = drop.drop_ratio if drop is not None else 0.0
        progressive_equivalent = reading.available_agents
        ringing_ceiling = int(
            self._settings.MAX_RINGING_RATIO * reading.available_agents
        )

        fallback_reason = should_fallback_to_progressive(
            FallbackInputs(
                provider_status=reading.provider_status,
                availability_drop_ratio=availability_drop_ratio,
                availability_drop_threshold=self._settings.AVAILABILITY_DROP_THRESHOLD,
                ringing_calls=reading.ringing_calls,
                ringing_ceiling=ringing_ceiling,
                call_failure_rate=reading.call_failure_rate,
                failure_rate_threshold=self._settings.HIGH_FAILURE_RATE_THRESHOLD,
                snapshot_is_stale=snapshot_is_stale,
            )
        )

        constraints = self._build_constraints(
            campaign=campaign,
            request=request,
            reading=reading,
            progressive_equivalent=progressive_equivalent,
            ringing_ceiling=ringing_ceiling,
            snapshot_is_stale=snapshot_is_stale,
            availability_drop_ratio=availability_drop_ratio,
        )

        approved = max(0, min(request.requested, *(item.limit for item in constraints)))
        constraints = self._mark_binding(constraints, approved, request.requested)
        binding = next((item.name for item in constraints if item.binding), None)
        verdict = self._verdict(request.requested, approved, fallback_reason)

        return SafetyDecision(
            campaign_id=campaign.id,
            requested=request.requested,
            approved=approved,
            verdict=verdict,
            constraints=constraints,
            binding_constraint=binding,
            snapshot_age_ms=snapshot_age_ms,
            created_at=utc_now(),
            fallback_reason=fallback_reason,
            pacing_decision_id=pacing_decision_id,
        )

    async def _read_capacity(self, campaign: Campaign) -> CapacityReading:
        now = utc_now()
        agent_counts = await self._agents.count_by_state(campaign.id)
        call_counts = await self._calls.count_by_state(campaign.id)
        expired_leases = await self._agents.count_expired_leases(now)
        window_start = now - timedelta(seconds=self._settings.ANSWER_RATE_WINDOW_SECONDS)
        outcomes = await self._calls.outcome_counts_between(campaign.id, window_start, now)

        failure_rate = 0.0
        if outcomes["total"]:
            failure_rate = outcomes["system_failed"] / outcomes["total"]

        return CapacityReading(
            available_agents=agent_counts[AgentState.AVAILABLE],
            reserved_agents=agent_counts[AgentState.RESERVED],
            active_calls=sum(call_counts[state] for state in ACTIVE_CALL_STATES),
            ringing_calls=call_counts[CallState.RINGING],
            expired_leases=expired_leases,
            call_failure_rate=failure_rate,
            provider_status=self._health.get_health(campaign.provider_name).status,
        )

    def _build_constraints(
        self,
        campaign: Campaign,
        request: PacingRequest,
        reading: CapacityReading,
        progressive_equivalent: int,
        ringing_ceiling: int,
        snapshot_is_stale: bool,
        availability_drop_ratio: float,
    ) -> list[SafetyConstraintResult]:
        uncapped = request.requested

        health_limit = uncapped
        if reading.provider_status is ProviderHealthStatus.UNHEALTHY:
            health_limit = 0
        elif reading.provider_status is ProviderHealthStatus.DEGRADED:
            health_limit = progressive_equivalent

        return [
            SafetyConstraintResult(
                name=CONSTRAINT_AGENT_CAPACITY,
                limit=max(0, reading.available_agents - reading.reserved_agents),
                value=float(reading.available_agents),
            ),
            SafetyConstraintResult(
                name=CONSTRAINT_CAMPAIGN_CONCURRENCY,
                limit=max(0, campaign.max_concurrent_calls - reading.active_calls),
                value=float(reading.active_calls),
            ),
            SafetyConstraintResult(
                name=CONSTRAINT_RINGING_CEILING,
                limit=max(0, ringing_ceiling - reading.ringing_calls),
                value=float(reading.ringing_calls),
            ),
            SafetyConstraintResult(
                name=CONSTRAINT_PROVIDER_HEALTH,
                limit=health_limit,
                value=None,
            ),
            SafetyConstraintResult(
                name=CONSTRAINT_PROGRESSIVE_MODE,
                limit=(
                    progressive_equivalent
                    if request.mode is DialingMode.PROGRESSIVE
                    else uncapped
                ),
                value=float(progressive_equivalent),
            ),
            SafetyConstraintResult(
                name=CONSTRAINT_STALE_STATE,
                limit=0 if snapshot_is_stale else uncapped,
                value=float(reading.expired_leases),
            ),
            SafetyConstraintResult(
                name=CONSTRAINT_AVAILABILITY_DROP,
                limit=(
                    progressive_equivalent
                    if availability_drop_ratio > self._settings.AVAILABILITY_DROP_THRESHOLD
                    else uncapped
                ),
                value=availability_drop_ratio,
            ),
            SafetyConstraintResult(
                name=CONSTRAINT_FAILURE_RATE,
                limit=(
                    progressive_equivalent
                    if reading.call_failure_rate > self._settings.HIGH_FAILURE_RATE_THRESHOLD
                    else uncapped
                ),
                value=reading.call_failure_rate,
            ),
        ]

    def _mark_binding(
        self,
        constraints: list[SafetyConstraintResult],
        approved: int,
        requested: int,
    ) -> list[SafetyConstraintResult]:
        if approved >= requested:
            return constraints
        marked = False
        result = []
        for constraint in constraints:
            binding = not marked and constraint.limit == approved
            marked = marked or binding
            result.append(
                SafetyConstraintResult(
                    name=constraint.name,
                    limit=constraint.limit,
                    value=constraint.value,
                    binding=binding,
                )
            )
        return result

    def _verdict(
        self,
        requested: int,
        approved: int,
        fallback_reason: str | None,
    ) -> SafetyVerdict:
        if requested == 0:
            return SafetyVerdict.APPROVED
        if fallback_reason is not None and approved > 0:
            return SafetyVerdict.FALLBACK_PROGRESSIVE
        if approved == 0:
            return SafetyVerdict.REJECTED
        if approved < requested:
            return SafetyVerdict.REDUCED
        return SafetyVerdict.APPROVED

    def _snapshot_age_ms(self, request: PacingRequest) -> int:
        age = utc_now() - request.snapshot_captured_at
        return max(0, int(age.total_seconds() * 1000))

    async def _persist(self, decision: SafetyDecision) -> None:
        try:
            await self._decisions.record_safety_decision(decision)
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "safety_decision_persist_failed",
                f"Could not persist the safety decision: {error}",
                campaign_id=decision.campaign_id,
            )
