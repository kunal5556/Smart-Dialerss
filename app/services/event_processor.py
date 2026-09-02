import logging
from dataclasses import dataclass

from app.config import Settings
from app.logging_config import log_event
from app.models.call import Call
from app.models.enums import AgentState, CallState, EventProcessingStatus
from app.providers.base import ProviderEvent
from app.repositories.agent_repo import AgentRepository
from app.repositories.borrower_repo import BorrowerRepository
from app.repositories.call_repo import CallRepository
from app.repositories.event_repo import EventRecordResult, EventRepository
from app.services.retry_service import RetryService
from app.state_machines.agent_sm import TransitionActor
from app.state_machines.errors import StateMachineError
from app.state_machines.call_sm import (
    EventApplicability,
    agent_state_for_call_state,
    should_apply_event,
)

logger = logging.getLogger(__name__)

EVENT_TYPE_TO_CALL_STATE: dict[str, CallState] = {
    "RINGING": CallState.RINGING,
    "ANSWERED": CallState.ANSWERED,
    "CONNECTED": CallState.CONNECTED,
    "COMPLETED": CallState.COMPLETED,
    "FAILED": CallState.FAILED,
    "CANCELLED": CallState.CANCELLED,
}

IGNORED_APPLICABILITY_STATUS: dict[EventApplicability, EventProcessingStatus] = {
    EventApplicability.IGNORE_TERMINAL: EventProcessingStatus.STALE_IGNORED,
    EventApplicability.IGNORE_STALE: EventProcessingStatus.STALE_IGNORED,
    EventApplicability.IGNORE_INVALID: EventProcessingStatus.INVALID_IGNORED,
}

AGENT_RELEASING_CALL_STATES = frozenset(
    {CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}
)


@dataclass(frozen=True)
class EventProcessingResult:
    status: EventProcessingStatus
    applied_transition: str | None = None


class EventProcessor:
    def __init__(
        self,
        call_repository: CallRepository,
        event_repository: EventRepository,
        agent_repository: AgentRepository,
        borrower_repository: BorrowerRepository,
        retry_service: RetryService,
        settings: Settings,
    ) -> None:
        self._calls = call_repository
        self._events = event_repository
        self._agents = agent_repository
        self._borrowers = borrower_repository
        self._retries = retry_service
        self._settings = settings

    async def process_event(self, event: ProviderEvent) -> EventProcessingResult:
        record = await self._events.record_event(event)
        if record is EventRecordResult.DUPLICATE:
            log_event(
                logger,
                logging.INFO,
                "provider_event_duplicate",
                f"Duplicate {event.event_type} event ignored",
                call_id=event.provider_call_id,
            )
            return EventProcessingResult(EventProcessingStatus.DUPLICATE_IGNORED)

        try:
            return await self._apply_event(event)
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "provider_event_processing_failed",
                f"Failed to process {event.event_type} event: {error}",
                call_id=event.provider_call_id,
            )
            return await self._finish(
                event,
                EventProcessingStatus.INVALID_IGNORED,
                f"error:{type(error).__name__}",
            )

    async def _apply_event(self, event: ProviderEvent) -> EventProcessingResult:
        call = await self._calls.find_by_provider_call_id(
            event.provider_name, event.provider_call_id
        )
        if call is None:
            return await self._finish(event, EventProcessingStatus.INVALID_IGNORED)

        target_state = EVENT_TYPE_TO_CALL_STATE.get(event.event_type)
        if target_state is None:
            return await self._finish(event, EventProcessingStatus.INVALID_IGNORED)

        applicability = should_apply_event(call.state, target_state)
        if applicability is not EventApplicability.APPLY:
            return await self._finish(event, IGNORED_APPLICABILITY_STATUS[applicability])

        updated = await self._calls.transition_call(
            call_id=call.id,
            target_state=target_state,
            failure_reason=self._failure_reason(event, target_state),
        )
        if updated is None:
            return await self._finish(event, EventProcessingStatus.STALE_IGNORED)

        await self._apply_agent_transition(updated, target_state)
        if target_state in AGENT_RELEASING_CALL_STATES:
            await self._release_borrower(updated, target_state)

        return await self._finish(
            event,
            EventProcessingStatus.PROCESSED,
            f"{call.state.value}->{target_state.value}",
        )

    async def _apply_agent_transition(self, call: Call, target_state: CallState) -> None:
        agent_state = agent_state_for_call_state(target_state)
        if agent_state is None:
            return

        agent = await self._agents.find_by_id(call.agent_id)
        if agent is None or agent.state is agent_state:
            return

        if target_state in AGENT_RELEASING_CALL_STATES:
            released = await self._agents.release_agent(
                agent_id=agent.id,
                worker_id=call.created_by_worker,
                target_state=agent_state,
                actor=TransitionActor.EVENT_PROCESSOR,
            )
            if released is None:
                self._log_agent_anomaly(call, agent.state, agent_state)
            return

        try:
            updated = await self._agents.transition_agent(
                agent_id=agent.id,
                from_state=agent.state,
                to_state=agent_state,
                actor=TransitionActor.EVENT_PROCESSOR,
                expected_version=agent.state_version,
            )
        except StateMachineError:
            self._log_agent_anomaly(call, agent.state, agent_state)
            return
        if updated is None:
            self._log_agent_anomaly(call, agent.state, agent_state)

    async def _release_borrower(self, call: Call, target_state: CallState) -> None:
        answered = target_state is CallState.COMPLETED and call.answered_at is not None
        outcome = self._retries.outcome_for_terminal_call(answered)

        await self._borrowers.release_borrower(
            borrower_id=call.borrower_id,
            worker_id=call.created_by_worker,
            outcome=outcome,
            max_attempts=self._settings.MAX_CALL_ATTEMPTS,
            backoff_base_seconds=self._settings.RETRY_BACKOFF_BASE_SECONDS,
        )

    def _failure_reason(self, event: ProviderEvent, target_state: CallState) -> str | None:
        if target_state is not CallState.FAILED:
            return None
        return str(event.payload.get("reason", "provider_reported_failure"))

    def _log_agent_anomaly(self, call: Call, current: AgentState, target: AgentState) -> None:
        log_event(
            logger,
            logging.WARNING,
            "agent_transition_skipped",
            f"Could not move agent from {current.value} to {target.value}; "
            "the call transition still stands and the lease will recover the agent",
            agent_id=call.agent_id,
            call_id=call.id,
        )

    async def _finish(
        self,
        event: ProviderEvent,
        status: EventProcessingStatus,
        applied_transition: str | None = None,
    ) -> EventProcessingResult:
        await self._events.mark_processed(
            provider_name=event.provider_name,
            provider_event_id=event.provider_event_id,
            status=status,
            applied_transition=applied_transition,
        )
        return EventProcessingResult(status, applied_transition)
