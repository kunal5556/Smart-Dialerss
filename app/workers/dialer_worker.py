import asyncio
import logging

from app.config import Settings
from app.dialers.mode_router import ModeRouter
from app.logging_config import log_event
from app.models.campaign import Campaign
from app.models.enums import AgentState
from app.repositories.agent_repo import AgentRepository
from app.repositories.campaign_repo import CampaignRepository
from app.services.wrap_up_service import WrapUpService
from app.state_machines.agent_sm import TransitionActor
from app.workers.worker_identity import get_worker_id

logger = logging.getLogger(__name__)

GRACEFUL_STOP_SECONDS = 15.0


class DialerWorker:
    def __init__(
        self,
        campaign_repository: CampaignRepository,
        agent_repository: AgentRepository,
        mode_router: ModeRouter,
        wrap_up_service: WrapUpService,
        settings: Settings,
    ) -> None:
        self._campaigns = campaign_repository
        self._agents = agent_repository
        self._router = mode_router
        self._wrap_up = wrap_up_service
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(self._task, timeout=GRACEFUL_STOP_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_event(
                    logger,
                    logging.ERROR,
                    "dialer_tick_failed",
                    f"Dialer tick raised an error, continuing: {error}",
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.DIALER_TICK_SECONDS
                )
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> None:
        worker_id = get_worker_id()
        for campaign in await self._campaigns.find_running():
            await self._tick_campaign(campaign, worker_id)

    async def _tick_campaign(self, campaign: Campaign, worker_id: str) -> None:
        try:
            dialer = self._router.select(campaign)
            await dialer.tick(campaign, worker_id)
            await self._release_finished_wrap_ups(campaign)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "campaign_tick_failed",
                f"Campaign tick raised an error, continuing: {error}",
                campaign_id=campaign.id,
                worker_id=worker_id,
            )

    async def _release_finished_wrap_ups(self, campaign: Campaign) -> None:
        for agent in await self._wrap_up.find_finished_wrap_ups(campaign.id):
            await self._agents.transition_agent(
                agent_id=agent.id,
                from_state=AgentState.WRAP_UP,
                to_state=AgentState.AVAILABLE,
                actor=TransitionActor.WORKER_TIMER,
                expected_version=agent.state_version,
            )
