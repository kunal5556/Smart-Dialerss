from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from app.db import get_db
from app.models.base import utc_now

COLLECTION_CAMPAIGNS = "campaigns"
COLLECTION_AGENTS = "agents"
COLLECTION_BORROWERS = "borrowers"
COLLECTION_CALLS = "calls"
COLLECTION_PROVIDER_EVENTS = "provider_events"
COLLECTION_PACING_DECISIONS = "pacing_decisions"
COLLECTION_SAFETY_DECISIONS = "safety_decisions"
COLLECTION_PROVIDER_HEALTH_SAMPLES = "provider_health_samples"
COLLECTION_METRICS_SAMPLES = "metrics_samples"
COLLECTION_SIMULATION_RUNS = "simulation_runs"

CANDIDATE_WINDOW_MULTIPLIER = 3


def build_lease_fields(worker_id: str, ttl_seconds: int, now: datetime) -> dict[str, Any]:
    return {
        "reserved_by": worker_id,
        "reserved_at": now,
        "lease_expires_at": now + timedelta(seconds=ttl_seconds),
    }


def cleared_lease_fields() -> dict[str, Any]:
    return {
        "reserved_by": None,
        "reserved_at": None,
        "lease_expires_at": None,
    }


def candidate_window(needed: int) -> int:
    return max(needed, 0) * CANDIDATE_WINDOW_MULTIPLIER


class BaseRepository:
    collection_name: str

    def __init__(self, database: AsyncIOMotorDatabase | None = None) -> None:
        self._database = database

    @property
    def database(self) -> AsyncIOMotorDatabase:
        if self._database is not None:
            return self._database
        return get_db()

    @property
    def collection(self) -> AsyncIOMotorCollection:
        return self.database[self.collection_name]

    @staticmethod
    def now() -> datetime:
        return utc_now()
