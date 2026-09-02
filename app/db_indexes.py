from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.config import get_settings
from app.db import get_db
from app.repositories.base import (
    COLLECTION_AGENTS,
    COLLECTION_BORROWERS,
    COLLECTION_CALLS,
    COLLECTION_PACING_DECISIONS,
    COLLECTION_PROVIDER_EVENTS,
    COLLECTION_METRICS_SAMPLES,
    COLLECTION_SIMULATION_RUNS,
    COLLECTION_PROVIDER_HEALTH_SAMPLES,
    COLLECTION_SAFETY_DECISIONS,
)

INDEXES_BY_COLLECTION: dict[str, list[IndexModel]] = {
    COLLECTION_AGENTS: [
        IndexModel(
            [("campaign_id", ASCENDING), ("state", ASCENDING)],
            name="agents_campaign_state",
        ),
        IndexModel(
            [("state", ASCENDING), ("lease_expires_at", ASCENDING)],
            name="agents_state_lease_expiry",
        ),
        IndexModel(
            [("campaign_id", ASCENDING), ("last_heartbeat_at", ASCENDING)],
            name="agents_campaign_heartbeat",
        ),
    ],
    COLLECTION_BORROWERS: [
        IndexModel(
            [
                ("campaign_id", ASCENDING),
                ("status", ASCENDING),
                ("next_eligible_at", ASCENDING),
            ],
            name="borrowers_campaign_status_eligibility",
        ),
        IndexModel(
            [("status", ASCENDING), ("lease_expires_at", ASCENDING)],
            name="borrowers_status_lease_expiry",
        ),
    ],
    COLLECTION_CALLS: [
        IndexModel(
            [("idempotency_key", ASCENDING)],
            name="calls_idempotency_key_unique",
            unique=True,
        ),
        IndexModel(
            [("provider_name", ASCENDING), ("provider_call_id", ASCENDING)],
            name="calls_provider_call_id_unique",
            unique=True,
            partialFilterExpression={"provider_call_id": {"$type": "string"}},
        ),
        IndexModel(
            [("campaign_id", ASCENDING), ("state", ASCENDING)],
            name="calls_campaign_state",
        ),
        IndexModel(
            [("state", ASCENDING), ("updated_at", ASCENDING)],
            name="calls_state_updated_at",
        ),
        IndexModel(
            [("agent_id", ASCENDING), ("state", ASCENDING)],
            name="calls_agent_state",
        ),
    ],
    COLLECTION_PROVIDER_EVENTS: [
        IndexModel(
            [("provider_name", ASCENDING), ("provider_event_id", ASCENDING)],
            name="provider_events_event_id_unique",
            unique=True,
        ),
        IndexModel(
            [("provider_call_id", ASCENDING), ("received_at", ASCENDING)],
            name="provider_events_call_received_at",
        ),
    ],
    COLLECTION_PACING_DECISIONS: [
        IndexModel(
            [("campaign_id", ASCENDING), ("created_at", ASCENDING)],
            name="pacing_decisions_campaign_created_at",
        ),
    ],
    COLLECTION_SAFETY_DECISIONS: [
        IndexModel(
            [("campaign_id", ASCENDING), ("created_at", ASCENDING)],
            name="safety_decisions_campaign_created_at",
        ),
    ],
    COLLECTION_PROVIDER_HEALTH_SAMPLES: [
        IndexModel(
            [("provider_name", ASCENDING), ("computed_at", ASCENDING)],
            name="provider_health_samples_provider_computed_at",
        ),
    ],
    COLLECTION_METRICS_SAMPLES: [
        IndexModel(
            [("campaign_id", ASCENDING), ("collected_at", ASCENDING)],
            name="metrics_samples_campaign_collected_at",
        ),
    ],
    COLLECTION_SIMULATION_RUNS: [
        IndexModel([("started_at", DESCENDING)], name="simulation_runs_started_at"),
        IndexModel([("status", ASCENDING)], name="simulation_runs_status"),
    ],
}


def metrics_retention_index(retention_minutes: int) -> IndexModel:
    return IndexModel(
        [("collected_at", ASCENDING)],
        name="metrics_samples_ttl",
        expireAfterSeconds=retention_minutes * 60,
    )


def decision_retention_index(name: str, retention_minutes: int) -> IndexModel:
    return IndexModel(
        [("created_at", ASCENDING)],
        name=name,
        expireAfterSeconds=retention_minutes * 60,
    )


async def ensure_indexes(database: AsyncIOMotorDatabase | None = None) -> None:
    target = database if database is not None else get_db()
    settings = get_settings()
    for collection_name, indexes in INDEXES_BY_COLLECTION.items():
        await target[collection_name].create_indexes(indexes)
    await target[COLLECTION_METRICS_SAMPLES].create_indexes(
        [metrics_retention_index(settings.METRICS_RETENTION_MINUTES)]
    )
    await target[COLLECTION_PACING_DECISIONS].create_indexes(
        [decision_retention_index("pacing_decisions_ttl", settings.DECISION_RETENTION_MINUTES)]
    )
    await target[COLLECTION_SAFETY_DECISIONS].create_indexes(
        [decision_retention_index("safety_decisions_ttl", settings.DECISION_RETENTION_MINUTES)]
    )
