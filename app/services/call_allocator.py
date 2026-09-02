import asyncio
import logging
from dataclasses import dataclass

from pymongo.errors import PyMongoError

from app.config import Settings
from app.logging_config import log_event
from app.metrics.registry import (
    COUNTER_PROVIDER_FAILURES,
    COUNTER_RETRY_ATTEMPTS,
    COUNTER_RETRY_SUPPRESSED,
    MetricsRegistry,
)
from app.models.campaign import Campaign
from app.models.enums import AgentState, CallState
from app.providers.base import OriginateRequest, OriginateResult
from app.providers.errors import ProviderError, ProviderRejected, ProviderTimeout
from app.providers.registry import ProviderRegistry
from app.repositories.agent_repo import AgentRepository
from app.repositories.borrower_repo import BorrowerReleaseOutcome, BorrowerRepository
from app.repositories.call_repo import CallRepository
from app.safety.models import SafetyDecision
from app.services.provider_health import ProviderHealthManager
from app.services.retry_service import RetryService
from app.services.reservation_service import ReservationPair, ReservationService
from app.state_machines.agent_sm import TransitionActor
from app.utils.redaction import redact_phone

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_CONTENTIONS = 3


@dataclass(frozen=True)
class AllocationResult:
    attempted: int
    allocated: int
    failed: int
    contended: int


