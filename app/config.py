from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    google_cloud_project: str | None
    google_cloud_location: str
    gemini_model: str
    firestore_database: str
    use_in_memory_store: bool
    commercegov_api_url: str | None
    commercegov_api_token: str | None
    commercegov_oauth_token_url: str
    commercegov_oauth_client_id: str
    commercegov_access_secret: str
    commercegov_refresh_secret: str
    taskmaster_api_token: str | None

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            firestore_database=os.getenv("FIRESTORE_DATABASE", "(default)"),
            use_in_memory_store=os.getenv("USE_IN_MEMORY_STORE", "false").lower() == "true",
            commercegov_api_url=os.getenv("COMMERCEGOV_API_URL"),
            commercegov_api_token=os.getenv("COMMERCEGOV_API_TOKEN"),
            commercegov_oauth_token_url=os.getenv(
                "COMMERCEGOV_OAUTH_TOKEN_URL",
                "https://app.commercegov.io/oauth/integration/token",
            ),
            commercegov_oauth_client_id=os.getenv(
                "COMMERCEGOV_OAUTH_CLIENT_ID",
                "taskmaster-hackathon-client-direct",
            ),
            commercegov_access_secret=os.getenv(
                "COMMERCEGOV_ACCESS_SECRET",
                "commercegov-taskmaster-api-token",
            ),
            commercegov_refresh_secret=os.getenv(
                "COMMERCEGOV_REFRESH_SECRET",
                "commercegov-taskmaster-refresh-token",
            ),
            taskmaster_api_token=os.getenv("TASKMASTER_API_TOKEN"),
        )
