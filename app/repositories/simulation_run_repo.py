from typing import Any

from pymongo import DESCENDING

from app.repositories.base import COLLECTION_SIMULATION_RUNS, BaseRepository

STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
INTERRUPTED_ERROR = "interrupted_by_restart"


class SimulationRunRepository(BaseRepository):
    collection_name = COLLECTION_SIMULATION_RUNS

    async def create(self, record: dict[str, Any]) -> None:
        await self.collection.insert_one(record)

    async def finish(self, run_id: str, update: dict[str, Any]) -> None:
        await self.collection.update_one({"_id": run_id}, {"$set": update})

    async def find_by_id(self, run_id: str) -> dict[str, Any] | None:
        return await self.collection.find_one({"_id": run_id})

    async def find_recent(self, limit: int) -> list[dict[str, Any]]:
        cursor = self.collection.find({}).sort("started_at", DESCENDING).limit(limit)
        return [document async for document in cursor]

    async def find_running(self) -> dict[str, Any] | None:
        return await self.collection.find_one({"status": STATUS_RUNNING})

    async def mark_interrupted_runs(self) -> int:
        result = await self.collection.update_many(
            {"status": STATUS_RUNNING},
            {
                "$set": {
                    "status": STATUS_FAILED,
                    "error": INTERRUPTED_ERROR,
                    "finished_at": self.now(),
                }
            },
        )
        return result.modified_count