class CallAllocator:
    def __init__(
        self,
        reservation_service: ReservationService,
        call_repository: CallRepository,
        agent_repository: AgentRepository,
        borrower_repository: BorrowerRepository,
        provider_registry: ProviderRegistry,
        health_manager: ProviderHealthManager,
        retry_service: RetryService,
        settings: Settings,
        registry: MetricsRegistry | None = None,
    ) -> None:
        self._reservations = reservation_service
        self._calls = call_repository
        self._agents = agent_repository
        self._borrowers = borrower_repository
        self._providers = provider_registry
        self._health = health_manager
        self._retries = retry_service
        self._settings = settings
        self._registry = registry or MetricsRegistry()

    async def allocate(
        self,
        campaign: Campaign,
        decision: SafetyDecision,
        worker_id: str,
    ) -> AllocationResult:
        attempted = 0
        allocated = 0
        failed = 0
        contended = 0
        consecutive_contentions = 0

        for _ in range(decision.approved):
            attempted += 1
            pair = await self._reserve_pair(campaign.id, worker_id)
            if pair is None:
                contended += 1
                consecutive_contentions += 1
                if consecutive_contentions >= MAX_CONSECUTIVE_CONTENTIONS:
                    break
                continue

            consecutive_contentions = 0
            try:
                dialled = await self._dial(campaign, pair)
            except asyncio.CancelledError:
                await self._compensate_cancelled_dial(campaign, pair)
                raise

            if dialled:
                allocated += 1
            else:
                failed += 1

        return AllocationResult(
            attempted=attempted,
            allocated=allocated,
            failed=failed,
            contended=contended,
        )

    async def _reserve_pair(self, campaign_id: str, worker_id: str) -> ReservationPair | None:
        try:
            return await self._reservations.reserve_pair(campaign_id, worker_id)
        except PyMongoError as error:
            log_event(
                logger,
                logging.ERROR,
                "reservation_failed",
                f"Database error while reserving a pair: {error}",
                campaign_id=campaign_id,
                worker_id=worker_id,
            )
            return None

    async def _dial(self, campaign: Campaign, pair: ReservationPair) -> bool:
        if pair.borrower.attempt_count > 0 and not self._retries.should_retry(
            pair.borrower, campaign.provider_name
        ):
            self._registry.increment(COUNTER_RETRY_SUPPRESSED)
            log_event(
                logger,
                logging.INFO,
                "retry_suppressed",
                "Retry gate is closed, this borrower will be dialled again later",
                campaign_id=campaign.id,
                borrower_id=pair.borrower.id,
                worker_id=pair.worker_id,
            )
            await self._release(pair, BorrowerReleaseOutcome.RELEASED)
            return False

        if pair.borrower.attempt_count > 0:
            self._registry.increment(COUNTER_RETRY_ATTEMPTS)

        call = await self._calls.create_call(
            campaign_id=campaign.id,
            agent_id=pair.agent.id,
            borrower_id=pair.borrower.id,
            provider_name=campaign.provider_name,
            worker_id=pair.worker_id,
            attempt=pair.borrower.attempt_count + 1,
        )
        if call.state is not CallState.QUEUED:
            await self._release(pair, BorrowerReleaseOutcome.RELEASED)
            return False

        await self._calls.transition_call(call.id, CallState.RESERVED)
        initiated = await self._calls.transition_call(call.id, CallState.INITIATED)
        if initiated is None:
            await self._release(pair, BorrowerReleaseOutcome.RELEASED)
            return False

        await self._agents.transition_agent(
            agent_id=pair.agent.id,
            from_state=AgentState.RESERVED,
            to_state=AgentState.DIALING,
            actor=TransitionActor.ALLOCATOR,
            expected_version=pair.agent.state_version,
        )
        await self._agents.bind_call(pair.agent.id, pair.worker_id, call.id)

        result = await self._originate(campaign, pair, call.id)
        if result is None or not result.accepted:
            self._registry.increment(COUNTER_PROVIDER_FAILURES)
            reason = "provider_timeout" if result is None else result.error_code
            await self._calls.transition_call(
                call.id, CallState.FAILED, failure_reason=reason
            )
            await self._release(pair, BorrowerReleaseOutcome.RETRY)
            return False

        attached = await self._calls.attach_provider_call_id(call.id, result.provider_call_id)
        if attached is None:
            log_event(
                logger,
                logging.ERROR,
                "provider_call_id_not_attached",
                "Could not record the provider call id, hanging up and failing the call",
                campaign_id=campaign.id,
                call_id=call.id,
                worker_id=pair.worker_id,
            )
            await self._hangup(campaign.provider_name, result.provider_call_id)
            await self._calls.transition_call(
                call.id, CallState.FAILED, failure_reason="provider_call_id_conflict"
            )
            await self._release(pair, BorrowerReleaseOutcome.RETRY)
            return False

        log_event(
            logger,
            logging.INFO,
            "call_initiated",
            f"Call handed to provider {campaign.provider_name} "
            f"for {redact_phone(pair.borrower.phone_number)}",
            campaign_id=campaign.id,
            agent_id=pair.agent.id,
            borrower_id=pair.borrower.id,
            call_id=call.id,
            worker_id=pair.worker_id,
        )
        return True

    async def _originate(
        self,
        campaign: Campaign,
        pair: ReservationPair,
        call_id: str,
    ) -> OriginateResult | None:
        provider = self._providers.get(campaign.provider_name)
        request = OriginateRequest(
            call_id=call_id,
            campaign_id=campaign.id,
            phone_number=pair.borrower.phone_number,
            timeout_seconds=self._settings.PROVIDER_TIMEOUT_SECONDS,
        )
        try:
            result = await asyncio.wait_for(
                provider.originate_call(request),
                timeout=self._settings.PROVIDER_TIMEOUT_SECONDS,
            )
        except (ProviderTimeout, asyncio.TimeoutError):
            self._health.record_originate(
                provider_name=campaign.provider_name,
                success=False,
                latency_ms=int(self._settings.PROVIDER_TIMEOUT_SECONDS * 1000),
                timed_out=True,
            )
            return None
        except ProviderRejected as error:
            self._health.record_originate(
                provider_name=campaign.provider_name,
                success=False,
                latency_ms=0,
            )
            return OriginateResult(accepted=False, latency_ms=0, error_code=error.reason)

        self._health.record_originate(
            provider_name=campaign.provider_name,
            success=result.accepted,
            latency_ms=result.latency_ms,
        )
        return result

    async def _hangup(self, provider_name: str, provider_call_id: str) -> None:
        try:
            await self._providers.get(provider_name).hangup_call(provider_call_id)
        except ProviderError as error:
            log_event(
                logger,
                logging.WARNING,
                "provider_hangup_failed",
                f"Could not hang up {provider_call_id}: {error}",
            )

    async def _release(self, pair: ReservationPair, outcome: BorrowerReleaseOutcome) -> None:
        await self._reservations.release_pair(pair, outcome)

    async def _compensate_cancelled_dial(
        self,
        campaign: Campaign,
        pair: ReservationPair,
    ) -> None:
        try:
            await asyncio.shield(self._release(pair, BorrowerReleaseOutcome.RELEASED))
        except asyncio.CancelledError:
            pass
        except PyMongoError as error:
            log_event(
                logger,
                logging.ERROR,
                "cancelled_dial_release_failed",
                f"Could not release a cancelled dial, the lease will expire instead: {error}",
                campaign_id=campaign.id,
                agent_id=pair.agent.id,
                borrower_id=pair.borrower.id,
                worker_id=pair.worker_id,
            )
        else:
            log_event(
                logger,
                logging.WARNING,
                "cancelled_dial_released",
                "Dial was cancelled mid-flight, agent and borrower were released",
                campaign_id=campaign.id,
                agent_id=pair.agent.id,
                borrower_id=pair.borrower.id,
                worker_id=pair.worker_id,
            )
