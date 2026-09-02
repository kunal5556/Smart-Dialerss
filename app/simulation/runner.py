import asyncio
import logging
from dataclasses import asdict
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import Settings
from app.logging_config import log_event
from app.models.base import new_id, utc_now
from app.models.enums import CampaignStatus
from app.repositories.simulation_run_repo import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SimulationRunRepository,
)
from app.simulation.config import SimulationConfig
from app.simulation.engine import SimulationEngine, SimulationReport

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 50


async def stop_orphaned_simulation_campaigns(database: AsyncIOMotorDatabase) -> int:
    result = await database["campaigns"].update_many(
        {"status": CampaignStatus.RUNNING.value},
        {"$set": {"status": CampaignStatus.STOPPED.value, "updated_at": utc_now()}},
    )
    if result.modified_count:
        log_event(
            logger,
            logging.WARNING,
            "orphaned_simulation_campaigns_stopped",
            f"Stopped {result.modified_count} simulation campaigns left running by a restart",
        )
    return result.modified_count


class SimulationRunner:
    def __init__(
        self,
        database: AsyncIOMotorDatabase,
        settings: Settings,
        run_repository: SimulationRunRepository | None = None,
    ) -> None:
        self._engine = SimulationEngine(database, settings)
        self._runs = run_repository or SimulationRunRepository()
        self._task: asyncio.Task | None = None

    async def start(self, config: SimulationConfig) -> dict[str, Any]:
        if await self._runs.find_running() is not None:
            raise RuntimeError("A simulation is already running")

        record = {
            "_id": new_id(),
            "scenario": config.name,
            "dialing_mode": config.dialing_mode.value,
            "status": STATUS_RUNNING,
            "started_at": utc_now(),
            "finished_at": None,
            "passed": None,
            "violations": [],
            "error": None,
            "metrics": None,
        }
        await self._runs.create(record)
        self._task = asyncio.create_task(self._execute(record["_id"], config))
        return record

    async def get(self, run_id: str) -> dict[str, Any] | None:
        return await self._runs.find_by_id(run_id)

    async def history(self) -> list[dict[str, Any]]:
        return await self._runs.find_recent(HISTORY_LIMIT)

    async def reconcile(self) -> int:
        interrupted = await self._runs.mark_interrupted_runs()
        if interrupted:
            log_event(
                logger,
                logging.WARNING,
                "simulation_runs_interrupted",
                f"Marked {interrupted} simulation runs as interrupted by a restart",
            )
        return interrupted

    async def shutdown(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _execute(self, run_id: str, config: SimulationConfig) -> None:
        try:
            report = await self._engine.run(config)
        except asyncio.CancelledError:
            await self._finish(run_id, STATUS_FAILED, error="cancelled")
            raise
        except Exception as error:
            await self._finish(run_id, STATUS_FAILED, error=f"{type(error).__name__}: {error}")
            log_event(
                logger,
                logging.ERROR,
                "simulation_run_failed",
                f"Simulation {run_id} failed: {error}",
            )
            return

        await self._finish(
            run_id,
            STATUS_COMPLETED if report.passed else STATUS_FAILED,
            error=report.error,
            report=report,
        )

    async def _finish(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
        report: SimulationReport | None = None,
    ) -> None:
        update: dict[str, Any] = {
            "status": status,
            "finished_at": utc_now(),
            "error": error,
        }
        if report is not None:
            update["passed"] = report.passed
            update["violations"] = [violation.name for violation in report.violations]
            update["metrics"] = asdict(report.metrics) if report.metrics else None
        await self._runs.finish(run_id, update)
