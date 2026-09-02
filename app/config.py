from functools import lru_cache

from pydantic import ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    MONGODB_URI: str
    MONGODB_DB_NAME: str = "smartdialer"
    MONGO_MAX_POOL_SIZE: int = 20

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "http://localhost:8501"

    RESERVATION_TTL_SECONDS: int = 30
    AGENT_HEARTBEAT_TIMEOUT_SECONDS: int = 30
    CALL_STALE_TIMEOUT_SECONDS: int = 120
    WRAP_UP_SECONDS: int = 10
    MAX_CALL_ATTEMPTS: int = 3
    RETRY_BACKOFF_BASE_SECONDS: int = 60
    DIALER_TICK_SECONDS: float = 1.0
    RECOVERY_TICK_SECONDS: float = 5.0
    SAFETY_MARGIN: float = 0.85
    MAX_RINGING_RATIO: float = 2.0
    MAX_SNAPSHOT_AGE_SECONDS: float = 5.0
    AVAILABILITY_DROP_THRESHOLD: float = 0.25

    DEFAULT_PROVIDER: str = "mock_a"
    PROVIDER_TIMEOUT_SECONDS: float = 5.0
    PROVIDER_RANDOM_SEED: int = 42

    DIALER_ENABLED: bool = False
    RECOVERY_ENABLED: bool = False
    RECOVERY_SWEEP_LIMIT: int = 500

    HEALTH_WINDOW_SECONDS: float = 60.0
    HEALTH_MIN_SAMPLES: int = 10
    UNHEALTHY_CONSECUTIVE_FAILURES: int = 5
    UNHEALTHY_TIMEOUT_RATE: float = 0.5
    DEGRADED_FAILURE_RATE: float = 0.2
    DEGRADED_LATENCY_MS: float = 3000.0

    HIGH_FAILURE_RATE_THRESHOLD: float = 0.3

    SOON_FREE_WEIGHT: float = 0.5
    MIN_ANSWER_RATE: float = 0.05
    MAX_ANSWER_RATE: float = 0.95
    ANSWER_RATE_WINDOW_SECONDS: float = 120.0
    VOLATILITY_THRESHOLD: float = 0.15
    VOLATILITY_FACTOR: float = 0.6
    MAX_REQUEST_PER_TICK: int = 50

    METRICS_SAMPLE_SECONDS: float = 5.0
    METRICS_RETENTION_MINUTES: int = 120
    DECISION_RETENTION_MINUTES: int = 1440

    SIMULATION_TIME_SCALE: float = 1.0
    SIMULATION_DEFAULT_SEED: int = 1234

    API_KEY: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @model_validator(mode="after")
    def check_recovery_timeout_invariant(self) -> "Settings":
        minimum = self.PROVIDER_TIMEOUT_SECONDS + self.RESERVATION_TTL_SECONDS
        if self.CALL_STALE_TIMEOUT_SECONDS <= minimum:
            raise ValueError(
                "CALL_STALE_TIMEOUT_SECONDS must exceed "
                "PROVIDER_TIMEOUT_SECONDS + RESERVATION_TTL_SECONDS "
                f"({self.CALL_STALE_TIMEOUT_SECONDS} <= {minimum}); "
                "otherwise recovery would cancel calls that are still being set up."
            )
        return self


class ConfigurationError(RuntimeError):
    pass


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        missing = [
            ".".join(str(part) for part in item["loc"])
            for item in error.errors()
            if item["type"] == "missing"
        ]
        if missing:
            raise ConfigurationError(
                "Missing required configuration: "
                + ", ".join(missing)
                + ". Set them in the environment or in a .env file (see .env.example)."
            ) from error
        raise ConfigurationError(f"Invalid configuration: {error}") from error


@lru_cache
def get_settings() -> Settings:
    return load_settings()
